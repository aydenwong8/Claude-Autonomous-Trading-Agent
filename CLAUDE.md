# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project status

Implemented and running. The full five-stage pipeline exists and executes
automatically on weekdays against an Alpaca **paper** account (Phase 1 of the
rollout in README.md). Nothing runs against live money, and Phase 2 (live) is
deferred with no date — there is no plan to go live soon. For current counts
(cycles, theses, trades), query `trading_agent.db` rather than trusting any
figure written here.

README.md is the design doc — where it and the code disagree, **the code wins**.

## What this project is

An experiment testing whether Claude can act as an autonomous trading
decision-maker, managing a portfolio via Alpaca and benchmarked against SPY.
Zero human override: every trade is proposed by Claude and then accepted or
rejected by code-enforced guardrails, never by a human in the loop.

**The objective is the best risk-adjusted return against the S&P 500 (SPY)** —
Sharpe, alpha, and drawdown, not raw return. Judge a change by whether it
improves that, measured per agent version (`decision_cycles.code_commit`).
The operator's stance is to let Claude mostly do its own thing: change the
inputs and the plumbing, not its individual decisions.

## Modules

| File | Role |
|---|---|
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
| `fundamentals.py` | Finnhub + SEC EDGAR facts per name: valuation against its own 5-year history, earnings dates with consensus, the last 4 quarters' EPS against estimate, the latest reported quarter, material filings. Never raises; failures become per-name `lookup_failed`. Cache in `cache/edgar/` (gitignored, pruned after 30 days). |
| `config.py` | Guardrail thresholds, `CLAUDE_MODEL` / `CLAUDE_MAX_TOKENS`, `VALID_EXIT_TRIGGERS`, and `WATCHLIST` (S&P 1500 ∪ Nasdaq-100 plus SPY/QQQ, ~1,500 symbols, snapshotted 2026-08-05 — index membership drifts, refresh occasionally). |
| `markettime.py` | Store UTC, display Eastern. Every display boundary — console, dashboard, report, *and the prompt Claude reads* — converts through here. Do not format timestamps ad hoc elsewhere. |
| `dashboard.py` | Reads Alpaca + the DB, writes a self-contained `dashboard.html` (agent equity curve vs. buy-and-hold SPY, holdings, risk stats, decision log). No server, no external requests at view time. |
| `metrics.py` | Pure functions over the DB for the README's evaluation metrics the equity curve alone can't give: annualized return, turnover (SEC N-1A `min(buys, sells)` plus a two-way variant), rejection stats, decision consistency. Imported by `dashboard.py`; covered by `test_metrics.py`. |
| `report.py` | Read-only, DB-only per-position report: thesis, recent verdicts, every trade including rejected ones. Safe to run mid-cycle — makes no broker calls. |
| `run_daily.sh` | Scheduled entrypoint. Pins `TZ`, skips market holidays via Alpaca's calendar, runs `agent.py` then `dashboard.py`. |

Writes happen in `broker.py` (orders) and in `db.py`, `theses.py` and `news.py`
(rows); `agent.py` orchestrates them.

`dashboard.py` imports `_trading_client` / `_data_client` from `agent`. They
live in `broker.py` and are re-exported by `agent.py`, so that import still
works, but changing their signatures breaks the dashboard. Importing `agent`
is also what runs `load_dotenv()` for the dashboard.

Tests (`venv/bin/python -m pytest -q`): `test_guardrails.py` covers every rule
in `validate_trades`; `test_prompt_golden.py` requires the decision prompt to
stay byte-identical against a gitignored fixture in `tests/fixtures/` (it skips
when the fixture is absent). **Re-capture it with `venv/bin/python tests/golden.py`
whenever the prompt is meant to change**, e.g. new data in the prompt.
`test_fundamentals.py` runs on recorded API responses in `tests/data/fundamentals/`
(re-record with `record.py` there); `test_prompt_fundamentals.py` covers the wiring.
`test_screen.py` covers the chunked, retried bar fetch behind the screen.
`test_reexports.py` guards the `agent.X` names; `test_memory.py`,
`test_version.py` and `test_metrics.py` cover memory, versioning and metrics.

