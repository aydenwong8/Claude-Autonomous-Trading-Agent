"""Investment cases: open theses, action history, and the thesis lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any

import config
import markettime
from models import PortfolioState, PositionReview, Thesis, ValidatedTrade


def fetch_open_theses(conn: sqlite3.Connection) -> dict[str, Thesis]:
    """Load the standing investment case for every currently-held position."""
    rows = conn.execute(
        """
        SELECT symbol, opened_at, thesis, target, invalidation, status, closed_at,
               exit_trigger, updated_at, revision
        FROM theses WHERE status = 'open'
        """
    ).fetchall()
    return {r[0]: Thesis(*r) for r in rows}


def fetch_action_history(
    conn: sqlite3.Connection, limit_per_symbol: int = config.ACTION_HISTORY_LIMIT
) -> dict[str, dict[str, Any]]:
    """Every trade this agent has previously proposed, grouped by symbol.

    The stored thesis tells the model what it *believed* about a name. This
    tells it what it *did* about it, in its own words at the time. Without the
    second half the only trace of a past action is the position's current
    weight, so a conclusion already acted on reads as new every cycle: MSFT was
    trimmed six times against one expired price marker, each trim restating the
    previous one's reasoning, and three separate cycles each independently
    decided to size GD up — the third exceeding the ~6-7% ceiling the first had
    written down for itself, because nothing carried that sentence forward.

    Rejected proposals are kept. A trade the guardrails refused is still a
    decision this agent made, and re-proposing it unchanged is precisely the
    repetition this is meant to surface. Trades that never reached the broker
    are labelled as such, so an intention is never read back as a fill.

    Only the most recent `limit_per_symbol` actions per name are returned; the
    summary line still counts them all, so truncation loses detail but never
    the fact that earlier actions happened.
    """
    rows = conn.execute(
        """
        SELECT t.symbol, t.cycle_id, d.timestamp, t.side, t.notional_usd,
               t.reasoning, t.is_valid, t.rejection_reason, t.order_status
        FROM trades t JOIN decision_cycles d ON d.id = t.cycle_id
        ORDER BY t.symbol, t.cycle_id
        """
    ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for (symbol, cycle_id, ts, side, notional, reasoning,
         is_valid, rejection, order_status) in rows:
        status = order_status or ""
        if not is_valid:
            outcome = f"REJECTED by guardrails — {rejection}"
        elif status.startswith("duplicate_suppressed"):
            outcome = "not sent — duplicate of an order already at the broker"
        elif status.startswith("submit_failed"):
            outcome = f"NOT EXECUTED — {status}"
        else:
            outcome = "executed"
        grouped.setdefault(symbol, []).append({
            "cycle": cycle_id,
            "date": markettime.fmt_et_date(ts),
            "side": side,
            "notional_usd": notional,
            "outcome": outcome,
            "your_reasoning_at_the_time": reasoning,
        })

    # Verdict tallies are near-free next to the notes that produced them, and
    # they are what makes a streak visible: a name held through thirty reviews
    # has been actively re-decided thirty times, which reads very differently
    # from never having been examined.
    verdicts: dict[str, dict[str, int]] = {}
    for symbol, verdict, n in conn.execute(
        """
        SELECT symbol, verdict, COUNT(*) FROM position_reviews
        GROUP BY symbol, verdict
        """
    ):
        verdicts.setdefault(symbol, {})[verdict] = n

    # The most recent case this agent closed in each name, with why. Trades
    # carry the sell's reasoning but not the case it ended, and once a position
    # is gone its thesis is never shown again — so without this, a name exited
    # last week can come back as a candidate looking like a first encounter.
    closed: dict[str, dict[str, Any]] = {}
    for symbol, opened_at, closed_at, trigger, thesis, target, invalidation in conn.execute(
        """
        SELECT symbol, opened_at, closed_at, exit_trigger, thesis, target, invalidation
        FROM theses WHERE status = 'closed' ORDER BY closed_at
        """
    ):
        closed[symbol] = {
            "opened_at": markettime.fmt_et_date(opened_at),
            "closed_at": markettime.fmt_et_date(closed_at) if closed_at else None,
            "exit_trigger": trigger,
            "why_you_owned_it": thesis,
            "target": target,
            "invalidation": invalidation,
        }

    history: dict[str, dict[str, Any]] = {}
    for symbol in set(grouped) | set(verdicts) | set(closed):
        actions = grouped.get(symbol, [])
        record: dict[str, Any] = {}
        if actions:
            executed = [a for a in actions if a["outcome"] == "executed"]
            bought = sum(a["notional_usd"] for a in executed if a["side"] == "buy")
            sold = sum(a["notional_usd"] for a in executed if a["side"] == "sell")
            parts = []
            if bought:
                n = sum(1 for a in executed if a["side"] == "buy")
                parts.append(f"{n} buy{'s' if n > 1 else ''} totalling ${bought:,.0f}")
            if sold:
                n = sum(1 for a in executed if a["side"] == "sell")
                parts.append(f"{n} sell{'s' if n > 1 else ''} totalling ${sold:,.0f}")
            refused = len(actions) - len(executed)
            if refused:
                parts.append(f"{refused} that never reached the broker")
            record["summary"] = (
                f"You have acted on {symbol} {len(actions)} time"
                f"{'s' if len(actions) > 1 else ''} "
                f"({', '.join(parts) if parts else 'none executed'}), "
                f"between {actions[0]['date']} and {actions[-1]['date']}."
            )
            shown = actions[-limit_per_symbol:]
            if len(shown) < len(actions):
                record["note"] = (
                    f"Showing the most recent {len(shown)} of {len(actions)} actions."
                )
            record["actions"] = shown
        if symbol in verdicts:
            record["review_verdicts_you_have_returned"] = verdicts[symbol]
        if symbol in closed:
            record["last_closed_case"] = closed[symbol]
        history[symbol] = record
    return history


CASE_FIELDS = ("thesis", "target", "invalidation")


def _full_case(obj: Any) -> tuple[str, str, str] | None:
    """The three case fields off a review or trade, or None if any is missing.

    A case is all-or-nothing: a target rewritten without its matching
    invalidation describes a position nobody could later be held to.
    """
    values = tuple((getattr(obj, f) or "").strip() for f in CASE_FIELDS)
    return values if all(values) else None  # type: ignore[return-value]


def apply_thesis_lifecycle(
    conn: sqlite3.Connection,
    validated_trades: list[ValidatedTrade],
    reviews: list[PositionReview],
    portfolio: PortfolioState,
    timestamp: datetime,
    cycle_id: int,
) -> dict[str, int]:
    """Open, carry, re-anchor, and close theses from executed trades and reviews.

    A buy opens a thesis (or refreshes the existing one when adding to a winner).
    A sell that closes out the whole position closes the thesis; a partial sell
    leaves it open, since the case still stands on the remaining shares.

    A review may adopt an untracked position by supplying a full case — that is
    how holdings opened before thesis tracking get one without having to be sold
    and rebought — or **re-anchor** the case on a position that already has one.
    Re-anchoring matters because a stored target that has been passed keeps
    firing as if it were still a decision criterion: MSFT was trimmed six times
    over five weeks against a "$420+" target it had already cleared by 19%,
    because the only way to change the wording was to sell the position and buy
    it back. Every version is kept in `thesis_revisions`, so a rewrite is a
    dated amendment rather than a quiet edit of the record.

    Returns per-outcome counts for the cycle summary. An incomplete case on a
    review is counted, not applied — silently dropping it is what let the model
    report re-anchoring a thesis that never changed.
    """
    now = timestamp.isoformat()
    open_theses = fetch_open_theses(conn)
    counts = {
        "opened": 0, "adopted": 0, "reanchored": 0,
        "closed": 0, "incomplete": 0, "orphaned": 0,
    }

    def record_revision(symbol: str, revision: int, source: str, case: tuple[str, str, str]) -> None:
        conn.execute(
            """
            INSERT INTO thesis_revisions
                (symbol, cycle_id, revised_at, revision, source, thesis, target, invalidation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, cycle_id, now, revision, source, *case),
        )

    def write_case(symbol: str, source: str, case: tuple[str, str, str]) -> None:
        """Open a new case, or supersede the standing one, keeping both on record."""
        existing = open_theses.get(symbol)
        if existing is None:
            conn.execute(
                """
                INSERT INTO theses
                    (symbol, opened_at, thesis, target, invalidation, status, revision)
                VALUES (?, ?, ?, ?, ?, 'open', 1)
                """,
                (symbol, now, *case),
            )
            record_revision(symbol, 1, source, case)
            open_theses[symbol] = Thesis(symbol, now, *case)
            return

        revision = existing.revision + 1
        conn.execute(
            """
            UPDATE theses
               SET thesis = ?, target = ?, invalidation = ?, updated_at = ?, revision = ?
             WHERE symbol = ? AND status = 'open'
            """,
            (*case, now, revision, symbol),
        )
        record_revision(symbol, revision, source, case)
        open_theses[symbol] = replace(
            existing,
            thesis=case[0], target=case[1], invalidation=case[2],
            updated_at=now, revision=revision,
        )

    for r in reviews:
        # A review can only speak for something actually held. A verdict on a
        # symbol not in the book is stale or hallucinated either way.
        if r.symbol not in portfolio.positions:
            continue
        existing = open_theses.get(r.symbol)
        case = _full_case(r)
        if case is None:
            # Partial case fields on a review: the model tried to say something
            # about the standing case and it did not take.
            if any((getattr(r, f) or "").strip() for f in CASE_FIELDS):
                counts["incomplete"] += 1
            continue
        if existing is None:
            write_case(r.symbol, "adopt", case)
            counts["adopted"] += 1
        elif case != (existing.thesis, existing.target, existing.invalidation):
            write_case(r.symbol, "review_reanchor", case)
            counts["reanchored"] += 1

    for v in validated_trades:
        if not v.is_valid or v.order is None or v.submit_failed:
            continue
        trade = v.trade

        if trade.side == "buy":
            case = _full_case(trade)
            if case is None:
                # Unreachable via validate_trades, which rejects such a buy
                # before it can be ordered. Guarded anyway: writing NULL into a
                # NOT NULL column here would abort the whole cycle's logging.
                continue
            is_new = trade.symbol not in open_theses
            write_case(trade.symbol, "open" if is_new else "buy_refresh", case)
            counts["opened" if is_new else "reanchored"] += 1
        else:
            held_value = portfolio.positions[trade.symbol].market_value
            # Treat a near-total sell as a full exit; market fills rarely land exact.
            if trade.notional_usd >= held_value * 0.99:
                conn.execute(
                    """
                    UPDATE theses SET status = 'closed', closed_at = ?, exit_trigger = ?
                    WHERE symbol = ? AND status = 'open'
                    """,
                    (now, trade.exit_trigger, trade.symbol),
                )
                open_theses.pop(trade.symbol, None)
                counts["closed"] += 1

    counts["orphaned"] = _close_orphaned_theses(
        conn, open_theses, portfolio, now,
        traded={v.trade.symbol for v in validated_trades if v.is_valid and not v.submit_failed},
    )
    conn.commit()
    return counts


