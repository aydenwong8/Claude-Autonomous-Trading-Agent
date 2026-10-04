# Split agent.py into modules — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 2,518-line `agent.py` into nine focused modules with zero behaviour change, proven by a byte-identical golden prompt and new guardrail tests.

**Architecture:** Flat modules at the repo root. A one-off script (`tools/split_agent.py`) moves named top-level definitions out of `agent.py` **verbatim** (with the comments above them), generates each new module's imports, and makes `agent.py` import every moved name back, so `agent.X` keeps working everywhere. The safety net is written and committed against the unsplit code *first*; then one module moves per commit.

**Tech Stack:** Python 3 (venv at `venv/`), pytest, pyflakes (new, dev-only), alpaca-py, anthropic, SQLite.

**Spec:** `docs/superpowers/specs/2026-10-04-agent-refactor-design.md`

## Global Constraints

- **No behaviour change.** No logic, prompt wording, threshold, schema or output changes. Function bodies move byte-for-byte; only import lines are new.
- **These files stay unmodified:** `dashboard.py`, `test_memory.py`, `test_version.py`, `test_metrics.py`, `run_daily.sh`, `config.py`, `markettime.py`, `metrics.py`, `report.py`.
- `current_agent_version`, `run_decision_cycle`, `_print_cycle_summary`, `main` and `load_dotenv()` **stay in `agent.py`**.
- **Never write to `trading_agent.db`** and never run `agent.py` without `--help`: a real run places orders and calls the Anthropic API.
- Work on branch `refactor/split-agent`. The launchd job runs whatever is checked out at **16:30 ET on weekdays**, so `main` must be checked out with the work either fully merged or not merged at all by then.
- Commit messages: a short imperative subject, then a body summarising the change in the repo's existing style. **No `Co-Authored-By` or "Generated with" trailers.**
- Always use `venv/bin/python`, run from the repo root.

## Review Focus

- **Pickled fixture after classes move.** The golden inputs are pickled while the classes live in `agent`. Unpickling after the move must still find `agent.PortfolioState` etc. through the re-exports. Covered by Task 3's golden test, which runs after every move.
- **`monkeypatch.setattr(agent, "SYSTEM_PROMPT", ...)` in `test_version.py`.** This only reaches functions that read `SYSTEM_PROMPT` from `agent`'s namespace. That is why `current_agent_version` stays in `agent.py` (Global Constraints), and the existing test checks it after every move.
- **Names used only on rarely-run paths** (e.g. an error branch in `execute_trades`) would pass every test, then raise `NameError` on the first live cycle that hits them. Guarded by the `pyflakes` undefined-name check in every move task.
- **`dashboard.py` still imports `_data_client` / `_trading_client` from `agent`,** and relies on importing `agent` to run `load_dotenv()`. Checked in Task 14 by importing `dashboard` and confirming the env is loaded.
- **launchd starts the agent with `cwd=/`.** Module imports must resolve from the script's own directory. Checked in Task 14 by running `agent.py --help` from `/`.

---

### Task 1: Dev tooling and the re-export guard

**Files:**
- Modify: `requirements.txt`
- Create: `test_reexports.py`

**Interfaces:**
- Produces: `test_reexports.NAMES`, the 54 top-level names `agent.py` defines today, every one of which must stay reachable as `agent.<name>`.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b refactor/split-agent
```

- [ ] **Step 2: Add pyflakes as a dev dependency**

In `requirements.txt`, under the existing `# Dev only — the scheduled run does not import it.` comment, add the line after `pytest==9.1.1`:

```
pyflakes==4.0.2
```

Run: `venv/bin/pip install -r requirements.txt`
Expected: completes; `venv/bin/python -m pyflakes --version` prints `4.0.2`.

- [ ] **Step 3: Write the re-export guard**

Create `test_reexports.py`:

```python
"""Every name agent.py defined before the module split stays reachable as agent.X.

dashboard.py, the tests and the pickled golden fixture all reach into agent by
name. The split moves definitions into other modules and imports them back;
this test fails if any name is dropped on the way.
"""

import agent

NAMES = [
    "PROMPT_LOG_DIR", "Position", "PendingOrder", "PortfolioState", "NewsArticle",
    "MarketContext", "Thesis", "PositionReview", "ProposedTrade", "ValidatedTrade",
    "_trading_client", "_data_client", "_news_client", "fetch_pending_orders",
    "price_pending_orders", "fetch_portfolio_state", "fetch_market_context",
    "_pct_change", "compute_screen_stats", "select_candidates", "_news_symbol",
    "_fetch_articles", "SYSTEM_PROMPT", "build_decision_prompt",
    "_parse_trades_response", "get_claude_decision", "validate_trades",
    "_client_order_id", "execute_trades", "init_db", "_migrate_cycle_usage",
    "_migrate_cycle_version", "AgentVersion", "current_agent_version",
    "_migrate_thesis_revisions", "fetch_open_theses", "fetch_action_history",
    "store_news", "_spread_by_day", "_clip", "_archived_news",
    "fetch_candidate_news", "fetch_news_history", "CASE_FIELDS", "_full_case",
    "apply_thesis_lifecycle", "ORPHAN_EXIT_TRIGGER", "_close_orphaned_theses",
    "log_run", "log_decision_cycle", "write_prompt_log", "run_decision_cycle",
    "_print_cycle_summary", "main",
]


def test_every_original_name_is_still_reachable():
    assert len(NAMES) == 54
    assert [n for n in NAMES if not hasattr(agent, n)] == []
```

- [ ] **Step 4: Run it**

