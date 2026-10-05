# Fundamentals feed (Finnhub + SEC EDGAR) — design

Date: 2026-10-05
Status: approved in conversation; awaiting review of this written spec
Follows: `2026-10-04-agent-refactor-design.md` (merged as 06d06ac)

## Why

Cycles 48–52 (Opus 5.5) proposed zero trades. The agent's own commentary names
the cause as missing information, not a lack of conviction:

- "cannot tell broken from mispriced": no valuation, and no history to read it against
- "[spin-off mechanics] I cannot verify from what I was given": no corporate events
- "the conditions … all resolve within ~3 weeks of earnings": no earnings dates,
  and no reported results once they land

Every weak holding is deferred to an earnings print it cannot see coming and
could not read when it arrived. JPM and JNJ report on 2026-10-13, GD on 2026-10-22
and IQV on 2026-10-27, so the feed must be live before 2026-10-13.

## Goal and non-goals

**Goal:** every name in the prompt (holdings and screen candidates, ~48) carries
a `fundamentals` block of facts: valuation against its own five-year history,
growth and quality, upcoming earnings with consensus, recent actuals against
estimate, the latest reported quarter against the same quarter a year earlier,
and material SEC filings from the last 90 days.

**Success:** around the October prints, reviews cite these figures and dates, and
the agent acts on its written tests (e.g. GD's book-to-bill test after
2026-10-22) instead of deferring. The phrase "cannot tell / verify from what I
was given" stops recurring.

**Non-goals:**
- No guardrail, schema, or decision-rule change. The agent gets facts, not
  instructions on how to use them. The operator's stance is to let Claude
  mostly do its own thing.
- No verdict words in the data ("cheap", "expensive", "beat", "miss"). Numbers only.
- No peer/sector comparison (considered and rejected: double the calls, and a
  "peer" definition to get right).
- No paid tiers. If reviews later show a specific gap (e.g. revenue estimate
  history), that is a separate decision.
- The screen's spin-off price artefact (HON, BDX) is not fixed here. The filings
  list lets the agent see the cause; correcting the price series is separate.

## Verified API facts (probed 2026-10-05)

- **Finnhub free tier**, 60 calls/min (`X-Ratelimit-Limit: 60`):
  - `/stock/metric?symbol=X&metric=all`: 133 current metrics (e.g. `peTTM`,
    `forwardPE`, `evEbitdaTTM`, `psTTM`, `revenueGrowthTTMYoy`,
    `revenueGrowthQuarterlyYoy`, `epsGrowthTTMYoy`, `operatingMarginTTM`,
    `roeTTM`, `totalDebt/totalEquityQuarterly`, `dividendYieldIndicatedAnnual`),
    plus `series.quarterly` history (e.g. `peTTM` back to 2000, `operatingMargin`).
    `BRK.B` works as-is. SPY returns 19 metrics and no `peTTM`.
  - `/calendar/earnings?symbol=X&from=&to=`: next report date, `hour`
    (`bmo`/`amc`/blank), `epsEstimate`, `revenueEstimate`. **A date-range query
    without `symbol` is silently capped at 1,500 rows and drops names**, so it
    must be queried per symbol. Past windows return empty.
  - `/stock/earnings?symbol=X&limit=4`: last four quarters of
    `actual`/`estimate`/`surprisePercent` EPS.