## Scheduling (the real setup)

`~/Library/LaunchAgents/com.aydenwong.tradingagent.plist` runs `run_daily.sh`
**weekdays at 16:30 local**, and the machine is `America/New_York`, so it
tracks the 16:00 ET close through DST without adjustment. stdout/stderr go to
`logs/agent.log`; `RunAtLoad` is false.

**PDT is not enforced in code.** The cycle runs daily, and the mitigation for
pattern-day-trader rules on accounts under $25k is behavioral
(the system prompt tells Claude it is a swing investor, not a daily trader),
not structural. The prompt no longer says most days should be zero trades —
that line was removed 2026-10-08 so the agent sets its own trading frequency. If the account ever goes live
under $25k, this needs a real look.

launchd gives a bare environment with `cwd=/`, which is why `run_daily.sh` uses
absolute paths, `cd`s into the project (so `load_dotenv()` finds `./.env`), and
exports `TZ` explicitly. Anything scheduled must keep those properties.

If the Mac is asleep at 16:30, launchd fires the job on the next wake instead —
and Wi-Fi is not up yet, so every host fails DNS and the cycle dies in seconds.
That cost the 2026-08-06 cycle. `run_daily.sh` now waits up to 15 minutes for
the broker/data/Anthropic hosts to accept TLS before doing anything, and writes
a `failed` `run_log` row if they never do, so a stalled day is visible in the DB
rather than looking like a no-trade day. **The cycle is still not retried as a
whole** — `execute_trades` runs before `log_decision_cycle`, so a rerun after a
mid-cycle failure re-enters the pipeline with orders already at the broker.
Recovery belongs in pre-flight, not in a retry loop. What a rerun can no longer
do is double-fill: every order carries a deterministic `client_order_id`
(`agt-{ET date}-{symbol}-{side}-{cents}`, `broker.py:_client_order_id`), and the
broker rejects the duplicate. The rejection is recorded as
`duplicate_suppressed:` in `order_status`, distinct from a real
`submit_failed:` — one means the fill already exists, the other that it never
happened.

## Thesis lifecycle and exit triggers

This is the core behavioral design. README.md summarises it; the detail is here.

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
- **Re-anchoring**: a review on a position that *already* has a case may also
  supply all three fields, which replaces the stored wording. Cases go stale
  without going wrong — most often the price marker in `target` is passed while
  the operating condition behind it still holds — and before this existed the
  only way to change the wording was to sell and rebuy. The cost of not having
  it was concrete: MSFT was trimmed six times across five weeks against a
  "$420+" target it had already cleared by 19%, and the cycle-28 attempt to
  rewrite it was silently dropped, so the model reported re-anchoring a thesis
  that never changed. **All three fields are required together** — a partial
  case is counted and reported, never applied field-by-field. Nothing about a
  review is applied for a symbol not currently held.
- Every version of a case is kept in `thesis_revisions`; `theses` holds only the
  current wording, plus `revision` and `updated_at`. `revision > 1` is surfaced
  back into the prompt as `case_revision` / `last_reanchored_at` so the model
  can see when it is tracking real change versus ratcheting a target behind the
  price. `report.py` prints superseded versions under the current one.
- A sell of ≥99% of position value closes the thesis (market fills rarely land
  exact); a partial sell leaves it open. A buy into an existing open thesis
  refreshes it rather than creating a second one — a unique partial index
  enforces one open thesis per symbol.
- **Orphan cleanup**: a case is only ever shown to the model attached to a held
  position, so a case whose position is gone is invisible — it gets no review,
  and no sell can close it because there is nothing to sell. Left alone it stays
  open forever and blocks the symbol's next buy from opening a fresh case.
  `_close_orphaned_theses` closes these with `exit_trigger = 'position_gone'`
  (written only by code, deliberately **not** in `config.VALID_EXIT_TRIGGERS` —
  it records an absence, not a decision) and the count prints as
  `ORPHANED CASES CLOSED`. Three things count as explaining an absent position
  and are not orphans: an unfilled order still at the broker, a trade placed in
  this same cycle (the snapshot predates it), and — the important one — an
  empty `positions` dict, which is far likelier to be a broker glitch than a
  liquidated account, and closing every case at once on that reading is
  unrecoverable.
