"""Tests for performance.py — pure series in, scorecard out; no broker.

The live numbers were also checked once against dashboard.build_comparison on
the account's own history (2026-10-20): return, SPY return, excess, max
drawdown and Sharpe agreed to the second decimal.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import agent
import dashboard
import performance as perf


def _series(values: list[float]) -> list[tuple[str, float]]:
    return [((date(2026, 1, 1) + timedelta(days=i)).isoformat(), v)
            for i, v in enumerate(values)]


def _wiggle(n: int, drift: float, amp: float, start: float = 100.0) -> list[float]:
    out = [start]
    for i in range(n):
        out.append(out[-1] * (1 + drift + amp * math.sin(i * 1.7)))
    return out


def test_too_little_history_is_unavailable_not_zero():
    assert "unavailable" in perf.build_performance(_series([100.0]), _series([50.0]))
    assert "unavailable" in perf.build_performance([], [])


def test_short_record_reports_returns_but_withholds_ratios():
    out = perf.build_performance(_series([100.0, 101.0, 103.0]), _series([50.0, 50.5, 51.0]))
    since = out["since_inception"]
    assert since["return_pct"] == {"you": 3.0, "spy": 2.0, "excess_pp": 1.0}
    assert since["sharpe"] == {"you": None, "spy": None}
    assert since["beta_to_spy"] is None
    assert since["annualized_return_pct"] is None
    assert "last_21_trading_days" not in out


def test_series_join_on_date_not_position():
    agent_pts = [("2026-01-02", 100.0), ("2026-01-03", 110.0), ("2026-01-05", 121.0)]
    # SPY has an extra day the account does not; it must be skipped, not shift pairs.
    spy_pts = [("2026-01-02", 10.0), ("2026-01-03", 10.0),
               ("2026-01-04", 99.0), ("2026-01-05", 10.0)]
    out = perf.build_performance(agent_pts, spy_pts)
    assert out["trading_days"] == 2
    assert out["since_inception"]["return_pct"]["spy"] == 0.0


def test_drawdowns():
    assert perf.drawdowns_pct([100, 120, 90, 110]) == (-25.0, (110 / 120 - 1) * 100)
    assert perf.drawdowns_pct([100, 101, 102]) == (0.0, 0.0)


def test_book_that_is_twice_the_market_has_beta_two():
    spy = _wiggle(40, 0.0005, 0.01)
    spy_r = perf._returns(spy)
    book = [100.0]
    for r in spy_r:
        book.append(book[-1] * (1 + 2 * r))
    beta, corr = perf.beta_and_correlation(perf._returns(book), spy_r)
    assert math.isclose(beta, 2.0, rel_tol=1e-9)
    assert math.isclose(corr, 1.0, rel_tol=1e-9)


def test_sharpe_matches_the_dashboard_definition():
    values = _wiggle(30, 0.001, 0.008)
    pts = _series(values)
    assert math.isclose(perf.sharpe(perf._returns(values)), dashboard.compute_sharpe(pts))


def test_trailing_windows_appear_once_there_is_enough_history():
    values = _wiggle(70, 0.0004, 0.006)
    out = perf.build_performance(_series(values), _series(_wiggle(70, 0.0002, 0.005, 500.0)))
    assert "last_21_trading_days" in out and "last_63_trading_days" in out
    w = out["last_21_trading_days"]["return_pct"]["you"]
    assert w == round((values[-1] / values[-22] - 1) * 100, 2)


def test_annualized_return_is_not_scaled_twice():
    # 70 points spanning ~70 calendar days at +5% must annualise to tens of
    # percent, not thousands: metrics already returns a percentage.
    values = [100.0 * (1.05 ** (i / 69)) for i in range(70)]
    out = perf.build_performance(_series(values), _series(values))
    assert 10 < out["since_inception"]["annualized_return_pct"] < 100


def test_prompt_carries_the_block_and_the_objective():
    assert "YOUR OBJECTIVE" in agent.SYSTEM_PROMPT
    assert 'WHAT "your_performance" IS' in agent.SYSTEM_PROMPT


def test_fetch_never_raises(monkeypatch):
    conn = agent.init_db(":memory:")
    assert "unavailable" in perf.fetch_performance(conn, "paper", agent.markettime.now_utc())
    conn.execute("INSERT INTO decision_cycles (timestamp, mode, prompt, claude_response,"
                 " portfolio_cash, portfolio_equity) VALUES ('2026-01-02T21:00:00+00:00',"
                 " 'paper', '', '', 0, 0)")

    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(perf, "_agent_equity_curve", boom)
    out = perf.fetch_performance(conn, "paper", agent.markettime.now_utc())
    assert out == {"unavailable": "the equity or SPY history could not be fetched this cycle"}