Run: `venv/bin/python -m pytest -q test_reexports.py`
Expected: `1 passed`. It passes on the unsplit code by construction; its job is to keep passing through Tasks 5–13.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt test_reexports.py
git commit -F - <<'EOF'
Add pyflakes and a guard that agent.X names survive the split

- requirements.txt: pyflakes (dev only), used to catch undefined names
  in moved code that no test happens to execute.
- test_reexports.py: lists the 54 top-level names agent.py defines
  today and asserts each is still reachable as agent.X.
EOF
```

---

### Task 2: Guardrail tests

**Files:**
- Create: `test_guardrails.py`

**Interfaces:**
- Consumes: `agent.validate_trades(trades, portfolio, mode) -> list[ValidatedTrade]`, `agent.PortfolioState`, `agent.Position`, `agent.ProposedTrade`, `agent.PendingOrder`, `agent._trading_client(mode)`; `config.MAX_TRADES_PER_CYCLE`, `config.WATCHLIST`, `config.VALID_EXIT_TRIGGERS`, `config.LIVE_UNLOCK_ENV`.
- Produces: nothing consumed by later tasks. The file must keep passing, unmodified, after every move.

These tests characterise **existing** behaviour, so they pass on first run. There is no red phase: they are the "before" half of a before/after comparison. They were verified to pass against both the unsplit code and a dry-run split (36 passed).

- [ ] **Step 1: Write the tests**

Create `test_guardrails.py`:

```python
"""Guardrail tests: at least one case per rule enforced by validate_trades.

Written against the unsplit agent.py and kept unchanged through the module
split, so they show the guardrails behave the same before and after it.
Every rejection reason is asserted verbatim: it is what the trades table
records, and what the agent is shown about its own rejected proposals.
"""

import math
from datetime import datetime, timezone

import pytest

import agent
import config

NOW = datetime(2026, 10, 2, 20, 30, tzinfo=timezone.utc)
OK = (True, None)


class FakeAsset:
    def __init__(self, tradable: bool):
        self.tradable = tradable


class FakeBroker:
    """Stands in for Alpaca's TradingClient; validation only calls get_asset."""

    def __init__(self):
        self.untradable: set[str] = set()
        self.unknown: set[str] = set()

    def get_asset(self, symbol):
        if symbol in self.unknown:
            raise RuntimeError(f"asset not found: {symbol}")
        return FakeAsset(symbol not in self.untradable)


@pytest.fixture
def broker(monkeypatch):
    # Patched in whichever module defines validate_trades, so this keeps
    # working unchanged after validate_trades moves out of agent.py.
    fake = FakeBroker()
    monkeypatch.setitem(
        agent.validate_trades.__globals__, "_trading_client", lambda mode: fake,
    )
    return fake


def portfolio(cash=20_000.0, equity=100_000.0, positions=None, **pending):
    held = {"AAPL": 10_000.0} if positions is None else positions
    return agent.PortfolioState(
        cash=cash,
        equity=equity,
        positions={
            s: agent.Position(symbol=s, qty=1.0, market_value=v, avg_entry_price=v)
            for s, v in held.items()
        },
        timestamp=NOW,
        **pending,
    )


def buy(symbol, usd, **case):
    fields = {"thesis": "t", "target": "g", "invalidation": "i"} | case
    return agent.ProposedTrade(
        symbol=symbol, side="buy", notional_usd=usd, reasoning="r", **fields,
    )


def sell(symbol, usd, trigger="risk"):
    return agent.ProposedTrade(
        symbol=symbol, side="sell", notional_usd=usd, reasoning="r", exit_trigger=trigger,
    )


def pending_buy(symbol, usd):
    return agent.PendingOrder(
        symbol=symbol, side="buy", notional_usd=usd, qty=None, status="new",
        submitted_at=None,
    )


def check(trades, state):
    return [
        (v.is_valid, v.rejection_reason)
        for v in agent.validate_trades(trades, state, "paper")
    ]


# --- 15% position cap ------------------------------------------------------


def test_buy_up_to_the_cap_passes_and_past_it_is_rejected(broker):
    assert check([buy("AAPL", 5_000)], portfolio()) == [OK]
    assert check([buy("AAPL", 6_000)], portfolio()) == [
        (False, "exceeds max position size (15%)")]


def test_cap_counts_unfilled_buys(broker):
    state = portfolio(pending_orders={"MSFT": [pending_buy("MSFT", 14_000)]})
    assert check([buy("MSFT", 2_000)], state) == [
        (False, "exceeds max position size (15%)")]


def test_two_buys_cannot_jointly_breach_the_cap(broker):
    assert check([buy("JNJ", 10_000), buy("JNJ", 6_000)], portfolio(cash=50_000)) == [
        OK, (False, "exceeds max position size (15%)")]


def test_buy_against_non_positive_equity_fails_closed(broker):
    assert check([buy("JNJ", 100)], portfolio(equity=0.0)) == [
        (False, "cannot size position against non-positive equity")]


def test_rejected_trades_are_recorded_unadjusted(broker):
    trade = buy("AAPL", 6_000)
    [result] = agent.validate_trades([trade], portfolio(), "paper")
    assert result.trade is trade and trade.notional_usd == 6_000


# --- cash and the 5% buffer --------------------------------------------------


def test_buy_larger_than_cash_is_rejected(broker):
    assert check([buy("JNJ", 4_000)], portfolio(cash=3_000)) == [
        (False, "insufficient cash (no margin/leverage)")]


def test_buy_that_breaches_the_buffer_is_rejected(broker):
    # 6,000 - 2,000 leaves 4,000, under the 5,000 floor (5% of 100,000).
    assert check([buy("JNJ", 2_000)], portfolio(cash=6_000)) == [
        (False, "would breach minimum cash buffer (5%)")]


