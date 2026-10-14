"""Golden prompt fixture: capture once from live data, then rebuild offline.

The agent.py module split must not change a single byte of what Claude is
shown. `capture()` takes one real, read-only snapshot of the account and the
market, and stores it with a copy of the database and the prompt the current
code builds from them. `build_prompt()` rebuilds that prompt from the stored
snapshot with no network access, so test_prompt_golden.py can compare the two.

Capturing only reads from the broker and never writes trading_agent.db: the
snapshot's news is archived into the copy, as a real cycle would archive it.

Re-capture deliberately whenever the prompt is *meant* to change, for example
when the fundamentals feed lands:   venv/bin/python tests/golden.py
"""

from __future__ import annotations

import hashlib
import pickle
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import config  # noqa: E402
import markettime  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DB = FIXTURES / "golden.db"
INPUTS = FIXTURES / "golden_inputs.pkl"
PROMPT = FIXTURES / "golden_prompt.txt"
SYSTEM_SHA = FIXTURES / "golden_system_prompt.sha256"


def system_prompt_sha() -> str:
    return hashlib.sha256(agent.SYSTEM_PROMPT.encode()).hexdigest()


def build_prompt(db_path: Path, inputs: dict) -> str:
    """Rebuild the decision prompt as run_decision_cycle does, from stored inputs."""
    portfolio, market, now = inputs["portfolio"], inputs["market"], inputs["now"]
    conn = sqlite3.connect(db_path)
    try:
        news_history = agent.fetch_news_history(
            conn, portfolio.positions, now, market.news_lookup_failed,
        )
        candidate_news = agent.fetch_candidate_news(
            conn, market.candidates, now, market.news_lookup_failed,
        )
        drawdown_news = agent.fetch_drawdown_news(
            conn, market.drawdown_windows, market.drawdown_lookup_failed,
        )
        theses = agent.fetch_open_theses(conn)
        actions = agent.fetch_action_history(conn)
    finally:
        conn.close()
    return agent.build_decision_prompt(
        portfolio, market, theses, actions, news_history, candidate_news, now,
        inputs.get("fundamentals"), drawdown_news,
    )


def capture(source_db: Path = ROOT / config.DEFAULT_DB_PATH) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    dst = sqlite3.connect(DB)
    src.backup(dst)
    src.close()

    now = markettime.now_utc()
    portfolio = agent.fetch_portfolio_state("paper")
    market = agent.fetch_market_context(config.WATCHLIST, portfolio)
    agent.price_pending_orders(portfolio, market.prices)
    agent.store_news(dst, market.articles, now)
    fundamentals = agent.fundamentals_for(dst, portfolio, market, now)
    dst.close()

    inputs = {"now": now, "portfolio": portfolio, "market": market,
              "fundamentals": fundamentals}
    INPUTS.write_bytes(pickle.dumps(inputs))
    PROMPT.write_text(build_prompt(DB, inputs))
    SYSTEM_SHA.write_text(system_prompt_sha())
    print(f"captured golden fixture in {FIXTURES} "
          f"({PROMPT.stat().st_size:,} bytes of prompt)")


if __name__ == "__main__":
    capture()
