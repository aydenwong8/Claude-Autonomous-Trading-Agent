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
import html
import json
import math
import hashlib
import os
import sqlite3
import statistics
import subprocess
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
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
class PendingOrder:
    """An order accepted by the broker but not yet filled.

    The agent decides after the close, so its orders queue until the next
    session's open. Neither `cash` nor `positions` reflects them — the money
    has not left and the shares have not arrived — so without this a cycle
    that runs before the fill sees a portfolio that looks untouched and can
    place the same order again.
    """
    symbol: str
    side: str
    notional_usd: float | None
    qty: float | None
    status: str
    submitted_at: str | None


@dataclass
class PortfolioState:
    """Snapshot of account positions, cash, and P&L pulled from the broker."""
    cash: float
    equity: float
    positions: dict[str, Position]
    timestamp: datetime
    # Orders already at the broker and not yet filled, keyed by symbol. A
    # symbol can carry more than one.
    pending_orders: dict[str, list[PendingOrder]] = field(default_factory=dict)
    # Dollars that pending BUYS will consume from `cash` when they fill. Set
    # once per cycle by `price_pending_orders` and read by both the prompt and
    # validation, so the two can never disagree about what is spendable.
    # Pending sells contribute nothing: their proceeds are unsettled, the same
    # reason validation refuses to credit a sell within its own cycle.
    pending_buy_commitment: float = 0.0
    # Pending buys whose dollar value could not be established. Their cost is
    # NOT in the commitment above, so it understates what is spoken for.
    unpriced_pending_buys: list[str] = field(default_factory=list)
    # Dollars of each position already being sold by an unfilled order. That
    # much of the holding is spoken for and cannot be sold a second time.
    pending_sell_value: dict[str, float] = field(default_factory=dict)
    # Symbols with an unfilled sell of unknowable size. How much of the holding
    # remains disposable cannot be computed, so further sells are refused
    # outright rather than guessed at.
    unvaluable_pending_sells: list[str] = field(default_factory=list)


@dataclass
class NewsArticle:
    """One Alpaca news item, as archived in `news_articles`.

    `id` is Alpaca's own article id, which is what makes the archive
    idempotent: the same story fetched on ten consecutive days is one row.
    """
    id: int
    headline: str
    summary: str
    source: str
    url: str
    published_at: str   # UTC ISO
    symbols: list[str]


@dataclass
class MarketContext:
    """Fresh prices and news for current holdings + watchlist, injected into the prompt.

    `articles` is everything fetched this cycle, for the archive. Each held
    position gets its own lookup, so once archived its news is complete for the
    window — an empty result means "no recent news", not "we didn't look" —
    except for the symbols in `news_lookup_failed`, where it means exactly that.
    `market_news` is a broad sample across the watchlist — it cannot be complete
    at watchlist scale and must not be read as per-symbol coverage.
    """
    prices: dict[str, float]
    articles: list[NewsArticle]
    news_lookup_failed: set[str]
    market_news: list[NewsArticle]
    watchlist: list[str]
    # Per-symbol screen statistics (momentum, 52-week range position, realised
    # volatility, liquidity) for every watchlist name that had enough history.
    # Held positions are looked up here too, so a holding and a candidate can be
    # compared on identical axes — which is what makes a relative verdict possible.
    screen: dict[str, dict[str, Any]]
    # The shortlist actually shown to the model, drawn across four buckets.
    candidates: list[str]
    # True when the bar fetch behind `screen` failed. An empty shortlist then
    # means "not looked up", and the prompt has to say so: on cycle 48 a
    # connection reset produced `"candidates": []`, which the model read as a
    # screen that found nothing worth considering.
    screen_failed: bool = False


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
    # A case is allowed to be re-anchored while the position is still held —
    # a target that has been passed stops being a decision criterion and starts
    # being noise. `revision` is 1 at open and increments on every rewrite;
    # every version is kept in `thesis_revisions`.
    updated_at: str | None = None
    revision: int = 1


@dataclass
class PositionReview:
    """Claude's daily verdict on one held position. Not a trade — a judgement.

    Every open position must be reviewed each cycle, which is what makes a
    no-trade day an active decision rather than an absence of one.
    """
    symbol: str
    verdict: str  # "hold" | "trim" | "exit"
    note: str
    # Two uses, both persisted to `theses`: adopting a position that has no
    # stored case yet (opened before thesis tracking, or outside the agent),
    # and re-anchoring the case on a position that already has one. All three
    # must be supplied together — a case patched field-by-field is incoherent.
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

