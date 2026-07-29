"""
Claude Autonomous Trading Agent — decision loop.

Pipeline (see README.md#architecture):
    1. Gather state   — positions, cash, prices, news
    2. Decision        — Claude proposes trades as structured JSON
    3. Validate         — code-enforced guardrails (README.md#guardrails)
    4. Execute          — place validated orders via Alpaca (paper or live)
    5. Log              — persist prompt, response, validation, orders, P&L to SQLite
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

import config

load_dotenv()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """A single held position."""
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float


@dataclass
class PortfolioState:
    """Snapshot of account positions, cash, and P&L pulled from the broker."""
    cash: float
    equity: float
    positions: dict[str, Position]
    timestamp: datetime


@dataclass
class MarketContext:
    """Fresh prices and news for current holdings + watchlist, injected into the prompt."""
    prices: dict[str, float]
    news: dict[str, list[str]]
    watchlist: list[str]


@dataclass
class Thesis:
    """The standing investment case for a held position.

    Written by Claude when it opens a position and carried forward into every
    later cycle's prompt, so the agent reasons against its own prior case
    instead of re-deriving an opinion from scratch each day.
    """
    symbol: str
    opened_at: str
    thesis: str          # why we own it
    target: str          # what would count as the case playing out
    invalidation: str    # what would prove the case wrong
    status: str = "open"  # "open" | "closed"
    closed_at: str | None = None
    exit_trigger: str | None = None


@dataclass
class PositionReview:
    """Claude's daily verdict on one held position. Not a trade — a judgement.

    Every open position must be reviewed each cycle, which is what makes a
    no-trade day an active decision rather than an absence of one.
    """
    symbol: str
    verdict: str  # "hold" | "trim" | "exit"
    note: str
    # Set when adopting a position that has no stored thesis yet (opened before
    # thesis tracking, or outside the agent). Persisted as a new open thesis.
    thesis: str | None = None
    target: str | None = None
    invalidation: str | None = None


@dataclass
class ProposedTrade:
    """A single trade proposed by Claude, before validation."""
    symbol: str
    side: str  # "buy" or "sell"
    notional_usd: float
    reasoning: str
    # Buys must open a thesis; sells must name which exit trigger fired.
    thesis: str | None = None
    target: str | None = None
    invalidation: str | None = None
    exit_trigger: str | None = None


@dataclass
class ValidatedTrade:
    """A proposed trade after guardrail validation."""
    trade: ProposedTrade
    is_valid: bool
    rejection_reason: str | None = None
    order: dict[str, Any] | None = field(default=None)


# ---------------------------------------------------------------------------
# Broker client helpers
# ---------------------------------------------------------------------------

def _trading_client(mode: str):
    from alpaca.trading.client import TradingClient

    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=(mode == "paper"),
    )


def _data_client():
    from alpaca.data.historical.stock import StockHistoricalDataClient

    return StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )


def _news_client():
    from alpaca.data.historical.news import NewsClient

    return NewsClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )


# ---------------------------------------------------------------------------
# Stage 1: Gather state
# ---------------------------------------------------------------------------

def fetch_portfolio_state(mode: str) -> PortfolioState:
    """Pull current positions, cash, and P&L from the Alpaca API."""
    client = _trading_client(mode)
    account = client.get_account()
    raw_positions = client.get_all_positions()

    positions = {
        p.symbol: Position(
            symbol=p.symbol,
            qty=float(p.qty),
            market_value=float(p.market_value),
            avg_entry_price=float(p.avg_entry_price),
        )
        for p in raw_positions
    }

    return PortfolioState(
        cash=float(account.cash),
        equity=float(account.equity),
        positions=positions,
        timestamp=datetime.now(timezone.utc),
    )


def fetch_market_context(watchlist: list[str], portfolio: PortfolioState) -> MarketContext:
    """Fetch current prices and recent news for holdings and the watchlist.

    Market data must be fetched fresh every cycle — Claude's training data
    is stale for markets and must not be relied on for current prices/news.
    """
    from alpaca.data.requests import StockLatestTradeRequest, NewsRequest

    symbols = sorted(set(watchlist) | set(portfolio.positions.keys()))
    if not symbols:
        return MarketContext(prices={}, news={}, watchlist=watchlist)

    data_client = _data_client()
    latest_trades = data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbols)
    )
    prices = {symbol: float(trade.price) for symbol, trade in latest_trades.items()}

    news_client = _news_client()
    news: dict[str, list[str]] = {symbol: [] for symbol in symbols}
    news_set = news_client.get_news(
        NewsRequest(symbols=",".join(symbols), limit=50)
    )
    for article in news_set.data.get("news", []) if hasattr(news_set, "data") else news_set:
        headline = getattr(article, "headline", None)
        article_symbols = getattr(article, "symbols", []) or []
        for symbol in article_symbols:
            if symbol in news and len(news[symbol]) < config.NEWS_HEADLINES_PER_SYMBOL and headline:
                news[symbol].append(headline)

    return MarketContext(prices=prices, news=news, watchlist=watchlist)


# ---------------------------------------------------------------------------
# Stage 2: Decision
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the decision engine for a fully autonomous, code-guardrailed \
trading agent. You propose trades; you do not execute them. Every proposal is checked \
by code against hard guardrails before anything is sent to the broker, so you must \
still reason carefully, but do not assume your proposals will be accepted verbatim.

YOUR MANDATE

You are a thesis-driven swing investor, not a daily trader. You run once per day \
after the close, but acting every day is not the goal and is usually a mistake. \
Most days the correct output is zero trades. You are being measured on the quality \
of your positions over weeks and months, not on activity.

When you buy, you are opening an investment case. You must write down that case: \
why you own it, what would count as it playing out, and what would prove you wrong. \
That case is stored and shown back to you every subsequent day. You are accountable \
to it.

When you sell, it must be because a specific, nameable thing happened. Valid exit \
triggers:
- "thesis_broken"    — the case you wrote down is no longer true
- "target_hit"       — the case played out; take the gain
- "bad_news"         — new information materially damages the case
- "no_longer_fits"   — better use of the capital, or the position no longer earns its place
- "risk"             — position/portfolio risk management

Do not sell because a position is merely down, and do not hold because it is merely \
up. Price alone is not a thesis. Judge the case, not the P&L. Equally, do not let a \
broken thesis sit just because you are not yet at a loss.

DAILY REVIEW

You must return a review verdict for EVERY open position, every cycle, even when you \
propose no trades. This is the core of the job: re-read each stored thesis against \
today's price and news, and state whether it still holds.
- "hold" — case intact, no action
- "trim" — case intact but position should be smaller (pair with a partial sell)
- "exit" — case is done or broken (pair with a sell for the full position value)
A "trim" or "exit" verdict must have a matching sell in "trades", or it is ignored.

Guardrails your proposals will be checked against:
- Max position size: 15% of portfolio equity per ticker (post-trade)
- Universe: long-only, whitelisted US equities/ETFs only
- No leverage, margin, or options — buys are funded from available cash only
- Minimum cash buffer: 5% of equity must remain uninvested
- At most 5 trades per decision cycle

Respond with ONLY a single JSON object (no prose, no markdown fences) matching this shape:
{
  "reviews": [
    {"symbol": "AAPL", "verdict": "hold", "note": "why the stored thesis still holds, or why it doesn't"}
  ],
  "trades": [
    {"symbol": "NVDA", "side": "buy", "notional_usd": 1000.0, "reasoning": "...",
     "thesis": "the investment case in 1-3 sentences",
     "target": "what playing out looks like — level, event, or condition",
     "invalidation": "the specific thing that would prove this wrong"},
    {"symbol": "AAPL", "side": "sell", "notional_usd": 500.0, "reasoning": "...",
     "exit_trigger": "thesis_broken"}
  ],
  "commentary": "overall reasoning for the cycle, including why you are not trading if trades is empty"
}
Buys REQUIRE thesis, target, and invalidation. Sells REQUIRE exit_trigger. Trades \
missing these fields are rejected by code before reaching the broker.
"trades" may be an empty list — and on most days it should be.

ADOPTING AN UNTRACKED POSITION

A position may appear with "thesis": null — opened before thesis tracking existed, \
or outside the agent. Do not leave it untracked: either exit it, or adopt it by \
returning its review with "verdict": "hold" plus "thesis", "target", and \
"invalidation" fields alongside "note". Those are stored and shown back to you from \
the next cycle onward. Writing the case only in "note" does NOT store it, and you \
will be asked again tomorrow."""