def test_buffer_is_measured_net_of_unfilled_buys(broker):
    # 20,000 of cash, but 14,000 is already committed to an order in flight.
    state = portfolio(cash=20_000, pending_buy_commitment=14_000)
    assert check([buy("JNJ", 2_000)], state) == [
        (False, "would breach minimum cash buffer (5%)")]
    assert check([buy("JNJ", 1_000)], state) == [OK]


def test_sell_proceeds_are_not_spendable_in_the_same_cycle(broker):
    # Credited, the 5,000 sale would make the 3,000 buy affordable.
    assert check([sell("AAPL", 5_000), buy("JNJ", 3_000)], portfolio(cash=6_000)) == [
        OK, (False, "would breach minimum cash buffer (5%)")]


# --- trade count -------------------------------------------------------------


def test_at_most_five_trades_per_cycle(broker):
    assert config.MAX_TRADES_PER_CYCLE == 5
    results = check([sell("AAPL", 10)] * 6, portfolio())
    assert results[:5] == [OK] * 5
    assert results[5] == (False, "exceeds max trades per decision cycle")


# --- universe and ticker validation ------------------------------------------


def test_whitelist_gates_buys(broker):
    assert "ZZZZ" not in config.WATCHLIST
    assert check([buy("ZZZZ", 100)], portfolio()) == [
        (False, "symbol not in whitelisted universe")]


def test_held_name_outside_the_whitelist_stays_sellable(broker):
    assert check([sell("ZZZZ", 500)], portfolio(positions={"ZZZZ": 1_000.0})) == [OK]


def test_untradable_ticker_is_rejected(broker):
    broker.untradable.add("JNJ")
    assert check([buy("JNJ", 100)], portfolio()) == [
        (False, "symbol not tradable at broker")]


def test_ticker_the_broker_cannot_find_is_rejected(broker):
    broker.unknown.add("JNJ")
    assert check([buy("JNJ", 100)], portfolio()) == [
        (False, "symbol failed broker asset validation")]


# --- long-only ---------------------------------------------------------------


def test_cannot_sell_a_name_not_held(broker):
    assert check([sell("MSFT", 100)], portfolio()) == [
        (False, "no position held (long-only, no shorting)")]


def test_cannot_sell_more_than_held(broker):
    assert check([sell("AAPL", 10_001)], portfolio()) == [
        (False, "cannot sell more than held position (net of unfilled sells)")]


def test_sells_are_checked_cumulatively(broker):
    assert check([sell("AAPL", 6_000), sell("AAPL", 6_000)], portfolio()) == [
        OK, (False, "cannot sell more than held position (net of unfilled sells)")]


def test_shares_already_being_sold_cannot_be_sold_again(broker):
    state = portfolio(pending_sell_value={"AAPL": 8_000.0})
    assert check([sell("AAPL", 3_000)], state) == [
        (False, "cannot sell more than held position (net of unfilled sells)")]
    assert check([sell("AAPL", 2_000)], state) == [OK]


def test_unfilled_buys_are_not_sellable(broker):
    state = portfolio(pending_orders={"MSFT": [pending_buy("MSFT", 5_000)]})
    assert check([sell("MSFT", 100)], state) == [
        (False, "no position held (long-only, no shorting)")]


def test_unvaluable_unfilled_sell_blocks_further_sells(broker):
    state = portfolio(unvaluable_pending_sells=["AAPL"])
    assert check([sell("AAPL", 100)], state) == [
        (False, "unfilled sell of unknown size for this symbol; cannot size a further sell")]


# --- required fields ---------------------------------------------------------


@pytest.mark.parametrize("missing", ["thesis", "target", "invalidation"])
def test_buy_needs_the_full_case(broker, missing):
    assert check([buy("JNJ", 100, **{missing: None})], portfolio()) == [
        (False, f"buy missing required thesis field(s): {missing}")]


def test_blank_case_fields_count_as_missing(broker):
    assert check([buy("JNJ", 100, thesis="  ", target="")], portfolio()) == [
        (False, "buy missing required thesis field(s): thesis, target")]


@pytest.mark.parametrize("trigger", [None, "price_drift", "position_gone"])
def test_sell_needs_a_valid_exit_trigger(broker, trigger):
    assert check([sell("AAPL", 100, trigger)], portfolio()) == [
        (False, f"sell has invalid or missing exit_trigger '{trigger}'")]


def test_every_listed_exit_trigger_is_accepted(broker):
    trades = [sell("AAPL", 10, t) for t in sorted(config.VALID_EXIT_TRIGGERS)]
    assert check(trades, portfolio()) == [OK] * len(trades)


# --- sizes and sides ---------------------------------------------------------


@pytest.mark.parametrize("usd", [math.nan, math.inf, -math.inf, 0.0, -50.0])
def test_size_must_be_positive_and_finite(broker, usd):
    assert check([buy("JNJ", usd)], portfolio()) == [
        (False, "notional must be a positive, finite number")]


def test_size_below_the_broker_minimum_is_rejected(broker):
    assert check([buy("JNJ", 0.5)], portfolio()) == [
        (False, "notional below broker minimum ($1.00)")]


def test_unrecognised_side_is_rejected(broker):
    trade = agent.ProposedTrade(symbol="AAPL", side="short", notional_usd=100, reasoning="r")
    assert check([trade], portfolio()) == [(False, "unrecognized side 'short'")]


# --- live lock ---------------------------------------------------------------


