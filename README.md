# Claude Autonomous Trading Agent

[![tests](https://github.com/aydenwong8/Claude-Autonomous-Trading-Agent/actions/workflows/tests.yml/badge.svg)](https://github.com/aydenwong8/Claude-Autonomous-Trading-Agent/actions/workflows/tests.yml)
 
## Overview
 
This project evaluates the effectiveness of **Claude (Anthropic)** as an autonomous algorithmic trading decision-maker. The goal is to systematically test whether a large language model can construct and manage a portfolio that earns the **best risk-adjusted return against the S&P 500 (SPY)** — judged on Sharpe ratio, alpha, and drawdown, not raw return alone. It currently runs on an Alpaca paper account.
 
Unlike traditional quantitative models, this project explores how well an AI agent can:
 
- Interpret current market conditions from live data
- Allocate capital across assets
- Adjust strategy over time based on its own logged history
- Compete against a passive benchmark — with **zero human override**
The agent operates fully autonomously within hard, code-enforced guardrails. No human approves or vetoes individual trades.
 
## Objectives
 
- Measure the risk-adjusted performance of Claude-driven portfolio decisions
- Compare results against the S&P 500 (SPY) benchmark
- Analyze strengths and weaknesses of LLM-based trading strategies
- Evaluate consistency, risk exposure, and adaptability over time
- Build a reusable agent framework (data → decision → validation → execution → logging)

## Architecture
 
The system is a scheduled loop:
 
```
┌─────────────┐    ┌──────────────┐    ┌────────────┐    ┌───────────┐    ┌─────────┐
│ 1. Gather    │ →  │ 2. Claude     │ →  │ 3. Validate │ →  │ 4. Execute │ →  │ 5. Log   │
│ portfolio +  │    │ decides       │    │ against     │    │ orders via │    │ decision │
│ market data  │    │ (JSON trades) │    │ guardrails  │    │ broker API │    │ + P&L    │
└─────────────┘    └──────────────┘    └────────────┘    └───────────┘    └─────────┘
```
 
1. **Gather state** — pull current positions, cash, P&L, and any unfilled orders from the Alpaca API; fetch current prices and recent news for holdings and the watchlist. Unfilled orders are gathered because an accepted order moves neither cash nor positions until it fills: the agent decides after the close, its orders queue until the next open, and a cycle that runs in between would otherwise see an untouched portfolio and place the same trade twice.
2. **Decision** — send the full state to the Claude API (Anthropic `/v1/messages`) with instructions to output trades as structured JSON, each with written reasoning.
3. **Validate** — code (not a human) checks every proposed trade against the guardrails below. Invalid trades are rejected and logged; valid trades proceed.
4. **Execute** — place validated orders through the Alpaca trading API (paper first, then live).
5. **Log** — store the full prompt, Claude's response and reasoning, validation results, executed orders, and daily portfolio value in a local SQLite database.

## Guardrails (Code-Enforced, Not Human Override)
 
The agent is fully autonomous, but it operates inside hard constraints:
 
| Rule | Value |
|---|---|
| Max position size | 15% of portfolio per ticker, measured against holdings **plus** unfilled buys, so two orders cannot together breach it. Unfilled buys are deliberately *not* counted as holdings for sells — shares that have not arrived must not be sellable |
| Universe | Long-only, whitelisted US equities/ETFs — S&P 1500 ∪ Nasdaq-100 (1,519 symbols) |
| Leverage / margin / options | Not permitted. Buys are funded only from cash already settled at the start of the cycle — proceeds from a sell in the same cycle are **not** spendable, since that sell has not filled yet and a margin account would fund the gap as leverage |
| Unfilled orders | Neither cash nor positions moves until an order fills, so both are double-countable while one is in flight. Sells are checked against the position **net of unfilled sells** (the same shares cannot be sold twice) and unfilled buys are never treated as sellable (shares that have not arrived must not be disposable). A pending order too ambiguous to value blocks further sells of that symbol rather than being estimated |
| Minimum cash buffer | 5%, measured against cash **net of unfilled buy orders**. An accepted order has not touched cash yet, so raw cash overstates what is spendable by the size of that order — enough to breach the buffer on fill without any single decision looking wrong |
| Minimum trade size | $1 notional (the broker's floor) |
| Ticker validation | Every symbol verified against the broker asset list before ordering. The whitelist gates **buys** only — a held position whose ticker later leaves the index must always remain sellable |
| Order idempotency | Every order carries a deterministic `client_order_id` (session date + symbol + side + amount). Orders are placed before the cycle is logged, so a crash between the two leaves fills the database never recorded; the broker rejects the duplicate on a rerun rather than doubling the position |
| Live-trading lock | `--mode live` refuses to run unless `TRADING_AGENT_ALLOW_LIVE` is set explicitly in the shell. Paper is the only thing reachable by accident |

Enforcement lives in `validate_trades()` and is checked at the single broker-client
chokepoint, so no call path can reach a live account or place an unvalidated order.
Non-finite (`NaN`/`Infinity`) trade sizes are rejected at parse time — every guardrail
is a numeric comparison, and comparisons against `NaN` are always false, so such a
value would otherwise satisfy all of them.

**Every guardrail is unit-tested.** Each rejection path in `validate_trades` has a test
in `test_guardrails.py` — both sides of every limit, the unfilled-order cases, and the
thesis / exit-trigger requirements — as do the live-trading lock and order idempotency
(`test_order_idempotency.py`: a rerun of the same session is suppressed, not doubled).
The suite runs on fakes and recorded API responses, needs no keys, and runs in CI on
every push.

**Trade frequency.** The agent runs once daily after the close, not weekly as originally
designed. Nothing in code enforces a trade-frequency limit, and the prompt no longer
advises one either: "most days should be zero trades" was removed on 2026-10-08, so
how often to trade is the agent's own call. The PDT rule that line was originally meant to guard against
cannot actually bind: pattern-day-trading counts same-session round trips, and an agent
that decides once after the close and holds overnight has no way to make one. The real
observed risk runs the other way — see below.

**What the guardrails have actually caught: nothing.** Over the first 60 cycles (to
2026-10-14), all 36 proposed trades passed validation and none were rejected. The set is
worth keeping — it is what makes autonomy safe to run unattended, and an untriggered
guardrail is doing its job — but the binding constraint on this agent is inertia, not
permissiveness: 35 of 60 cycles proposed no trades at all, review verdicts ran 603
`hold` / 13 `trim` / 0 `exit`, and not one thesis has been closed, so the exit path has
never executed in production. Any new rule proposed here should first be checked against
`trades.rejection_reason` for evidence the existing ones bind.

## Thesis Lifecycle

Every position carries a written investment case, which is what makes the agent
auditable rather than merely active.

- **Opening.** A buy is rejected unless it supplies three fields: the `thesis` (why we
  own it), the `target` (what it playing out looks like), and the `invalidation` (what
  would prove it wrong). A position without a written case cannot be reviewed later.
- **Carrying.** Each stored thesis is replayed into every subsequent prompt alongside
  that position's current price, unrealised P&L, and days held — so the agent argues
  against its own prior reasoning instead of forming a fresh opinion daily.
- **Re-anchoring.** A case can go stale without going wrong: the commonest failure is a
  price marker in `target` being passed while the operating condition it stood in for
  still holds. A review may rewrite the case in place by supplying all three fields
  again, and every version is kept and dated in `thesis_revisions`, so a rewrite is an
  amendment on the record rather than an edit to history. This was not always possible,
  and the cost showed: MSFT was trimmed six separate times over five weeks against a
  "$420+" target it had already cleared by 19%, because selling and rebuying was the
  only way to change the wording. The prompt shows the revision count back to the agent
  precisely so that ratcheting a target behind a rising price stays visible as the
  goalpost-moving it would be.
- **Adopting.** A held position with no stored case — opened before thesis tracking
  existed, or outside the agent — can be adopted by a review that supplies all three
  fields, rather than having to be sold and rebought to get one.
- **Reviewing.** Every cycle returns a verdict for *every* open position — `hold`,
  `trim`, or `exit` — even on days with no trades. A `trim`/`exit` verdict with no
  matching sell is recorded as the agent contradicting itself, and surfaces on the
  dashboard as a "says vs. does" rate.
- **Closing.** Every sell must name a valid exit trigger — `thesis_broken`,
  `target_hit`, `bad_news`, `no_longer_fits`, or `risk`. This is enforced, not advisory:
  a sell without one is rejected. Positions cannot be closed on price drift alone.
- **Orphans.** A case is only ever shown to the agent attached to a held position, so a
  case whose position is gone — an order that never filled, a holding disposed of
  outside the agent — receives no review and cannot be closed by a sell, because there
  is nothing to sell. These are detected and closed by code, marked `position_gone` to
  distinguish an absence from a decision. Orders still in flight and trades placed in
  the same cycle are not orphans, and an empty position list is treated as a broker
  glitch rather than a liquidated account.


## Rollout Phases
 
- **Phase 1 — Paper (running since 2026-07-16; originally planned as 4–8 weeks):** identical code, Alpaca paper-trading keys. Goal: shake out bugs, verify logging, watch behavior.
- **Phase 2 — Live (small):** swap to live API keys with a small, loss-tolerable amount of capital. **Deferred, no date set** — the focus for now is improving risk-adjusted returns on paper.
- **Phase 3 — Project layer:** performance dashboard vs. SPY, automated trade-reasoning writeups, optional auto-posting. (The dashboard is built — `dashboard.py`.)

Phase 1 is not finished. The gate for Phase 2 is not elapsed time but evidence the full
lifecycle works end to end, and one arc has never run: no thesis has been closed, so the
`exit_trigger` path has executed exactly zero times outside tests. Going live on an
untested exit is going live on the half of the system that decides when to stop losing
money.
## Evaluation Metrics
 
**Performance (primary — risk-adjusted):** Sharpe Ratio, Alpha vs. S&P 500, Maximum Drawdown

**Performance (context):** Total Return (%), Annualized Return
 
**Behavioral:** Turnover rate, consistency of decisions, reaction to volatility/news events, frequency of rejected (guardrail-violating) proposals

**Measurement window.** All of these are computed from the first decision cycle to the
present, never a rolling lookback. This is not a detail: the dashboard originally asked
Alpaca for a fixed `period="1M"`, which meant the window moved forward every day and
eventually started *after* the portfolio had been built. On 2026-09-02 that reported
+0.18% agent vs. +0.52% SPY — alpha of **−0.34pp** — while the since-inception figures
for the same account were +4.17% vs. +1.37%, alpha of **+2.80pp**. A benchmark
comparison that silently redefines its own start date reverses its own conclusion.
 
## Data Sources
 
**Alpaca** covers positions, account state, order execution, prices and news. **Finnhub**
(free tier) supplies valuation and its 5-year history, earnings dates with consensus, and
recent EPS against estimate. **SEC EDGAR** supplies reported quarterly results and material
filings. Claude's training data is stale for markets, so all of this is injected fresh every
cycle and never recalled from the model.

News coverage is split deliberately, because the two cases have different requirements:

- **Position news** — one lookup per held position, so coverage is *complete*. An empty
  list means no recent headlines were found for that holding, which is itself
  information the agent can act on.
- **Market news** — a 40-headline sample across the ~1,500-name watchlist. This cannot
  be complete at that scale, and the prompt says so explicitly: a ticker's absence from
  the sample tells the agent nothing about that ticker.

That split matters. A single shared request across the whole watchlist gave **7% symbol
coverage** (50 articles reaching 37 of 520 names), and most holdings were reviewed with
no news at all — while the model had no way to tell "no news" from "not looked up".
Per-position lookups take coverage of holdings to 100% for about 0.6s per cycle.

Beyond holdings and the market sample, three more feeds are injected:

- **Candidate news** — each name on the day's screened shortlist gets its own lookup,
  showing the newest 3 dated headlines. Without it, the only current information behind
  a case for a name not yet held was the price table.
- **Drawdown news** — candidates trading at least 25% below their 52-week high get up to
  5 dated headlines from around their steepest 5-day fall. Those falls are often months
  old, so the last 30 days of news cannot explain them; the agent was passing on names
  saying it "was not given the cause".
- **Filings text (holdings only)** — the opening of the latest earnings release (8-K item
  2.02, exhibit 99.1) for 21 days after filing, plus the item text of up to three other
  8-Ks from the last 30 days. A one-line filing list cannot test an invalidation like
  "segment growth stalling"; the release usually states it on page one.

## Tech Stack
 
- Python — standard library only for analysis; no Pandas/NumPy dependency
- **Anthropic API (Claude Opus 5.5, medium effort)** — decision engine, structured JSON output
- **Alpaca API** — brokerage, market data, and news
- **Finnhub + SEC EDGAR** — fundamentals (valuation, earnings, reported results, filings)
- SQLite — decision, thesis, and performance logging
- launchd (`run_daily.sh`) — weekdays at 16:30 ET, skipping market holidays
## API Costs
 
- **Anthropic API:** pay-as-you-go per token, separate from any Claude.ai subscription. Measured, not estimated: on Claude Opus 5.5 at medium effort ($4/$20 per million tokens), cycles 56–60 averaged **~79,000 input and ~7,000 output tokens ≈ $0.46/run**, or **~$10/month** at 21 trading days. The prompt grew from ~27k tokens as the self-history, news archive, and candidate screen were added, then to ~45k (≈$0.29/run at cycles 48–50), and roughly doubled again once fundamentals and filings text arrived; output fell sharply on the move from Opus 5 at xhigh effort (~17k output tokens, ~$0.67/run).
- **Alpaca:** free (commission-free trading, free paper environment)
- **Market data and news:** included with Alpaca at no additional cost

## Key Questions This Project Explores
 
- Can an autonomous LLM agent beat passive investing on a risk-adjusted basis?
- Does written AI reasoning translate into coherent, consistent strategy?
- How does Claude handle volatility and negative news?
- How often does the model propose trades that violate its constraints?

## Limitations
 
- Results depend heavily on prompt quality and injected data quality
- LLMs are not optimized for financial prediction; outperformance over short windows is statistical noise
- Real-money trading creates taxable events (short-term capital gains)
- Single-model, single-account experiment — results may not generalize

## Future Improvements
 
- Hybrid mode: feed quant model signals (see companion stock-prediction project) into Claude's context
- Sentiment analysis pipeline
- Multi-model comparison (Claude vs. other LLMs, head-to-head accounts)
- Public dashboard / X auto-posting

## How to Run
 
```bash
git clone https://github.com/aydenwong8/Claude-Autonomous-Trading-Agent.git
cd Claude-Autonomous-Trading-Agent
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # add Anthropic, Alpaca (paper first!), Finnhub keys + SEC_USER_AGENT
venv/bin/python agent.py --mode paper       # one decision cycle
venv/bin/python dashboard.py --mode paper   # regenerate dashboard.html
venv/bin/python report.py                   # per-position thesis / verdict report
venv/bin/python -m pytest -q                # test suite
```

`SEC_USER_AGENT` is required by the SEC (`"<project name> <contact email>"`); without it
every EDGAR lookup fails. `run_daily.sh` is what the scheduler invokes and expects the
`venv/` above.

`--mode live` is locked and will refuse to run. Unlocking it is a deliberate, separate
act, and is not something to put in `.env` where a scheduled run could pick it up:

```bash
TRADING_AGENT_ALLOW_LIVE=yes-trade-real-money python agent.py --mode live
```
 
## Disclaimer
 
This project is for research and educational purposes only. It does not constitute financial advice. Trading involves risk of loss; the autonomous agent trades only capital the operator can afford to lose. Past performance is not indicative of future results.