def fetch_pending_orders(client) -> dict[str, list[PendingOrder]]:
    """Open (unfilled) orders at the broker, grouped by symbol.

    A failure here must not take the cycle down: the account state is still
    usable without it, and an empty result is the same shape as "nothing
    pending". It does mean the prompt loses its in-flight warning, so the
    exception is surfaced rather than swallowed silently.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    pending: dict[str, list[PendingOrder]] = {}
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as exc:
        print(f"  warning: could not read open orders ({type(exc).__name__}: {exc})")
        return pending

    for o in orders:
        pending.setdefault(o.symbol, []).append(
            PendingOrder(
                symbol=o.symbol,
                side=str(o.side).split(".")[-1].lower(),
                notional_usd=float(o.notional) if o.notional is not None else None,
                qty=float(o.qty) if o.qty is not None else None,
                status=str(o.status).split(".")[-1].lower(),
                submitted_at=o.submitted_at.isoformat() if o.submitted_at else None,
            )
        )
    return pending


def price_pending_orders(portfolio: PortfolioState, prices: dict[str, float]) -> None:
    """Value the unfilled orders against `prices` and record them on `portfolio`.

    Both sides of the book are double-countable while an order sits unfilled,
    because neither `cash` nor `positions` has moved yet:

    - A pending **buy** means cash alone overstates what can be spent, by
      exactly the size of the order. Spending it twice is how an account
      breaches its own cash buffer with no single decision looking wrong: cycle
      29 left a $2,900 buy in flight, and cycle 30 was shown $3,093 of
      "deployable" cash when barely $190 was genuinely free.
    - A pending **sell** means the position overstates what can still be sold.
      Selling the same shares twice would take a long-only account short.

    Needs prices because an order placed outside the agent may be sized in
    shares rather than dollars. The two unvaluable cases are handled
    differently, because caution points opposite ways: an unvaluable buy is
    merely named (the commitment becomes a floor, and the prompt says so),
    while an unvaluable sell blocks further sells of that symbol outright —
    there is no safe way to guess how much of a holding is still disposable.
    """
    def value_of(o: PendingOrder) -> float | None:
        if o.notional_usd is not None:
            return o.notional_usd
        if o.qty is not None and prices.get(o.symbol):
            return o.qty * prices[o.symbol]
        return None

    committed = 0.0
    unpriced: list[str] = []
    sell_value: dict[str, float] = {}
    unvaluable_sells: list[str] = []

    for symbol, orders in portfolio.pending_orders.items():
        for o in orders:
            value = value_of(o)
            if o.side == "buy":
                if value is None:
                    unpriced.append(symbol)
                else:
                    committed += value
            else:
                if value is None:
                    unvaluable_sells.append(symbol)
                else:
                    sell_value[symbol] = sell_value.get(symbol, 0.0) + value

    portfolio.pending_buy_commitment = committed
    portfolio.unpriced_pending_buys = unpriced
    portfolio.pending_sell_value = sell_value
    portfolio.unvaluable_pending_sells = unvaluable_sells


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
        pending_orders=fetch_pending_orders(client),
    )


def fetch_market_context(watchlist: list[str], portfolio: PortfolioState) -> MarketContext:
    """Fetch current prices and recent news for holdings and the watchlist.

    Market data must be fetched fresh every cycle — Claude's training data
    is stale for markets and must not be relied on for current prices/news.
    """
    from alpaca.data.requests import StockLatestTradeRequest, NewsRequest

    symbols = sorted(set(watchlist) | set(portfolio.positions.keys()))
    if not symbols:
        return MarketContext(
            prices={}, articles=[], news_lookup_failed=set(), market_news=[],
            watchlist=watchlist, screen={}, candidates=[],
        )

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
    articles: list[NewsArticle] = []
    news_lookup_failed: set[str] = set()
    window_start = markettime.now_utc() - timedelta(days=config.NEWS_HISTORY_DAYS)
    for symbol in sorted(portfolio.positions):
        fetched = _fetch_articles(
            news_client,
            NewsRequest(
                symbols=_news_symbol(symbol), start=window_start,
                limit=config.NEWS_FETCH_PER_SYMBOL,
            ),
        )
        if fetched is None:
            news_lookup_failed.add(symbol)
            continue
        # Filed under the holding's own ticker as well as whatever the feed
        # tagged: the request was for this symbol, and the feed's spelling of
        # it (BRK/B, BRK) is not the one the rest of the agent uses.
        for a in fetched:
            if symbol not in a.symbols:
                a.symbols.append(symbol)
        articles.extend(fetched)

    # Broad sweep over the tradable universe for opportunities the agent doesn't
    # already own. Deliberately not per-symbol: at watchlist scale that is not
    # achievable in one cycle, so this is labelled a sample in the prompt.
    market_news = _fetch_articles(
        news_client,
        NewsRequest(symbols=",".join(watchlist), limit=config.MARKET_NEWS_ARTICLES),
    ) or []
    articles.extend(market_news)

    # Screen statistics come from daily bars, not the latest trade: `prices`
    # above stays the freshest available mark because it sizes orders, while the
    # screen answers a different question and wants a consistent bar series.
    screen = compute_screen_stats(symbols)
    screen_failed = screen is None
    screen = screen or {}
    candidates = select_candidates(screen, portfolio.positions.keys())

    # Candidates get their own lookup too, smaller than a holding's. A buy has
    # to open a written case, and without this the only current information
    # behind a case for a name the agent does not own was the price table. The
    # fetch still covers the whole window, so a name that sits on the shortlist
    # for days — or is bought — already has history in the archive.
    for symbol in candidates:
        fetched = _fetch_articles(
            news_client,
            NewsRequest(
                symbols=_news_symbol(symbol), start=window_start,
                limit=config.NEWS_FETCH_PER_CANDIDATE,
            ),
        )
        if fetched is None:
            news_lookup_failed.add(symbol)
            continue
        for a in fetched:
            if symbol not in a.symbols:
                a.symbols.append(symbol)
        articles.extend(fetched)

    return MarketContext(
        prices=prices,
        articles=articles,
        news_lookup_failed=news_lookup_failed,
        market_news=market_news,
        watchlist=watchlist,
        screen=screen,
        candidates=candidates,
        screen_failed=screen_failed,
    )


def _pct_change(now: float, then: float) -> float | None:
    """Percent change, or None when the base is unusable."""
    if not then:
        return None
    return round((now / then - 1) * 100, 2)


def compute_screen_stats(symbols: list[str]) -> dict[str, dict[str, Any]] | None:
    """Momentum, range position, realised volatility and liquidity per symbol.

    Computed from daily bars over `config.SCREEN_LOOKBACK_DAYS`. The whole
    watchlist costs one paginated request (~26s for 1,519 names), so this runs
    over everything rather than a pre-filtered subset — the filtering happens
    in `select_candidates`, which needs the full distribution to rank against.

    A failure here degrades the prompt to positions-only; it must not abort a
    cycle, because the position reviews and the guardrails do not depend on it.
    It is retried once, then returns None — distinct from an empty result, so
    the prompt can say the screen did not run rather than that it found nothing.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment, DataFeed

    start = datetime.now(timezone.utc) - timedelta(days=config.SCREEN_LOOKBACK_DAYS)
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        # The free tier rejects recent SIP data; IEX is what dashboard.py
        # uses for its SPY series too, so both curves come off one feed.
        feed=DataFeed.IEX,
        # alpaca-py defaults to RAW, which turns every split in the lookback
        # into a fake crash: BKNG and NFLX were shown ~95% below their 52-week
        # highs on cycle 50. SPLIT rather than ALL keeps "last" the real close
        # instead of a dividend-adjusted figure.
        adjustment=Adjustment.SPLIT,
    )
    for attempt in (1, 2):
        try:
            bars = _data_client().get_stock_bars(request).data
            break
        except Exception as exc:
            if attempt == 2:
                print(f"  screen: bar fetch failed twice ({exc}) — continuing without candidates")
                return None
            print(f"  screen: bar fetch failed ({exc}) — retrying once")

    stats: dict[str, dict[str, Any]] = {}
    for symbol, series in bars.items():
        if len(series) < config.SCREEN_MIN_BARS:
            continue
        closes = [float(b.close) for b in series]
        volumes = [float(b.volume) for b in series]
        last, high, low = closes[-1], max(closes), min(closes)

        # 20-day realised volatility, annualised. Present so the model can weigh
        # return against the risk taken to get it rather than chasing raw move.
        window = closes[-21:]
        rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
        vol = (
            round(statistics.stdev(rets) * math.sqrt(252) * 100, 1)
            if len(rets) > 2 and statistics.stdev(rets)
            else None
        )
        stats[symbol] = {
            "last": round(last, 2),
            "chg_5d": _pct_change(last, closes[-6]) if len(closes) > 6 else None,
            "chg_20d": _pct_change(last, closes[-21]) if len(closes) > 21 else None,
            "chg_60d": _pct_change(last, closes[-61]) if len(closes) > 61 else None,
            "pct_52w_range": round((last - low) / (high - low) * 100) if high > low else None,
            "from_52w_high": _pct_change(last, high),
            "vol_20d_ann": vol,
            "dollar_vol_20d_m": round(statistics.median(volumes[-20:]) * last / 1e6, 1),
        }
    return stats