def test_live_mode_is_locked_without_the_unlock_variable(monkeypatch):
    monkeypatch.delenv(config.LIVE_UNLOCK_ENV, raising=False)
    with pytest.raises(SystemExit):
        agent._trading_client("live")
```

- [ ] **Step 2: Run them**

Run: `venv/bin/python -m pytest -q test_guardrails.py`
Expected: `36 passed`. If any fails, the code disagrees with this plan's reading of it. **Stop and report; do not change `agent.py`.**

- [ ] **Step 3: Run the whole suite**

Run: `venv/bin/python -m pytest -q`
Expected: `90 passed` (53 existing + 1 re-export + 36 guardrail).

- [ ] **Step 4: Commit**

```bash
git add test_guardrails.py
git commit -F - <<'EOF'
Test every guardrail in validate_trades

The guardrails had no tests. These pin current behaviour, one or more
cases per rule, with rejection reasons asserted verbatim: the 15% cap
(including unfilled buys and two buys together), the 5% buffer net of
unfilled buys, same-cycle sell proceeds not spendable, max 5 trades,
whitelist gating buys only, broker tradability, long-only checks net of
unfilled sells, required case fields and exit triggers, finite sizes and
the $1 minimum, and the live-mode lock.

The broker is faked through validate_trades.__globals__, so the tests
keep working unchanged once validate_trades moves out of agent.py.
EOF
```

---

### Task 3: Golden prompt fixture and test

**Files:**
- Create: `tests/golden.py`
- Create: `test_prompt_golden.py`
- Modify: `.gitignore` (append one line)

**Interfaces:**
- Consumes: `agent.fetch_portfolio_state(mode)`, `agent.fetch_market_context(watchlist, portfolio)`, `agent.price_pending_orders(portfolio, prices)`, `agent.store_news(conn, articles, now)`, `agent.fetch_news_history(conn, positions, now, lookup_failed)`, `agent.fetch_candidate_news(conn, candidates, now, lookup_failed)`, `agent.fetch_open_theses(conn)`, `agent.fetch_action_history(conn)`, `agent.build_decision_prompt(portfolio, market, theses, actions, news_history, candidate_news, as_of)`, `agent.SYSTEM_PROMPT`, `markettime.now_utc()`, `config.WATCHLIST`, `config.DEFAULT_DB_PATH`.
- Produces: `tests/golden.py` exposing `FIXTURES`, `DB`, `INPUTS`, `PROMPT`, `SYSTEM_SHA` (Paths), `system_prompt_sha() -> str`, `build_prompt(db_path: Path, inputs: dict) -> str`, `capture() -> None`. Re-run `capture()` deliberately whenever the prompt is **meant** to change (the fundamentals work will).

- [ ] **Step 1: Gitignore the fixtures**

Append to `.gitignore`:

```
tests/fixtures/
```

- [ ] **Step 2: Write the fixture module**

Create `tests/golden.py`:

```python
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
        theses = agent.fetch_open_theses(conn)
        actions = agent.fetch_action_history(conn)
    finally:
        conn.close()
    return agent.build_decision_prompt(
        portfolio, market, theses, actions, news_history, candidate_news, now,
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
    dst.close()

    inputs = {"now": now, "portfolio": portfolio, "market": market}
    INPUTS.write_bytes(pickle.dumps(inputs))
    PROMPT.write_text(build_prompt(DB, inputs))
    SYSTEM_SHA.write_text(system_prompt_sha())
    print(f"captured golden fixture in {FIXTURES} "
          f"({PROMPT.stat().st_size:,} bytes of prompt)")


if __name__ == "__main__":
    capture()
```

- [ ] **Step 3: Write the test**

Create `test_prompt_golden.py`:

```python
"""The decision prompt stays byte-identical through the agent.py module split.

Skips, rather than fails, when the fixture has not been captured: it is
gitignored because it holds a copy of the trading database.
"""

import pickle
import shutil

import pytest

from tests import golden

pytestmark = pytest.mark.skipif(
    not golden.PROMPT.exists(),
    reason="golden fixture not captured (gitignored): run venv/bin/python tests/golden.py",
)


def test_decision_prompt_is_byte_identical(tmp_path):
    db = tmp_path / "golden.db"
    shutil.copy(golden.DB, db)
    inputs = pickle.loads(golden.INPUTS.read_bytes())
    assert golden.build_prompt(db, inputs) == golden.PROMPT.read_text()


def test_system_prompt_is_unchanged():
    assert golden.system_prompt_sha() == golden.SYSTEM_SHA.read_text()
```

- [ ] **Step 4: Run before capturing, to see the skip**

Run: `venv/bin/python -m pytest -q test_prompt_golden.py -rs`
Expected: `2 skipped`, with the reason `golden fixture not captured`.

- [ ] **Step 5: Capture the fixture from live data (read-only)**

Run from the repo root: `venv/bin/python tests/golden.py`
Expected: `captured golden fixture in .../tests/fixtures (NNN,NNN bytes of prompt)`, roughly 100–130 KB. It takes a minute or two (bars for ~1,500 symbols). Then confirm the real DB was not touched: `git status --short` must not list `trading_agent.db` (it is gitignored anyway). Also check `ls -l trading_agent.db`: its modification time must be unchanged (last written by the most recent 16:30 cycle).

- [ ] **Step 6: Run the golden test**

Run: `venv/bin/python -m pytest -q test_prompt_golden.py`
Expected: `2 passed`.

- [ ] **Step 7: Run the whole suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`.

- [ ] **Step 8: Commit** (the fixtures are gitignored, so they are not included)

```bash
git add .gitignore tests/golden.py test_prompt_golden.py
git commit -F - <<'EOF'
Pin the decision prompt with a golden fixture

tests/golden.py captures one read-only snapshot of the account and the
market, plus a copy of the database, and stores the prompt the current
code builds from them. test_prompt_golden.py rebuilds that prompt offline
and requires it byte-identical, system prompt included.

The fixture is gitignored, since it holds a copy of the trading database.
The test skips when it is absent. Re-capture deliberately whenever the
prompt is meant to change.
EOF
```

---

### Task 4: The move script

**Files:**
- Create: `tools/split_agent.py`

**Interfaces:**
- Produces: CLI `venv/bin/python tools/split_agent.py <module> <name> [<name> ...]`. Moves the named top-level definitions from `agent.py` into a new `<module>.py` (refusing if that file already exists, or if a moved definition depends on a name still in `agent.py`), then inserts `from <module> import (...)` into `agent.py` after `import markettime` and after any earlier re-export blocks.

The script was dry-run on a scratch copy of the repo: all nine moves in Tasks 5–13, then pyflakes clean on every new module, and the full suite passing.

- [ ] **Step 1: Write the script**

Create `tools/split_agent.py`:

```python
"""Move named top-level definitions out of agent.py into a new module, verbatim.

One-off tool for the 2026-10 module split (docs/superpowers/specs/
2026-10-04-agent-refactor-design.md). Each definition is copied byte-for-byte,
together with the comments and blank lines directly above it; only imports are
generated. agent.py then imports every moved name back, so `agent.X` keeps
working for dashboard.py, the tests and pickled fixtures.

Usage: python tools/split_agent.py <module> <name> [<name> ...]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent.py"

STDLIB_MODULES = [
    "argparse", "html", "json", "math", "hashlib", "os", "sqlite3",
    "statistics", "subprocess", "traceback",
]
FROM_IMPORTS = {
    "dataclasses": ["dataclass", "field", "replace"],
    "datetime": ["datetime", "timedelta", "timezone"],
    "collections.abc": ["Iterable"],
    "typing": ["Any"],
}
PROJECT_MODULES = ["config", "markettime"]
SPLIT_MODULES = [
    "models", "broker", "news", "screen", "prompt", "decide", "guardrails",
    "theses", "db",
]
DOCSTRINGS = {
    "models": "Data model shared across the pipeline: portfolio, market, theses, trades.",
    "broker": "Alpaca clients, account and pending-order state, and order execution.",
    "news": "Alpaca news: fetching, the news archive, and per-symbol news history.",
    "screen": "Market context and the candidate screen.",
    "prompt": "The system prompt and the per-cycle decision prompt.",
    "decide": "The Claude call and parsing of its response.",
    "guardrails": "Code-enforced guardrails: every proposed trade is validated here.",
    "theses": "Investment cases: open theses, action history, and the thesis lifecycle.",
    "db": "SQLite schema, migrations, and cycle/run logging.",
}


def defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def module_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n for node in tree.body for n in defined_names(node)}


def main(module: str, names: list[str]) -> None:
    if module not in SPLIT_MODULES:
        sys.exit(f"unknown module {module!r}")
    target = ROOT / f"{module}.py"
    if target.exists():
        sys.exit(f"{target.name} already exists")

    src = AGENT.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    # Each definition's chunk runs from the line after the previous top-level
    # statement to its own last line, so leading comments travel with it.
    spans: dict[str, tuple[int, int, ast.stmt]] = {}
    prev_end = 0
    for node in tree.body:
        for name in defined_names(node):
            spans[name] = (prev_end + 1, node.end_lineno, node)
        prev_end = node.end_lineno

    missing = [n for n in names if n not in spans]
    if missing:
        sys.exit(f"not top-level in agent.py: {missing}")
    names = sorted(names, key=lambda n: spans[n][0])  # keep file order

    used: set[str] = set()
    for n in names:
        used |= {x.id for x in ast.walk(spans[n][2]) if isinstance(x, ast.Name)}

    # Where each referenced agent-level name now lives.
    homes = {m: module_defs(ROOT / f"{m}.py") for m in SPLIT_MODULES
             if (ROOT / f"{m}.py").exists()}
    cross: dict[str, list[str]] = {}
    for name in sorted(used - set(names)):
        home = next((m for m, defs in homes.items() if name in defs), None)
        if home is not None:
            cross.setdefault(home, []).append(name)
        elif name in spans:
            sys.exit(f"{name!r} is still in agent.py; move its module first")

    header = [f'"""{DOCSTRINGS[module]}"""\n', "\n", "from __future__ import annotations\n", "\n"]
    std = [m for m in STDLIB_MODULES if m in used]
    frm = [(mod, [n for n in ns if n in used]) for mod, ns in FROM_IMPORTS.items()]
    frm = [(mod, ns) for mod, ns in frm if ns]
    header += [f"import {m}\n" for m in sorted(std)]
    header += [f"from {mod} import {', '.join(ns)}\n" for mod, ns in frm]
    proj = [m for m in PROJECT_MODULES if m in used]
    if proj or cross:
        if std or frm:
            header.append("\n")
        header += [f"import {m}\n" for m in proj]
        for home in SPLIT_MODULES:
            if home in cross:
                header.append(f"from {home} import {', '.join(cross[home])}\n")

    body: list[str] = []
    for n in names:
        start, end, _ = spans[n]
        body += lines[start - 1:end]
    while body and not body[0].strip():
        body.pop(0)
    target.write_text("".join(header) + "\n\n" + "".join(body).rstrip("\n") + "\n")

    # Remove the chunks from agent.py and import the names back.
    drop = {i for n in names for i in range(spans[n][0] - 1, spans[n][1])}
    kept = [line for i, line in enumerate(lines) if i not in drop]
    anchor = kept.index("import markettime\n") + 1
    while kept[anchor].startswith("from ") and " import (" in kept[anchor]:
        while kept[anchor] != ")\n":
            anchor += 1
        anchor += 1
    block = [f"from {module} import (  # re-exported: agent.X must keep working\n"]
    block += [f"    {n},\n" for n in names]
    block.append(")\n")
    kept[anchor:anchor] = block
    AGENT.write_text("".join(kept))
    print(f"moved {len(names)} names to {target.name}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
```

- [ ] **Step 2: Check its refusal paths without touching anything**

Run: `venv/bin/python tools/split_agent.py nope X`
Expected: exits with `unknown module 'nope'`.

Run: `venv/bin/python tools/split_agent.py models NotAName`
Expected: exits with `not top-level in agent.py: ['NotAName']`. Then `git status --short` shows only `tools/split_agent.py` as new, with `agent.py` unmodified.

- [ ] **Step 3: Commit**

```bash
git add tools/split_agent.py
git commit -F - <<'EOF'
Add the script that moves code out of agent.py verbatim

tools/split_agent.py moves named top-level definitions, with the
comments above them, into a new module byte-for-byte, generates only
that module's imports, and imports every moved name back into agent.py
so agent.X keeps working. It refuses to overwrite a module, or to move
code that depends on something still in agent.py.
EOF
```

---

### Task 5: Move the data model → `models.py`

**Files:**
- Create: `models.py` (generated)
- Modify: `agent.py` (definitions removed, re-export block added)

**Interfaces:**
- Produces: `models.Position`, `PendingOrder`, `PortfolioState`, `NewsArticle`, `MarketContext`, `Thesis`, `PositionReview`, `ProposedTrade`, `ValidatedTrade`, `AgentVersion`, all also reachable as `agent.<name>`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py models Position PendingOrder PortfolioState NewsArticle MarketContext Thesis PositionReview ProposedTrade ValidatedTrade AgentVersion`
Expected: `moved 10 names to models.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes models.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. The golden test in particular must pass. If anything fails, `git checkout agent.py && rm models.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? models.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py models.py
git commit -F - <<'EOF'
Move the data model out of agent.py into models.py

Position, PendingOrder, PortfolioState, NewsArticle, MarketContext,
Thesis, PositionReview, ProposedTrade, ValidatedTrade and AgentVersion,
moved verbatim and re-exported from agent. No behaviour change: the
golden prompt, guardrail and existing tests pass unchanged.
EOF
```

---

### Task 6: Move broker access → `broker.py`

**Files:**
- Create: `broker.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5).
- Produces: `broker._trading_client(mode)`, `_data_client()`, `_news_client()`, `fetch_pending_orders(client)`, `price_pending_orders(portfolio, prices)`, `fetch_portfolio_state(mode)`, `_client_order_id(trade, session_date)`, `execute_trades(validated, mode, session_date)`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py broker _trading_client _data_client _news_client fetch_pending_orders price_pending_orders fetch_portfolio_state _client_order_id execute_trades`
Expected: `moved 8 names to broker.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes broker.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. If anything fails, `git checkout agent.py && rm broker.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? broker.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py broker.py
git commit -F - <<'EOF'
Move broker access out of agent.py into broker.py

The Alpaca clients (including the live-mode lock in _trading_client),
account and pending-order state, pending-order pricing, client order
ids and order execution, moved verbatim and re-exported from agent.
No behaviour change: all tests pass unchanged.
EOF
```

---

### Task 7: Move news → `news.py`

**Files:**
- Create: `news.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5).
- Produces: `news._news_symbol(symbol)`, `_fetch_articles(news_client, request)`, `store_news(conn, articles, now)`, `_spread_by_day(rows, limit)`, `_clip(text, limit)`, `_archived_news(...)`, `fetch_candidate_news(conn, candidates, now, lookup_failed)`, `fetch_news_history(conn, positions, now, lookup_failed=...)`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py news _news_symbol _fetch_articles store_news _spread_by_day _clip _archived_news fetch_candidate_news fetch_news_history`
Expected: `moved 8 names to news.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes news.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. If anything fails, `git checkout agent.py && rm news.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? news.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py news.py
git commit -F - <<'EOF'
Move news handling out of agent.py into news.py

Fetching from Alpaca, the news archive, and per-symbol news history for
holdings and candidates, moved verbatim and re-exported from agent. No
behaviour change: all tests pass unchanged.
EOF
```

