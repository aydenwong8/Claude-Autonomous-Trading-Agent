"""The fundamentals block reaches the prompt on the right names, and fetching
it can never fail the cycle."""

import json
from datetime import datetime, timezone

import agent

NOW = datetime(2026, 10, 5, 20, 30, tzinfo=timezone.utc)
GD_BLOCK = {"valuation": {"pe_ttm": 19.9}}
IQV_BLOCK = {"lookup_failed": {"finnhub": "metrics: HTTP 500"}}


def portfolio():
    return agent.PortfolioState(
        cash=10_000.0, equity=20_000.0,
        positions={"GD": agent.Position("GD", 10.0, 10_000.0, 300.0)}, timestamp=NOW,
    )


def market():
    screen = {s: {"chg_5d": 1.0} for s in ("GD", "IQV", "XOM")}
    return agent.MarketContext(
        prices={"GD": 1_000.0}, articles=[], news_lookup_failed=set(), market_news=[],
        watchlist=list(screen), screen=screen, candidates=["IQV", "XOM"],
    )


def state(fundamentals):
    prompt = agent.build_decision_prompt(
        portfolio(), market(), {}, {}, {}, {}, NOW, fundamentals,
    )
    return json.loads(prompt.split("(JSON):\n", 1)[1].rsplit("\n\n", 1)[0])


def test_blocks_attach_to_holdings_and_candidates():
    s = state({"GD": GD_BLOCK, "IQV": IQV_BLOCK})
    assert s["positions"][0]["fundamentals"] == GD_BLOCK
    candidates = {c["symbol"]: c for c in s["candidates"]}
    assert candidates["IQV"]["fundamentals"] == IQV_BLOCK  # a flagged block, verbatim
    assert "fundamentals" not in candidates["XOM"]


def test_no_fundamentals_means_no_field():
    s = state(None)
    assert "fundamentals" not in s["positions"][0]
    assert all("fundamentals" not in c for c in s["candidates"])


def test_system_prompt_explains_the_block():
    assert 'WHAT "fundamentals" IS' in agent.SYSTEM_PROMPT
    assert "lookup_failed" in agent.SYSTEM_PROMPT
    assert '"earnings_release"' in agent.SYSTEM_PROMPT
    assert '"recent_8k_text"' in agent.SYSTEM_PROMPT


def test_fundamentals_for_asks_for_holdings_and_candidates(monkeypatch):
    conn = agent.init_db(":memory:")
    agent.log_decision_cycle(conn, "p", "r", [], [], portfolio(), NOW, "paper", (1, 2), None)
    seen = {}

    def fake(symbols, last_cycle_at, now, held=frozenset()):
        seen.update(symbols=symbols, last=last_cycle_at, held=set(held))
        return {s: {} for s in symbols}

    # Patched where fundamentals_for looks it up, never via another module.
    monkeypatch.setitem(agent.fundamentals_for.__globals__, "fetch_fundamentals", fake)
    agent.fundamentals_for(conn, portfolio(), market(), NOW)
    # Only holdings get filed documents: they are a few thousand tokens a name.
    assert seen == {"symbols": ["GD", "IQV", "XOM"], "last": NOW, "held": {"GD"}}


def test_a_bug_in_fetching_never_fails_the_cycle(monkeypatch, capsys):
    def broken(symbols, last_cycle_at, now, held=frozenset()):
        raise RuntimeError("bug")

    monkeypatch.setitem(agent.fundamentals_for.__globals__, "fetch_fundamentals", broken)
    out = agent.fundamentals_for(agent.init_db(":memory:"), portfolio(), market(), NOW)
    assert out == {s: {"lookup_failed": {"fundamentals": "internal error"}}
                   for s in ("GD", "IQV", "XOM")}
    assert "RuntimeError: bug" in capsys.readouterr().err
