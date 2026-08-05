"""Evaluation metrics (README.md#evaluation-metrics) that the dashboard doesn't yet compute.

Pure functions over the decision log and the daily equity curve: no broker
calls, no network, no writes. Everything here takes an already-open
`sqlite3.Connection` (or a list of `(date, equity)` points) and returns plain
dicts / floats / None, so the same functions can be called from the dashboard,
from a notebook, or from a test with an in-memory database.

Deliberately NOT implemented here: the README's "reaction to volatility/news
events". Measuring it honestly needs per-symbol price history and the news
payloads joined against decision timestamps — market-data work that belongs in
a later pass. A version built only on what's in the database would measure the
agent's reaction to nothing, so there is no stub for it.

Two things are extrapolated to a year — CAGR and turnover — and both are
guarded. Roughly three weeks of history exists; annualising it is a ~17x
extrapolation dominated by whichever way the last few days happened to go.
Following `dashboard.compute_sharpe`, these return None below a minimum rather
than a confident-looking number.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date
from typing import Any

import markettime

# One quarter. Annualising a quarter is a 4x extrapolation — still noisy, but
# it is at least a full earnings cycle and covers more than one market regime.
# Below it, the annualised figure says more about the last week than about the
# strategy. 63 trading days / 90 calendar days are the same quarter measured on
# the two different clocks these metrics use: the equity curve advances one
# point per trading day, while trades and turnover live on the calendar.
MIN_TRADING_DAYS_FOR_ANNUALIZING = 63
MIN_CALENDAR_DAYS_FOR_ANNUALIZING = 90

DAYS_PER_YEAR = 365.25

# Verdicts that assert something should change. A "trim"/"exit" with no sell
# behind it is the agent contradicting itself within a single cycle, which is
# the sharpest available read on decision consistency.
ACTION_VERDICTS = frozenset({"trim", "exit"})


# ---------------------------------------------------------------------------
# Equity curve helpers
# ---------------------------------------------------------------------------

def equity_curve_from_cycles(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """(date, equity) per logged decision cycle, oldest first.

    A fallback curve for callers with no broker access. It is coarser than
    Alpaca's daily portfolio history — one point per cycle, and several cycles
    can land on the same day — so it is fine for averages and period spans but
    is not a substitute for the daily curve when measuring volatility.
    """
    rows = conn.execute(
        "SELECT timestamp, portfolio_equity FROM decision_cycles ORDER BY id"
    ).fetchall()
    # Bucket on the market's calendar day for the same reason dashboard.py does:
    # a post-close run is 20:00+ UTC and would otherwise be dated tomorrow.
    return [(markettime.fmt_et_date(ts), float(eq)) for ts, eq in rows if eq]


def _span_days(points: list[tuple[str, float]]) -> int | None:
    """Calendar days between the first and last point; None if undatable."""
    try:
        first = date.fromisoformat(points[0][0])
        last = date.fromisoformat(points[-1][0])
    except (IndexError, ValueError):
        return None
    return (last - first).days


# ---------------------------------------------------------------------------
# Performance: annualised return
# ---------------------------------------------------------------------------

def compute_annualized_return(points: list[tuple[str, float]]) -> float | None:
    """Compound annual growth rate (CAGR), as a percentage, from a daily equity curve.

    CAGR = (end / start) ** (1 / years) - 1, with years measured in calendar
    days between the first and last point (365.25 days/year).

    Returns None below MIN_TRADING_DAYS_FOR_ANNUALIZING points. That guard is
    the point of this function: raising a three-week return to the 17th power
    produces a number that looks like a forecast and is really an artefact of
    the last few sessions. Callers should render "not yet" rather than invent
    one, exactly as the dashboard does for Sharpe.

    Also None when the curve can't support the maths at all: unparseable or
    zero-length date span, non-positive starting equity, or an account wiped
    out to zero or below (the root of a non-positive ratio is undefined).
    """
    if len(points) < MIN_TRADING_DAYS_FOR_ANNUALIZING:
        return None

    days = _span_days(points)
    if not days or days <= 0:
        return None

    start = points[0][1]
    end = points[-1][1]
    if start <= 0 or end <= 0:
        return None

    years = days / DAYS_PER_YEAR
    return ((end / start) ** (1 / years) - 1) * 100


# ---------------------------------------------------------------------------
# Behavioural: turnover
# ---------------------------------------------------------------------------

def compute_turnover(
    conn: sqlite3.Connection, points: list[tuple[str, float]] | None = None
) -> dict[str, Any]:
    """Portfolio turnover from executed trades, for the observed period.

    Headline definition is the investment-company standard (SEC N-1A / the
    number funds report):

        turnover = min(total purchases, total sales) / average net assets

    The `min` is what makes it a measure of *churn* rather than of growth:
    money that arrives as new capital and is put to work shows up as purchases
    with no matching sale, and would otherwise register as frantic trading. For
    this account that matters a lot — the first cycles deployed a $100k opening
    balance, which is a one-time event, not a trading habit.

    `two_way_turnover_pct` — (purchases + sales) / 2 / average net assets — is
    reported alongside because during a deployment phase the `min` definition
    reads near zero and understates how much the agent actually did. Use the
    headline for comparison against funds, the two-way for "how busy was it".

    Only trades with is_valid = 1 are counted: a guardrail-rejected proposal
    never reached the broker, so it moved no capital. (Rejections are the
    subject of `compute_rejection_stats`, not of turnover.) Order fills are not
    re-checked against the broker — notional_usd of an accepted market order is
    taken as the traded amount.

    Average net assets is the mean equity across `points`, defaulting to the
    per-cycle curve from decision_cycles. Annualised turnover is
    period turnover x (365.25 / period days), and is None below
    MIN_CALENDAR_DAYS_FOR_ANNUALIZING for the same reason CAGR is.
    """
    if points is None:
        points = equity_curve_from_cycles(conn)

    rows = conn.execute(
        """SELECT side, COALESCE(SUM(notional_usd), 0), COUNT(*)
           FROM trades WHERE is_valid = 1 GROUP BY side"""
    ).fetchall()
    totals = {side: (float(usd), int(n)) for side, usd, n in rows}
    buy_usd, buy_n = totals.get("buy", (0.0, 0))
    sell_usd, sell_n = totals.get("sell", (0.0, 0))

    equities = [eq for _, eq in points if eq > 0]
    avg_net_assets = statistics.fmean(equities) if equities else None
    period_days = _span_days(points)

    result: dict[str, Any] = {
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "gross_traded_usd": buy_usd + sell_usd,
        "executed_trades": buy_n + sell_n,
        "avg_portfolio_usd": avg_net_assets,
        "period_days": period_days,
        "first_date": points[0][0] if points else None,
        "last_date": points[-1][0] if points else None,
        "turnover_pct": None,
        "two_way_turnover_pct": None,
        "annualized_turnover_pct": None,
        "annualized_min_days": MIN_CALENDAR_DAYS_FOR_ANNUALIZING,
        "definition": "min(purchases, sales) / average net assets",
    }

    # No equity history means no denominator — report the dollar amounts and
    # leave the ratios None rather than dividing by zero or by a guessed base.
    if not avg_net_assets:
        return result

    result["turnover_pct"] = min(buy_usd, sell_usd) / avg_net_assets * 100
    result["two_way_turnover_pct"] = (buy_usd + sell_usd) / 2 / avg_net_assets * 100

    if period_days and period_days >= MIN_CALENDAR_DAYS_FOR_ANNUALIZING:
        result["annualized_turnover_pct"] = (
            result["turnover_pct"] * DAYS_PER_YEAR / period_days
        )
    return result


# ---------------------------------------------------------------------------
# Behavioural: guardrail rejections
# ---------------------------------------------------------------------------

def compute_rejection_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Frequency of rejected (guardrail-violating) proposals, with a reason breakdown.

    Counts every row in `trades` as one proposal, since a row is only written
    once Claude has proposed the trade and validation has ruled on it.

    A 0% rate is the expected reading today — no proposal has ever been
    rejected in this database. That is worth stating rather than treating as a
    bug: it means the prompt's restatement of the guardrails is holding, and
    the interesting event will be the first non-zero reading. It does NOT mean
    validation is unnecessary; an unenforced guardrail can't be observed to
    hold.

    Rates are 0.0 on an empty table rather than a ZeroDivisionError, so this is
    safe to call before the agent's first cycle.
    """
    total, rejected = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(CASE WHEN is_valid = 0 THEN 1 ELSE 0 END), 0)
           FROM trades"""
    ).fetchone()
    total, rejected = int(total), int(rejected)

    reasons = conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(rejection_reason), ''), 'unspecified'), COUNT(*)
           FROM trades WHERE is_valid = 0
           GROUP BY 1 ORDER BY 2 DESC, 1"""
    ).fetchall()

    return {
        "total_proposed": total,
        "accepted": total - rejected,
        "rejected": rejected,
        "rejected_pct": (rejected / total * 100) if total else 0.0,
        "by_reason": [
            {
                "reason": reason,
                "count": int(count),
                "pct_of_proposals": (count / total * 100) if total else 0.0,
            }
            for reason, count in reasons
        ],
    }