---

### Task 8: Move the screen → `screen.py`

**Files:**
- Create: `screen.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5); `broker._data_client`, `broker._news_client` (Task 6); `news._fetch_articles`, `news._news_symbol` (Task 7).
- Produces: `screen.fetch_market_context(watchlist, portfolio)`, `_pct_change(now, then)`, `compute_screen_stats(symbols)`, `select_candidates(...)`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py screen fetch_market_context _pct_change compute_screen_stats select_candidates`
Expected: `moved 4 names to screen.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes screen.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. If anything fails, `git checkout agent.py && rm screen.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? screen.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py screen.py
git commit -F - <<'EOF'
Move market context and the screen out of agent.py into screen.py

fetch_market_context, the screen statistics and candidate selection,
moved verbatim and re-exported from agent. No behaviour change: all
tests pass unchanged.
EOF
```

---

### Task 9: Move the prompt → `prompt.py`

**Files:**
- Create: `prompt.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5).
- Produces: `prompt.SYSTEM_PROMPT` (str, byte-identical), `prompt.build_decision_prompt(portfolio, market, theses, actions, news_history, candidate_news, as_of) -> str`. Must move **before** Tasks 10 and 13, whose code reads `SYSTEM_PROMPT`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py prompt SYSTEM_PROMPT build_decision_prompt`
Expected: `moved 2 names to prompt.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes prompt.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. `test_system_prompt_is_unchanged` and `test_prompt_hash_tracks_the_system_prompt` both matter here. If anything fails, `git checkout agent.py && rm prompt.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? prompt.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py prompt.py
git commit -F - <<'EOF'
Move the prompts out of agent.py into prompt.py

SYSTEM_PROMPT and build_decision_prompt, moved verbatim and re-exported
from agent. The golden test confirms the prompt is byte-identical and
the system prompt hash is unchanged.
EOF
```