- `apply_thesis_lifecycle` now runs **after** `log_decision_cycle` (it takes the
  `cycle_id`, so revisions name the cycle that made them). The audit record of
  what was decided is written before any state derived from it.

## What the model can see of its own past

Each cycle is a **stateless single-turn call** — `messages=[{"role": "user", ...}]`,
no conversation history. Everything the model knows about its own past has to be
put in the prompt deliberately. Three things are, plus one fact feed:

- **The case** (`fetch_open_theses`) — what it *believed*, injected per position.
- **The record** (`fetch_action_history`) — what it *did*, injected per position
  as `your_track_record`: every prior trade in that name with the reasoning
  given at the time, a one-line summary counting all of them, and a tally of
  review verdicts returned. Capped at `config.ACTION_HISTORY_LIMIT` (5) actions
  per symbol; the summary still counts them all, so truncation loses detail but
  never the fact that earlier actions happened. Also carries `last_closed_case`
  (the case it held the name on, when it closed, and the exit trigger), and is
  attached to **candidates** the agent has traded before, not only to holdings —
  once exits started happening, a name sold last week could otherwise come back
  through the screen looking like a first encounter.
- **The news** (`fetch_news_history`) — what it *was told*. Every article
  fetched is archived in `news_articles` / `news_symbols` (idempotent on
  Alpaca's article id), and each holding's `position_news` is read back from the
  archive over `config.NEWS_HISTORY_DAYS` (30), split at the previous decision
  cycle: `published_since_your_last_cycle` (with summaries — genuinely new) and
  `earlier` (dated headlines, spread across distinct days so a heavily covered
  name doesn't fill every slot from one afternoon). Before this the model got
  three undated headlines per holding, rebuilt from scratch daily, so it could
  not tell a story it had already reacted to from one that broke today — the
  same spent-fact failure as re-deciding, arriving through the news instead of
  the record. Each holding is fetched over the whole window every cycle, which
  makes the archive self-healing after a missed day. A failed lookup is flagged
  `lookup_failed` rather than shown as an empty (i.e. quiet) list. Candidates
  get a lighter version (`fetch_candidate_news`): their own lookup, archived the
  same way, but only the newest `config.NEWS_PER_CANDIDATE` (3) dated headlines
  shown as `recent_news` — without it the only current information behind a
  case for a name not held was the price table. Measured with `count_tokens` on
  2026-09-22 against cycle 44: +4.1k tokens for holdings' news, +4.8k for
  candidates', +0.5k for dating the market sample, +0.5k of system prompt —
  ~10k in all, about $0.05 a cycle. Every screen row also carries
  `steepest_5d_fall_since_high_pct` / `_window` (worst 5-session drop after the
  52-week high), and candidates at least 25% below their high get
  `news_around_steepest_fall` (`fetch_drawdown_news`): up to 5 dated headlines
  from that window through the day after, fetched and archived each cycle.
  Added 2026-10-14 because the agent passed on BSX, INTU, FICO and CHTR saying
  it "was not given the cause": the falls were months old, so the newest 30
  days of headlines could not say. On that day's book it reached 12 of 31
  candidates, ~1.6k tokens, plus ~0.75k for the screen fields.
  Share-class tickers are queried by base symbol
  (`_news_symbol`: `BRK.B` → `BRK`) because the feed tags `BRK` / `BRK/A` /
  `BRK/B` and never `BRK.B` — until 2026-09-22 every BRK.B lookup returned
  nothing, and the prompt presented that as a complete lookup finding no news
  across 40 reviews. Same applies to `BF.B` and `MOG.A`.
- **The fundamentals** (`agent.fundamentals_for` → `fundamentals.fetch_fundamentals`):
  facts, not verdicts, for every holding and candidate, attached as `fundamentals`.
  Added 2026-10-05 because cycles 48–52 deferred every decision for lack of
  valuation, earnings dates and reported results. Measured cost: ~+40.6k prompt
  tokens (~$0.16/cycle) and ~2.5 min per cycle (Finnhub's free tier, 144 calls at
  55/min); the estimate had been 12k, and the operator chose to keep every field
  over a 20k cap. The SEC's structured data can lag a 10-Q by months (GD's
  2026-07-29 10-Q was absent on 2026-10-05); this is named as
  `newer_filing_not_yet_in_sec_data`, and Finnhub's EPS actuals arrive faster.
  BRK.B's estimates are filed under BRK.A (per Class A share) and are withheld.
  **Holdings also get `earnings_release`** (added 2026-10-14): the opening
  8,000 characters (~2k tokens) of the latest 8-K item 2.02 exhibit 99.1, for
  21 days after filing, converted to text with table rows on one line. Added
  because on 2026-10-13 the agent could not test JNJ's "MedTech stalling"
  invalidation or JPM's credit leg, though both releases state them on page
  one (MedTech +2.5%; $2.2B net charge-offs, $539M reserve build). Filing
  indexes and converted documents are cached under `cache/edgar/docs/` (they
  never change) and pruned with the rest. `test_earnings_release.py` runs on
  JNJ's real recorded exhibit. Holdings also get `recent_8k_text`: the item
  text of up to 3 non-results 8-Ks from the last 30 days (cover page, 9.01
  exhibit list and signature cut; ≤3,000 characters each), because the
  one-line filing list left IQV's 23 Sep debt 8-K an open question for three
  weeks when the filing says it refinances existing notes. Exhibits are not
  followed, so a 7.01 that only points to slides says little. ~3k tokens on
  the 2026-10-14 book. Both document feeds are gated by `held` in
  `fetch_fundamentals` / `symbol_block`; candidates get neither.

The record exists because position weight is a lossy encoding of history, and
re-deriving intent from it produces two specific failures, both observed:

- **Re-deciding.** MSFT was trimmed six times (cycles 7, 8, 9, 11, 25, 27), each
  trim restating the previous one's reasoning about the same passed `$420+`
  marker. Nothing carried "you already acted on this" forward, so a spent
  observation read as news every cycle. It stopped after the case was
  re-anchored at cycle 29 — three clean cycles, which is consistent with the fix
  but not yet proof of it.
- **Drifting past its own stated intent.** Cycle 29 wrote "I will size the
  position up toward ~6-7% next cycle"; cycles 31 and 32 executed that plan
  without being able to read it, reconstructing it from weight and days held,
  and cycle 32 took GD to 7.4%. No guardrail covers this — a self-imposed
  ceiling is not a code invariant, so only the model can enforce it, and only if
  it can see it.

Rejected and unexecuted proposals are included and labelled, so an intention is
never read back as a fill. Slow tranching is the agent's dominant mode (of 25
trades in 32 cycles, ~12 are follow-on tranches in a name it had already
traded), and it is legitimate — the record is there so it happens on purpose.

