"""Market context and the candidate screen."""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
from typing import Any

import config
import markettime
from models import MarketContext, NewsArticle, PortfolioState
from broker import _data_client, _news_client
from news import _fetch_articles, _news_symbol


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