---

### Task 10: Move the Claude call → `decide.py`

**Files:**
- Create: `decide.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5); `prompt.SYSTEM_PROMPT` (Task 9).
- Produces: `decide._parse_trades_response(...)`, `decide.get_claude_decision(prompt) -> (trades, reviews, raw_response, usage)`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py decide _parse_trades_response get_claude_decision`
Expected: `moved 2 names to decide.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes decide.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. If anything fails, `git checkout agent.py && rm decide.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? decide.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py decide.py
git commit -F - <<'EOF'
Move the Claude call out of agent.py into decide.py

get_claude_decision (streaming, refusal handling) and response parsing,
moved verbatim and re-exported from agent. No behaviour change: all
tests pass unchanged.
EOF
```

---

### Task 11: Move the guardrails → `guardrails.py`

**Files:**
- Create: `guardrails.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5); `broker._trading_client` (Task 6).
- Produces: `guardrails.validate_trades(trades, portfolio, mode) -> list[ValidatedTrade]`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py guardrails validate_trades`
Expected: `moved 1 names to guardrails.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes guardrails.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. All 36 in `test_guardrails.py` must pass. Their fake broker is now injected into `guardrails`' namespace through `validate_trades.__globals__`. If anything fails, `git checkout agent.py && rm guardrails.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? guardrails.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py guardrails.py
git commit -F - <<'EOF'
Move the guardrails out of agent.py into guardrails.py

