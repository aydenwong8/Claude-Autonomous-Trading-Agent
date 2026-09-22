"""Tests for what the agent can see of its own past — in-memory SQLite, no broker.

Covers the news archive (idempotent storage, the split at the previous cycle,
failed lookups) and the track record carried by closed cases, which is what a
candidate the agent previously exited shows up with.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import agent
import config


NOW = datetime(2026, 9, 22, 20, 35, tzinfo=timezone.utc)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = agent.init_db(":memory:")
    yield connection
    connection.close()


def article(id: int, hours_ago: float, symbols=("AAPL",), headline=None) -> agent.NewsArticle:
    return agent.NewsArticle(
        id=id,
        headline=headline or f"headline {id}",
        summary=f"summary {id}",
        source="benzinga",
        url=f"https://example.com/{id}",
        published_at=(NOW - timedelta(hours=hours_ago)).isoformat(),
        symbols=list(symbols),
    )


def add_cycle(conn: sqlite3.Connection, timestamp: datetime) -> int:
    return conn.execute(
        """INSERT INTO decision_cycles
           (timestamp, mode, prompt, claude_response, portfolio_cash, portfolio_equity)
           VALUES (?, 'paper', '', '', 0, 0)""",
        (timestamp.isoformat(),),
    ).lastrowid


# ---------------------------------------------------------------------------
# News archive
# ---------------------------------------------------------------------------

def test_store_news_is_idempotent_on_article_id(conn):
    batch = [article(1, 2), article(2, 3, symbols=("AAPL", "MSFT"))]
    assert agent.store_news(conn, batch, NOW) == 2
    # The same stories fetched again the next day add nothing.
    assert agent.store_news(conn, batch, NOW + timedelta(days=1)) == 0
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM news_symbols").fetchone() == (3,)


def test_first_seen_is_kept_from_the_first_fetch(conn):
    agent.store_news(conn, [article(1, 2)], NOW)
    agent.store_news(conn, [article(1, 2)], NOW + timedelta(days=3))
    (first_seen,) = conn.execute("SELECT first_seen_at FROM news_articles").fetchone()
    assert first_seen == NOW.isoformat()


def test_history_splits_at_the_previous_cycle(conn):
    add_cycle(conn, NOW - timedelta(days=1))
    agent.store_news(conn, [
        article(1, 2),          # after the last cycle: new
        article(2, 30),         # before it: earlier
        article(3, 50),
    ], NOW)
    h = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]
    assert [a["headline"] for a in h["published_since_your_last_cycle"]] == ["headline 1"]
    assert h["published_since_your_last_cycle"][0]["summary"] == "summary 1"
    # Newest first, dated, headline only.
    assert len(h["earlier"]) == 2
    assert h["earlier"][0].endswith("headline 2")
    assert "summary" not in h["earlier"][0]


def test_everything_is_new_before_the_first_cycle(conn):
    agent.store_news(conn, [article(1, 2), article(2, 100)], NOW)
    h = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]
    assert len(h["published_since_your_last_cycle"]) == 2
    assert h["earlier"] == []


def test_history_ignores_articles_outside_the_window(conn):
    old = config.NEWS_HISTORY_DAYS * 24 + 1
    agent.store_news(conn, [article(1, old)], NOW)
    h = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]
    assert h["published_since_your_last_cycle"] == [] and h["earlier"] == []


def test_history_is_per_symbol(conn):
    agent.store_news(conn, [article(1, 2, symbols=("MSFT",))], NOW)
    history = agent.fetch_news_history(conn, ["AAPL", "MSFT"], NOW)
    assert history["AAPL"]["published_since_your_last_cycle"] == []
    assert len(history["MSFT"]["published_since_your_last_cycle"]) == 1


def test_truncation_is_reported_not_silent(conn):
    add_cycle(conn, NOW - timedelta(days=1))
    n_new = config.NEWS_NEW_PER_SYMBOL + 2
    n_old = config.NEWS_EARLIER_PER_SYMBOL + 3
    batch = [article(i, 1 + i * 0.1) for i in range(n_new)]
    batch += [article(100 + i, 30 + i) for i in range(n_old)]
    agent.store_news(conn, batch, NOW)
    h = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]
    assert len(h["published_since_your_last_cycle"]) == config.NEWS_NEW_PER_SYMBOL
    assert len(h["earlier"]) == config.NEWS_EARLIER_PER_SYMBOL
    assert "2 newer and 3 older" in h["not_shown"]


def test_earlier_spreads_across_days_before_filling_by_recency(conn):
    add_cycle(conn, NOW - timedelta(hours=1))
    # A busy afternoon of twelve stories, then one story on each of three older days.
    batch = [article(i, 2 + i * 0.1) for i in range(12)]
    batch += [article(100 + d, 24 * d + 2) for d in (3, 6, 9)]
    agent.store_news(conn, batch, NOW)
    earlier = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]["earlier"]
    assert len(earlier) == config.NEWS_EARLIER_PER_SYMBOL
    # All three older days survive the busy afternoon, and the list is newest first.
    assert {"headline 103", "headline 106", "headline 109"} <= {e.split(" — ")[1] for e in earlier}
    dates = [e.split(" — ")[0] for e in earlier]
    assert dates == sorted(dates, reverse=True)


def test_html_entities_are_decoded(conn):
    agent.store_news(conn, [article(1, 2, headline="Storage &amp; Peripherals")], NOW)
    h = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]
    assert h["published_since_your_last_cycle"][0]["headline"] == "Storage & Peripherals"


def test_failed_lookup_is_flagged(conn):
    agent.store_news(conn, [article(1, 50)], NOW)
    h = agent.fetch_news_history(conn, ["AAPL", "MSFT"], NOW, lookup_failed={"MSFT"})
    assert "lookup_failed" in h["MSFT"]
    assert "lookup_failed" not in h["AAPL"]


def test_long_summaries_are_clipped(conn):
    a = article(1, 2)
    a.summary = "word " * 200
    agent.store_news(conn, [a], NOW)
    h = agent.fetch_news_history(conn, ["AAPL"], NOW)["AAPL"]
    summary = h["published_since_your_last_cycle"][0]["summary"]
    assert len(summary) <= config.NEWS_SUMMARY_CHARS
    assert summary.endswith("…")


# ---------------------------------------------------------------------------
# Closed cases in the track record
# ---------------------------------------------------------------------------

def test_closed_case_appears_in_track_record(conn):
    cycle = add_cycle(conn, NOW - timedelta(days=10))
    conn.execute(
        """INSERT INTO trades (cycle_id, symbol, side, notional_usd, reasoning, is_valid)
           VALUES (?, 'XOM', 'sell', 1800, 'war premium unwinding', 1)""",
        (cycle,),
    )
    conn.execute(
        """INSERT INTO theses (symbol, opened_at, thesis, target, invalidation,
                               status, closed_at, exit_trigger)
           VALUES ('XOM', ?, 'hedge', 'crude spike', 'de-escalation',
                   'closed', ?, 'thesis_broken')""",
        ((NOW - timedelta(days=60)).isoformat(), (NOW - timedelta(days=10)).isoformat()),
    )
    record = agent.fetch_action_history(conn)["XOM"]
    assert record["last_closed_case"]["exit_trigger"] == "thesis_broken"
    assert record["last_closed_case"]["why_you_owned_it"] == "hedge"
    assert record["actions"][0]["your_reasoning_at_the_time"] == "war premium unwinding"


def test_only_the_most_recent_closed_case_is_kept(conn):
    for days_ago, trigger in ((40, "target_hit"), (5, "risk")):
        conn.execute(
            """INSERT INTO theses (symbol, opened_at, thesis, target, invalidation,
                                   status, closed_at, exit_trigger)
               VALUES ('GD', ?, 't', 't', 'i', 'closed', ?, ?)""",
            ((NOW - timedelta(days=days_ago + 20)).isoformat(),
             (NOW - timedelta(days=days_ago)).isoformat(), trigger),
        )
    assert agent.fetch_action_history(conn)["GD"]["last_closed_case"]["exit_trigger"] == "risk"


def test_candidate_carries_its_track_record():
    portfolio = agent.PortfolioState(
        cash=10_000.0, equity=10_000.0, positions={}, timestamp=NOW,
    )
    screen = {"XOM": {"chg_5d": -6.2}, "IQV": {"chg_5d": 1.0}}
    market = agent.MarketContext(
        prices={}, articles=[], news_lookup_failed=set(), market_news=[],
        watchlist=["XOM", "IQV"], screen=screen, candidates=["XOM", "IQV"],
    )
    actions = {"XOM": {"summary": "You have acted on XOM 1 time"}}
    prompt = agent.build_decision_prompt(
        portfolio, market, {}, actions, {}, {"XOM": ["2026-09-22 — oil falls"]}, NOW,
    )
    state = json.loads(prompt.split("(JSON):\n", 1)[1].rsplit("\n\n", 1)[0])
    by_symbol = {c["symbol"]: c for c in state["candidates"]}
    assert by_symbol["XOM"]["your_track_record"]["summary"].startswith("You have acted")
    assert "your_track_record" not in by_symbol["IQV"]
    assert by_symbol["XOM"]["recent_news"] == ["2026-09-22 — oil falls"]
    assert "recent_news" not in by_symbol["IQV"]


def test_share_class_tickers_query_the_base_symbol():
    # The feed tags Berkshire as BRK / BRK/A / BRK/B; "BRK.B" matches nothing.
    assert agent._news_symbol("BRK.B") == "BRK"
    assert agent._news_symbol("BF.B") == "BF"
    assert agent._news_symbol("AAPL") == "AAPL"


# ---------------------------------------------------------------------------
# Candidate news
# ---------------------------------------------------------------------------

def test_candidate_news_is_newest_few_dated_headlines(conn):
    batch = [article(i, 2 + i * 10, symbols=("IQV",)) for i in range(6)]
    agent.store_news(conn, batch, NOW)
    news = agent.fetch_candidate_news(conn, ["IQV", "CRL"], NOW)
    assert len(news["IQV"]) == config.NEWS_PER_CANDIDATE
    assert news["IQV"][0].endswith("headline 0")
    # Looked up, nothing found: an empty list, which the prompt defines as "none".
    assert news["CRL"] == []


def test_candidate_failed_lookup_is_not_an_empty_list(conn):
    news = agent.fetch_candidate_news(conn, ["CRL"], NOW, lookup_failed={"CRL"})
    assert news["CRL"] and "failed" in news["CRL"][0]