# Written only by code, never proposed by the model, so it is deliberately not
# in config.VALID_EXIT_TRIGGERS — it records an absence, not a decision.
ORPHAN_EXIT_TRIGGER = "position_gone"


def _close_orphaned_theses(
    conn: sqlite3.Connection,
    open_theses: dict[str, Thesis],
    portfolio: PortfolioState,
    now: str,
    traded: set[str],
) -> int:
    """Close cases whose position is gone and has nothing in flight to explain it.

    A case is only ever shown to the model attached to a held position, so one
    without a position is invisible: it can never be reviewed, and no sell can
    close it because there is nothing to sell. Left alone it stays open forever
    and blocks the symbol's next buy from opening a fresh case.

    Three things are treated as explaining an absent position, and none of them
    is an orphan: an unfilled order still at the broker, a trade placed in this
    very cycle (the snapshot predates it), and an empty position list. That last
    one is the important guard — a broker call that returns no positions is far
    more likely to be a glitch than a liquidated account, and closing every case
    at once on that reading is unrecoverable.
    """
    if not portfolio.positions:
        return 0

    orphans = [
        symbol for symbol in open_theses
        if symbol not in portfolio.positions
        and symbol not in portfolio.pending_orders
        and symbol not in traded
    ]
    for symbol in orphans:
        conn.execute(
            """
            UPDATE theses SET status = 'closed', closed_at = ?, exit_trigger = ?
            WHERE symbol = ? AND status = 'open'
            """,
            (now, ORPHAN_EXIT_TRIGGER, symbol),
        )
        open_theses.pop(symbol, None)
    return len(orphans)
