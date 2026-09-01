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
import math
import os
import sqlite3
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

import config
import markettime

load_dotenv()

# Prompts are large and belong in files, not in the console or a `sqlite3`
# one-liner's output. One plain-text file per cycle, named by cycle id.
PROMPT_LOG_DIR = os.path.join("logs", "prompts")


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
    """Fresh prices and news for current holdings + watchlist, injected into the prompt.

    `news` is per-held-symbol and complete: every open position gets its own
    lookup, so an empty list means "no recent news", not "we didn't look".
    `market_news` is a broad sample across the watchlist — it cannot be complete
    at watchlist scale and must not be read as per-symbol coverage.
    """
    prices: dict[str, float]
    news: dict[str, list[str]]
    market_news: list[str]
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
    # Set when submission raised. The trade passed validation and has an order
    # record for the audit trail, but nothing reached the broker — so it must
    # not move the thesis lifecycle.
    submit_failed: bool = False


# ---------------------------------------------------------------------------
# Broker client helpers
# ---------------------------------------------------------------------------

def _trading_client(mode: str):
    from alpaca.trading.client import TradingClient

    # Enforced here rather than only in main(): this is the single chokepoint
    # every live order and account read must pass through, so no call path can
    # reach a real account without clearing the lock.
    config.assert_mode_allowed(mode)

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
        timestamp=markettime.now_utc(),
    )


def fetch_market_context(watchlist: list[str], portfolio: PortfolioState) -> MarketContext:
    """Fetch current prices and recent news for holdings and the watchlist.

    Market data must be fetched fresh every cycle — Claude's training data
    is stale for markets and must not be relied on for current prices/news.
    """
    from alpaca.data.requests import StockLatestTradeRequest, NewsRequest

    symbols = sorted(set(watchlist) | set(portfolio.positions.keys()))
    if not symbols:
        return MarketContext(prices={}, news={}, market_news=[], watchlist=watchlist)

    data_client = _data_client()
    latest_trades = data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbols)
    )
    prices = {symbol: float(trade.price) for symbol, trade in latest_trades.items()}

    news_client = _news_client()

    # One request per held symbol. A single shared request across the whole
    # watchlist splits a fixed article cap over hundreds of names, so most
    # holdings came back with nothing — measured at 7% symbol coverage on a
    # 520-name watchlist, and it degrades as the universe grows. The agent has
    # to review every open thesis against today's news, so that coverage cannot
    # be left to chance; positions are few, so the extra calls are cheap.
    news: dict[str, list[str]] = {}
    for symbol in sorted(portfolio.positions):
        news[symbol] = _headlines(
            news_client, NewsRequest(symbols=symbol, limit=config.NEWS_HEADLINES_PER_SYMBOL)
        )

    # Broad sweep over the tradable universe for opportunities the agent doesn't
    # already own. Deliberately not per-symbol: at watchlist scale that is not
    # achievable in one cycle, so this is labelled a sample in the prompt.
    market_news = _headlines(
        news_client,
        NewsRequest(symbols=",".join(watchlist), limit=config.MARKET_NEWS_ARTICLES),
    )

    return MarketContext(
        prices=prices, news=news, market_news=market_news, watchlist=watchlist
    )


def _headlines(news_client, request) -> list[str]:
    """Headlines for one news request, de-duplicated, newest first.

    News is context, not a guardrail input — a failed lookup degrades the
    prompt but must not abort a cycle that can still trade on prices.
    """
    try:
        news_set = news_client.get_news(request)
    except Exception:
        return []

    articles = news_set.data.get("news", []) if hasattr(news_set, "data") else news_set
    seen: dict[str, None] = {}
    for article in articles:
        headline = getattr(article, "headline", None)
        if headline:
            seen.setdefault(headline, None)
    return list(seen)


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

WHAT THE NEWS FIELDS MEAN

"position_news" is complete: every open position was looked up individually, so an \
empty list there means no recent headlines were found for that holding — that is real \
information, not a gap.

"market_news_sample" is a small sample across a watchlist of hundreds of tickers. It \
cannot cover them all. A ticker missing from it tells you nothing about that ticker, so \
never infer from this field that a name has no news, and never treat its absence as a \
reason to act or not act. Use it only as a prompt for what to look at.

