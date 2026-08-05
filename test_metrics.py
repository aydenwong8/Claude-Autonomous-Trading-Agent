"""Tests for metrics.py — in-memory SQLite, no broker, no filesystem.

The interesting cases here are all degenerate ones: an empty log, a single
data point, a period too short to annualise, and every ratio's
divide-by-zero path. Those are the states the agent is actually in for its
first weeks, so they are the states these functions must survive.
"""

from __future__ import annotations

import sqlite3

import pytest

import agent
import metrics


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def conn() -> sqlite3.Connection:
    """Empty database with the real schema — init_db is the schema's source of truth."""
    connection = agent.init_db(":memory:")
    yield connection
    connection.close()


def add_cycle(
    conn: sqlite3.Connection, timestamp: str, equity: float, cash: float = 0.0
) -> int:
    cursor = conn.execute(
        """INSERT INTO decision_cycles
           (timestamp, mode, prompt, claude_response, portfolio_cash, portfolio_equity)
           VALUES (?, 'paper', '', '', ?, ?)""",
        (timestamp, cash, equity),
    )
    return cursor.lastrowid


def add_trade(
    conn: sqlite3.Connection,
    cycle_id: int,
    symbol: str,
    side: str,
    notional: float,
    is_valid: bool = True,
    rejection_reason: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO trades
           (cycle_id, symbol, side, notional_usd, reasoning, is_valid, rejection_reason)
           VALUES (?, ?, ?, ?, '', ?, ?)""",
        (cycle_id, symbol, side, notional, int(is_valid), rejection_reason),
    )


def add_review(conn: sqlite3.Connection, cycle_id: int, symbol: str, verdict: str) -> None:
    conn.execute(
        "INSERT INTO position_reviews (cycle_id, symbol, verdict, note) VALUES (?, ?, ?, '')",
        (cycle_id, symbol, verdict, ),
    )


def add_thesis(
    conn: sqlite3.Connection,
    symbol: str,
    opened_at: str,
    status: str = "open",
    closed_at: str | None = None,
    exit_trigger: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO theses
           (symbol, opened_at, thesis, target, invalidation, status, closed_at, exit_trigger)
           VALUES (?, ?, 'why', 'target', 'invalidation', ?, ?, ?)""",
        (symbol, opened_at, status, closed_at, exit_trigger),
    )


def flat_curve(days: int, equity: float = 100_000.0) -> list[tuple[str, float]]:
    """`days` consecutive points at a constant equity — zero volatility, zero return."""
    return [(f"2026-01-{d:02d}", equity) for d in range(1, days + 1)]


# ---------------------------------------------------------------------------
# compute_annualized_return
# ---------------------------------------------------------------------------

def test_annualized_return_empty_curve() -> None:
    assert metrics.compute_annualized_return([]) is None


def test_annualized_return_single_point() -> None:
    assert metrics.compute_annualized_return([("2026-01-02", 100_000.0)]) is None


def test_annualized_return_below_minimum_days_is_none() -> None:
    """Three weeks of data must not produce a confident-looking annual figure."""
    points = [(f"2026-07-{d:02d}", 100_000.0 + d * 100) for d in range(1, 22)]
    assert len(points) < metrics.MIN_TRADING_DAYS_FOR_ANNUALIZING
    assert metrics.compute_annualized_return(points) is None


def test_annualized_return_flat_curve_is_zero() -> None:
    points = [
        (f"2026-{m:02d}-{d:02d}", 100_000.0)
        for m in (1, 2, 3, 4)
        for d in range(1, 21)
    ]
    assert len(points) >= metrics.MIN_TRADING_DAYS_FOR_ANNUALIZING
    assert metrics.compute_annualized_return(points) == pytest.approx(0.0)


def test_annualized_return_doubling_over_a_year() -> None:
    points = [(f"2026-{m:02d}-{d:02d}", 100_000.0) for m in range(1, 13) for d in (1, 5, 10, 15, 20, 25)]
    points[-1] = ("2026-12-25", 200_000.0)
    days = 358  # 2026-01-01 -> 2026-12-25
    expected = (2 ** (metrics.DAYS_PER_YEAR / days) - 1) * 100
    assert metrics.compute_annualized_return(points) == pytest.approx(expected)


def test_annualized_return_zero_span_is_none() -> None:
    """Enough points, but all stamped the same day — dividing by a zero span."""
    points = [("2026-01-02", 100_000.0)] * metrics.MIN_TRADING_DAYS_FOR_ANNUALIZING
    assert metrics.compute_annualized_return(points) is None