Costs ~4k tokens on a ~13k-token prompt (+33%). Most of that is the reasoning
text, not the number of actions, so lowering the cap barely helps: limit 3 vs 10
differs by ~700 tokens.

**Still not injected:** past cycles' full commentary (`decision_cycles`), review
*notes* (only the verdict tally is — the notes are ~6.5k tokens for five cycles),
and superseded thesis wordings. Names that are neither held nor on today's
shortlist get no per-symbol news; they appear only if the 40-article market
sample happens to cover them.

## Guardrails (code-enforced invariants, not advisory)

Enforced in `guardrails.py:validate_trades`, thresholds in `config.py`. A trade that
fails any of these must be **rejected and logged, never silently adjusted**.

| Rule | Value |
|---|---|
| Max position size | 15% of portfolio per ticker (post-trade) |
| Universe | Long-only, whitelisted US equities/ETFs (`config.WATCHLIST`) |
| Leverage / margin / options | Not permitted (buys funded from available cash only) |
| Minimum cash buffer | 5% of equity |
| Max trades per decision cycle | 5 |
| Ticker validation | Every symbol verified tradable against the broker asset list before ordering |
| Order idempotency | Deterministic `client_order_id` per session/symbol/side/amount; the broker rejects a repeat |

Plus two structural rules with the same standing: buys require
thesis/target/invalidation, sells require a valid `exit_trigger`.

