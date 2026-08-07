# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project status

Implemented and running. The full five-stage pipeline exists and executes
automatically on weekdays against an Alpaca **paper** account (Phase 1 of the
rollout in README.md). As of the latest cycles the DB holds ~10 decision cycles
and ~10 open theses. Nothing runs against live money yet.

README.md is the original design doc and is now partly stale — where it and the
code disagree, **the code wins**. Known divergences are flagged below.

## What this project is

An experiment testing whether Claude can act as an autonomous trading
decision-maker, managing a portfolio via Alpaca and benchmarked against SPY.
Zero human override: every trade is proposed by Claude and then accepted or
rejected by code-enforced guardrails, never by a human in the loop.

## Modules

| File | Role |
|---|---|
| `agent.py` | The whole pipeline: gather → decide → validate → execute → log, plus the SQLite schema, the thesis lifecycle, and the system prompt. The only module that writes anything. |
| `config.py` | Guardrail thresholds, `CLAUDE_MODEL` / `CLAUDE_MAX_TOKENS`, `VALID_EXIT_TRIGGERS`, and `WATCHLIST` (S&P 500 ∪ Nasdaq-100, snapshotted 2026-07-21 — index membership drifts, refresh occasionally). |
| `markettime.py` | Store UTC, display Eastern. Every display boundary — console, dashboard, report, *and the prompt Claude reads* — converts through here. Do not format timestamps ad hoc elsewhere. |
| `dashboard.py` | Reads Alpaca + the DB, writes a self-contained `dashboard.html` (agent equity curve vs. buy-and-hold SPY, holdings, risk stats, decision log). No server, no external requests at view time. |
| `report.py` | Read-only, DB-only per-position report: thesis, recent verdicts, every trade including rejected ones. Safe to run mid-cycle — makes no broker calls. |
| `run_daily.sh` | Scheduled entrypoint. Pins `TZ`, skips market holidays via Alpaca's calendar, runs `agent.py` then `dashboard.py`. |

`dashboard.py` imports `_trading_client` / `_data_client` from `agent.py`, so
changing those signatures breaks it.

## Scheduling (the real setup)

`~/Library/LaunchAgents/com.aydenwong.tradingagent.plist` runs `run_daily.sh`
**weekdays at 16:30 local**, and the machine is `America/New_York`, so it
tracks the 16:00 ET close through DST without adjustment. stdout/stderr go to
`logs/agent.log`; `RunAtLoad` is false.

**Discrepancy:** README.md still says "weekly decision cycle (avoids PDT rules
on accounts < $25k)". The implementation runs **daily** post-close. The PDT
rationale is not enforced anywhere in code — the mitigation is now behavioral
(the system prompt tells Claude it is a swing investor and that most days the
correct output is zero trades), not structural. If the account ever goes live
under $25k, this needs a real look.

launchd gives a bare environment with `cwd=/`, which is why `run_daily.sh` uses
absolute paths, `cd`s into the project (so `load_dotenv()` finds `./.env`), and
exports `TZ` explicitly. Anything scheduled must keep those properties.

If the Mac is asleep at 16:30, launchd fires the job on the next wake instead —
and Wi-Fi is not up yet, so every host fails DNS and the cycle dies in seconds.
That cost the 2026-08-06 cycle. `run_daily.sh` now waits up to 15 minutes for
the broker/data/Anthropic hosts to accept TLS before doing anything, and writes
a `failed` `run_log` row if they never do, so a stalled day is visible in the DB
rather than looking like a no-trade day. **The cycle is never retried as a
whole** — `execute_trades` runs before `log_decision_cycle`, so a rerun after a
mid-cycle failure could place the same orders twice. Recovery belongs in
pre-flight, not in a retry loop.

## Thesis lifecycle and exit triggers (not in README)

This is the core behavioral design and has no README coverage.

- **Buys must open a written case**: `thesis` (why we own it), `target` (what
  playing out looks like), `invalidation` (what would prove it wrong).
  Validation rejects a buy missing any of the three.
- The stored thesis is injected back into every later cycle's prompt with days
  held and unrealised P&L, so the model reasons against its own prior case
  instead of re-deriving an opinion daily.
- **Every open position gets a review verdict every cycle** (`hold` / `trim` /
  `exit`), even on a zero-trade day. This is what makes doing nothing an active
  decision. A `trim`/`exit` verdict without a matching sell is ignored.