def test_annualized_return_wipeout_is_none() -> None:
    """A zero (or negative) ending equity has no real root — report nothing."""
    points = flat_curve(metrics.MIN_TRADING_DAYS_FOR_ANNUALIZING - 1)
    points += [("2026-04-01", 0.0)]
    assert metrics.compute_annualized_return(points) is None


def test_annualized_return_zero_start_is_none() -> None:
    points = [("2026-01-01", 0.0)] + flat_curve(metrics.MIN_TRADING_DAYS_FOR_ANNUALIZING)
    assert metrics.compute_annualized_return(points) is None


def test_annualized_return_unparseable_dates_is_none() -> None:
    points = [("not-a-date", 100_000.0)] * metrics.MIN_TRADING_DAYS_FOR_ANNUALIZING
    assert metrics.compute_annualized_return(points) is None


# ---------------------------------------------------------------------------
# compute_turnover
# ---------------------------------------------------------------------------

def test_turnover_empty_db(conn: sqlite3.Connection) -> None:
    result = metrics.compute_turnover(conn)
    assert result["buy_usd"] == 0.0
    assert result["sell_usd"] == 0.0
    assert result["executed_trades"] == 0
    # No equity history: no denominator, so ratios stay None instead of 0/0.
    assert result["avg_portfolio_usd"] is None
    assert result["turnover_pct"] is None
    assert result["annualized_turnover_pct"] is None


def test_turnover_ignores_rejected_trades(conn: sqlite3.Connection) -> None:
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_trade(conn, cycle, "AAPL", "buy", 10_000.0)
    add_trade(conn, cycle, "TSLA", "buy", 90_000.0, is_valid=False, rejection_reason="exceeds max position size (15%)")
    result = metrics.compute_turnover(conn)
    assert result["buy_usd"] == 10_000.0
    assert result["executed_trades"] == 1


def test_turnover_min_definition_and_two_way(conn: sqlite3.Connection) -> None:
    """min(buys, sells) is the headline; the two-way figure sees the whole flow."""
    c1 = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    c2 = add_cycle(conn, "2026-08-15T20:30:00+00:00", 100_000.0)
    add_trade(conn, c1, "AAPL", "buy", 20_000.0)
    add_trade(conn, c2, "AAPL", "sell", 5_000.0)
    result = metrics.compute_turnover(conn)
    assert result["avg_portfolio_usd"] == 100_000.0
    assert result["turnover_pct"] == pytest.approx(5.0)
    assert result["two_way_turnover_pct"] == pytest.approx(12.5)


def test_turnover_all_buys_no_sells_is_zero_not_error(conn: sqlite3.Connection) -> None:
    """The deployment phase: everything bought, nothing sold — min() is 0."""
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_trade(conn, cycle, "SPY", "buy", 15_000.0)
    result = metrics.compute_turnover(conn)
    assert result["turnover_pct"] == 0.0
    assert result["two_way_turnover_pct"] == pytest.approx(7.5)


def test_turnover_not_annualized_below_minimum(conn: sqlite3.Connection) -> None:
    c1 = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    c2 = add_cycle(conn, "2026-08-04T20:30:00+00:00", 100_000.0)
    add_trade(conn, c1, "AAPL", "buy", 10_000.0)
    add_trade(conn, c2, "AAPL", "sell", 10_000.0)
    result = metrics.compute_turnover(conn)
    assert result["period_days"] < metrics.MIN_CALENDAR_DAYS_FOR_ANNUALIZING
    assert result["turnover_pct"] == pytest.approx(10.0)
    assert result["annualized_turnover_pct"] is None


def test_turnover_annualized_above_minimum(conn: sqlite3.Connection) -> None:
    c1 = add_cycle(conn, "2026-01-01T20:30:00+00:00", 100_000.0)
    c2 = add_cycle(conn, "2026-07-01T20:30:00+00:00", 100_000.0)
    add_trade(conn, c1, "AAPL", "buy", 10_000.0)
    add_trade(conn, c2, "AAPL", "sell", 10_000.0)
    result = metrics.compute_turnover(conn)
    days = result["period_days"]
    assert days >= metrics.MIN_CALENDAR_DAYS_FOR_ANNUALIZING
    assert result["annualized_turnover_pct"] == pytest.approx(10.0 * metrics.DAYS_PER_YEAR / days)


