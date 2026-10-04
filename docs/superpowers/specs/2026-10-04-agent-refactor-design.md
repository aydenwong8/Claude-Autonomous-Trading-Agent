# Split agent.py into modules — design

Date: 2026-10-04
Status: approved in conversation; awaiting review of this written spec

## Why

`agent.py` is 2,518 lines doing about a dozen jobs. The next piece of work, a
Finnhub + SEC EDGAR fundamentals feed, needs to plug in somewhere, and the
earnings prints it is for begin around 2026-10-14. Splitting the file first
means fundamentals arrives as its own module instead of another ~300 lines in
the big one, and lets the guardrails get the tests they have never had.

Background: cycles 48–52 (Opus 5.5) proposed zero trades. The agent's own
reasoning attributes this to missing information: no earnings dates, reported
results or valuation figures. That is the fundamentals project. This spec covers
only the refactor that precedes it.

## Goal and non-goals

**Goal:** the same behaviour, reorganised. Given identical inputs, the refactored
code builds a byte-identical prompt, validates trades identically, and writes
the same rows.

**Non-goals — explicitly out of scope:**
- Any change to logic, prompt wording, thresholds, schema, or output.
- Renaming functions or tidying internals (dead code, duplicated helpers).
  These are collected in a follow-up list, not done here.
- The HON/BDX spin-off artefact in the screen, the price-only screen, and the
  fundamentals feed. All three change what the agent sees, so they are separate
  work.

## Layout (approach A: flat modules, `agent.py` re-exports)

New files sit at the repo root beside `markettime.py`, `metrics.py` and
`report.py`. Each function moves **verbatim**, with only its imports changing.

| Module | Contents (current names) |
|---|---|
| `models.py` | `Position`, `PendingOrder`, `PortfolioState`, `NewsArticle`, `MarketContext`, `Thesis`, `PositionReview`, `ProposedTrade`, `ValidatedTrade`, `AgentVersion` |
| `broker.py` | `_trading_client`, `_data_client`, `_news_client`, `fetch_pending_orders`, `price_pending_orders`, `fetch_portfolio_state`, `_client_order_id`, `execute_trades` |
| `news.py` | `_news_symbol`, `_fetch_articles` and the other Alpaca news fetchers, `store_news`, `_spread_by_day`, `_clip`, `_archived_news`, `fetch_candidate_news`, `fetch_news_history` |
| `screen.py` | `fetch_market_context`, `_pct_change`, `compute_screen_stats`, `select_candidates` |
| `prompt.py` | `SYSTEM_PROMPT`, `build_decision_prompt` |
| `decide.py` | `_parse_trades_response`, `get_claude_decision` |
| `guardrails.py` | `validate_trades` |
| `theses.py` | `fetch_open_theses`, `fetch_action_history`, `CASE_FIELDS`, `_full_case`, `apply_thesis_lifecycle`, `ORPHAN_EXIT_TRIGGER`, `_close_orphaned_theses` |
| `db.py` | `PROMPT_LOG_DIR`, `init_db`, `_migrate_cycle_usage`, `_migrate_cycle_version`, `_migrate_thesis_revisions`, `log_run`, `log_decision_cycle`, `write_prompt_log` |
| `agent.py` | `load_dotenv()`, `current_agent_version`, `run_decision_cycle`, `_print_cycle_summary`, `main`, plus re-exports |

Where a helper's home is ambiguous, it goes with its only caller. The
implementation plan pins every name, including helpers not listed here.

**Dependency direction (no cycles):**
`models` ← `broker`, `news`, `db`, `theses` ← `screen` (uses `news`) ←
`prompt`, `guardrails`, `decide` ← `agent`. No module imports `agent`.

**Kept in `agent.py` on purpose:**
- `load_dotenv()` at import. Both entry points (`agent.py` itself, and
  `dashboard.py` via `from agent import ...`) still trigger it before any
  credential is read. Credentials are read at call time, so the order is
  unchanged.