- **Sells must name an exit trigger** from `config.VALID_EXIT_TRIGGERS`:
  `thesis_broken`, `target_hit`, `bad_news`, `no_longer_fits`, `risk`. This
  keeps exits auditable and stops positions being closed on price drift alone.
- **Adoption path**: a position with no stored thesis (opened before tracking
  existed, or outside the agent) can be adopted by a review that supplies the
  full case, instead of having to be sold and rebought.
- A sell of ≥99% of position value closes the thesis (market fills rarely land
  exact); a partial sell leaves it open. A buy into an existing open thesis
  refreshes it rather than creating a second one — a unique partial index
  enforces one open thesis per symbol, and the lifecycle code's `ON CONFLICT`
  clauses depend on that index existing.

## Guardrails (code-enforced invariants, not advisory)

Enforced in `agent.py:validate_trades`, thresholds in `config.py`. A trade that
fails any of these must be **rejected and logged, never silently adjusted**.

| Rule | Value |
|---|---|
| Max position size | 15% of portfolio per ticker (post-trade) |
| Universe | Long-only, whitelisted US equities/ETFs (`config.WATCHLIST`) |
| Leverage / margin / options | Not permitted (buys funded from available cash only) |
| Minimum cash buffer | 5% of equity |
| Max trades per decision cycle | 5 |
| Ticker validation | Every symbol verified tradable against the broker asset list before ordering |

Plus two structural rules with the same standing: buys require
thesis/target/invalidation, sells require a valid `exit_trigger`.

Validation runs cumulatively across the proposed list (running cash and running
position values), so trade N is checked against the state trades 1..N-1 would
leave behind — not against the opening snapshot.

## Database (`trading_agent.db`, gitignored)

Created idempotently by `agent.py:init_db`.

- `decision_cycles` — one row per successful cycle: `timestamp` (UTC ISO),
  `mode`, full `prompt`, raw `claude_response`, `portfolio_cash`,
  `portfolio_equity`.
- `trades` — every *proposed* trade, valid or not: `cycle_id`, `symbol`, `side`,
  `notional_usd`, `reasoning`, `is_valid`, `rejection_reason`, `order_id`,
  `order_status`. Rejections are kept deliberately; they are decisions.
- `theses` — `symbol`, `opened_at`, `thesis`, `target`, `invalidation`,
  `status` ('open'/'closed'), `closed_at`, `exit_trigger`. Unique partial index
  `theses_one_open_per_symbol` on `(symbol) WHERE status='open'`.
- `position_reviews` — `cycle_id`, `symbol`, `verdict`, `note`.
- `run_log` — one row per *invocation*, success or failure, with `error_text`
  and `cycles_completed`. Written on the failure path before the exception
  propagates: without it, a scheduled run that died is indistinguishable from a
  no-trade day.

Timestamps are timezone-aware UTC ISO strings so rows stay lexicographically
sortable and unambiguous across DST. Daily bucketing must use
`markettime.fmt_et_date`, not the UTC date — a post-close snapshot is 20:00+ UTC.

Full prompts are also mirrored to `logs/prompts/{cycle_id}.txt` (plain text, no
ANSI) for grepping; the DB copy is authoritative.

## Running things

```bash
python agent.py     [--mode paper|live] [--db-path trading_agent.db]
python dashboard.py [--mode paper|live] [--db-path trading_agent.db] [--out dashboard.html]
python report.py    [SYMBOL ...] [--closed] [--all] [--cycles N] [--db-path trading_agent.db]
./run_daily.sh                  # what launchd invokes; hardcodes MODE=paper
```

`--mode` defaults to `paper` everywhere. Paper vs. live is a config flag, not a
separate code path — keep it that way. Use `venv/bin/python`; `run_daily.sh`
does.

Secrets come from `.env` (see `.env.example`): `ANTHROPIC_API_KEY`,
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`. Note `ALPACA_PAPER` is
present but **not read by any code** — the mode flag decides.

`agent.py` and `dashboard.py` hit the broker and the Anthropic API and write
rows; only `report.py` is safe to run casually.

## Other README divergences worth knowing

- Tech stack lists Pandas/NumPy, yfinance, Alpha Vantage and a separate news
  API. None are used. Dependencies are exactly `alpaca-py`, `anthropic`,
  `python-dotenv`; prices, SPY closes, and news all come from Alpaca.
- Scheduling is launchd, not cron or GitHub Actions.
- Phase 3 ("performance dashboard vs. SPY") is already built — `dashboard.py`.
- Universe is S&P 500 ∪ Nasdaq-100, not "S&P 1500".