validate_trades now has a file of its own, moved verbatim and
re-exported from agent. The guardrail tests pass unchanged against it.
EOF
```

---

### Task 12: Move the thesis lifecycle → `theses.py`

**Files:**
- Create: `theses.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5).
- Produces: `theses.fetch_open_theses(conn)`, `fetch_action_history(conn)`, `CASE_FIELDS`, `_full_case(obj)`, `apply_thesis_lifecycle(conn, validated, reviews, portfolio, now, cycle_id)`, `ORPHAN_EXIT_TRIGGER`, `_close_orphaned_theses(...)`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py theses fetch_open_theses fetch_action_history CASE_FIELDS _full_case apply_thesis_lifecycle ORPHAN_EXIT_TRIGGER _close_orphaned_theses`
Expected: `moved 7 names to theses.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes theses.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. If anything fails, `git checkout agent.py && rm theses.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? theses.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py theses.py
git commit -F - <<'EOF'
Move the thesis lifecycle out of agent.py into theses.py

Open theses, the per-symbol action history, the lifecycle (open,
refresh, adopt, re-anchor, close) and orphan cleanup, moved verbatim
and re-exported from agent. No behaviour change: all tests pass
unchanged.
EOF
```

---

### Task 13: Move the database layer → `db.py`

**Files:**
- Create: `db.py` (generated)
- Modify: `agent.py`

**Interfaces:**
- Consumes: `models` (Task 5); `prompt.SYSTEM_PROMPT` (Task 9).
- Produces: `db.PROMPT_LOG_DIR`, `init_db(db_path)`, `_migrate_cycle_usage(conn)`, `_migrate_cycle_version(conn)`, `_migrate_thesis_revisions(conn)`, `log_run(conn, status, error_text, cycles_completed)`, `log_decision_cycle(...)`, `write_prompt_log(cycle_id, prompt, mode, timestamp, version=None, log_dir=PROMPT_LOG_DIR)`.

- [ ] **Step 1: Move**

Run: `venv/bin/python tools/split_agent.py db PROMPT_LOG_DIR init_db _migrate_cycle_usage _migrate_cycle_version _migrate_thesis_revisions log_run log_decision_cycle write_prompt_log`
Expected: `moved 8 names to db.py`

- [ ] **Step 2: Check for undefined names**

Run: `venv/bin/python -m pyflakes db.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: `92 passed`. `test_prompt_log_header_names_the_version` exercises `write_prompt_log`'s use of `SYSTEM_PROMPT`. If anything fails, `git checkout agent.py && rm db.py` and stop.

- [ ] **Step 4: Confirm only the expected files changed**

Run: `git status --short`
Expected: ` M agent.py` and `?? db.py`, and nothing else.

- [ ] **Step 5: Commit**

```bash
git add agent.py db.py
git commit -F - <<'EOF'
Move the database layer out of agent.py into db.py