- `current_agent_version`. `test_version.py` does
  `monkeypatch.setattr(agent, "SYSTEM_PROMPT", ...)` and expects the version hash
  to change. A patch on a re-exported name does not reach a copy held in
  `prompt.py`, so the function reads `SYSTEM_PROMPT` from `agent`'s own
  namespace. Its dataclass `AgentVersion` moves to `models.py`, because `db.py`
  needs it.

**Re-exports:** `agent.py` imports back every name that outside code uses today,
so these files stay **unmodified**: `dashboard.py` (`_data_client`,
`_trading_client`), `test_memory.py`, `test_version.py`, `test_metrics.py`,
`run_daily.sh`, and the launchd plist. The re-export list is every top-level
name `agent.py` defines today, so nothing reachable as `agent.X` disappears.

## Proof of no behaviour change

All new tests are written and committed **against the current, unsplit code
first**, and must pass there before any code moves. Code then moves one module
per commit, with the full suite run after each.

1. **Golden prompt test** (`test_prompt_golden.py`).
   - Capture once, before the refactor: a read-only broker/market snapshot
     (`PortfolioState`, `MarketContext` including candidates and news) is pickled
     to `tests/fixtures/`, with a copy of `trading_agent.db`. The capture never
     writes to the real DB, and every call is either a read or goes to the copy.
   - The test rebuilds the prompt from the fixture and asserts it is
     byte-identical to the stored output, system prompt included. That also pins
     `system_prompt_sha`.
   - `tests/fixtures/` is **gitignored**, like `trading_agent.db`: the DB copy
     carries the full decision history and account state. The test **skips**
     (does not fail) when the fixture is absent, so a fresh clone's suite still
     runs. The fixture holds no credentials.
   - The clock is pinned, so date-relative text is stable.
2. **Guardrail tests** (`test_guardrails.py`), directly against `validate_trades`,
   at least one case per rule:
   - 15% cap measured on holdings plus pending buys; two buys cannot jointly
     breach it.
   - 5% cash buffer measured on cash net of pending buys; same-cycle sell
     proceeds are not spendable.
   - Max 5 trades per cycle.
   - The whitelist gates buys only; a held non-whitelisted name remains sellable.
   - Ticker must be tradable at the broker.
   - Long-only: a sell is capped at the position net of pending sells; pending
     buys are not sellable; an unvaluable pending sell blocks further sells of
     that symbol.
   - A buy needs thesis, target and invalidation; a sell needs a valid
     `exit_trigger`.
   - NaN/Infinity sizes are rejected; the $1 minimum applies.
   - Cumulative: trade N is checked against the state trades 1..N−1 leave.
   - Live lock: `_trading_client("live")` raises without `TRADING_AGENT_ALLOW_LIVE`.
3. **Existing 53 tests pass unchanged**, through the re-exports.
4. **First real cycle on the new layout**: the 16:30 ET run after merge succeeds
   in `run_log`, records the new `code_commit` with `code_dirty = 0`, and writes
   its prompt log. The scheduled run must be on a clean, committed tree.

## Error handling

Unchanged by definition: every `try`/`except`, warning path and `run_log`
failure write moves with its function. The only new failure mode is an import
error, and the first `pytest` run after each move catches it before anything is
scheduled.

## Rollout

- Work on a branch and merge to `main` before the next scheduled cycle. The
  launchd job runs whatever is checked out in the project directory, so an
  unfinished split must never be checked out at 16:30 ET on a weekday.
- Afterwards, update CLAUDE.md's module table and the "`dashboard.py` imports
  `_trading_client` / `_data_client` from `agent.py`" note.

## Follow-ups (recorded, not done here)

- Fundamentals feed (Finnhub + EDGAR) as `fundamentals.py`, with hooks in
  `prompt.py` and `agent.py`. Next spec.
- Spin-off-adjusted screen data (HON, BDX).
- Internal clean-ups noticed during the move.