- **SEC EDGAR**, no key, requires a descriptive `User-Agent` with contact info:
  - `https://www.sec.gov/files/company_tickers.json`: ticker→CIK map for 10,434
    tickers. Share classes use `-` (`BRK-B`, `BF-B`, `MOG-A`); `BRK.B` is absent.
  - `https://data.sec.gov/submissions/CIK##########.json` (~150 KB): the recent
    filings list, dominated by Form 4/144 insider filings that must be filtered out.
  - `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (~3.6 MB for
    GD): reported XBRL facts. Concept names vary by company (`Revenues` vs
    `RevenueFromContractWithCustomerExcludingAssessedTax`; banks differ again).
    Entries are not ordered by period, so they must be sorted by `filed`/`end`.

## What the agent sees per name

A `fundamentals` object is attached beside each name's existing price and screen
data, for holdings and candidates alike. Shape (GD, illustrative values):

```json
"fundamentals": {
  "valuation": {
    "pe_ttm": 19.9, "pe_ttm_5y_range": [16.8, 23.1], "pe_ttm_5y_median": 20.4,
    "forward_pe": 19.0, "ev_ebitda_ttm": 14.0, "price_to_sales_ttm": 1.6,
    "dividend_yield_pct": 2.1
  },
  "growth_and_quality": {
    "revenue_growth_yoy_ttm_pct": 9.1, "revenue_growth_yoy_last_quarter_pct": 8.1,
    "eps_growth_yoy_ttm_pct": 10.1, "operating_margin_ttm_pct": 10.3,
    "operating_margin_5y_range_pct": [9.6, 11.2], "roe_ttm_pct": 17.4,
    "debt_to_equity": 0.28
  },
  "earnings": {
    "next_report": "2026-10-22 before open", "days_until": 17,
    "consensus_eps": 4.25, "consensus_revenue_usd_bn": 14.14,
    "last_4_quarters": [
      {"quarter": "2026Q2", "eps_actual": 4.24, "eps_estimate": 4.09, "surprise_pct": 3.7}
    ]
  },
  "reported_results": {
    "source": "10-Q, quarter ended 2026-07-05, filed 2026-07-29",
    "revenue_usd_bn": 13.9, "operating_income_usd_bn": 1.45,
    "net_income_usd_bn": 1.14, "diluted_eps": 4.24,
    "same_quarter_last_year": {"revenue_usd_bn": 12.8, "diluted_eps": 3.74}
  },
  "material_filings_90d": [
    {"date": "2026-07-22", "form": "8-K", "items": "2.02 Results of operations",
     "new_since_your_last_cycle": false}
  ]
}
```

Rules:
- **Five-year ranges and medians** use the last 20 quarterly points of the
  Finnhub series (`peTTM`, `operatingMargin`). With fewer than 8 points the range
  is omitted and named in `not_available`.
- **Rounding:** ratios to 1 decimal, dollar amounts to `usd_bn` with 2 decimals,
  EPS to 2 decimals. Keys sort deterministically, so the prompt is reproducible.
- **`new_since_your_last_cycle`** is true for a filing, or a `last_4_quarters`
  entry, dated after the previous decision cycle's timestamp. This is the same
  rule `news.py` uses.
- **Absences are named:** `"not_available": ["forward_pe", ...]` for fields a
  source did not return; `"not_applicable": "ETF"` for funds (SPY, QQQ, any
  symbol EDGAR marks as an investment company or that has no `us-gaap` facts).
- **Failures are named:** `"lookup_failed": {"finnhub": "<short reason>"}` and/or
  `{"edgar": "<short reason>"}`. Data from the source that worked is still shown.
- **Size target:** ~250 tokens per name, ~12k per cycle, about $0.05 at Opus 5.5
  input pricing. The dry run (see Rollout) must confirm it; if it exceeds 20k,
  `last_4_quarters` drops to 2 for candidates before anything else is cut.
- **System prompt:** one paragraph says what the block is, where each part comes
  from, the as-of semantics (TTM, latest filed quarter), and that
  `lookup_failed` means "not looked up", not "nothing to report". It gives no
  advice on how to weigh the data.

## Architecture

**New module `fundamentals.py`.** Public interface:

```python
def fetch_fundamentals(
    symbols: list[str], last_cycle_at: datetime | None, now: datetime,
) -> dict[str, dict[str, Any]]
```

It returns one prompt-ready block per symbol, and never raises for data or
network errors; those become `lookup_failed`. Internals, each a small function
tested on its own:

- `FinnhubClient`: three GETs per symbol, stdlib `urllib` (no new dependency),
  key from `FINNHUB_API_KEY`, through a `RateLimiter` (≤ 55 calls/min, injectable
  clock). On HTTP 429 it waits for `Retry-After` (default 2 s), retries once, and
  other errors also retry once.
- `EdgarClient`: ticker map (cached for a day), submissions (fetched each
  cycle), companyfacts (cached in `cache/edgar/{cik}.json`, re-downloaded only
  when submissions show a 10-Q/10-K accession newer than the cached one). Rate
  ≤ 5 req/s. The `User-Agent` comes from `SEC_USER_AGENT` in `.env`; if it is
  missing, EDGAR is flagged for every name.
- `edgar_symbol(symbol)`: maps `.` to `-` (`BRK.B` → `BRK-B`).
- `quarter_results(companyfacts)`: the latest 10-Q/10-K quarter and the same
  quarter a year earlier, using concept fallback lists:
  - revenue: `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`,
    `RevenuesNetOfInterestExpense`, `InterestAndDividendIncomeOperating`
  - operating income: `OperatingIncomeLoss`
  - net income: `NetIncomeLoss`, `ProfitLoss`
  - diluted EPS: `EarningsPerShareDiluted`

  Quarterly values are the ~3-month-duration facts (a `frame` like `CY2026Q2`).
  A figure no concept supplies goes in `not_available`.
- `material_filings(submissions, since, last_cycle_at)`: keeps forms `8-K`,
  `8-K/A`, `10-Q`, `10-K`, `10-12B`, `10-12G`, `S-4`, `425`, filed within 90 days.
  8-K item codes are translated with a fixed table (e.g. `2.02` "Results of
  operations", `2.01` "Completion of acquisition or disposition", `1.01`
  "Material definitive agreement", `5.02` "Officer/director change",
  `2.05` "Exit/disposal costs", `8.01` "Other events").
- `build_block(...)`: assembles the shape above from the parsed parts.

**Hooks into existing code (small):**
- `agent.py` `run_decision_cycle`: after `price_pending_orders`, call
  `fetch_fundamentals(sorted(set(portfolio.positions) | set(market.candidates)),
  last_cycle_at, now)`, and pass the result to `build_decision_prompt`.
  `last_cycle_at` is read the same way `news.py` already reads it.
- `prompt.py`: `build_decision_prompt` gains a `fundamentals` parameter and
  attaches each block to its symbol, plus the system-prompt paragraph.
- `config.py`: `FINNHUB_CALLS_PER_MIN = 55`, `EDGAR_CALLS_PER_SEC = 5`,
  `EDGAR_CACHE_DIR = "cache/edgar"`, `FILINGS_LOOKBACK_DAYS = 90`,
  `FUNDAMENTALS_HISTORY_QUARTERS = 20`.
- `.gitignore`: `cache/`. `.env.example`: `FINNHUB_API_KEY=`, `SEC_USER_AGENT=`.

**Runtime:** sequential, with no threads. About 144 Finnhub calls at 55/min is
~2.6 min. EDGAR adds ~10 s for 48 submissions at 5/s, plus a ~3.6 MB download
only for names with a new 10-Q/10-K. A cycle runs about 3 minutes longer, which
is acceptable for a daily post-close job.

## Error handling

The cycle never fails because of fundamentals:

| Condition | Result |
|---|---|
| One call fails (network/5xx) | Retry once, then that source → `lookup_failed` for that name |
| HTTP 429 | Wait `Retry-After`, retry once, then flag |
| Source down / every call failing | Every name flagged; one warning line in `agent.log` |
| `FINNHUB_API_KEY` or `SEC_USER_AGENT` missing | That source flagged for every name; warning |
| Symbol unknown to EDGAR ticker map | `lookup_failed: {"edgar": "no CIK for <symbol>"}` |
| Malformed/unexpected JSON | Treated as a failed call for that name and source |
| `fetch_fundamentals` itself raises (a bug) | Caught in `run_decision_cycle`; every name gets `lookup_failed: {"fundamentals": "internal error"}`; traceback printed |

## Testing

- **Recorded fixtures** in `tests/data/fundamentals/` (committed: public market
  data): trimmed real Finnhub and EDGAR responses for GD (ordinary), JPM (bank
  concepts), BRK.B (share class) and SPY (ETF). Tests make **no network calls**.
- **Unit tests** (`test_fundamentals.py`), written before the code they cover:
  - the symbol mapping
  - 5-year range and median, including the fewer-than-8-points case
  - latest-quarter and year-earlier selection
  - concept fallbacks (JPM)
  - the form filter and 8-K item translation
  - the `new_since_your_last_cycle` boundary
  - ETF `not_applicable`
  - rounding and deterministic key order
  - every failure row in the table above
  - the rate limiter under a fake clock
  - cache re-download only on a new 10-Q/10-K accession
- **Prompt:** a test that `build_decision_prompt` attaches each block to the
  right symbol and that a flagged block renders its `lookup_failed`.
- **Golden fixture:** **deliberately re-captured** with fundamentals included
  (the prompt is meant to change). `tests/golden.py` is extended to store the
  fundamentals result alongside portfolio/market and pass it through.
- **Existing suite:** the 92 tests from the refactor pass unchanged, except the
  golden pair, which is re-captured as above.
- **Patching rule:** test fakes patch the defining module (or `fn.__globals__`),
  never `agent.X`. Since the split, `agent.X` is a copy and patching it would not
  reach the code, so a "faked" call could reach a real API.

## Rollout

1. Build on a branch in a separate worktree. The main folder stays on `main` at 16:30 ET.
2. **Dry run before merge:** a read-only script runs `fetch_fundamentals` against
   the real APIs for today's ~48 names, with no trading and no Claude call.
   Report: names with full / partial / flagged data, runtime, and prompt
   tokens (via `count_tokens`) against the ~12k estimate.
3. Merge before the 2026-10-06 or 2026-10-07 cycle, and in any case before
   2026-10-13.
4. The first live cycle records the new `code_commit` and `system_prompt_sha`.
   From then on, compare behaviour **by version** (`decision_cycles.code_commit`).
5. Update CLAUDE.md (module table, data sources, the README divergence: "Alpaca
   is the only data source" is no longer true) and README.md's Data Sources section.

## Open item for the operator

- `SEC_USER_AGENT` must contain contact information the operator chooses (SEC
  policy), e.g. `"Claude-Autonomous-Trading-Agent you@example.com"`. It goes in
  `.env`, not in code.