**Nothing here has ever fired.** As of 2026-10-05 (53 cycles), 0 of 34 proposed
trades were rejected. The binding constraint on this agent is not the
guardrails, it is inertia: 30 of 53 cycles proposed no trades, verdicts ran 520
`hold` / 12 `trim` / 0 `exit`, and no thesis has ever been closed — so the exit
path is untested in production. Before adding a rule here, re-check against the
DB (`select count(*), sum(is_valid=0) from trades`; `select verdict, count(*)
from position_reviews group by 1`) for evidence the existing set is doing
anything.

Validation runs cumulatively across the proposed list (running cash and running
position values), so trade N is checked against the state trades 1..N-1 would
leave behind — not against the opening snapshot.

### Capital mechanics the prompt has to explain

Sell proceeds are deliberately **not** credited within the same cycle — the sell
hasn't filled, and spending it would be leverage — which makes rotation a
two-cycle operation. The model was never told this, so for 19 consecutive cycles
it recorded some version of "no room to act" (cash ~7-8%, minus the 5% buffer,
leaves ~3% deployable) without ever starting a rotation to create room. The
prompt now states the two-cycle rule explicitly and calls it a supported move,
and `build_decision_prompt` injects the cash figures rather than leaving the
model to re-derive them.

An accepted **buy** has the mirror problem: it has not touched `cash` either, so
cash alone *overstates* what is spendable, by exactly the size of the order. So
three figures are injected, not one — `deployable_cash` (cash − buffer),
`committed_to_pending_buys`, and `uncommitted_cash`, which is the one to size
buys against. `validate_trades` starts `running_cash` from the same netted
number, so this is **enforced, not advised**: on 2026-09-03 cycle 30 was shown
$3,093 deployable with a $2,900 GD buy in flight, when only ~$192 was genuinely
free. The agent caught that unaided, but validation would have approved a $1,000
buy that breached the 5% buffer the moment GD filled. `price_pending_orders` runs
after `fetch_market_context`, because a share-sized order (one placed outside
the agent — the agent only ever sends notional orders) needs a price to value;
anything still unvaluable is named in `unpriced_pending_buys` and flagged to the
model as making `uncommitted_cash` a ceiling rather than an exact figure.

Validation keeps **two** position baselines for the same reason, taking opposite
cautions about the same pending orders:

- `position_values` — what is still *disposable*, and gates sells. Pending
  **sells** are netted off, or the same shares could be sold twice, once by the
  order in flight and again by this cycle's proposal. Pending **buys** are not
  added: those shares have not arrived, and treating them as sellable is how a
  long-only account ends up short.
- `cap_values` — where the book is *heading*, and gates the 15% cap. Pending
  **buys** are added, so a second buy cannot push a position past the cap once
  both fill. Pending **sells** are not netted off here: treating a position as
  already smaller would loosen the cap on an order that has not filled, and the
  cap should only ever err toward strictness.

The two unvaluable cases also diverge, for the same reason. An unvaluable
pending buy is merely named in `unpriced_pending_buys` (the commitment becomes a
floor). An unvaluable pending sell blocks further sells of that symbol outright
via `unvaluable_pending_sells` — there is no safe way to guess how much of a
holding is still disposable, so it refuses rather than estimates.

The prompt also flags that the 15% per-ticker cap is not a diversification
guarantee (five names on one driver is one position wearing five tickers) and
that owning SPY, while allowed, is index beta that cannot generate alpha against
the index it is benchmarked to, so it needs a case like anything else.

### Orders in flight