def build_decision_prompt(
    portfolio: PortfolioState,
    market: MarketContext,
    theses: dict[str, Thesis],
    as_of: datetime,
) -> str:
    """Assemble the prompt sent to Claude, instructing it to return trades as
    structured JSON with written reasoning per trade.

    Each held position is paired with the thesis Claude wrote when it opened
    that position, plus unrealised P&L and days held, so the model reviews its
    own standing case rather than forming a fresh opinion every cycle.
    """
    positions_summary = []
    for p in portfolio.positions.values():
        current_price = market.prices.get(p.symbol)
        entry = p.avg_entry_price
        row = {
            "symbol": p.symbol,
            "qty": p.qty,
            "market_value": round(p.market_value, 2),
            "avg_entry_price": round(entry, 2),
            "current_price": current_price,
            "unrealized_pnl_pct": (
                round((current_price - entry) / entry * 100, 2)
                if current_price and entry else None
            ),
            "pct_of_equity": round(p.market_value / portfolio.equity * 100, 2) if portfolio.equity else 0,
        }
        t = theses.get(p.symbol)
        if t:
            opened = datetime.fromisoformat(t.opened_at)
            row["thesis"] = {
                "opened_at": t.opened_at,
                "days_held": (as_of - opened).days,
                "why_we_own_it": t.thesis,
                "target": t.target,
                "invalidation": t.invalidation,
            }
        else:
            # Pre-dates thesis tracking, or was opened outside the agent.
            row["thesis"] = None
            row["note"] = (
                "No stored thesis for this position. Either write one by "
                "reviewing it as 'hold' with a note stating the case, or exit it."
            )
        positions_summary.append(row)

    state = {
        "as_of": portfolio.timestamp.isoformat(),
        "cash": round(portfolio.cash, 2),
        "equity": round(portfolio.equity, 2),
        "positions": positions_summary,
        "watchlist_prices": {s: market.prices.get(s) for s in market.watchlist},
        "recent_news": {s: h for s, h in market.news.items() if h},
    }

    return (
        "Current portfolio and market state (JSON):\n"
        f"{json.dumps(state, indent=2)}\n\n"
        "Review every open position against its stored thesis and return a verdict "
        "for each. Then decide on trades for this cycle — including none, if none "
        "are warranted."
    )


