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
class ProposedTrade:
    """A single trade proposed by Claude, before validation."""
    symbol: str
    side: str  # "buy" or "sell"
    notional_usd: float
    reasoning: str


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

Guardrails your proposals will be checked against:
- Max position size: 15% of portfolio equity per ticker (post-trade)
- Universe: long-only, whitelisted US equities/ETFs only
- No leverage, margin, or options — buys are funded from available cash only
- Minimum cash buffer: 5% of equity must remain uninvested
- At most 5 trades per decision cycle

Respond with ONLY a single JSON object (no prose, no markdown fences) matching this shape:
{
  "trades": [
    {"symbol": "AAPL", "side": "buy", "notional_usd": 1000.0, "reasoning": "..."}
  ],
  "commentary": "brief overall reasoning for this cycle, including a no-trade decision if empty"
}
"trades" may be an empty list if no action is warranted this cycle."""


def build_decision_prompt(portfolio: PortfolioState, market: MarketContext) -> str:
    """Assemble the prompt sent to Claude, instructing it to return trades as
    structured JSON with written reasoning per trade.
    """
    positions_summary = [
        {
            "symbol": p.symbol,
            "qty": p.qty,
            "market_value": round(p.market_value, 2),
            "avg_entry_price": round(p.avg_entry_price, 2),
            "pct_of_equity": round(p.market_value / portfolio.equity * 100, 2) if portfolio.equity else 0,
        }
        for p in portfolio.positions.values()
    ]

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
        "Decide on trades for this cycle, if any, per the instructions above."
    )


def _parse_trades_response(raw_text: str) -> tuple[list[ProposedTrade], str]:
    """Parse Claude's JSON response into ProposedTrade objects.

    Returns the trades plus the raw response text (for logging).
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
        )
        for t in payload.get("trades", [])
    ]
    return trades, raw_text


def get_claude_decision(prompt: str) -> tuple[list[ProposedTrade], str]:
    """Call the Anthropic API (/v1/messages) and parse the structured JSON
    trade proposals from the response.

    Returns the parsed trades plus the raw response text (for logging).
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
    conn.commit()
    return conn


def log_decision_cycle(
    conn: sqlite3.Connection,
    prompt: str,
    claude_response: str,
    validated_trades: list[ValidatedTrade],
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
    conn.commit()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_decision_cycle(mode: str, watchlist: list[str], db_path: str) -> None:
    """Run one full gather -> decide -> validate -> execute -> log cycle."""
    conn = init_db(db_path)
    try:
        portfolio = fetch_portfolio_state(mode)
        market = fetch_market_context(watchlist, portfolio)
        prompt = build_decision_prompt(portfolio, market)
        trades, raw_response = get_claude_decision(prompt)
        validated = validate_trades(trades, portfolio)
        execute_trades(validated, mode)
        log_decision_cycle(
            conn, prompt, raw_response, validated, portfolio,
            datetime.now(timezone.utc), mode,
        )
    finally:
        conn.close()


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