You are not being given full-market news coverage. Where a decision would turn on \
information you were not given, say so in your reasoning rather than inferring it.

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
                # Shown on market time — the model is reasoning about a
                # session that opens and closes in New York.
                "opened_at": markettime.fmt_et(t.opened_at),
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
        "as_of": markettime.fmt_et(portfolio.timestamp),
        "cash": round(portfolio.cash, 2),
        "equity": round(portfolio.equity, 2),
        "positions": positions_summary,
        "watchlist_prices": {s: market.prices.get(s) for s in market.watchlist},
        # Kept separate on purpose. position_news is complete — every holding was
        # looked up, so an empty list genuinely means no recent news and is
        # itself information. market_news is a sample across the watchlist; a
        # ticker's absence there says nothing about that ticker.
        "position_news": {s: market.news.get(s, []) for s in portfolio.positions},
        "market_news_sample": market.market_news,
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

    # json.loads accepts the bare literals NaN/Infinity by default. A NaN
    # notional would pass every guardrail below — each one is a `<`/`>`
    # comparison, and every comparison against NaN is False — so refuse the
    # value at the door rather than relying on downstream checks to catch it.
    def _reject_non_finite(literal: str) -> float:
        raise ValueError(f"non-finite number in response: {literal}")

    payload = json.loads(text, parse_constant=_reject_non_finite)
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
    # Streamed, not messages.create: the SDK refuses a non-streaming call whose
    # max_tokens implies more than ten minutes of generation, and at
    # CLAUDE_MAX_TOKENS=32000 it raises before sending anything. We still want
    # the whole message at once, so collect the stream and take the final one.
    with client.messages.stream(
        model=config.CLAUDE_MODEL,
        max_tokens=config.CLAUDE_MAX_TOKENS,
        # Nested under output_config — `effort` is not a top-level parameter and
        # is silently ignored if passed as one.
        output_config={"effort": config.CLAUDE_EFFORT},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        # Truncated JSON would surface as an opaque decode error; say what it is.
        raise RuntimeError(
            f"Claude response hit the {config.CLAUDE_MAX_TOKENS}-token cap and was "
            "truncated mid-JSON — raise config.CLAUDE_MAX_TOKENS, or lower "
            f"config.CLAUDE_EFFORT (currently {config.CLAUDE_EFFORT}; thinking "
            "tokens count against the same cap). No trades placed."
        )
    return _parse_trades_response(raw_text)


# ---------------------------------------------------------------------------
# Stage 3: Validate
# ---------------------------------------------------------------------------

def validate_trades(
    trades: list[ProposedTrade], portfolio: PortfolioState, mode: str
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

        # The whitelist bounds what we may *acquire*. Applying it to sells too
        # would strand any holding whose ticker later leaves the list (renames,
        # index changes — WATCHLIST is a hand-maintained snapshot), and with no
        # human override that capital could never be recovered. Exiting a
        # position is risk-reducing and stays permitted.
        if trade.side == "buy" and trade.symbol not in config.WATCHLIST:
            results.append(ValidatedTrade(trade, False, "symbol not in whitelisted universe"))
            continue

        # isfinite rejects NaN and ±Infinity. NaN in particular would satisfy
        # every guardrail below by making each comparison False.
        if not math.isfinite(trade.notional_usd) or trade.notional_usd <= 0:
            results.append(ValidatedTrade(trade, False, "notional must be a positive, finite number"))
            continue

        # Validate the amount that will actually be submitted — execute_trades
        # rounds to cents, and a value under a cent would round to a $0 order.
        if round(trade.notional_usd, 2) < config.MIN_TRADE_NOTIONAL_USD:
            results.append(ValidatedTrade(
                trade, False, f"notional below broker minimum (${config.MIN_TRADE_NOTIONAL_USD:.2f})"
            ))
            continue

        if trading_client is None:
            # Must match the execution environment: paper and live keys are not
            # valid against each other's endpoint, so a hardcoded "paper" here
            # would 401 on every symbol in live mode and reject every trade.
            trading_client = _trading_client(mode)
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

            # Fail closed on a non-positive equity snapshot (API glitch, or a
            # mid-funding account): without a denominator the 15% cap cannot be
            # evaluated, and skipping it would silently disable the guardrail.
            if equity <= 0:
                results.append(ValidatedTrade(trade, False, "cannot size position against non-positive equity"))
                continue

            projected_position_value = position_values.get(trade.symbol, 0.0) + trade.notional_usd
            if projected_position_value / equity > config.MAX_POSITION_PCT:
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
            # Deliberately does NOT credit running_cash. The sell has not been
            # submitted yet, let alone filled or settled, so spending its
            # proceeds on a buy later in the same cycle would place an order
            # against money the account does not hold — which a margin account
            # funds as leverage rather than rejecting. It also made the outcome
            # depend on the order Claude happened to list the trades in.
            # Proceeds become spendable next cycle, from the real balance.
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

    A failure on one order must not abort the loop. Earlier orders in the list
    are already live at the broker, and letting the exception propagate would
    skip logging entirely — leaving real fills with no row in `trades`, no
    thesis, and no decision_cycle. Each failure is recorded against its own
    trade and the cycle proceeds to the logging stage.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _trading_client(mode)
    orders: list[dict[str, Any]] = []

    for validated in trades:
        if not validated.is_valid:
            continue
        trade = validated.trade
        order_record: dict[str, Any] = {
            "symbol": trade.symbol,
            "side": trade.side,
            "notional_usd": trade.notional_usd,
            "order_id": None,
            "status": None,
        }
        try:
            order_request = MarketOrderRequest(
                symbol=trade.symbol,
                notional=round(trade.notional_usd, 2),
                side=OrderSide.BUY if trade.side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = client.submit_order(order_data=order_request)
            order_record["order_id"] = str(order.id)
            order_record["status"] = str(order.status)
        except Exception as exc:
            # Submission failed, so no fill exists for this trade — but the
            # attempt still belongs in the log. A thesis must not be opened off
            # an order that never reached the broker.
            order_record["status"] = f"submit_failed: {type(exc).__name__}: {exc}"
            validated.order = order_record
            validated.submit_failed = True
            orders.append(order_record)
            continue

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
        if not v.is_valid or v.order is None or v.submit_failed:
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
            (timestamp or markettime.now_utc()).isoformat(),
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
) -> int:
    """Persist the full prompt, Claude's response/reasoning, validation results,
    executed orders, and portfolio value for this cycle.

    Returns the new cycle id, which names this cycle's prompt log file.
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
    return cycle_id


def write_prompt_log(
    cycle_id: int,
    prompt: str,
    mode: str,
    timestamp: datetime,
    log_dir: str = PROMPT_LOG_DIR,
) -> str:
    """Write this cycle's prompt to logs/prompts/{cycle_id}.txt and return the path.

    Plain text, no ANSI — this is meant to be opened, diffed, and grepped long
    after the run, where escape codes would be noise. The database still holds
    the authoritative copy (README.md#architecture); this is the readable one.
    """
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{cycle_id}.txt")
    with open(path, "w") as f:
        f.write(
            f"cycle     {cycle_id}\n"
            f"timestamp {markettime.fmt_et(timestamp)}\n"
            f"mode      {mode}\n"
            f"model     {config.CLAUDE_MODEL}\n"
            f"effort    {config.CLAUDE_EFFORT}\n"
            "\n"
            "=== SYSTEM PROMPT ===\n"
            f"{SYSTEM_PROMPT}\n"
            "\n"
            "=== USER PROMPT ===\n"
            f"{prompt}\n"
        )
    return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_decision_cycle(mode: str, watchlist: list[str], db_path: str) -> None:
    """Run one full gather -> decide -> validate -> execute -> log cycle."""
    conn = init_db(db_path)
    cycles_completed = 0
    try:
        now = markettime.now_utc()
        portfolio = fetch_portfolio_state(mode)
        market = fetch_market_context(watchlist, portfolio)
        theses = fetch_open_theses(conn)
        prompt = build_decision_prompt(portfolio, market, theses, now)
        trades, reviews, raw_response = get_claude_decision(prompt)
        validated = validate_trades(trades, portfolio, mode)
        execute_trades(validated, mode)
        apply_thesis_lifecycle(conn, validated, reviews, portfolio, now)
        cycle_id = log_decision_cycle(
            conn, prompt, raw_response, validated, reviews, portfolio, now, mode,
        )
        cycles_completed = 1
        prompt_path = write_prompt_log(cycle_id, prompt, mode, now)
        _print_cycle_summary(cycle_id, now, validated, reviews, prompt_path)
    except Exception:
        # Record the failure before the exception propagates and exits non-zero.
        log_run(conn, "failed", traceback.format_exc(), cycles_completed)
        raise
    else:
        log_run(conn, "success", None, cycles_completed)
    finally:
        conn.close()


def _print_cycle_summary(
    cycle_id: int,
    timestamp: datetime,
    validated: list[ValidatedTrade],
    reviews: list[PositionReview],
    prompt_path: str,
) -> None:
    """Five-line cycle summary — this is what lands in the launchd log.

    A count, not a payload. The reasoning behind these numbers lives in the
    database (`python report.py`) and the prompt file; a scheduled run's log
    only needs to answer "did it run, and did anything happen".
    """
    rejected = sum(1 for v in validated if not v.is_valid)
    failed = sum(1 for v in validated if v.submit_failed)
    print(f"cycle {cycle_id}  {markettime.fmt_et(timestamp)}")
    print(f"  positions reviewed      {len(reviews)}")
    print(f"  trades proposed         {len(validated)}")
    print(f"  rejected by guardrails  {rejected}")
    # A validated trade the broker refused is not a no-op — surface it here or
    # it is only visible by reading the trades table.
    if failed:
        print(f"  FAILED AT BROKER        {failed}")
    print(f"  prompt                  {prompt_path}")


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

    # Fail before any broker or Claude call, so a mistaken `--mode live` costs
    # nothing and prints the reason rather than a client error mid-cycle.
    config.assert_mode_allowed(args.mode)

    run_decision_cycle(mode=args.mode, watchlist=config.WATCHLIST, db_path=args.db_path)


if __name__ == "__main__":
    main()