def test_turnover_zero_equity_curve_leaves_ratios_none(conn: sqlite3.Connection) -> None:
    """A funded-at-zero account must not divide by zero."""
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 0.0)
    add_trade(conn, cycle, "AAPL", "buy", 1_000.0)
    result = metrics.compute_turnover(conn)
    assert result["avg_portfolio_usd"] is None
    assert result["turnover_pct"] is None


def test_turnover_accepts_supplied_curve(conn: sqlite3.Connection) -> None:
    """The broker's daily curve is a better denominator than the per-cycle one."""
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 999_999.0)
    add_trade(conn, cycle, "AAPL", "buy", 10_000.0)
    add_trade(conn, cycle, "AAPL", "sell", 10_000.0)
    result = metrics.compute_turnover(conn, points=flat_curve(10, 200_000.0))
    assert result["avg_portfolio_usd"] == 200_000.0
    assert result["turnover_pct"] == pytest.approx(5.0)


def test_equity_curve_from_cycles_buckets_on_market_day(conn: sqlite3.Connection) -> None:
    """20:30 UTC is the same market day (4:30 PM ET), not the next one."""
    add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    assert metrics.equity_curve_from_cycles(conn) == [("2026-07-16", 100_000.0)]


# ---------------------------------------------------------------------------
# compute_rejection_stats
# ---------------------------------------------------------------------------

def test_rejection_stats_empty_table(conn: sqlite3.Connection) -> None:
    stats = metrics.compute_rejection_stats(conn)
    assert stats == {
        "total_proposed": 0,
        "accepted": 0,
        "rejected": 0,
        "rejected_pct": 0.0,
        "by_reason": [],
    }


def test_rejection_stats_all_valid_is_zero_pct(conn: sqlite3.Connection) -> None:
    """The current real-world reading: nothing has ever been rejected."""
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    for symbol in ("AAPL", "MSFT", "SPY"):
        add_trade(conn, cycle, symbol, "buy", 1_000.0)
    stats = metrics.compute_rejection_stats(conn)
    assert stats["total_proposed"] == 3
    assert stats["accepted"] == 3
    assert stats["rejected"] == 0
    assert stats["rejected_pct"] == 0.0
    assert stats["by_reason"] == []


def test_rejection_stats_breakdown_by_reason(conn: sqlite3.Connection) -> None:
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_trade(conn, cycle, "AAPL", "buy", 1_000.0)
    add_trade(conn, cycle, "TSLA", "buy", 90_000.0, is_valid=False, rejection_reason="exceeds max position size (15%)")
    add_trade(conn, cycle, "NVDA", "buy", 90_000.0, is_valid=False, rejection_reason="exceeds max position size (15%)")
    add_trade(conn, cycle, "GME", "buy", 100.0, is_valid=False, rejection_reason="symbol not in whitelisted universe")
    stats = metrics.compute_rejection_stats(conn)
    assert stats["total_proposed"] == 4
    assert stats["rejected"] == 3
    assert stats["rejected_pct"] == pytest.approx(75.0)
    assert [r["reason"] for r in stats["by_reason"]] == [
        "exceeds max position size (15%)",
        "symbol not in whitelisted universe",
    ]
    assert stats["by_reason"][0]["count"] == 2
    assert stats["by_reason"][0]["pct_of_proposals"] == pytest.approx(50.0)


def test_rejection_stats_missing_reason_is_labelled(conn: sqlite3.Connection) -> None:
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_trade(conn, cycle, "AAPL", "buy", 1.0, is_valid=False, rejection_reason=None)
    add_trade(conn, cycle, "MSFT", "buy", 1.0, is_valid=False, rejection_reason="  ")
    stats = metrics.compute_rejection_stats(conn)
    assert stats["by_reason"] == [
        {"reason": "unspecified", "count": 2, "pct_of_proposals": 100.0}
    ]


# ---------------------------------------------------------------------------
# compute_decision_consistency
# ---------------------------------------------------------------------------

def test_consistency_empty_db(conn: sqlite3.Connection) -> None:
    result = metrics.compute_decision_consistency(conn)
    assert result["reviews_total"] == 0
    # Nothing to measure is not the same as perfectly stable.
    assert result["verdict_flip_pct"] is None
    assert result["unmatched_action_pct"] is None
    assert result["mean_holding_days"] is None
    assert result["median_holding_days"] is None
    assert result["exit_triggers"] == []


