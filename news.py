"""Alpaca news: fetching, the news archive, and per-symbol news history."""

from __future__ import annotations

import html
import sqlite3
from datetime import date, datetime, timedelta, timezone
from collections.abc import Iterable
from typing import Any

import config
import markettime
from models import NewsArticle


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


def fetch_drawdown_news(
    conn: sqlite3.Connection,
    windows: dict[str, tuple[str, str]],
    lookup_failed: set[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Dated headlines from each deep-drawdown candidate's steepest fall, from
    the archive: the window's dates through the day after it ends, newest first.

    The cause of a fall months ago is not in the newest 30 days of headlines,
    and that cause is what decides whether the name is broken or mispriced.
    """
    out: dict[str, dict[str, Any]] = {}
    for symbol, (start, end) in sorted(windows.items()):
        if symbol in lookup_failed:
            headlines = ["(news lookup for this window failed today — no information either way)"]
        else:
            until = (date.fromisoformat(end) + timedelta(days=2)).isoformat()
            rows = [r for r in _archived_news(conn, symbol, start) if r[0] < until]
            headlines = [f"{markettime.fmt_et_date(ts)} — {hl}"
                         for ts, _, hl, _ in rows[: config.DRAWDOWN_NEWS_SHOWN]]
        out[symbol] = {"window": f"{start} to {end}", "headlines": headlines}
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
