"""News from when a deep-drawdown candidate actually fell.

A candidate gets its newest few headlines from the last 30 days. For a name
57% below its high that is the wrong window: the fall happened months ago, so
the agent passed on BSX, INTU, FICO and CHTR saying it "was not given the
cause". This finds the steepest 5-day fall since the 52-week high in the bars
the screen already has, and shows the news from that window.
"""

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agent
import config
import screen
from models import NewsArticle

NOW = datetime(2026, 10, 14, 20, 30, tzinfo=timezone.utc)


def days(n, start=date(2026, 6, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


# --- finding the fall -----------------------------------------------------------


def test_steepest_five_day_fall_after_the_high():
    # A bigger 5-session fall before the high (130 -> 60) must be ignored: it
    # is not part of how the stock got from its high to here.
    closes = [130, 129, 128, 127, 126, 60, 80, 100, 150, 148, 140, 139, 100, 98, 97, 99, 96, 95]
    d = days(len(closes))
    pct, window = screen.steepest_fall_since_high(closes, d)
    # Worst close-to-close change over 5 sessions from the high at index 8.
    assert pct == pytest.approx(-34.7, abs=0.05)   # 150 -> 98, index 8 -> 13
    assert window == (d[8], d[13])


def test_no_fall_window_until_five_sessions_after_the_high():
    closes = [100, 101, 102, 103, 104, 105, 104, 103]
    assert screen.steepest_fall_since_high(closes, days(len(closes))) is None


def bar(close, day):
    return SimpleNamespace(close=close, volume=1_000_000,
                           timestamp=datetime.fromisoformat(day).replace(tzinfo=timezone.utc))


def test_screen_rows_carry_the_fall_window():
    closes = [100 + i for i in range(60)] + [150, 120, 110, 105, 100, 99] + [100] * 10
    d = days(len(closes))
    bars = {"ABC": [bar(c, day) for c, day in zip(closes, d)]}
    stats = screen.compute_screen_stats(["ABC"], get=lambda chunk: bars)["ABC"]
    # The high is 159 at index 59; 159 -> 100 over the next 5 sessions.
    assert stats["steepest_5d_fall_since_high_pct"] == pytest.approx(-37.1, abs=0.05)
    assert stats["steepest_5d_fall_window"] == f"{d[59]} to {d[64]}"


def test_only_deep_drawdown_candidates_get_a_window():
    stats = {
        "DEEP": {"from_52w_high": -57.0, "steepest_5d_fall_window": "2026-07-24 to 2026-07-31"},
        "SHALLOW": {"from_52w_high": -12.0, "steepest_5d_fall_window": "2026-09-01 to 2026-09-08"},
        "NOWIN": {"from_52w_high": -40.0},
        "HELD": {"from_52w_high": -60.0, "steepest_5d_fall_window": "2026-05-01 to 2026-05-08"},
    }
    assert screen.drawdown_windows(stats, ["DEEP", "SHALLOW", "NOWIN"]) == {
        "DEEP": ("2026-07-24", "2026-07-31"),
    }


# --- fetching and archiving -------------------------------------------------------


class FakeNews:
    def __init__(self, fail=()):
        self.requests = []
        self.fail = set(fail)

    def get_news(self, request):
        self.requests.append(request)
        if request.symbols in self.fail:
            raise ConnectionResetError(54, "reset")
        # Shaped like alpaca-py's NewsSet.
        return SimpleNamespace(data={"news": [SimpleNamespace(
            id=7, headline=f"{request.symbols} cuts guidance",
            summary="", source="test", url="", symbols=[],
            created_at=datetime(2026, 7, 25, 13, tzinfo=timezone.utc),
        )]})


def test_fall_window_news_is_requested_for_that_window_and_tagged():
    news = FakeNews(fail={"BSX"})
    articles, failed = screen.fetch_drawdown_articles(
        news, {"INTU": ("2026-07-24", "2026-07-31"), "BSX": ("2026-05-01", "2026-05-08")},
    )
    (intu,) = [r for r in news.requests if r.symbols == "INTU"]
    assert intu.start == datetime(2026, 7, 24, tzinfo=timezone.utc)
    # Through the day after the window ends: a fall that ends on a print's
    # reaction day is often written up that evening or the next morning.
    assert intu.end == datetime(2026, 8, 2, tzinfo=timezone.utc)
    assert [a.headline for a in articles] == ["INTU cuts guidance"]
    assert "INTU" in articles[0].symbols
    assert failed == {"BSX"}


# --- reading it back --------------------------------------------------------------


def article(i, symbol, published):
    return NewsArticle(id=i, headline=f"{symbol} story {i}", summary="", source="s", url="",
                       published_at=published, symbols=[symbol])


def test_headlines_from_inside_the_window_only(tmp_path):
    conn = agent.init_db(":memory:")
    agent.store_news(conn, [
        article(1, "INTU", "2026-07-20T14:00:00+00:00"),   # before
        article(2, "INTU", "2026-07-25T14:00:00+00:00"),
        article(3, "INTU", "2026-08-01T14:00:00+00:00"),   # the day after the window
        article(4, "INTU", "2026-08-05T14:00:00+00:00"),   # after
    ], NOW)
    out = agent.fetch_drawdown_news(conn, {"INTU": ("2026-07-24", "2026-07-31")})
    assert out == {"INTU": {
        "window": "2026-07-24 to 2026-07-31",
        "headlines": ["2026-08-01 — INTU story 3", "2026-07-25 — INTU story 2"],
    }}


def test_a_failed_fall_window_lookup_says_so():
    conn = agent.init_db(":memory:")
    out = agent.fetch_drawdown_news(conn, {"BSX": ("2026-05-01", "2026-05-08")},
                                    lookup_failed={"BSX"})
    assert out["BSX"]["headlines"] == [
        "(news lookup for this window failed today — no information either way)"]


def test_headlines_are_capped():
    conn = agent.init_db(":memory:")
    agent.store_news(conn, [article(i, "X", f"2026-07-{24 + i % 7:02d}T14:00:00+00:00")
                            for i in range(20)], NOW)
    out = agent.fetch_drawdown_news(conn, {"X": ("2026-07-24", "2026-07-31")})
    assert len(out["X"]["headlines"]) == config.DRAWDOWN_NEWS_SHOWN


# --- the prompt -------------------------------------------------------------------


def test_candidate_row_carries_it_and_the_system_prompt_explains_it():
    portfolio = agent.PortfolioState(cash=10_000.0, equity=10_000.0, positions={}, timestamp=NOW)
    market = agent.MarketContext(
        prices={}, articles=[], news_lookup_failed=set(), market_news=[],
        watchlist=["INTU", "XOM"], screen={"INTU": {"chg_5d": 1.0}, "XOM": {"chg_5d": 1.0}},
        candidates=["INTU", "XOM"],
    )
    fall = {"window": "2026-07-24 to 2026-07-31", "headlines": ["2026-07-25 — INTU story"]}
    prompt = agent.build_decision_prompt(portfolio, market, {}, {}, {}, {}, NOW, None,
                                         {"INTU": fall})
    state = json.loads(prompt.split("(JSON):\n", 1)[1].rsplit("\n\n", 1)[0])
    rows = {c["symbol"]: c for c in state["candidates"]}
    assert rows["INTU"]["news_around_steepest_fall"] == fall
    assert "news_around_steepest_fall" not in rows["XOM"]
    assert '"news_around_steepest_fall"' in agent.SYSTEM_PROMPT