`fetch_portfolio_state` also pulls open (unfilled) orders into
`PortfolioState.pending_orders`, surfaced to the model as `orders_in_flight`
plus `cases_not_yet_held` (each flagged with whether a pending order explains
it). This matters because an accepted order changes **neither `cash` nor
`positions`** until it fills: the money hasn't left and the shares haven't
arrived. The agent decides after the close, so its orders queue until the next
open, and a cycle running before that fill would otherwise see a portfolio
identical to the one that produced the orders — and propose them again. In
normal daily operation the list is empty, because the previous cycle's orders
fill before the next one runs; a non-empty list means something ran twice or a
fill did not happen. A failure reading orders warns and returns empty rather
than killing the cycle, since account state is still usable without it.

## Database (`trading_agent.db`, gitignored)

Created idempotently by `db.py:init_db`.

- `decision_cycles` — one row per successful cycle: `timestamp` (UTC ISO),
  `mode`, full `prompt`, raw `claude_response`, `portfolio_cash`,
  `portfolio_equity`, `input_tokens` / `output_tokens`, and the agent version
  that made the decision: `code_commit`, `code_dirty` (uncommitted edits to
  tracked `.py` files — `dashboard.html` is excluded, it is regenerated every
  run), `system_prompt_sha`, `model`, `effort`. Captured at the start of the
  cycle by `current_agent_version`. NULL for cycles before 2026-09-23 and
  deliberately not backfilled — cycle 43 ran uncommitted code, so a commit
  inferred from timestamps would be a guess. **Compare performance by version,
  not across the whole history**: the instructions changed a dozen times in
  the first two months. Keep scheduled runs on a clean tree, or `code_dirty`
  makes the commit meaningless.
- `trades` — every *proposed* trade, valid or not: `cycle_id`, `symbol`, `side`,
  `notional_usd`, `reasoning`, `is_valid`, `rejection_reason`, `order_id`,
  `order_status`. Rejections are kept deliberately; they are decisions.
- `theses` — the *current* case only: `symbol`, `opened_at`, `thesis`, `target`,
  `invalidation`, `status` ('open'/'closed'), `closed_at`, `exit_trigger`,
  `updated_at`, `revision`. Unique partial index `theses_one_open_per_symbol`
  on `(symbol) WHERE status='open'`.
- `thesis_revisions` — every version a case has ever had, including the one it
  opened with: `symbol`, `cycle_id`, `revised_at`, `revision`, `source`
  ('open' / 'buy_refresh' / 'adopt' / 'review_reanchor'), and the three case
  fields. Without this a re-anchor would rewrite history and the record would
  read as though a position had always been held for today's reason.
  `db.py:_migrate_thesis_revisions` adds the two `theses` columns and
  backfills revision 1 for pre-existing rows; it is keyed on the presence of
  `theses.revision`, so it is idempotent.
- `position_reviews` — `cycle_id`, `symbol`, `verdict`, `note`. Case fields
  supplied on a review land in `theses`/`thesis_revisions`, not here.
- `news_articles` — every news article fetched: `id` (Alpaca's, the primary
  key), `published_at` (UTC ISO), `headline`, `summary`, `source`, `url`,
  `first_seen_at` (audit only — "new" is decided by `published_at` against the
  last cycle's timestamp). `news_symbols` maps `(symbol, article_id)`. Stored and
  committed before the decision is made: it records what was available, so a
  cycle that later fails still keeps it.
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
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `FINNHUB_API_KEY`, `SEC_USER_AGENT` (the
SEC requires contact details in it). There is no paper/live setting in `.env` —
the mode flag decides, and live is further locked behind
`config.LIVE_UNLOCK_ENV`.

`agent.py` and `dashboard.py` hit the broker and the Anthropic API and write
rows; only `report.py` is safe to run casually.

## Dependencies and data sources

Runtime dependencies are exactly `alpaca-py`, `anthropic`, `python-dotenv`
(`pytest` and `pyflakes` are dev-only). Prices, SPY closes and news come from
Alpaca; fundamentals from Finnhub (free tier) and SEC EDGAR, over stdlib HTTP.
