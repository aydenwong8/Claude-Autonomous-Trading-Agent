"""The agent's own scorecard, as shown to it in the prompt.

The objective is the best risk-adjusted return against SPY (Sharpe, excess
return, drawdown), but until 2026-10-20 the agent was told that only in
passing and never shown a single one of those numbers: the dashboard computed
them for the operator and nothing fed them back. This builds the same picture
for the prompt.

Same sources and conventions as dashboard.py, so the two never disagree: the
broker's daily equity curve from the first decision cycle, IEX daily SPY
closes, Sharpe annualised over 252 days with a zero risk-free rate. The two
series are joined on date rather than paired by position; on the account's
own history (68 days to 2026-10-19) every date matched.

`fetch_performance` never fails the cycle. Any problem becomes
`{"unavailable": reason}`, which the prompt explains, so the agent decides
without its scorecard rather than not at all.
"""

from __future__ import annotations

import math
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import markettime
import metrics
from broker import _data_client, _trading_client

TRADING_DAYS_PER_YEAR = 252
# Mirrors dashboard.MIN_RETURNS_FOR_SHARPE: below this a standard deviation
# says nothing, and None is more honest than a confident-looking number.
MIN_RETURNS_FOR_RATIOS = 5
# Beta and correlation off fewer daily pairs than this are mostly noise.
MIN_RETURNS_FOR_BETA = 20
# Trailing windows, in trading days: roughly one and three months.
TRAILING_WINDOWS = {"last_21_trading_days": 21, "last_63_trading_days": 63}


def _returns(values: list[float]) -> list[float]:
    return [curr / prev - 1 for prev, curr in zip(values, values[1:]) if prev > 0]


def _stdev(xs: list[float]) -> float:
    mean = sum(xs) / len(xs)
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(returns: list[float]) -> float | None:
    """Annualised, zero risk-free rate — the same definition as dashboard.compute_sharpe."""
    if len(returns) < MIN_RETURNS_FOR_RATIOS:
        return None
    sd = _stdev(returns)
    if sd == 0:
        return None
    return sum(returns) / len(returns) / sd * math.sqrt(TRADING_DAYS_PER_YEAR)


def annualized_vol_pct(returns: list[float]) -> float | None:
    if len(returns) < MIN_RETURNS_FOR_RATIOS:
        return None
    return _stdev(returns) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100


def drawdowns_pct(values: list[float]) -> tuple[float, float]:
    """(max drawdown, current drawdown from the running peak), both <= 0."""
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, (v / peak - 1) * 100)
    return worst, (values[-1] / peak - 1) * 100


def beta_and_correlation(
    agent: list[float], spy: list[float]
) -> tuple[float | None, float | None]:
    if len(agent) < MIN_RETURNS_FOR_BETA:
        return None, None
    ma, ms = sum(agent) / len(agent), sum(spy) / len(spy)
    cov = sum((a - ma) * (s - ms) for a, s in zip(agent, spy)) / (len(agent) - 1)
    va, vs = _stdev(agent) ** 2, _stdev(spy) ** 2
    if vs == 0:
        return None, None
    corr = cov / math.sqrt(va * vs) if va else None
    return cov / vs, corr


def _r(x: float | None, places: int = 2) -> float | None:
    return None if x is None else round(x, places)


def _period(agent_eq: list[float], spy_px: list[float]) -> dict[str, Any]:
    """Return, Sharpe, volatility and drawdown for both series over one window."""
    ra, rs = _returns(agent_eq), _returns(spy_px)
    agent_ret = (agent_eq[-1] / agent_eq[0] - 1) * 100
    spy_ret = (spy_px[-1] / spy_px[0] - 1) * 100
    agent_mdd, _ = drawdowns_pct(agent_eq)
    spy_mdd, _ = drawdowns_pct(spy_px)
    return {
        "return_pct": {"you": _r(agent_ret), "spy": _r(spy_ret),
                       "excess_pp": _r(agent_ret - spy_ret)},
        "sharpe": {"you": _r(sharpe(ra)), "spy": _r(sharpe(rs))},
        "annualized_volatility_pct": {"you": _r(annualized_vol_pct(ra)),
                                      "spy": _r(annualized_vol_pct(rs))},
        "max_drawdown_pct": {"you": _r(agent_mdd), "spy": _r(spy_mdd)},
    }


def build_performance(
    agent_points: list[tuple[str, float]], spy_points: list[tuple[str, float]]
) -> dict[str, Any]:
    """The scorecard from two (date, value) daily series. Pure; covered by tests."""
    spy_by_date = dict(spy_points)
    joined = [(d, eq, spy_by_date[d]) for d, eq in agent_points if d in spy_by_date]
    if len(joined) < 2:
        return {"unavailable": "fewer than two trading days of history"}

    dates = [d for d, _, _ in joined]
    agent_eq = [eq for _, eq, _ in joined]
    spy_px = [px for _, _, px in joined]

    since = _period(agent_eq, spy_px)
    _, current_dd = drawdowns_pct(agent_eq)
    since["current_drawdown_from_peak_pct"] = _r(current_dd)
    beta, corr = beta_and_correlation(_returns(agent_eq), _returns(spy_px))
    since["beta_to_spy"] = _r(beta)
    since["correlation_to_spy"] = _r(corr)
    # Already a percentage.
    since["annualized_return_pct"] = _r(
        metrics.compute_annualized_return(list(zip(dates, agent_eq)))
    )

    out: dict[str, Any] = {
        "first_day": dates[0],
        "through_close_of": dates[-1],
        "trading_days": len(dates) - 1,
        "since_inception": since,
    }
    for label, n in TRAILING_WINDOWS.items():
        if len(dates) > n:
            out[label] = _period(agent_eq[-(n + 1):], spy_px[-(n + 1):])
    return out


def _experiment_start(conn: sqlite3.Connection) -> datetime | None:
    (first,) = conn.execute("SELECT MIN(timestamp) FROM decision_cycles").fetchone()
    return datetime.fromisoformat(first) if first else None


def _agent_equity_curve(mode: str, start: datetime, now: datetime) -> list[tuple[str, float]]:
    """As dashboard.fetch_agent_equity_curve: `end` is required alongside `start`,
    or the endpoint silently truncates the window to one month."""
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    hist = _trading_client(mode).get_portfolio_history(
        GetPortfolioHistoryRequest(start=start, end=now, timeframe="1D")
    )
    return [
        (markettime.fmt_et_date(datetime.fromtimestamp(ts, tz=timezone.utc)), float(eq))
        for ts, eq in zip(hist.timestamp, hist.equity)
        if eq and eq > 0
    ]


def _spy_closes(start: datetime, now: datetime) -> list[tuple[str, float]]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    bars = _data_client().get_stock_bars(StockBarsRequest(
        symbol_or_symbols=["SPY"],
        timeframe=TimeFrame.Day,
        # A day early so the first session is never cut by the time of day.
        start=start - timedelta(days=1),
        end=now,
        feed=DataFeed.IEX,
    ))
    return [(bar.timestamp.date().isoformat(), float(bar.close)) for bar in bars["SPY"]]


def fetch_performance(conn: sqlite3.Connection, mode: str, now: datetime) -> dict[str, Any]:
    start = _experiment_start(conn)
    if start is None:
        return {"unavailable": "no decision cycle has completed yet"}
    try:
        return build_performance(_agent_equity_curve(mode, start, now), _spy_closes(start, now))
    except Exception:
        traceback.print_exc()
        return {"unavailable": "the equity or SPY history could not be fetched this cycle"}