def select_candidates(
    stats: dict[str, dict[str, Any]], held: Iterable[str]
) -> list[str]:
    """Shortlist names worth arguing about, drawn across four opportunity shapes.

    Deliberately not one ranking. A single momentum sort returns nothing but
    high-beta names, which biases the book exactly the wrong way when the mandate
    is risk-adjusted return; the low-volatility bucket is the counterweight, and
    the drawdown and pullback buckets keep beaten-down names in view at all.

    Held symbols are excluded — they already appear in `positions` with their own
    case, track record and news, and listing them twice would double-count them.
    """
    held = set(held)
    liquid = {
        s: v
        for s, v in stats.items()
        if s not in held
        and (v["dollar_vol_20d_m"] or 0) >= config.SCREEN_MIN_DOLLAR_VOL_M
        and v["chg_60d"] is not None
    }

    def top(key, reverse=True, where=None):
        pool = {
            s: v for s, v in liquid.items()
            if v[key] is not None and (where is None or where(v))
        }
        ranked = sorted(pool, key=lambda s: pool[s][key], reverse=reverse)
        return ranked[: config.SCREEN_PER_BUCKET]

    buckets = [
        # Working, and the market agrees.
        top("chg_60d"),
        # Uptrend intact, short-term weakness — the same name at a better price.
        top("chg_60d", where=lambda v: v["chg_60d"] > 10 and (v["chg_5d"] or 0) < -3),
        # Furthest below its own 52-week high: broken, or mispriced.
        top("from_52w_high", reverse=False),
        # Return per unit of volatility — the shape that actually serves Sharpe.
        sorted(
            (s for s, v in liquid.items() if v["vol_20d_ann"] and v["chg_60d"] > 5),
            key=lambda s: liquid[s]["chg_60d"] / liquid[s]["vol_20d_ann"],
            reverse=True,
        )[: config.SCREEN_PER_BUCKET],
    ]

    # Order-preserving dedupe: a name qualifying in two buckets is listed once,
    # and the shortlist stays stable enough to diff across cycles.
    seen: dict[str, None] = {}
    for bucket in buckets:
        for symbol in bucket:
            seen.setdefault(symbol, None)
    return list(seen)


def _news_symbol(symbol: str) -> str:
    """The ticker to query the news feed with for a broker symbol.

    Share-class tickers are spelled differently by the feed: the broker's
    BRK.B is tagged BRK, BRK/A or BRK/B, and a query for "BRK.B" matches
    nothing — which the prompt then presented as a complete lookup that found
    no news, for every one of BRK.B's first 40 reviews. The base ticker catches
    all of the company's classes, which for news is the right scope anyway.
    """
    return symbol.split(".", 1)[0]


def _fetch_articles(news_client, request) -> list[NewsArticle] | None:
    """Articles for one news request, newest first, or None if the lookup failed.

    News is context, not a guardrail input — a failed lookup degrades the
    prompt but must not abort a cycle that can still trade on prices. None and
    an empty list are kept distinct because they mean different things to the
    model: "we could not look" versus "we looked and there was nothing".
    """
    try:
        news_set = news_client.get_news(request)
    except Exception:
        return None

    raw = news_set.data.get("news", []) if hasattr(news_set, "data") else news_set
    articles = []
    for a in raw:
        if not getattr(a, "headline", None) or getattr(a, "created_at", None) is None:
            continue
        articles.append(NewsArticle(
            id=int(a.id),
            headline=a.headline,
            summary=a.summary or "",
            source=a.source or "",
            url=a.url or "",
            published_at=a.created_at.astimezone(timezone.utc).isoformat(),
            symbols=list(a.symbols or []),
        ))
    return articles


# ---------------------------------------------------------------------------
# Stage 2: Decision
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the decision engine for a fully autonomous, code-guardrailed \
trading agent. You propose trades; you do not execute them. Every proposal is checked \
by code against hard guardrails before anything is sent to the broker, so you must \
still reason carefully, but do not assume your proposals will be accepted verbatim.

YOUR MANDATE

You are a thesis-driven swing investor, not a daily trader. You run once per day \
after the close, and you are measured on the quality of your positions over weeks \
and months, not on activity. Churn is a cost and a tell.

But inaction is a decision with exactly the same standing as a trade, and it is the \
one you are most likely to get wrong by default. Holding everything is not the \
neutral option — it is a choice to leave every dollar precisely where it is, and it \
has to be as defensible as a trade would be. "Nothing broke today" is not a reason; \
see DAILY REVIEW for what is.

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

You are benchmarked against SPY. Buying SPY is permitted and is sometimes the honest \
answer, but it is a decision you have to make on purpose, not a default place to park \
capital: index beta cannot generate alpha against the index, so a position in SPY \
should be justified as deliberately-chosen beta with its own case, and reviewed on \
those terms like anything else.

The 15% per-ticker cap is not a diversification guarantee. Five names moving on one \
driver — the same AI-capex cycle, the same rate path, the same consumer — is one \
position wearing five tickers, and nothing in code will stop you building it. Name \
the shared drivers across the book in your commentary, and treat concentration in a \
theme as real risk even when every individual weight is inside the cap.

DAILY REVIEW

You must return a review verdict for EVERY open position, every cycle, even when you \
propose no trades. This is the core of the job.

Every review answers TWO questions, and a position must pass BOTH to be held:

1. ABSOLUTE — is the written case still true? Re-read the stored thesis against \
   today's price and news.
2. RELATIVE — is this still the best use of this capital, judged against the names \
   in "candidates" and against your other holdings?

The second question is the one that does the real work, and it is the one that is \
easy to skip. A case can remain technically intact for months while the capital \
inside it quietly earns less than an alternative you were shown today. An intact \
case is not by itself a reason to keep owning something — it only means the reason \
you bought it has not been falsified. Those are different claims.

So: rank your holdings. If the weakest one is clearly worse risk-adjusted than a \
candidate you can write a real case for, the honest verdict on that holding is \
"trim" or "exit" with the "no_longer_fits" trigger, even though nothing about it \
broke. That trigger exists for exactly this and it does not require anything to have \
gone wrong. A book where every position passes the absolute test and none is ever \
tested against an alternative is not a managed portfolio; it is a frozen one.
- "hold" — passes both tests
- "trim" — position should be smaller (pair with a partial sell)
- "exit" — case done, broken, or beaten by a better use of the money (pair with a \
  sell for the full position value)
A "trim" or "exit" verdict must have a matching sell in "trades", or it is ignored.

None of this overrides the spent-fact discipline in YOUR OWN TRACK RECORD. Rotating \
into a better idea is a new decision; trimming the same name a seventh time against \
the same observation is not, and "I needed cash" does not convert one into the other.

RE-ANCHORING A CASE

A case you wrote weeks ago can go stale without being wrong. The commonest way: the \
price marker in "target" is passed while the operating condition it was standing in \
for is still intact. A passed marker is not a decision criterion any more, and \
trimming against it every cycle is not discipline — it is being driven by a number \
that has expired.

When that happens, rewrite the case. Return the review for that position with \
"thesis", "target" and "invalidation" alongside "note", and the new wording replaces \
the stored case from the next cycle onward. All three are required together: a \
partial rewrite is discarded, and you will be told it was discarded rather than left \
to believe it took. Every version is kept and dated, so a rewrite is an amendment on \
the record, not an edit to history — "case_revision" and "last_reanchored_at" on each \
position show you how often you have already done this.

Re-anchoring is for when a marker has expired or the facts have moved, and you should \
say in the note what specifically changed. It is not for routine housekeeping. \
Ratcheting a price target upward every time the price rises is moving the goalposts, \
not managing a position: if you find yourself raising a target for the third time \
with no new operating information, the honest conclusion is that you never had a \
price view worth writing down — anchor the case to the business condition you \
actually own instead.

YOUR OWN TRACK RECORD

Every position carries "your_track_record": the trades you have already proposed in \
that name, with the reasoning you gave at the time, and a tally of the review \
verdicts you have returned on it. This is your record, not market data. Rejected and \
unexecuted proposals appear too, labelled — an intention you had is not a fill you got.

A candidate you have owned or traded before carries its record as well, including \
"last_closed_case": the case you held it on, when you closed it, and the exit \
trigger you gave. Buying back a name you exited is allowed, but it is a reversal of \
your own decision, so say what has changed since the exit — the same test as any \
other repeat action.