Schema, migrations, run and cycle logging, and the prompt log, moved
verbatim and re-exported from agent. agent.py now holds only the
pipeline, current_agent_version and the CLI.
EOF
```

---

### Task 14: Tidy agent.py, update the docs, final checks, merge

**Files:**
- Modify: `agent.py` (import lines only)
- Modify: `CLAUDE.md` (module table, dashboard import note)

- [ ] **Step 1: Remove stdlib imports agent.py no longer uses**

Run: `venv/bin/python -m pyflakes agent.py | grep -v "^agent.py:[0-9]*:[0-9]*: '\(models\|broker\|news\|screen\|prompt\|decide\|guardrails\|theses\|db\)\."`
Expected: only lines for the leftover stdlib and typing imports. On the dry run these were `html`, `json`, `math`, `sqlite3`, `statistics`, `dataclasses.dataclass`, `dataclasses.field`, `dataclasses.replace`, `datetime.timedelta`, `datetime.timezone`, `collections.abc.Iterable` and `typing.Any`. Delete exactly the imports it names from the top of `agent.py`, then re-run the command. Expected: no output. (The re-export lines it filters out are intentional.)

- [ ] **Step 2: Run the full suite and the entry-point checks**

```bash
venv/bin/python -m pytest -q
(cd / && /Users/aydenwong/Projects/Claude-Autonomous-Trading-Agent/venv/bin/python /Users/aydenwong/Projects/Claude-Autonomous-Trading-Agent/agent.py --help | head -1)
venv/bin/python -c "import os, dashboard; print('dashboard ok', bool(os.environ.get('ALPACA_API_KEY')))"
venv/bin/python -m pyflakes models.py broker.py news.py screen.py prompt.py decide.py guardrails.py theses.py db.py
wc -l agent.py
```

Expected:
- `92 passed`
- `usage: agent.py [-h] [--mode {paper,live}] [--db-path DB_PATH]`, from `/`, as launchd runs it
- `dashboard ok True` (`.env` loaded through `import agent`)
- no pyflakes output
- `agent.py` around 250 lines

- [ ] **Step 3: Update CLAUDE.md**

In the `## Modules` table, replace the `agent.py` row with these rows:

```markdown
| `agent.py` | Entry point and pipeline: `run_decision_cycle` (gather → decide → validate → execute → log), `current_agent_version`, the CLI, and `load_dotenv()`. Re-exports every name the pre-split `agent.py` defined, so `agent.X` still works. |
| `models.py` | Dataclasses shared across the pipeline (portfolio, market, theses, trades, `AgentVersion`). |
| `broker.py` | Alpaca clients (the live-mode lock lives in `_trading_client`), account and pending-order state, order execution and `client_order_id`. |
| `screen.py` | `fetch_market_context` and the candidate screen. |
| `news.py` | News fetching, the archive, and per-symbol news history. |
| `prompt.py` | `SYSTEM_PROMPT` and `build_decision_prompt`. |
| `decide.py` | The Claude call and response parsing. |
| `guardrails.py` | `validate_trades`: every code-enforced guardrail. |
| `theses.py` | Open theses, action history (`fetch_action_history`), the thesis lifecycle, orphan cleanup. |
| `db.py` | Schema, migrations, run/cycle logging, prompt log. |
| `tools/split_agent.py` | One-off script that performed the 2026-10 split; kept for the record. |
```

Then make three more edits:
- The old `agent.py` row said "The only module that writes anything." That is no longer true of one file, so add this sentence directly under the table: `Writes happen in broker.py (orders) and in db.py, theses.py and news.py (rows); agent.py orchestrates them.`
- Update the note `` `dashboard.py` imports `_trading_client` / `_data_client` from `agent.py` `` to say they live in `broker.py` and are re-exported by `agent.py`, so that import still works.
- Add to the test description: `test_guardrails.py` (every guardrail), `test_prompt_golden.py` (prompt byte-identical against a gitignored fixture; re-capture with `venv/bin/python tests/golden.py` when the prompt is meant to change), `test_reexports.py`.

- [ ] **Step 4: Commit**

```bash
git add agent.py CLAUDE.md
git commit -F - <<'EOF'
Finish the agent.py split: drop unused imports, document the layout

- agent.py: remove the stdlib imports that moved with the code it now
  re-exports; ~250 lines remain (pipeline, version, CLI).
- CLAUDE.md: module table for the nine new modules, the dashboard
  import note, and the new tests, including how to re-capture the
  golden prompt when it is meant to change.
EOF
```

- [ ] **Step 5: Merge to main before the next weekday 16:30 ET run, and push**

```bash
git checkout main
git merge --ff-only refactor/split-agent
venv/bin/python -m pytest -q
git status --short          # must show nothing except possibly dashboard.html
git push origin main
git branch -d refactor/split-agent
```

Expected: fast-forward; `92 passed`; clean tree, so the next cycle records `code_dirty = 0`.

---

### Task 15: Verify the first real cycle on the new layout

Runs after the next scheduled 16:30 ET cycle. Nothing is executed by hand.

- [ ] **Step 1: Check the run and the version it recorded**

```bash
tail -12 logs/agent.log
sqlite3 trading_agent.db "select id, outcome, substr(error_text,1,200) from run_log order by id desc limit 1;"
sqlite3 trading_agent.db "select id, code_commit, code_dirty, system_prompt_sha, model, effort from decision_cycles order by id desc limit 2;"
```

Expected: `=== done (agent=0 dashboard=0) ===`; `success`; the newest cycle's `code_commit` equals `git rev-parse --short=12 HEAD`, `code_dirty` is `0`, and `system_prompt_sha` **equals the previous cycle's**, which is independent proof the prompt did not change.

- [ ] **Step 2: If the run failed**

The traceback is in `run_log.error_text` and `logs/agent.log`. Revert the move commits on `main` (newest first, which is the order `git log` lists them), then push, so the next cycle runs the old layout. The tests from Tasks 1–3 stay. Then diagnose on a branch:

```bash
git revert --no-edit $(git log --format=%h --grep='^Move .* out of agent.py' --grep='^Finish the agent.py split' main)
venv/bin/python -m pytest -q
git push origin main
```
