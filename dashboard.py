"""Generate a static HTML performance dashboard: agent portfolio vs. buy-and-hold SPY.

Pulls the account's daily equity curve from Alpaca's portfolio-history endpoint,
pulls SPY daily closes for the same window, indexes both to a $100k start, and
renders a self-contained dashboard.html (no server, no external requests at
view time) alongside the decision-cycle / trade log from trading_agent.db.

Run: python dashboard.py [--mode paper|live] [--db-path trading_agent.db] [--out dashboard.html]
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

# The market this agent trades runs on New York time; report timestamps there
# rather than UTC so "after the close" reads as 4:00 PM.
EASTERN = ZoneInfo("America/New_York")


def _now_eastern() -> datetime:
    return datetime.now(EASTERN)
from agent import _data_client, _trading_client


def fetch_agent_equity_curve(mode: str) -> list[tuple[str, float]]:
    """Daily (date, equity) points since account inception, weekend/zero days dropped."""
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    client = _trading_client(mode)
    hist = client.get_portfolio_history(
        GetPortfolioHistoryRequest(period="1M", timeframe="1D")
    )
    points = []
    for ts, equity in zip(hist.timestamp, hist.equity):
        if equity and equity > 0:
            date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            points.append((date, float(equity)))
    return points


def fetch_account_summary(mode: str, holdings: list[dict]) -> dict:
    """Cash, equity, and today's P&L, with equity derived from `holdings`.

    Equity is deliberately computed as (sum of position values + cash) using the
    same position snapshot the holdings table renders, rather than read from
    account.equity. The broker marks positions continuously, so an independent
    account call lands a few seconds later at slightly different prices and the
    headline number would disagree with the table it sits above.

    Day P&L is that equity against the prior session's closing equity. Cash is
    reported against the 5% minimum buffer the validation stage enforces, so a
    position-sizing squeeze is visible on the dashboard.
    """
    account = _trading_client(mode).get_account()
    cash = float(account.cash)
    last_equity = float(account.last_equity)
    equity = sum(h["value"] for h in holdings) + cash
    return {
        "cash": cash,
        "equity": equity,
        "cash_pct": (cash / equity * 100) if equity else 0.0,
        "min_cash_buffer_pct": config.MIN_CASH_BUFFER_PCT * 100,
        "day_pnl_usd": equity - last_equity,
        "day_pnl_pct": (equity / last_equity - 1) * 100 if last_equity else 0.0,
    }


def fetch_holdings(mode: str) -> list[dict]:
    """Current positions with per-holding day change and unrealised return.

    Company names come from the broker asset record; a lookup failure falls
    back to the ticker rather than dropping the row.
    """
    client = _trading_client(mode)
    holdings = []
    for p in client.get_all_positions():
        try:
            company = client.get_asset(p.symbol).name
        except Exception:
            company = p.symbol
        holdings.append(
            {
                "company": company,
                "symbol": p.symbol,
                "price": float(p.current_price),
                "qty": float(p.qty),
                "value": float(p.market_value),
                "day_change_usd": float(p.unrealized_intraday_pl),
                "day_change_pct": float(p.unrealized_intraday_plpc) * 100,
                "roi_usd": float(p.unrealized_pl),
                "roi_pct": float(p.unrealized_plpc) * 100,
            }
        )
    holdings.sort(key=lambda h: h["value"], reverse=True)
    return holdings


def fetch_spy_closes(start: str, end: str) -> list[tuple[str, float]]:
    """Daily SPY closing prices between start and end (inclusive), as trading-day sequence."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed

    client = _data_client()
    req = StockBarsRequest(
        symbol_or_symbols=["SPY"],
        timeframe=TimeFrame.Day,
        start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
        end=datetime.fromisoformat(end).replace(hour=23, minute=59, tzinfo=timezone.utc),
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(req)
    return [(bar.timestamp.date().isoformat(), float(bar.close)) for bar in bars["SPY"]]


def fetch_decision_log(db_path: str) -> list[dict]:
    """Decision cycles with their trades, most recent first."""
    conn = sqlite3.connect(db_path)
    cycles = conn.execute(
        "SELECT id, timestamp, mode, portfolio_cash, portfolio_equity FROM decision_cycles ORDER BY id DESC"
    ).fetchall()
    log = []
    for cid, ts, mode, cash, equity in cycles:
        trades = conn.execute(
            """SELECT symbol, side, notional_usd, is_valid, rejection_reason, order_status
               FROM trades WHERE cycle_id = ? ORDER BY id""",
            (cid,),
        ).fetchall()
        log.append(
            {
                "id": cid,
                "timestamp": ts,
                "mode": mode,
                "cash": cash,
                "equity": equity,
                "trades": [
                    {
                        "symbol": t[0],
                        "side": t[1],
                        "notional_usd": t[2],
                        "is_valid": bool(t[3]),
                        "rejection_reason": t[4],
                        "order_status": t[5],
                    }
                    for t in trades
                ],
            }
        )
    conn.close()
    return log


STALE_RUN_AFTER = timedelta(hours=24)


def fetch_last_successful_run(db_path: str, now: datetime | None = None) -> dict | None:
    """Newest successful run_log row, with whether it is older than 24h.

    Returns None when the agent has never completed a run — or when the db
    pre-dates run_log, which reads the same way from the dashboard's point of
    view: no successful cycle we can point at.
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT timestamp FROM run_log WHERE outcome = 'success' ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    if not row:
        return None

    timestamp = row[0]
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return {"timestamp": timestamp, "stale": False}
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    age = (now or datetime.now(timezone.utc)) - parsed
    return {
        # Runs are logged in UTC; show them on market time like everything else.
        "timestamp": parsed.astimezone(EASTERN).strftime("%Y-%m-%d %-I:%M %p ET"),
        "stale": age > STALE_RUN_AFTER,
    }


TRADING_DAYS_PER_YEAR = 252
# Below this, the standard deviation of a handful of days says nothing useful —
# better to show "not yet" than a confident-looking number built on 3 returns.
MIN_RETURNS_FOR_SHARPE = 5


def compute_sharpe(
    points: list[tuple[str, float]], risk_free_annual: float = 0.0
) -> float | None:
    """Annualised Sharpe ratio from a daily equity curve.

    Returns None when there are too few daily returns to be meaningful, or when
    the curve is perfectly flat (zero volatility — Sharpe undefined).
    """
    returns = [
        curr / prev - 1
        for (_, prev), (_, curr) in zip(points, points[1:])
        if prev > 0
    ]
    if len(returns) < MIN_RETURNS_FOR_SHARPE:
        return None

    daily_rf = (1 + risk_free_annual) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    excess = [r - daily_rf for r in returns]
    mean = sum(excess) / len(excess)
    stdev = math.sqrt(sum((r - mean) ** 2 for r in excess) / (len(excess) - 1))
    if stdev == 0:
        return None
    return mean / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)


def build_comparison(
    agent_points: list[tuple[str, float]], spy_points: list[tuple[str, float]]
) -> dict:
    """Align the two series by trading-day sequence (see dashboard.py docstring caveat:
    Alpaca's account-history day boundaries don't always match market-data day
    boundaries 1:1, so we pair by position rather than by date string) and index
    both to 100 at day 0.
    """
    n = min(len(agent_points), len(spy_points))
    agent_points = agent_points[-n:]
    spy_points = spy_points[-n:]

    agent_base = agent_points[0][1]
    spy_base = spy_points[0][1]

    series = []
    for (_, agent_eq), (spy_date, spy_close) in zip(agent_points, spy_points):
        series.append(
            {
                "date": spy_date,
                "agent_equity": agent_eq,
                "agent_index": agent_eq / agent_base * 100,
                "spy_close": spy_close,
                "spy_index": spy_close / spy_base * 100,
            }
        )

    agent_return_pct = (agent_points[-1][1] / agent_base - 1) * 100
    spy_return_pct = (spy_points[-1][1] / spy_base - 1) * 100

    running_peak = agent_points[0][1]
    max_drawdown_pct = 0.0
    for _, eq in agent_points:
        running_peak = max(running_peak, eq)
        drawdown = (eq / running_peak - 1) * 100
        max_drawdown_pct = min(max_drawdown_pct, drawdown)

    return {
        "series": series,
        "agent_return_pct": agent_return_pct,
        "spy_return_pct": spy_return_pct,
        "alpha_pp": agent_return_pct - spy_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "current_equity": agent_points[-1][1],
        "sharpe": compute_sharpe(agent_points),
        "sharpe_min_days": MIN_RETURNS_FOR_SHARPE,
    }


def render_dashboard(
    comparison: dict,
    decision_log: list[dict],
    mode: str,
    last_run: dict | None = None,
    holdings: list[dict] | None = None,
    account: dict | None = None,
) -> str:
    data_json = json.dumps(
        {
            "comparison": comparison,
            "decisions": decision_log,
            "mode": mode,
            "last_run": last_run,
            "holdings": holdings or [],
            "account": account,
        }
    )
    generated_at = _now_eastern().strftime("%Y-%m-%d %-I:%M %p ET")
    return DASHBOARD_TEMPLATE.replace("__DATA_JSON__", data_json).replace(
        "__GENERATED_AT__", html.escape(generated_at)
    )


DASHBOARD_TEMPLATE = r"""<!doctype html>
<title>Agent vs. SPY — Performance Dashboard</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  .viz-root {
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --surface-2:      #f3f2ef;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #86847d;
    --border:         #e4e2dc;
    --series-1:       #2a78d6; /* agent */
    --series-2:       #008300; /* spy buy & hold */
    --good:           #008300;
    --bad:            #e34948;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --surface-2:      #232322;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #8b897f;
      --border:         #38372f;
      --series-1:       #3987e5;
      --series-2:       #008300;
      --good:           #3fbf3f;
      --bad:            #e66767;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --surface-2:      #232322;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #8b897f;
    --border:         #38372f;
    --series-1:       #3987e5;
    --series-2:       #008300;
    --good:           #3fbf3f;
    --bad:            #e66767;
  }

  * { box-sizing: border-box; }
  body { margin: 0; }
  .viz-root {
    background: var(--surface-1);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: 24px 20px 64px;
    max-width: 980px;
    margin: 0 auto;
  }
  h1 { font-size: 1.35rem; margin: 0 0 4px; }
  .subtitle { color: var(--text-secondary); font-size: 0.9rem; margin: 0 0 28px; }

  .stat-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 28px;
  }
  .stat-tile {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .stat-label { font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 6px; }
  .stat-value { font-size: 1.5rem; font-weight: 600; }
  .stat-value.good { color: var(--good); }
  .stat-value.bad { color: var(--bad); }

  .card {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 24px;
    overflow-x: auto;
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    flex-wrap: wrap;
    gap: 8px;
  }
  .card-title { font-size: 0.95rem; font-weight: 600; }

  .legend { display: flex; gap: 16px; font-size: 0.82rem; color: var(--text-secondary); }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .legend-swatch { width: 14px; height: 2px; border-radius: 1px; }

  .toggle-btn {
    font-size: 0.78rem;
    color: var(--text-secondary);
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 10px;
    cursor: pointer;
  }
  .toggle-btn:hover { color: var(--text-primary); border-color: var(--text-muted); }

  svg { display: block; width: 100%; height: auto; overflow: visible; }
  .gridline { stroke: var(--border); stroke-width: 1; }
  .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .end-dot { r: 4; stroke: var(--surface-2); stroke-width: 2; }
  .axis-label { fill: var(--text-muted); font-size: 10px; }
  .direct-label { font-size: 11px; font-weight: 600; }
  .crosshair { stroke: var(--text-muted); stroke-width: 1; opacity: 0; pointer-events: none; }

  .tooltip {
    position: absolute;
    pointer-events: none;
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 0.78rem;
    box-shadow: 0 4px 14px rgba(0,0,0,0.15);
    opacity: 0;
    transition: opacity 0.08s;
    white-space: nowrap;
    z-index: 10;
  }
  .tooltip-date { color: var(--text-secondary); margin-bottom: 4px; }
  .tooltip-row { display: flex; align-items: center; gap: 6px; }
  .tooltip-key { width: 10px; height: 2px; border-radius: 1px; flex: none; }
  .tooltip-value { font-weight: 600; margin-left: auto; padding-left: 12px; }

  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--text-secondary); font-weight: 500; font-size: 0.75rem; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; }
  th.num { text-align: right; }
  td.num.good { color: var(--good); }
  td.num.bad { color: var(--bad); }
  td.ticker { font-weight: 600; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.72rem; }
  .pill.valid { background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }
  .pill.rejected { background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }
  .muted { color: var(--text-muted); }
  .chart-wrap { position: relative; }
  .hidden { display: none; }
  .caveat { font-size: 0.76rem; color: var(--text-muted); margin-top: 10px; }

  .last-run {
    font-size: 0.82rem;
    color: var(--text-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    margin-bottom: 16px;
  }
  .last-run.stale {
    color: var(--bad);
    border-color: color-mix(in srgb, var(--bad) 45%, transparent);
    background: color-mix(in srgb, var(--bad) 10%, transparent);
    font-weight: 600;
  }
  footer { color: var(--text-muted); font-size: 0.75rem; margin-top: 8px; }
</style>

<div class="viz-root">
  <div class="last-run" id="last-run"></div>
  <h1>Agent vs. SPY — Performance Dashboard</h1>
  <p class="subtitle" id="subtitle"></p>

  <div class="stat-row" id="stat-row"></div>

  <div class="card">
    <div class="card-title" style="margin-bottom: 12px;">Holdings</div>
    <table id="holdings-table"></table>
    <p class="caveat">Day change is per-position intraday P&amp;L from the broker. Its total can differ slightly from the account-level Day P&amp;L tile, which is measured against yesterday's closing equity and also reflects cash and any positions opened or closed today.</p>
  </div>

  <div class="card">
    <div class="card-header">
      <div class="card-title">Cumulative return, indexed to 100 at inception</div>
      <div style="display:flex; align-items:center; gap:14px;">
        <div class="legend" id="legend"></div>
        <button class="toggle-btn" id="table-toggle">View as table</button>
      </div>
    </div>
    <div class="chart-wrap" id="chart-wrap"></div>
    <table class="hidden" id="data-table"></table>
    <p class="caveat">Agent equity and SPY closes are paired by trading-day sequence, not raw date string — Alpaca's account-history day markers and market-data bar dates don't always land on identical UTC calendar days.</p>
  </div>

  <div class="card">
    <div class="card-title" style="margin-bottom: 12px;">Decision cycle log</div>
    <table id="decision-table"></table>
  </div>

  <footer id="footer"></footer>
</div>

<script>
const DATA = __DATA_JSON__;
const GENERATED_AT = "__GENERATED_AT__";

function fmtPct(v) {
  const s = (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  return s;
}
function fmtUsd(v) {
  return "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtUsd0(v) {
  return "$" + Math.round(v).toLocaleString();
}
function fmtUsdSigned(v) {
  return (v >= 0 ? "+" : "-") + fmtUsd(Math.abs(v));
}
function fmtQty(v) {
  // Notional orders produce fractional share counts; whole shares stay clean.
  return Number.isInteger(v) ? String(v) : v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

function renderStats(cmp, account) {
  const tiles = [];

  if (account) {
    tiles.push({
      label: "Day P&L",
      value: fmtUsdSigned(account.day_pnl_usd) + " (" + fmtPct(account.day_pnl_pct) + ")",
      cls: account.day_pnl_usd >= 0 ? "good" : "bad",
    });
    tiles.push({
      label: "Cash (" + account.cash_pct.toFixed(1) + "% of equity)",
      value: fmtUsd0(account.cash),
      // Below the enforced buffer, new buys get rejected — flag it.
      cls: account.cash_pct < account.min_cash_buffer_pct ? "bad" : "",
    });
  }

  tiles.push(
    { label: "Agent total return", value: fmtPct(cmp.agent_return_pct), cls: cmp.agent_return_pct >= 0 ? "good" : "bad" },
    { label: "SPY buy & hold return", value: fmtPct(cmp.spy_return_pct), cls: cmp.spy_return_pct >= 0 ? "good" : "bad" },
    { label: "Alpha vs. SPY", value: fmtPct(cmp.alpha_pp) + " pp", cls: cmp.alpha_pp >= 0 ? "good" : "bad" },
    cmp.sharpe === null || cmp.sharpe === undefined
      ? { label: "Sharpe — needs " + cmp.sharpe_min_days + "+ trading days", value: "—", cls: "" }
      : { label: "Sharpe (annualised)", value: cmp.sharpe.toFixed(2), cls: cmp.sharpe >= 0 ? "good" : "bad" },
    { label: "Agent max drawdown", value: fmtPct(cmp.max_drawdown_pct), cls: cmp.max_drawdown_pct < 0 ? "bad" : "good" },
    // Live account equity, not the daily-history curve's last point — that point
    // is the prior session's close and would disagree with the holdings table.
    { label: "Current equity", value: fmtUsd0(account ? account.equity : cmp.current_equity), cls: "" },
  );
  const row = document.getElementById("stat-row");
  tiles.forEach(t => {
    const el = document.createElement("div");
    el.className = "stat-tile";
    const label = document.createElement("div");
    label.className = "stat-label";
    label.textContent = t.label;
    const value = document.createElement("div");
    value.className = "stat-value" + (t.cls ? " " + t.cls : "");
    value.textContent = t.value;
    el.appendChild(label);
    el.appendChild(value);
    row.appendChild(el);
  });
}

function renderLegend() {
  const legend = document.getElementById("legend");
  const items = [
    { label: "Agent portfolio", color: "var(--series-1)" },
    { label: "SPY buy & hold", color: "var(--series-2)" },
  ];
  items.forEach(it => {
    const item = document.createElement("div");
    item.className = "legend-item";
    const swatch = document.createElement("div");
    swatch.className = "legend-swatch";
    swatch.style.background = it.color;
    const label = document.createElement("span");
    label.textContent = it.label;
    item.appendChild(swatch);
    item.appendChild(label);
    legend.appendChild(item);
  });
}

function renderChart(series) {
  const W = 900, H = 320, padL = 44, padR = 16, padT = 16, padB = 28;
  const innerW = W - padL - padR, innerH = H - padT - padB;

  const agentVals = series.map(d => d.agent_index);
  const spyVals = series.map(d => d.spy_index);
  const all = agentVals.concat(spyVals);
  const min = Math.min(...all), max = Math.max(...all);
  const pad = Math.max((max - min) * 0.15, 0.5);
  const yMin = min - pad, yMax = max + pad;

  const x = i => padL + (series.length === 1 ? innerW / 2 : (i / (series.length - 1)) * innerW);
  const y = v => padT + innerH - ((v - yMin) / (yMax - yMin)) * innerH;

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Line chart comparing agent portfolio return to SPY buy-and-hold return over time");

  // gridlines (4 horizontal steps)
  const steps = 4;
  for (let s = 0; s <= steps; s++) {
    const v = yMin + (s / steps) * (yMax - yMin);
    const yy = y(v);
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", padL); line.setAttribute("x2", W - padR);
    line.setAttribute("y1", yy); line.setAttribute("y2", yy);
    line.setAttribute("class", "gridline");
    svg.appendChild(line);
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", 4); label.setAttribute("y", yy + 3);
    label.setAttribute("class", "axis-label");
    label.textContent = v.toFixed(1);
    svg.appendChild(label);
  }

  function pathFor(vals) {
    return vals.map((v, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(" ");
  }

  const agentPath = document.createElementNS(svgNS, "path");
  agentPath.setAttribute("d", pathFor(agentVals));
  agentPath.setAttribute("class", "line");
  agentPath.setAttribute("stroke", "var(--series-1)");
  svg.appendChild(agentPath);

  const spyPath = document.createElementNS(svgNS, "path");
  spyPath.setAttribute("d", pathFor(spyVals));
  spyPath.setAttribute("class", "line");
  spyPath.setAttribute("stroke", "var(--series-2)");
  svg.appendChild(spyPath);

  // end dots + direct labels
  function endDot(vals, color, label) {
    const i = vals.length - 1;
    const dot = document.createElementNS(svgNS, "circle");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(vals[i]));
    dot.setAttribute("class", "end-dot");
    dot.setAttribute("fill", color);
    svg.appendChild(dot);
    const text = document.createElementNS(svgNS, "text");
    text.setAttribute("x", x(i) + 8); text.setAttribute("y", y(vals[i]) + 4);
    text.setAttribute("class", "direct-label");
    text.setAttribute("fill", color);
    text.textContent = label;
    svg.appendChild(text);
  }
  endDot(agentVals, "var(--series-1)", agentVals[agentVals.length - 1].toFixed(1));
  endDot(spyVals, "var(--series-2)", spyVals[spyVals.length - 1].toFixed(1));

  // x-axis date labels (first, last, and middle if room)
  [0, series.length - 1].forEach(i => {
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("x", x(i)); t.setAttribute("y", H - 6);
    t.setAttribute("class", "axis-label");
    t.setAttribute("text-anchor", i === 0 ? "start" : "end");
    t.textContent = series[i].date;
    svg.appendChild(t);
  });

  // crosshair
  const crosshair = document.createElementNS(svgNS, "line");
  crosshair.setAttribute("y1", padT); crosshair.setAttribute("y2", H - padB);
  crosshair.setAttribute("class", "crosshair");
  svg.appendChild(crosshair);

  const wrap = document.getElementById("chart-wrap");
  wrap.appendChild(svg);

  const tooltip = document.createElement("div");
  tooltip.className = "tooltip";
  wrap.appendChild(tooltip);

  function showAt(i) {
    const d = series[i];
    crosshair.setAttribute("x1", x(i));
    crosshair.setAttribute("x2", x(i));
    crosshair.style.opacity = 1;

    tooltip.innerHTML = "";
    const dateEl = document.createElement("div");
    dateEl.className = "tooltip-date";
    dateEl.textContent = d.date;
    tooltip.appendChild(dateEl);

    [
      { label: "Agent", value: d.agent_index, color: "var(--series-1)" },
      { label: "SPY", value: d.spy_index, color: "var(--series-2)" },
    ].forEach(row => {
      const r = document.createElement("div");
      r.className = "tooltip-row";
      const key = document.createElement("div");
      key.className = "tooltip-key";
      key.style.background = row.color;
      const label = document.createElement("span");
      label.textContent = row.label;
      const val = document.createElement("span");
      val.className = "tooltip-value";
      val.textContent = row.value.toFixed(2);
      r.appendChild(key); r.appendChild(label); r.appendChild(val);
      tooltip.appendChild(r);
    });

    const px = (x(i) / W) * wrap.clientWidth;
    tooltip.style.left = Math.min(px + 12, wrap.clientWidth - 140) + "px";
    tooltip.style.top = "8px";
    tooltip.style.opacity = 1;
  }
  function hide() {
    crosshair.style.opacity = 0;
    tooltip.style.opacity = 0;
  }

  svg.addEventListener("pointermove", (e) => {
    const rect = svg.getBoundingClientRect();
    const relX = ((e.clientX - rect.left) / rect.width) * W;
    let nearest = 0, best = Infinity;
    series.forEach((_, i) => {
      const dist = Math.abs(x(i) - relX);
      if (dist < best) { best = dist; nearest = i; }
    });
    showAt(nearest);
  });
  svg.addEventListener("pointerleave", hide);
}

function renderTable(series) {
  const table = document.getElementById("data-table");
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Date</th><th>Agent equity</th><th>Agent index</th><th>SPY close</th><th>SPY index</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  series.forEach(d => {
    const tr = document.createElement("tr");
    const cells = [
      d.date,
      fmtUsd(d.agent_equity),
      d.agent_index.toFixed(2),
      "$" + d.spy_close.toFixed(2),
      d.spy_index.toFixed(2),
    ];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      if (i > 0) td.className = "num";
      td.textContent = c;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  document.getElementById("table-toggle").addEventListener("click", () => {
    const chart = document.getElementById("chart-wrap");
    const isHidden = table.classList.contains("hidden");
    table.classList.toggle("hidden", !isHidden);
    chart.classList.toggle("hidden", isHidden);
    document.getElementById("table-toggle").textContent = isHidden ? "View as chart" : "View as table";
  });
}

function renderHoldings(holdings, account) {
  const table = document.getElementById("holdings-table");
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th>Company</th><th>Ticker</th><th class='num'>Price</th><th class='num'>Shares</th>" +
    "<th class='num'>Value</th><th class='num'>Day change</th><th class='num'>ROI $</th><th class='num'>ROI %</th></tr>";
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  if (holdings.length === 0 && !account) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "muted";
    td.textContent = "No open positions.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  holdings.forEach(h => {
    const tr = document.createElement("tr");
    const cells = [
      { v: h.company },
      { v: h.symbol, cls: "ticker" },
      { v: fmtUsd(h.price), num: true },
      { v: fmtQty(h.qty), num: true },
      { v: fmtUsd(h.value), num: true },
      { v: fmtUsdSigned(h.day_change_usd) + " (" + fmtPct(h.day_change_pct) + ")", num: true, sign: h.day_change_usd },
      { v: fmtUsdSigned(h.roi_usd), num: true, sign: h.roi_usd },
      { v: fmtPct(h.roi_pct), num: true, sign: h.roi_pct },
    ];
    cells.forEach(c => {
      const td = document.createElement("td");
      let cls = c.num ? "num" : (c.cls || "");
      if (c.sign !== undefined) cls += c.sign >= 0 ? " good" : " bad";
      if (cls) td.className = cls;
      td.textContent = c.v;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });

  // Cash sits in the same table so the total reconciles to account equity.
  if (account) {
    const tr = document.createElement("tr");
    [
      { v: "Cash (uninvested)", cls: "muted" },
      { v: "—", cls: "muted" },
      { v: "—", num: true },
      { v: "—", num: true },
      { v: fmtUsd(account.cash), num: true },
      { v: "—", num: true },
      { v: "—", num: true },
      { v: "—", num: true },
    ].forEach(c => {
      const td = document.createElement("td");
      const cls = c.num ? "num" : (c.cls || "");
      if (cls) td.className = cls;
      td.textContent = c.v;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  if (holdings.length === 0 && !account) return;

  const total = holdings.reduce(
    (a, h) => ({
      value: a.value + h.value,
      day: a.day + h.day_change_usd,
      roi: a.roi + h.roi_usd,
    }),
    { value: 0, day: 0, roi: 0 }
  );
  if (account) total.value += account.cash;

  const tfoot = document.createElement("tfoot");
  const tr = document.createElement("tr");
  const cells = [
    { v: account ? "Total equity" : "Total" },
    { v: "" },
    { v: "" },
    { v: "" },
    { v: fmtUsd(total.value), num: true },
    { v: fmtUsdSigned(total.day), num: true, sign: total.day },
    { v: fmtUsdSigned(total.roi), num: true, sign: total.roi },
    { v: "" },
  ];
  cells.forEach(c => {
    const td = document.createElement("td");
    let cls = c.num ? "num" : "";
    if (c.sign !== undefined) cls += c.sign >= 0 ? " good" : " bad";
    if (cls) td.className = cls;
    td.style.fontWeight = "600";
    td.textContent = c.v;
    tr.appendChild(td);
  });
  tfoot.appendChild(tr);
  table.appendChild(tfoot);
}

function renderDecisionLog(decisions, mode) {
  const table = document.getElementById("decision-table");
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Cycle</th><th>Timestamp (UTC)</th><th>Symbol</th><th>Side</th><th>Notional</th><th>Status</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");

  if (decisions.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.className = "muted";
    td.textContent = "No decision cycles logged yet.";
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  decisions.forEach(cycle => {
    if (cycle.trades.length === 0) {
      const tr = document.createElement("tr");
      const cellVals = [String(cycle.id), cycle.timestamp, "—", "—", "—", "no trades proposed"];
      cellVals.forEach((v, i) => {
        const td = document.createElement("td");
        if (i === 5) td.className = "muted";
        td.textContent = v;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
      return;
    }
    cycle.trades.forEach((t, idx) => {
      const tr = document.createElement("tr");
      const rowCells = [
        idx === 0 ? String(cycle.id) : "",
        idx === 0 ? cycle.timestamp : "",
        t.symbol,
        t.side,
        fmtUsd(t.notional_usd),
      ];
      rowCells.forEach(v => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      });
      const statusTd = document.createElement("td");
      const pill = document.createElement("span");
      if (t.is_valid) {
        pill.className = "pill valid";
        pill.textContent = t.order_status || "accepted";
      } else {
        pill.className = "pill rejected";
        pill.textContent = t.rejection_reason || "rejected";
      }
      statusTd.appendChild(pill);
      tr.appendChild(statusTd);
      tbody.appendChild(tr);
    });
  });
  table.appendChild(tbody);
}

function renderLastRun(lastRun) {
  const el = document.getElementById("last-run");
  if (!lastRun) {
    el.classList.add("stale");
    el.textContent = "No successful cycle logged yet";
    return;
  }
  el.classList.toggle("stale", !!lastRun.stale);
  el.textContent = "Last successful cycle: " + lastRun.timestamp;
}

renderLastRun(DATA.last_run);
renderStats(DATA.comparison, DATA.account);
renderHoldings(DATA.holdings, DATA.account);
renderLegend();
renderChart(DATA.comparison.series);
renderTable(DATA.comparison.series);
renderDecisionLog(DATA.decisions, DATA.mode);

document.getElementById("subtitle").textContent =
  `${DATA.mode} account · ${DATA.comparison.series.length} trading day(s) tracked · generated ${GENERATED_AT}`;
document.getElementById("footer").textContent =
  "Regenerate with: python dashboard.py";
</script>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the agent-vs-SPY performance dashboard")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--db-path", default=config.DEFAULT_DB_PATH)
    parser.add_argument("--out", default="dashboard.html")
    args = parser.parse_args()

    agent_points = fetch_agent_equity_curve(args.mode)
    if not agent_points:
        raise SystemExit("No account equity history yet — fund the paper account and run a cycle first.")

    spy_points = fetch_spy_closes(agent_points[0][0], agent_points[-1][0])
    comparison = build_comparison(agent_points, spy_points)
    decision_log = fetch_decision_log(args.db_path)
    last_run = fetch_last_successful_run(args.db_path)
    holdings = fetch_holdings(args.mode)
    account = fetch_account_summary(args.mode, holdings)

    html_out = render_dashboard(
        comparison, decision_log, args.mode, last_run, holdings, account
    )
    with open(args.out, "w") as f:
        f.write(html_out)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