Read it before acting on a name you have acted on before. It exists because of two \
failures that are invisible from position weight alone:

- Re-deciding. If your reasoning today restates the reasoning on an earlier action \
  in the same name, you are not responding to new information — you are meeting the \
  same fact again and treating it as news. An observation you have already acted on \
  is spent. Name what has changed since that action, or do not act. Trimming a \
  winner five times against one passed price marker is this failure, not discipline.
- Drifting past your own intentions. If an earlier reasoning said where you meant to \
  stop — a target weight, a number of tranches, a condition for the next add — that \
  is a commitment and you are held to it. Meet it, or say in your commentary that \
  you are departing from it and why. Exceeding it without remarking on it is the \
  failure; nothing in code will catch it.

Building a position in tranches is legitimate and often correct. Doing it three times \
without noticing you are doing it is a different thing, and only you can tell them \
apart. A long run of "hold" verdicts is likewise not evidence that a position is \
fine — it may equally mean you have stopped genuinely examining it.

WHAT THE NEWS FIELDS MEAN

"position_news" is complete: every open position was looked up individually, so an \
empty list there means no recent headlines were found for that holding — that is real \
information, not a gap. The one exception is a holding marked "lookup_failed", where \
today's lookup did not happen and silence means nothing.

It is read from an archive of every article you have been given, covering a rolling \
window of recent weeks, and split in two:
- "published_since_your_last_cycle" — you have never seen these. This is the news.
- "earlier" — dated headlines published before your previous cycle. The market has \
  had at least a session to price them, and most you will have been shown before. \
  They are here so you can recognise a story you have already weighed, and so you \
  can see how a situation has developed. A story in "earlier" is not by itself a new \
  reason to act; if you already acted on it, it is spent, exactly like an \
  observation in your track record.

A development is often only visible across days — three downgrades in two weeks, a \
guidance cut followed by a CFO departure. Read the two lists together when judging \
whether a case is weakening.

"market_news_sample" is a small sample across a watchlist of hundreds of tickers. It \
cannot cover them all. A ticker missing from it tells you nothing about that ticker, so \
never infer from this field that a name has no news, and never treat its absence as a \
reason to act or not act. Use it only as a prompt for what to look at.

You are not being given full-market news coverage. Where a decision would turn on \
information you were not given, say so in your reasoning rather than inferring it.

WHAT "candidates" IS

A shortlist of names you do not currently own, screened from the tradable universe \
and carrying the same statistics attached to each of your holdings under "screen" — \
so a candidate and a position can be compared on identical axes. Fields: "chg_5d" / \
"chg_20d" / "chg_60d" (percent), "pct_52w_range" (0 = at the 52-week low, 100 = at \
the high), "from_52w_high" (percent below it), "vol_20d_ann" (annualised realised \
volatility) and "dollar_vol_20d_m" (liquidity).

It is drawn across four deliberately different shapes — strong 60-day momentum; \
uptrends with short-term pullbacks; the deepest drawdowns from 52-week highs; and \
the best return per unit of volatility. The last bucket is there because you are \
judged risk-adjusted: the highest-return name is usually not the best position, and \
a screen that only surfaced big movers would push the book the wrong way.

Each candidate also carries "recent_news": its latest few dated headlines from the \
last month, from its own lookup. An empty list means that lookup found nothing. It is \
there so you can see whether something is happening in the business behind the price \
move — a headline is a lead to weigh, not a case, and three headlines are not \
coverage of a company.

What this list is NOT: a buy list, a ranking, or a research report. These are price \
statistics. They tell you what has happened, never why, and momentum is not a thesis \
— a name near its 52-week low is either broken or mispriced and nothing here tells \
you which. Use it as a place to look, then write a real case from what you actually \
know about the business, or pass on it. Proposing a buy whose entire justification \
is a number in this table is exactly the failure this field creates the opportunity \
for, and "it is up 40% in 60 days" is not a case you would defend in three months.

Nothing obliges you to act on it. An empty-handed cycle where you looked and found \
nothing worth owning is a fine outcome and you should say so. A cycle where you did \
not look is not.

Guardrails your proposals will be checked against:
- Max position size: 15% of portfolio equity per ticker (post-trade)
- Universe: long-only, whitelisted US equities/ETFs only
- No leverage, margin, or options — buys are funded from available cash only
- Minimum cash buffer: 5% of equity must remain uninvested
- At most 5 trades per decision cycle

HOW CAPITAL ACTUALLY MOVES

Three cash figures are given, already computed. Do not re-derive them, and do not \
propose buys against gross cash — a buy that would breach the buffer is rejected \
outright, not trimmed to fit.
- "deployable_cash" — cash above the 5% buffer, before orders already placed
- "committed_to_pending_buys" — what unfilled buys will take out of that on fill
- "uncommitted_cash" — the difference, and **the figure to size new buys against**

Only the third is really yours to spend. The other two are shown so you can see that \
capital is committed rather than merely gone. Validation uses the same arithmetic, so \
a buy sized against "deployable_cash" while an order is in flight is rejected, not \
accepted-and-regretted. If "pending_buys_of_unknown_value" appears, some pending buy \
could not be valued and "uncommitted_cash" is an over-estimate — treat it as a ceiling.

Sell proceeds are NOT spendable in the same cycle. A sell you propose now has not been \
submitted, filled or settled, so validation does not credit it, and a buy later in the \
same response is checked against the cash you hold today. This is deliberate: spending \
unsettled proceeds is leverage, and it would make the outcome depend on the order you \
happened to list the trades in.

The consequence is that **rotation is a two-cycle operation, and that is a supported \
move, not a failure**. Sell today with the exit trigger that fired; the proceeds are in \
"uncommitted_cash" tomorrow, and you buy then. If the reason you are not acting on a \
better idea is that the capital is currently inside a worse one, the answer is to start \
the rotation, not to record another day of having no room. Say in your commentary when \
you are doing this, so the next cycle knows the cash is earmarked.

Being fully invested is not itself a virtue. If every position is worth its weight, \
holding is right. But "no cash to act" is a position you chose, and it is reviewable \
like any other.

ORDERS ALREADY IN FLIGHT

"orders_in_flight" lists orders the broker has accepted but not yet filled. You place \
orders after the close, so they queue until the next session opens. Until they fill, \
neither "cash" nor "positions" reflects them: the money has not left and the shares \
have not arrived.

Read the book as those orders having already happened. A pending buy means you own \
that name; a pending sell means that position is already on its way out and its \
proceeds are already spoken for. Do not re-propose a trade that is sitting in this \
list — you would be doubling it, not placing it. This list is normally empty, because \
a cycle usually runs after the previous cycle's orders have filled; when it is not \
empty, something ran twice or a fill did not happen.

"cases_not_yet_held" lists written cases whose position is not in the book. Each says \
whether an in-flight order explains it:
- "explained_by_pending_order": true — the buy simply has not filled yet. Nothing to \
  do. Do not buy it again.
- "explained_by_pending_order": false — you hold a written case for something the \
  account does not own. The order never filled, or the position left outside your \
  control. These get no position review, so say what you want done with the case in \
  your commentary; code closes it out this cycle either way.