# ---------------------------------------------------------------------------
# Behavioural: decision consistency
# ---------------------------------------------------------------------------

def compute_decision_consistency(conn: sqlite3.Connection) -> dict[str, Any]:
    """How stable and self-consistent the agent's decisions are over time.

    Three complementary readings, chosen because each catches a different way
    an LLM decision-maker can be incoherent:

    1. `verdict_flip_pct` — how often a symbol's review verdict differs from
       its verdict in the previous cycle that reviewed it. Catches waffling:
       hold -> exit -> hold on unchanged facts. Consecutive *reviews* of a
       symbol are compared, not consecutive cycles, so a symbol skipped for a
       cycle doesn't manufacture a flip.
    2. `unmatched_action_verdicts` — "trim"/"exit" verdicts with no executed
       sell for that symbol in the same cycle. This is says-vs-does: the
       strongest signal available here, because it is internally contradictory
       within one response rather than merely a change of mind.
    3. Holding periods and exit mix — mean/median days from a thesis opening to
       its close, and the share of exits by `exit_trigger`. Catches the failure
       mode the system prompt is written against: a "thesis-driven swing
       investor" that actually churns in days, or one whose exits are all
       "risk" and never "target_hit".

    What this does NOT capture: whether any decision was *correct*. A perfectly
    consistent agent that is consistently wrong scores well here. It also says
    nothing about reasoning quality — only the verdict label is compared, so a
    "hold" whose note reverses last cycle's argument reads as stable. And on a
    log of ~10 cycles dominated by "hold", a low flip rate is close to trivial;
    treat these as behavioural description, not evidence of skill.

    All rates are None (not 0.0) when there is nothing to measure, so an empty
    log is distinguishable from a genuinely stable one.
    """
    reviews = conn.execute(
        """SELECT r.symbol, r.verdict, r.cycle_id
           FROM position_reviews r ORDER BY r.symbol, r.cycle_id"""
    ).fetchall()

    verdict_counts: dict[str, int] = {}
    by_symbol: dict[str, list[str]] = {}
    for symbol, verdict, _ in reviews:
        verdict = (verdict or "").strip().lower() or "unspecified"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        by_symbol.setdefault(symbol, []).append(verdict)

    comparisons = flips = 0
    for verdicts in by_symbol.values():
        for prev, curr in zip(verdicts, verdicts[1:]):
            comparisons += 1
            flips += prev != curr

    # A trim/exit verdict is only honoured if a sell for that symbol actually
    # executed in the same cycle — the system prompt says so, and code enforces
    # it by ignoring the verdict otherwise.
    unmatched = conn.execute(
        f"""SELECT COUNT(*) FROM position_reviews r
            WHERE LOWER(TRIM(r.verdict)) IN ({','.join('?' * len(ACTION_VERDICTS))})
              AND NOT EXISTS (
                  SELECT 1 FROM trades t
                  WHERE t.cycle_id = r.cycle_id AND t.symbol = r.symbol
                    AND t.side = 'sell' AND t.is_valid = 1
              )""",
        sorted(ACTION_VERDICTS),
    ).fetchone()[0]
    action_verdicts = sum(verdict_counts.get(v, 0) for v in ACTION_VERDICTS)

    holding_days = []
    for opened_at, closed_at in conn.execute(
        "SELECT opened_at, closed_at FROM theses WHERE status = 'closed' AND closed_at IS NOT NULL"
    ).fetchall():
        opened = markettime.to_eastern(opened_at)
        closed = markettime.to_eastern(closed_at)
        if opened and closed:
            holding_days.append((closed - opened).total_seconds() / 86400)

    triggers = conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(exit_trigger), ''), 'unspecified'), COUNT(*)
           FROM theses WHERE status = 'closed'
           GROUP BY 1 ORDER BY 2 DESC, 1"""
    ).fetchall()
    closed_total = sum(int(c) for _, c in triggers)

    return {
        "reviews_total": len(reviews),
        "symbols_reviewed": len(by_symbol),
        "verdict_counts": dict(sorted(verdict_counts.items(), key=lambda kv: -kv[1])),
        "verdict_comparisons": comparisons,
        "verdict_flips": flips,
        "verdict_flip_pct": (flips / comparisons * 100) if comparisons else None,
        "action_verdicts": action_verdicts,
        "unmatched_action_verdicts": int(unmatched),
        "unmatched_action_pct": (
            unmatched / action_verdicts * 100 if action_verdicts else None
        ),
        "closed_theses": closed_total,
        "mean_holding_days": statistics.fmean(holding_days) if holding_days else None,
        "median_holding_days": statistics.median(holding_days) if holding_days else None,
        "exit_triggers": [
            {
                "trigger": trigger,
                "count": int(count),
                "pct_of_exits": (count / closed_total * 100) if closed_total else 0.0,
            }
            for trigger, count in triggers
        ],
    }