def test_consistency_single_review_has_no_comparisons(conn: sqlite3.Connection) -> None:
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_review(conn, cycle, "AAPL", "hold")
    result = metrics.compute_decision_consistency(conn)
    assert result["verdict_comparisons"] == 0
    assert result["verdict_flip_pct"] is None


def test_consistency_flip_rate(conn: sqlite3.Connection) -> None:
    """AAPL holds throughout; MSFT flips hold -> trim -> hold."""
    for i, (aapl, msft) in enumerate(
        [("hold", "hold"), ("hold", "trim"), ("hold", "hold")], start=1
    ):
        cycle = add_cycle(conn, f"2026-07-{15 + i}T20:30:00+00:00", 100_000.0)
        add_review(conn, cycle, "AAPL", aapl)
        add_review(conn, cycle, "MSFT", msft)
    result = metrics.compute_decision_consistency(conn)
    assert result["symbols_reviewed"] == 2
    assert result["verdict_comparisons"] == 4
    assert result["verdict_flips"] == 2
    assert result["verdict_flip_pct"] == pytest.approx(50.0)
    assert result["verdict_counts"] == {"hold": 5, "trim": 1}


def test_consistency_verdicts_compared_across_gaps(conn: sqlite3.Connection) -> None:
    """A symbol skipped for a cycle must not register as a flip."""
    c1 = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    c2 = add_cycle(conn, "2026-07-17T20:30:00+00:00", 100_000.0)
    c3 = add_cycle(conn, "2026-07-18T20:30:00+00:00", 100_000.0)
    add_review(conn, c1, "AAPL", "hold")
    add_review(conn, c2, "MSFT", "hold")
    add_review(conn, c3, "AAPL", "hold")
    result = metrics.compute_decision_consistency(conn)
    assert result["verdict_comparisons"] == 1
    assert result["verdict_flips"] == 0


def test_consistency_action_verdict_matched_by_sell(conn: sqlite3.Connection) -> None:
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_review(conn, cycle, "MSFT", "trim")
    add_trade(conn, cycle, "MSFT", "sell", 700.0)
    result = metrics.compute_decision_consistency(conn)
    assert result["action_verdicts"] == 1
    assert result["unmatched_action_verdicts"] == 0
    assert result["unmatched_action_pct"] == pytest.approx(0.0)


def test_consistency_action_verdict_without_sell_is_unmatched(conn: sqlite3.Connection) -> None:
    """A rejected sell doesn't count — the verdict still went unacted on."""
    cycle = add_cycle(conn, "2026-07-16T20:30:00+00:00", 100_000.0)
    add_review(conn, cycle, "MSFT", "trim")
    add_review(conn, cycle, "AAPL", "exit")
    add_trade(conn, cycle, "AAPL", "sell", 100.0, is_valid=False, rejection_reason="cannot sell more than held position")
    result = metrics.compute_decision_consistency(conn)
    assert result["action_verdicts"] == 2
    assert result["unmatched_action_verdicts"] == 2
    assert result["unmatched_action_pct"] == pytest.approx(100.0)


def test_consistency_holding_periods_and_exit_mix(conn: sqlite3.Connection) -> None:
    add_thesis(conn, "AAPL", "2026-01-01T20:30:00+00:00", "closed", "2026-01-11T20:30:00+00:00", "target_hit")
    add_thesis(conn, "MSFT", "2026-01-01T20:30:00+00:00", "closed", "2026-01-21T20:30:00+00:00", "thesis_broken")
    add_thesis(conn, "SPY", "2026-01-01T20:30:00+00:00", "closed", "2026-02-01T20:30:00+00:00", "thesis_broken")
    add_thesis(conn, "JPM", "2026-01-01T20:30:00+00:00")  # still open — excluded
    result = metrics.compute_decision_consistency(conn)
    assert result["closed_theses"] == 3
    assert result["median_holding_days"] == pytest.approx(20.0)
    assert result["mean_holding_days"] == pytest.approx((10 + 20 + 31) / 3)
    assert result["exit_triggers"][0] == {
        "trigger": "thesis_broken",
        "count": 2,
        "pct_of_exits": pytest.approx(200 / 3),
    }


def test_consistency_open_theses_only_gives_no_holding_period(conn: sqlite3.Connection) -> None:
    """Nothing has been closed yet — the current real-world state."""
    add_thesis(conn, "AAPL", "2026-07-27T05:41:47+00:00")
    result = metrics.compute_decision_consistency(conn)
    assert result["closed_theses"] == 0
    assert result["mean_holding_days"] is None
    assert result["exit_triggers"] == []