Respond with ONLY a single JSON object (no prose, no markdown fences) matching this shape:
{
  "reviews": [
    {"symbol": "AAPL", "verdict": "hold", "note": "why the stored thesis still holds, or why it doesn't"},
    {"symbol": "MSFT", "verdict": "hold", "note": "what changed, and why the case is being rewritten",
     "thesis": "...", "target": "...", "invalidation": "..."}
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
will be asked again tomorrow — the same is true of a re-anchored case."""


def build_decision_prompt(
    portfolio: PortfolioState,
    market: MarketContext,
    theses: dict[str, Thesis],
    actions: dict[str, dict[str, Any]],
    news_history: dict[str, dict[str, Any]],
    candidate_news: dict[str, list[str]],
    as_of: datetime,
) -> str:
    """Assemble the prompt sent to Claude, instructing it to return trades as
    structured JSON with written reasoning per trade.

    Each held position is paired with the thesis Claude wrote when it opened
    that position, plus unrealised P&L and days held, so the model reviews its
    own standing case rather than forming a fresh opinion every cycle.

    It is also paired with `your_track_record` — what Claude has actually *done*
    in that name before, in its own words. The case and the record answer
    different questions ("what do I believe?" versus "what have I already done
    about it?"), and a model given only the first re-derives the second from
    position weight, which is how the same conclusion gets acted on repeatedly.
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
        # The same fields every candidate carries. Without this a holding and a
        # candidate cannot be compared on like terms, and "is this still the best
        # use of the capital?" has no basis to be answered on.
        if p.symbol in market.screen:
            row["screen"] = market.screen[p.symbol]
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
            # Surfaced so re-anchoring stays visible to the model that does it:
            # a case on its fourth revision is either tracking real change or
            # chasing the price, and it can only tell which if it can see the
            # count. Omitted entirely while the case is still the original.
            if t.revision > 1:
                row["thesis"]["case_revision"] = t.revision
                row["thesis"]["last_reanchored_at"] = markettime.fmt_et(t.updated_at)
        else:
            # Pre-dates thesis tracking, or was opened outside the agent.
            row["thesis"] = None
            row["note"] = (
                "No stored thesis for this position. Either write one by "
                "reviewing it as 'hold' with a note stating the case, or exit it."
            )
        # What this agent has already done in this name. Attached to the
        # position rather than listed separately so it is unavoidable at the
        # moment the position is being judged.
        if p.symbol in actions:
            row["your_track_record"] = actions[p.symbol]
        positions_summary.append(row)

    # Computed here rather than left to the model. It had been re-deriving
    # "cash minus the 5% buffer" from scratch in every cycle's commentary, which
    # is arithmetic the code already does authoritatively in validate_trades —
    # and a buy sized against gross cash is rejected outright, not trimmed.
    min_cash_required = config.MIN_CASH_BUFFER_PCT * portfolio.equity
    deployable_cash = max(0.0, portfolio.cash - min_cash_required)
    # Shown as three numbers rather than one netted figure: the model should be
    # able to see that capital is committed, not just that it is gone.
    uncommitted_cash = max(0.0, deployable_cash - portfolio.pending_buy_commitment)

    # An accepted order changes neither cash nor positions until it fills, so
    # without this the state looks identical to the state that produced the
    # order and the same trade gets proposed again.
    orders_in_flight = [
        {
            "symbol": o.symbol,
            "side": o.side,
            "notional_usd": o.notional_usd,
            "qty": o.qty,
            "status": o.status,
            "submitted_at": markettime.fmt_et(o.submitted_at) if o.submitted_at else None,
        }
        for orders in portfolio.pending_orders.values()
        for o in orders
    ]

    # A case whose position isn't in the book yet. Normally the other half of
    # an in-flight buy; with nothing in flight it is a case for something the
    # account does not own, which no position review would ever surface.
    cases_not_yet_held = [
        {
            "symbol": symbol,
            "opened_at": markettime.fmt_et(t.opened_at),
            "why_we_own_it": t.thesis,
            "target": t.target,
            "invalidation": t.invalidation,
            "explained_by_pending_order": symbol in portfolio.pending_orders,
            **({"your_track_record": actions[symbol]} if symbol in actions else {}),
        }
        for symbol, t in sorted(theses.items())
        if symbol not in portfolio.positions
    ]

    state = {
        "as_of": markettime.fmt_et(portfolio.timestamp),
        "cash": round(portfolio.cash, 2),
        "equity": round(portfolio.equity, 2),
        "min_cash_buffer_usd": round(min_cash_required, 2),
        # Cash above the buffer, before accounting for orders already placed.
        "deployable_cash": round(deployable_cash, 2),
        # What pending buys will take out of that when they fill.
        "committed_to_pending_buys": round(portfolio.pending_buy_commitment, 2),
        # The figure to size new buys against. Sell proceeds are not included:
        # they are unsettled, and validation will not credit them.
        "uncommitted_cash": round(uncommitted_cash, 2),
        **(
            {"pending_buys_of_unknown_value": portfolio.unpriced_pending_buys}
            if portfolio.unpriced_pending_buys else {}
        ),
        # Not reflected in cash or positions above — see the prompt's
        # ORDERS ALREADY IN FLIGHT section.
        "orders_in_flight": orders_in_flight,
        "cases_not_yet_held": cases_not_yet_held,
        "positions": positions_summary,
        # Replaces the old `watchlist_prices` dump. That was 1,519 symbols mapped
        # to a bare last price — a third of the prompt, and information-free: a
        # price with no reference point cannot support a thesis, so the agent
        # could only ever form views on names it already held. This is smaller
        # and answers the question the old field pretended to.
        #
        # A candidate this agent has traded before carries its record, closed
        # case included. Now that exits happen, a name sold last week can come
        # straight back through the screen, and without this it would arrive
        # looking like a first encounter.
        "candidates": [
            {
                "symbol": s,
                **market.screen[s],
                **({"recent_news": candidate_news[s]} if s in candidate_news else {}),
                **({"your_track_record": actions[s]} if s in actions else {}),
            }
            for s in market.candidates
            if s in market.screen
        ],
        **({"candidates_lookup_failed": (
            "Today's screen did not run (the price-history fetch failed), so "
            "\"candidates\" is empty because nothing was looked up, not because "
            "nothing qualified, and holdings carry no \"screen\" block. Do not "
            "read the empty list as an absence of alternatives."
        )} if market.screen_failed else {}),
        # Kept separate on purpose. position_news is complete — every holding was
        # looked up, so an empty list genuinely means no recent news and is
        # itself information. It is read from the archive, split at the previous
        # cycle, so a story already reacted to is labelled as such. market_news
        # is a sample across the watchlist; a ticker's absence there says
        # nothing about that ticker.
        "position_news": {s: news_history.get(s, {}) for s in portfolio.positions},
        "market_news_sample": [
            f"{markettime.fmt_et_date(a.published_at)} — {a.headline}"
            for a in market.market_news
        ],
    }

    return (
        "Current portfolio and market state (JSON):\n"
        f"{json.dumps(state, indent=2)}\n\n"
        "Review every open position against its stored thesis and return a verdict "
        "for each, re-anchoring any case whose written marker has expired. Judge each "
        "one on both tests: is the case still true, and is this still the best use of "
        "the capital against today's candidates. Rank your holdings weakest to "
        "strongest in your commentary and say what the weakest one would have to do "
        "to keep its place. Then decide on trades — including none, if none are "
        "warranted. Buys are limited to uncommitted_cash; when that is near zero, the "
        "question is not whether you have room but which position is least worth its "
        "money, and starting that rotation is the action. Before proposing a trade in "
        "a name you have traded before, read your own track record for it and say what "
        "has changed since that action — or do not act."
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
) -> tuple[list[ProposedTrade], list[PositionReview], str, tuple[int | None, int | None]]:
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
    if response.stop_reason == "refusal":
        # A safety-classifier decline returns HTTP 200 with little or no text,
        # which would otherwise surface as an opaque JSON decode error. No
        # fallback model is configured on purpose: a decision made by a
        # different model would be recorded under config.CLAUDE_MODEL.
        details = getattr(response, "stop_details", None)
        raise RuntimeError(
            f"{config.CLAUDE_MODEL} declined the request (stop_reason=refusal, "
            f"category={getattr(details, 'category', None)}). No trades placed."
        )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        # Truncated JSON would surface as an opaque decode error; say what it is.
        raise RuntimeError(
            f"Claude response hit the {config.CLAUDE_MAX_TOKENS}-token cap and was "
            "truncated mid-JSON — raise config.CLAUDE_MAX_TOKENS, or lower "
            f"config.CLAUDE_EFFORT (currently {config.CLAUDE_EFFORT}; thinking "
            "tokens count against the same cap). No trades placed."
        )
    # Recorded so cost stops being an estimate. output_tokens includes thinking,
    # which is billed but never lands in raw_text.
    usage = (
        getattr(response.usage, "input_tokens", None),
        getattr(response.usage, "output_tokens", None),
    )
    trades, reviews, text = _parse_trades_response(raw_text)
    return trades, reviews, text, usage


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
    # Cash the account holds, less what orders already at the broker will take
    # out of it when they fill. Validating against raw cash would approve a buy
    # that breaches the buffer the moment a pending order fills — no single
    # decision looking wrong, the account overdrawn against its own floor
    # anyway. This is the same principle as refusing to credit an unsettled
    # sell: only money that is both present and unspoken-for can fund a buy.
    running_cash = portfolio.cash - portfolio.pending_buy_commitment
    # Two baselines, because the buy cap and the sell check need opposite
    # kinds of caution about the same pending orders.
    #
    # `position_values` is what is still disposable, and gates sells. A pending
    # buy must NOT count here: those shares have not arrived, and treating them
    # as sellable is how a long-only account ends up short. A pending sell IS
    # netted off, or the same shares could be sold twice — once by the order in
    # flight and again by this cycle's proposal.
    #
    # `cap_values` is where the book is heading, and gates the 15% cap. A
    # pending buy DOES count here, or a second buy could take a position past
    # the cap the moment both fill. Pending sells are not netted off there:
    # assuming a position is already smaller would loosen the cap on the
    # strength of an order that has not filled, and the cap should only ever
    # err toward strictness.
    position_values = {s: p.market_value for s, p in portfolio.positions.items()}
    cap_values = dict(position_values)
    for symbol, sold in portfolio.pending_sell_value.items():
        if symbol in position_values:
            position_values[symbol] = max(0.0, position_values[symbol] - sold)
    for symbol, orders in portfolio.pending_orders.items():
        for o in orders:
            if o.side == "buy" and o.notional_usd is not None:
                cap_values[symbol] = cap_values.get(symbol, 0.0) + o.notional_usd
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

            projected_position_value = cap_values.get(trade.symbol, 0.0) + trade.notional_usd
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
            cap_values[trade.symbol] = projected_position_value
            # Bought shares are not sellable until they arrive, so this buy
            # does not raise what a later sell in this same list may dispose of.
            results.append(ValidatedTrade(trade, True))

        else:  # sell
            # A sell must name which trigger fired, so exits stay auditable.
            if trade.exit_trigger not in config.VALID_EXIT_TRIGGERS:
                results.append(ValidatedTrade(
                    trade, False,
                    f"sell has invalid or missing exit_trigger '{trade.exit_trigger}'",
                ))
                continue

            # An unfilled sell of unknowable size leaves no way to tell how much
            # of the holding is still disposable. Refuse rather than guess.
            if trade.symbol in portfolio.unvaluable_pending_sells:
                results.append(ValidatedTrade(
                    trade, False,
                    "unfilled sell of unknown size for this symbol; cannot size a further sell",
                ))
                continue

            held_value = position_values.get(trade.symbol, 0.0)
            if held_value <= 0:
                # Covers both "never held" and "the whole position is already
                # being sold by an order in flight".
                results.append(ValidatedTrade(trade, False, "no position held (long-only, no shorting)"))
                continue
            if trade.notional_usd > held_value:
                results.append(ValidatedTrade(
                    trade, False, "cannot sell more than held position (net of unfilled sells)"
                ))
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

def _client_order_id(trade: ProposedTrade, session_date: str) -> str:
    """A broker-side idempotency key: one identical order per symbol per session.

    Orders are placed before the cycle is logged, so a crash between the two
    leaves real fills the database never recorded — and rerunning the day would
    place them a second time. The broker rejects a duplicate client_order_id,
    which turns that second attempt into a no-op instead of a double position.

    Keyed on what the order *is* (session, symbol, side, amount in cents) rather
    than on its position in the list: a rerun re-asks Claude and gets a fresh
    decision, so an index would suppress whichever unrelated trade happened to
    land in the same slot while letting a genuine repeat through. Since the
    agent decides once per session, a second identical order the same day is a
    rerun artifact by construction.
    """
    symbol = "".join(ch for ch in trade.symbol.upper() if ch.isalnum())
    cents = int(round(trade.notional_usd * 100))
    return f"agt-{session_date}-{symbol}-{trade.side}-{cents}"


def execute_trades(
    trades: list[ValidatedTrade], mode: str, session_date: str
) -> list[dict[str, Any]]:
    """Place validated orders through the Alpaca trading API.

    Args:
        trades: Trades that passed validation (only is_valid=True trades are sent).
        mode: "paper" or "live" — selects which Alpaca base URL to use.
        session_date: Market-calendar date ('2026-09-03') scoping the
            idempotency key, so a rerun of the same session cannot double-order.

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
                client_order_id=_client_order_id(trade, session_date),
            )
            order = client.submit_order(order_data=order_request)
            order_record["order_id"] = str(order.id)
            order_record["status"] = str(order.status)
        except Exception as exc:
            # Submission failed, so no fill exists for this trade — but the
            # attempt still belongs in the log. A thesis must not be opened off
            # an order that never reached the broker.
            #
            # A rejected duplicate key is called out separately because it means
            # the opposite of the other failures: the order is not missing, it
            # already exists from an earlier run of this same session, and the
            # guard did its job. It is still submit_failed for this run — this
            # invocation placed nothing, and the fill it is pointing at was
            # already logged (or lost) by the run that actually made it.
            duplicate = "client_order_id" in str(exc).lower() or "duplicate" in str(exc).lower()
            prefix = "duplicate_suppressed" if duplicate else "submit_failed"
            order_record["status"] = f"{prefix}: {type(exc).__name__}: {exc}"
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
            portfolio_equity REAL NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            code_commit TEXT,
            code_dirty INTEGER,
            system_prompt_sha TEXT,
            model TEXT,
            effort TEXT
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
            exit_trigger TEXT,
            updated_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    # Every version a case has ever had, including the one it was opened with.
    # `theses` holds only the current wording; without this table a re-anchor
    # would quietly rewrite history and the record would show a position that
    # had always been owned for whatever reason it is owned for today.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thesis_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            cycle_id INTEGER REFERENCES decision_cycles(id),
            revised_at TEXT NOT NULL,
            revision INTEGER NOT NULL,
            -- 'open' | 'buy_refresh' | 'adopt' | 'review_reanchor'
            source TEXT NOT NULL,
            thesis TEXT NOT NULL,
            target TEXT NOT NULL,
            invalidation TEXT NOT NULL
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
    # Every news article the agent has been shown or could have been, keyed by
    # Alpaca's article id so a story fetched on ten days is one row. The prompt
    # reads a holding's news back from here, not from the day's fetch — this is
    # what lets the model tell a story it already reacted to from a new one.
    # `first_seen_at` is audit only; "new" is decided by publication time.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY,
            published_at TEXT NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT,
            source TEXT,
            url TEXT,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_symbols (
            article_id INTEGER NOT NULL REFERENCES news_articles(id),
            symbol TEXT NOT NULL,
            PRIMARY KEY (symbol, article_id)
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
    _migrate_thesis_revisions(conn)
    _migrate_cycle_usage(conn)
    _migrate_cycle_version(conn)
    conn.commit()
    return conn


def _migrate_cycle_usage(conn: sqlite3.Connection) -> None:
    """Add token-usage columns to a pre-usage `decision_cycles` table.

    Left NULL for cycles that ran before usage was recorded — those costs are
    not recoverable, and a zero would read as "this cycle was free" rather than
    "we were not counting yet".
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_cycles)")}
    if "input_tokens" in columns:
        return
    conn.execute("ALTER TABLE decision_cycles ADD COLUMN input_tokens INTEGER")
    conn.execute("ALTER TABLE decision_cycles ADD COLUMN output_tokens INTEGER")


def _migrate_cycle_version(conn: sqlite3.Connection) -> None:
    """Add agent-version columns to a pre-versioning `decision_cycles` table.

    Left NULL for earlier cycles rather than backfilled from git history: a
    cycle's timestamp says which commit was checked out only if the tree was
    clean, and it was not always — cycle 43 ran on the candidate screen before
    that code was committed. A guessed version is worse than an honest gap.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_cycles)")}
    for name, kind in (
        ("code_commit", "TEXT"),
        ("code_dirty", "INTEGER"),
        ("system_prompt_sha", "TEXT"),
        ("model", "TEXT"),
        ("effort", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE decision_cycles ADD COLUMN {name} {kind}")


@dataclass(frozen=True)
class AgentVersion:
    """Which agent made a decision — recorded on every cycle.

    The instructions change often, and performance can only be attributed to a
    version if each decision says which version made it. `code_commit` covers
    everything in the repo (prompt builder, guardrails, config); the prompt
    hash lets cycles be grouped across commits that left the system prompt
    alone; model and effort are recorded because config.py can change them
    without anything else moving.
    """
    code_commit: str | None
    # Uncommitted edits to tracked .py files: the commit alone does not
    # describe the code that ran. dashboard.html is regenerated every run and
    # is not code, so it is deliberately not counted.
    code_dirty: bool | None
    system_prompt_sha: str
    model: str
    effort: str


def current_agent_version() -> AgentVersion:
    """Best-effort: a missing git binary or repo yields None, never a failure."""
    repo = os.path.dirname(os.path.abspath(__file__))

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True, text=True, timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip()

    commit = git("rev-parse", "--short=12", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no", "--", "*.py")
    return AgentVersion(
        code_commit=commit,
        code_dirty=None if status is None else bool(status),
        system_prompt_sha=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12],
        model=config.CLAUDE_MODEL,
        effort=config.CLAUDE_EFFORT,
    )


def _migrate_thesis_revisions(conn: sqlite3.Connection) -> None:
    """Bring a pre-revision `theses` table up to the current schema.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so databases
    created before case re-anchoring existed need the new columns added and
    their current wording backfilled as revision 1 — otherwise the first
    re-anchor would look like the second version of a case with no first.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(theses)")}
    if "revision" in columns:
        return
    conn.execute("ALTER TABLE theses ADD COLUMN updated_at TEXT")
    conn.execute("ALTER TABLE theses ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    # cycle_id is left null: these predate the trail, and the cycle that opened
    # them is recoverable from opened_at if it is ever needed.
    conn.execute(
        """
        INSERT INTO thesis_revisions
            (symbol, cycle_id, revised_at, revision, source, thesis, target, invalidation)
        SELECT symbol, NULL, opened_at, 1, 'open', thesis, target, invalidation
        FROM theses
        """
    )


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


def store_news(
    conn: sqlite3.Connection, articles: list[NewsArticle], now: datetime
) -> int:
    """Archive fetched articles; returns how many were not already stored.

    Idempotent on Alpaca's article id. Stored before the decision is made and
    committed straight away: the archive is a record of what was available,
    not of what was decided, so a cycle that later fails should still keep it.
    """
    before = conn.total_changes
    for a in articles:
        conn.execute(
            """INSERT OR IGNORE INTO news_articles
               (id, published_at, headline, summary, source, url, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (a.id, a.published_at, a.headline, a.summary, a.source, a.url,
             now.isoformat()),
        )
    added = conn.total_changes - before
    for a in articles:
        for symbol in a.symbols:
            conn.execute(
                "INSERT OR IGNORE INTO news_symbols (article_id, symbol) VALUES (?, ?)",
                (a.id, symbol),
            )
    conn.commit()
    return added


def _spread_by_day(rows: list[tuple], limit: int) -> list[tuple]:
    """Pick up to `limit` rows covering as many distinct days as possible.

    Rows are newest first. Taking the newest N outright fails for a heavily
    covered name — AAPL fills eight slots from a single afternoon — and then
    the "history" is one day long. Instead take the newest article from each
    day first, newest day first, and only then fill the remaining slots by
    recency. Output stays newest first.
    """
    by_day: dict[str, list[tuple]] = {}
    for r in rows:
        by_day.setdefault(markettime.fmt_et_date(r[0]), []).append(r)
    picked = [articles[0] for articles in by_day.values()][:limit]
    chosen = set(map(id, picked))
    picked += [r for r in rows if id(r) not in chosen][: limit - len(picked)]
    return sorted(picked, key=lambda r: r[0], reverse=True)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _archived_news(
    conn: sqlite3.Connection, symbol: str, window_start: str
) -> list[tuple[str, str, str, str]]:
    """(published_at, source, headline, summary) for a symbol, newest first."""
    rows = conn.execute(
        """SELECT a.published_at, a.source, a.headline, a.summary
           FROM news_articles a JOIN news_symbols s ON s.article_id = a.id
           WHERE s.symbol = ? AND a.published_at >= ?
           ORDER BY a.published_at DESC, a.id DESC""",
        (symbol, window_start),
    ).fetchall()
    # The feed HTML-escapes some headlines ("Storage &amp; Peripherals").
    return [(ts, src, html.unescape(hl), html.unescape(sm or ""))
            for ts, src, hl, sm in rows]


def fetch_candidate_news(
    conn: sqlite3.Connection,
    symbols: Iterable[str],
    now: datetime,
    lookup_failed: set[str] = frozenset(),
) -> dict[str, list[str]]:
    """The most recent few dated headlines per candidate, from the archive.

    Deliberately thinner than a holding's news: no summaries and no
    new/earlier split. A candidate needs enough to tell whether something is
    going on in the business behind the price move — not a running story. A
    failed lookup is a one-item list saying so, never an empty list, since an
    empty list here means the lookup found nothing in the window.
    """
    window_start = (now - timedelta(days=config.NEWS_HISTORY_DAYS)).isoformat()
    out: dict[str, list[str]] = {}
    for symbol in symbols:
        if symbol in lookup_failed:
            out[symbol] = ["(news lookup failed today — no information either way)"]
            continue
        rows = _archived_news(conn, symbol, window_start)[: config.NEWS_PER_CANDIDATE]
        out[symbol] = [f"{markettime.fmt_et_date(ts)} — {hl}" for ts, _, hl, _ in rows]
    return out


def fetch_news_history(
    conn: sqlite3.Connection,
    symbols: Iterable[str],
    now: datetime,
    lookup_failed: set[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Each symbol's archived news over the window, split at the previous cycle.

    "New" means published after the last logged decision cycle — i.e. the
    model has never been shown it. Everything older in the window is listed as
    headlines it can recognise, so a story it reacted to last week reads as
    last week's story rather than as today's news. With no previous cycle,
    everything in the window counts as new.
    """
    (last_cycle,) = conn.execute("SELECT MAX(timestamp) FROM decision_cycles").fetchone()
    window_start = (now - timedelta(days=config.NEWS_HISTORY_DAYS)).isoformat()

    history: dict[str, dict[str, Any]] = {}
    for symbol in sorted(symbols):
        rows = _archived_news(conn, symbol, window_start)
        new = [r for r in rows if last_cycle is None or r[0] > last_cycle]
        earlier = _spread_by_day(
            [r for r in rows if not (last_cycle is None or r[0] > last_cycle)],
            config.NEWS_EARLIER_PER_SYMBOL,
        )

        entry: dict[str, Any] = {
            "published_since_your_last_cycle": [
                {
                    "published": markettime.fmt_et(ts),
                    "source": source,
                    "headline": headline,
                    **({"summary": _clip(summary, config.NEWS_SUMMARY_CHARS)}
                       if summary else {}),
                }
                for ts, source, headline, summary in new[: config.NEWS_NEW_PER_SYMBOL]
            ],
            "earlier": [
                f"{markettime.fmt_et_date(ts)} — {headline}"
                for ts, _, headline, _ in earlier
            ],
        }
        hidden_new = len(new) - config.NEWS_NEW_PER_SYMBOL
        hidden_old = (len(rows) - len(new)) - len(earlier)
        if hidden_new > 0 or hidden_old > 0:
            entry["not_shown"] = (
                f"{max(hidden_new, 0)} newer and {max(hidden_old, 0)} older articles "
                f"in the last {config.NEWS_HISTORY_DAYS} days were left out for length."
            )
        if symbol in lookup_failed:
            # The archive may still hold older items, but today's fetch did not
            # happen, so an empty "new" list here is not evidence of quiet.
            entry["lookup_failed"] = (
                "Today's news lookup for this symbol failed. Anything published "
                "since your last cycle may be missing — do not read silence as "
                "no news."
            )
        history[symbol] = entry
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
    usage: tuple[int | None, int | None] = (None, None),
    version: AgentVersion | None = None,
) -> int:
    """Persist the full prompt, Claude's response/reasoning, validation results,
    executed orders, and portfolio value for this cycle.

    `usage` is (input_tokens, output_tokens) as reported by the API. Output
    includes thinking tokens, which are billed but never appear in
    `claude_response` — without recording them here the cycle's real cost can
    only be guessed at, and effort tuning has nothing to measure against.

    Returns the new cycle id, which names this cycle's prompt log file.
    """
    cursor = conn.execute(
        """
        INSERT INTO decision_cycles (
            timestamp, mode, prompt, claude_response, portfolio_cash,
            portfolio_equity, input_tokens, output_tokens,
            code_commit, code_dirty, system_prompt_sha, model, effort
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp.isoformat(), mode, prompt, claude_response,
            portfolio.cash, portfolio.equity, usage[0], usage[1],
            version.code_commit if version else None,
            (None if version is None or version.code_dirty is None
             else int(version.code_dirty)),
            version.system_prompt_sha if version else None,
            version.model if version else None,
            version.effort if version else None,
        ),
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
    version: AgentVersion | None = None,
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
            + (
                f"code      {version.code_commit}"
                f"{' (uncommitted edits)' if version.code_dirty else ''}\n"
                f"sysprompt {version.system_prompt_sha}\n"
                if version else ""
            )
            + "\n"
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
    # Taken before anything runs, so an edit made mid-cycle cannot relabel it.
    version = current_agent_version()
    try:
        now = markettime.now_utc()
        portfolio = fetch_portfolio_state(mode)
        market = fetch_market_context(watchlist, portfolio)
        # Needs prices, so it runs here rather than in fetch_portfolio_state.
        # Must precede both the prompt and validation — they read the result.
        price_pending_orders(portfolio, market.prices)
        news_added = store_news(conn, market.articles, now)
        news_history = fetch_news_history(
            conn, portfolio.positions, now, market.news_lookup_failed,
        )
        candidate_news = fetch_candidate_news(
            conn, market.candidates, now, market.news_lookup_failed,
        )
        theses = fetch_open_theses(conn)
        actions = fetch_action_history(conn)
        prompt = build_decision_prompt(
            portfolio, market, theses, actions, news_history, candidate_news, now,
        )
        trades, reviews, raw_response, usage = get_claude_decision(prompt)
        validated = validate_trades(trades, portfolio, mode)
        execute_trades(validated, mode, markettime.fmt_et_date(now))
        # Logged before the lifecycle runs so the audit record of what was
        # decided exists before any state derived from it, and so revisions can
        # name the cycle that made them.
        cycle_id = log_decision_cycle(
            conn, prompt, raw_response, validated, reviews, portfolio, now, mode,
            usage, version,
        )
        thesis_counts = apply_thesis_lifecycle(
            conn, validated, reviews, portfolio, now, cycle_id,
        )
        cycles_completed = 1
        prompt_path = write_prompt_log(cycle_id, prompt, mode, now, version)
        _print_cycle_summary(
            cycle_id, now, validated, reviews, thesis_counts, portfolio, prompt_path,
            news_added,
        )
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
    thesis_counts: dict[str, int],
    portfolio: PortfolioState,
    prompt_path: str,
    news_added: int,
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
    in_flight = sum(len(v) for v in portfolio.pending_orders.values())
    if in_flight:
        # Normally zero: the previous cycle's orders fill before this one runs.
        print(f"  orders in flight        {in_flight} (unfilled, from an earlier run)")
    changed = {
        k: n for k, n in thesis_counts.items()
        if n and k not in ("incomplete", "orphaned")
    }
    if changed:
        print("  theses                  "
              + ", ".join(f"{n} {k}" for k, n in changed.items()))
    # A case for something the account does not own, with nothing in flight to
    # explain it — closed here, but worth seeing rather than inferring.
    if thesis_counts.get("orphaned"):
        print(f"  ORPHANED CASES CLOSED   {thesis_counts['orphaned']}")
    # A case the model tried to rewrite and couldn't is a prompt problem, not a
    # quiet no-op: it will keep reasoning against wording it thinks it changed.
    if thesis_counts.get("incomplete"):
        print(f"  INCOMPLETE CASE IGNORED {thesis_counts['incomplete']}")
    print(f"  news archived           {news_added} new article{'s' if news_added != 1 else ''}")
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
