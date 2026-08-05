# Claude Autonomous Trading Agent
 
## Overview
 
This project evaluates the effectiveness of **Claude (Anthropic)** as an autonomous algorithmic trading decision-maker. The goal is to systematically test whether a large language model can construct and manage a real-money portfolio that outperforms or competes with the S&P 500 Index.
 
Unlike traditional quantitative models, this project explores how well an AI agent can:
 
- Interpret current market conditions from live data
- Allocate capital across assets
- Adjust strategy over time based on its own logged history
- Compete against a passive benchmark — with **zero human override**
The agent operates fully autonomously within hard, code-enforced guardrails. No human approves or vetoes individual trades.
 
## Objectives
 
- Measure the performance of Claude-driven portfolio decisions with real capital
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
 
1. **Gather state** — pull current positions, cash, and P&L from the Alpaca API; fetch current prices and recent news for holdings and the watchlist.
2. **Decision** — send the full state to the Claude API (Anthropic `/v1/messages`) with instructions to output trades as structured JSON, each with written reasoning.
3. **Validate** — code (not a human) checks every proposed trade against the guardrails below. Invalid trades are rejected and logged; valid trades proceed.
4. **Execute** — place validated orders through the Alpaca trading API (paper first, then live).
5. **Log** — store the full prompt, Claude's response and reasoning, validation results, executed orders, and daily portfolio value in a local SQLite database.

## Guardrails (Code-Enforced, Not Human Override)
 
The agent is fully autonomous, but it operates inside hard constraints:
 
| Rule | Value |
|---|---|
| Max position size | 15% of portfolio per ticker |
| Universe | Long-only, whitelisted US equities/ETFs — S&P 1500 ∪ Nasdaq-100 (1,519 symbols) |
| Leverage / margin / options | Not permitted. Buys are funded only from cash already settled at the start of the cycle — proceeds from a sell in the same cycle are **not** spendable, since that sell has not filled yet and a margin account would fund the gap as leverage |
| Minimum cash buffer | 5% |
| Max trades per decision cycle | 5 |
| Minimum trade size | $1 notional (the broker's floor) |
| Ticker validation | Every symbol verified against the broker asset list before ordering. The whitelist gates **buys** only — a held position whose ticker later leaves the index must always remain sellable |
| Live-trading lock | `--mode live` refuses to run unless `TRADING_AGENT_ALLOW_LIVE` is set explicitly in the shell. Paper is the only thing reachable by accident |

Enforcement lives in `validate_trades()` and is checked at the single broker-client
chokepoint, so no call path can reach a live account or place an unvalidated order.
Non-finite (`NaN`/`Infinity`) trade sizes are rejected at parse time — every guardrail
is a numeric comparison, and comparisons against `NaN` are always false, so such a
value would otherwise satisfy all of them.

**Trade frequency.** The agent runs once daily after the close, not weekly as originally
designed. Nothing in code enforces a trade-frequency limit; "most days should be zero
trades" is instruction to the model, not a guardrail. On a paper account this is
harmless, but PDT rules apply to live accounts under $25k — this needs a code-enforced
limit before Phase 2, or it is the one advisory rule in an otherwise code-enforced set.

## Thesis Lifecycle

Every position carries a written investment case, which is what makes the agent
auditable rather than merely active.

- **Opening.** A buy is rejected unless it supplies three fields: the `thesis` (why we
  own it), the `target` (what it playing out looks like), and the `invalidation` (what
  would prove it wrong). A position without a written case cannot be reviewed later.
- **Carrying.** Each stored thesis is replayed into every subsequent prompt alongside
  that position's current price, unrealised P&L, and days held — so the agent argues
  against its own prior reasoning instead of forming a fresh opinion daily.
- **Reviewing.** Every cycle returns a verdict for *every* open position — `hold`,
  `trim`, or `exit` — even on days with no trades. A `trim`/`exit` verdict with no
  matching sell is recorded as the agent contradicting itself, and surfaces on the
  dashboard as a "says vs. does" rate.
- **Closing.** Every sell must name a valid exit trigger — `thesis_broken`,
  `target_hit`, `bad_news`, `no_longer_fits`, or `risk`. This is enforced, not advisory:
  a sell without one is rejected. Positions cannot be closed on price drift alone.


## Rollout Phases
 
- **Phase 1 — Paper (4–8 weeks):** identical code, Alpaca paper-trading keys. Goal: shake out bugs, verify logging, watch behavior.
- **Phase 2 — Live (small):** swap to live API keys with a small, loss-tolerable amount of capital.
- **Phase 3 — Project layer:** performance dashboard vs. SPY, automated trade-reasoning writeups, optional auto-posting.
## Evaluation Metrics
 
**Performance:** Total Return (%), Annualized Return, Alpha vs. S&P 500, Sharpe Ratio, Maximum Drawdown
 
**Behavioral:** Turnover rate, consistency of decisions, reaction to volatility/news events, frequency of rejected (guardrail-violating) proposals
 
## Data Sources
 
**Alpaca is the only data source.** It covers positions, account state, order execution,
prices, and news, so no second provider is needed. Claude's training data is stale for
markets, so all of this is injected fresh every cycle and never recalled from the model.

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

## Tech Stack
 
- Python — standard library only for analysis; no Pandas/NumPy dependency
- **Anthropic API (Claude Opus 5)** — decision engine, structured JSON output
- **Alpaca API** — brokerage, market data, and news
- SQLite — decision, thesis, and performance logging
- launchd (`run_daily.sh`) — weekdays at 16:30 ET, skipping market holidays
## API Costs
 
- **Anthropic API:** pay-as-you-go per token, separate from any Claude.ai subscription. Measured, not estimated: a daily cycle over the 1,519-symbol universe is **~26,800 input tokens ≈ $0.20/run**, or **~$4/month** at 21 trading days (Claude Opus 5, $5/$25 per million tokens). Widening the universe from 520 to 1,519 symbols added only ~$1.30/month — prompt size scales at roughly 12.6 tokens per ticker, and the news sample is capped so it does not scale at all.
- **Alpaca:** free (commission-free trading, free paper environment)
- **Market data and news:** included with Alpaca at no additional cost

## Key Questions This Project Explores
 
- Can an autonomous LLM agent outperform passive investing with real money?
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
git clone https://github.com/yourusername/claude-trading-agent.git
cd claude-trading-agent
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY + Alpaca keys (paper first!)
python agent.py --mode paper       # one decision cycle
python dashboard.py --mode paper   # regenerate dashboard.html
python report.py                   # per-position thesis / verdict report
```

`--mode live` is locked and will refuse to run. Unlocking it is a deliberate, separate
act, and is not something to put in `.env` where a scheduled run could pick it up:

```bash
TRADING_AGENT_ALLOW_LIVE=yes-trade-real-money python agent.py --mode live
```
 
## Disclaimer
 
This project is for research and educational purposes only. It does not constitute financial advice. Trading involves risk of loss; the autonomous agent trades only capital the operator can afford to lose. Past performance is not indicative of future results.