def _parse_trades_response(
    raw_text: str,
) -> tuple[list[ProposedTrade], list[PositionReview], str]:
    """Parse Claude's JSON response into ProposedTrade and PositionReview objects.

    Returns the trades, the per-position review verdicts, and the raw response
    text (for logging).
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    payload = json.loads(text)
    trades = [
        ProposedTrade(
            symbol=t["symbol"],
            side=t["side"],
            notional_usd=float(t["notional_usd"]),
            reasoning=t.get("reasoning", ""),
            thesis=t.get("thesis"),
            target=t.get("target"),
            invalidation=t.get("invalidation"),
            exit_trigger=t.get("exit_trigger"),
        )
        for t in payload.get("trades", [])
    ]
    reviews = [
        PositionReview(
            symbol=r["symbol"],
            verdict=r.get("verdict", ""),
            note=r.get("note", ""),
            thesis=r.get("thesis"),
            target=r.get("target"),
            invalidation=r.get("invalidation"),
        )
        for r in payload.get("reviews", [])
    ]
    return trades, reviews, raw_text


def get_claude_decision(
    prompt: str,
) -> tuple[list[ProposedTrade], list[PositionReview], str]:
    """Call the Anthropic API (/v1/messages) and parse the structured JSON
    trade proposals and position reviews from the response.

    Returns the parsed trades and reviews plus the raw response text (for logging).
    """
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        # Truncated JSON would surface as an opaque decode error; say what it is.
        raise RuntimeError(
            f"Claude response hit the {config.CLAUDE_MAX_TOKENS}-token cap and was "
            "truncated mid-JSON — raise config.CLAUDE_MAX_TOKENS. No trades placed."
        )
    return _parse_trades_response(raw_text)


# ---------------------------------------------------------------------------
# Stage 3: Validate
# ---------------------------------------------------------------------------

def validate_trades(
    trades: list[ProposedTrade], portfolio: PortfolioState
) -> list[ValidatedTrade]:
    """Check every proposed trade against the code-enforced guardrails.

    Guardrails (all enforced here, see README.md#guardrails):
        - Max position size: 15% of portfolio per ticker
        - Universe: long-only, whitelisted US equities/ETFs
        - No leverage, margin, or options
        - Minimum cash buffer: 5%
        - Max trades per decision cycle: 5
        - Every symbol verified against the broker asset list before ordering

    Invalid trades are rejected, never silently adjusted.
    """
    results: list[ValidatedTrade] = []
    running_cash = portfolio.cash
    position_values = {s: p.market_value for s, p in portfolio.positions.items()}
    equity = portfolio.equity
    min_cash_required = config.MIN_CASH_BUFFER_PCT * equity

    trading_client = None  # lazily created only if a ticker actually needs checking

    for i, trade in enumerate(trades):
        if i >= config.MAX_TRADES_PER_CYCLE:
            results.append(ValidatedTrade(trade, False, "exceeds max trades per decision cycle"))
            continue

        if trade.side not in ("buy", "sell"):
            results.append(ValidatedTrade(trade, False, f"unrecognized side '{trade.side}'"))
            continue

        if trade.symbol not in config.WATCHLIST:
            results.append(ValidatedTrade(trade, False, "symbol not in whitelisted universe"))
            continue

        if trade.notional_usd <= 0:
            results.append(ValidatedTrade(trade, False, "notional must be positive"))
            continue

        if trading_client is None:
            trading_client = _trading_client("paper")  # asset lookup is env-agnostic
        try:
            asset = trading_client.get_asset(trade.symbol)
            if not asset.tradable:
                results.append(ValidatedTrade(trade, False, "symbol not tradable at broker"))
                continue
        except Exception:
            results.append(ValidatedTrade(trade, False, "symbol failed broker asset validation"))
            continue

        if trade.side == "buy":
            # A buy without a written case cannot be reviewed against later.
            missing = [
                f for f in ("thesis", "target", "invalidation")
                if not (getattr(trade, f) or "").strip()
            ]
            if missing:
                results.append(ValidatedTrade(
                    trade, False, f"buy missing required thesis field(s): {', '.join(missing)}"
                ))
                continue

            projected_position_value = position_values.get(trade.symbol, 0.0) + trade.notional_usd
            if equity and projected_position_value / equity > config.MAX_POSITION_PCT:
                results.append(ValidatedTrade(trade, False, "exceeds max position size (15%)"))
                continue
            if trade.notional_usd > running_cash:
                results.append(ValidatedTrade(trade, False, "insufficient cash (no margin/leverage)"))
                continue
            if running_cash - trade.notional_usd < min_cash_required:
                results.append(ValidatedTrade(trade, False, "would breach minimum cash buffer (5%)"))
                continue
            running_cash -= trade.notional_usd
            position_values[trade.symbol] = projected_position_value
            results.append(ValidatedTrade(trade, True))

        else:  # sell
            # A sell must name which trigger fired, so exits stay auditable.
            if trade.exit_trigger not in config.VALID_EXIT_TRIGGERS:
                results.append(ValidatedTrade(
                    trade, False,
                    f"sell has invalid or missing exit_trigger '{trade.exit_trigger}'",
                ))
                continue

            held_value = position_values.get(trade.symbol, 0.0)
            if held_value <= 0:
                results.append(ValidatedTrade(trade, False, "no position held (long-only, no shorting)"))
                continue
            if trade.notional_usd > held_value:
                results.append(ValidatedTrade(trade, False, "cannot sell more than held position"))
                continue
            running_cash += trade.notional_usd
            position_values[trade.symbol] = held_value - trade.notional_usd
            results.append(ValidatedTrade(trade, True))

    return results


# ---------------------------------------------------------------------------
# Stage 4: Execute
# ---------------------------------------------------------------------------

def execute_trades(trades: list[ValidatedTrade], mode: str) -> list[dict[str, Any]]:
    """Place validated orders through the Alpaca trading API.

    Args:
        trades: Trades that passed validation (only is_valid=True trades are sent).
        mode: "paper" or "live" — selects which Alpaca base URL to use.

    Returns:
        Order confirmation records as returned by Alpaca.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _trading_client(mode)
    orders: list[dict[str, Any]] = []

    for validated in trades:
        if not validated.is_valid:
            continue
        trade = validated.trade
        order_request = MarketOrderRequest(
            symbol=trade.symbol,
            notional=round(trade.notional_usd, 2),
            side=OrderSide.BUY if trade.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_data=order_request)
        order_record = {
            "symbol": trade.symbol,
            "side": trade.side,
            "notional_usd": trade.notional_usd,
            "order_id": str(order.id),
            "status": str(order.status),
        }
        validated.order = order_record
        orders.append(order_record)

    return orders


# ---------------------------------------------------------------------------
# Stage 5: Log
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the local SQLite database and ensure schema exists."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            prompt TEXT NOT NULL,
            claude_response TEXT NOT NULL,
            portfolio_cash REAL NOT NULL,
            portfolio_equity REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL REFERENCES decision_cycles(id),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            notional_usd REAL NOT NULL,
            reasoning TEXT,
            is_valid INTEGER NOT NULL,
            rejection_reason TEXT,
            order_id TEXT,
            order_status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS theses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            thesis TEXT NOT NULL,
            target TEXT NOT NULL,
            invalidation TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            exit_trigger TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL REFERENCES decision_cycles(id),
            symbol TEXT NOT NULL,
            verdict TEXT NOT NULL,
            note TEXT
        )
        """
    )
    # Every invocation writes exactly one row here, success or failure, so a run
    # that dies before logging a decision cycle still leaves a trace — silence
    # from a scheduled run is otherwise indistinguishable from a no-trade day.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            outcome TEXT NOT NULL,
            error_text TEXT,
            cycles_completed INTEGER NOT NULL
        )
        """
    )
    # One open thesis per symbol is an invariant the lifecycle below relies on.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS theses_one_open_per_symbol
        ON theses (symbol) WHERE status = 'open'
        """
    )
    conn.commit()
    return conn


def fetch_open_theses(conn: sqlite3.Connection) -> dict[str, Thesis]:
    """Load the standing investment case for every currently-held position."""
    rows = conn.execute(
        """
        SELECT symbol, opened_at, thesis, target, invalidation, status, closed_at, exit_trigger
        FROM theses WHERE status = 'open'
        """
    ).fetchall()
    return {r[0]: Thesis(*r) for r in rows}


def apply_thesis_lifecycle(
    conn: sqlite3.Connection,
    validated_trades: list[ValidatedTrade],
    reviews: list[PositionReview],
    portfolio: PortfolioState,
    timestamp: datetime,
) -> None:
    """Open, carry, and close theses in response to executed trades and reviews.

    A buy opens a thesis (or refreshes the existing one when adding to a winner).
    A sell that closes out the whole position closes the thesis; a partial sell
    leaves it open, since the case still stands on the remaining shares.

    A review may also adopt an untracked position by supplying a full case —
    that is how holdings opened before thesis tracking get one without having
    to be sold and rebought.
    """
    now = timestamp.isoformat()
    open_symbols = set(fetch_open_theses(conn))

    for r in reviews:
        if r.symbol in open_symbols or r.symbol not in portfolio.positions:
            continue
        if all((getattr(r, f) or "").strip() for f in ("thesis", "target", "invalidation")):
            conn.execute(
                """
                INSERT INTO theses (symbol, opened_at, thesis, target, invalidation, status)
                VALUES (?, ?, ?, ?, ?, 'open')
                ON CONFLICT (symbol) WHERE status = 'open' DO NOTHING
                """,
                (r.symbol, now, r.thesis, r.target, r.invalidation),
            )
            open_symbols.add(r.symbol)

    for v in validated_trades:
        if not v.is_valid or v.order is None:
            continue
        trade = v.trade

        if trade.side == "buy":
            conn.execute(
                """
                INSERT INTO theses (symbol, opened_at, thesis, target, invalidation, status)
                VALUES (?, ?, ?, ?, ?, 'open')
                ON CONFLICT (symbol) WHERE status = 'open'
                DO UPDATE SET thesis = excluded.thesis,
                              target = excluded.target,
                              invalidation = excluded.invalidation
                """,
                (trade.symbol, now, trade.thesis, trade.target, trade.invalidation),
            )
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
    conn.commit()


def log_run(
    conn: sqlite3.Connection,
    outcome: str,
    error_text: str | None,
    cycles_completed: int,
    timestamp: datetime | None = None,
) -> None:
    """Write one run_log row recording how this invocation ended.

    Committed immediately: on the failure path this must be durable before the
    process exits non-zero.
    """
    conn.execute(
        """
        INSERT INTO run_log (timestamp, outcome, error_text, cycles_completed)
        VALUES (?, ?, ?, ?)
        """,
        (
            (timestamp or datetime.now(timezone.utc)).isoformat(),
            outcome,
            error_text,
            cycles_completed,
        ),
    )
    conn.commit()


def log_decision_cycle(
    conn: sqlite3.Connection,
    prompt: str,
    claude_response: str,
    validated_trades: list[ValidatedTrade],
    reviews: list[PositionReview],
    portfolio: PortfolioState,
    timestamp: datetime,
    mode: str,
) -> None:
    """Persist the full prompt, Claude's response/reasoning, validation results,
    executed orders, and portfolio value for this cycle.
    """
    cursor = conn.execute(
        """
        INSERT INTO decision_cycles (timestamp, mode, prompt, claude_response, portfolio_cash, portfolio_equity)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (timestamp.isoformat(), mode, prompt, claude_response, portfolio.cash, portfolio.equity),
    )
    cycle_id = cursor.lastrowid

    for v in validated_trades:
        conn.execute(
            """
            INSERT INTO trades (
                cycle_id, symbol, side, notional_usd, reasoning,
                is_valid, rejection_reason, order_id, order_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                v.trade.symbol,
                v.trade.side,
                v.trade.notional_usd,
                v.trade.reasoning,
                int(v.is_valid),
                v.rejection_reason,
                v.order["order_id"] if v.order else None,
                v.order["status"] if v.order else None,
            ),
        )

    for r in reviews:
        conn.execute(
            "INSERT INTO position_reviews (cycle_id, symbol, verdict, note) VALUES (?, ?, ?, ?)",
            (cycle_id, r.symbol, r.verdict, r.note),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_decision_cycle(mode: str, watchlist: list[str], db_path: str) -> None:
    """Run one full gather -> decide -> validate -> execute -> log cycle."""
    conn = init_db(db_path)
    cycles_completed = 0
    try:
        now = datetime.now(timezone.utc)
        portfolio = fetch_portfolio_state(mode)
        market = fetch_market_context(watchlist, portfolio)
        theses = fetch_open_theses(conn)
        prompt = build_decision_prompt(portfolio, market, theses, now)
        trades, reviews, raw_response = get_claude_decision(prompt)
        validated = validate_trades(trades, portfolio)
        execute_trades(validated, mode)
        apply_thesis_lifecycle(conn, validated, reviews, portfolio, now)
        log_decision_cycle(
            conn, prompt, raw_response, validated, reviews, portfolio, now, mode,
        )
        cycles_completed = 1
        _print_cycle_summary(validated, reviews)
    except Exception:
        # Record the failure before the exception propagates and exits non-zero.
        log_run(conn, "failed", traceback.format_exc(), cycles_completed)
        raise
    else:
        log_run(conn, "success", None, cycles_completed)
    finally:
        conn.close()


def _print_cycle_summary(
    validated: list[ValidatedTrade], reviews: list[PositionReview]
) -> None:
    """Human-readable cycle summary — this is what lands in the launchd log."""
    for r in reviews:
        print(f"  review  {r.symbol:<6} {r.verdict:<5} {r.note}")

    executed = [v for v in validated if v.is_valid]
    rejected = [v for v in validated if not v.is_valid]
    if not executed:
        print("  no trades this cycle")
    for v in executed:
        t = v.trade
        tag = t.exit_trigger if t.side == "sell" else "new position"
        print(f"  {t.side.upper():<4} {t.symbol:<6} ${t.notional_usd:,.2f}  [{tag}]")
    for v in rejected:
        print(f"  REJECTED {v.trade.side} {v.trade.symbol}: {v.rejection_reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Autonomous Trading Agent")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode: paper (default) or live.",
    )
    parser.add_argument(
        "--db-path",
        default=config.DEFAULT_DB_PATH,
        help=f"Path to the SQLite log database (default: {config.DEFAULT_DB_PATH}).",
    )
    args = parser.parse_args()

    run_decision_cycle(mode=args.mode, watchlist=config.WATCHLIST, db_path=args.db_path)


if __name__ == "__main__":
    main()
