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
| Universe | Long-only, whitelisted US equities/ETFs (e.g., S&P 1500) |
| Leverage / margin / options | Not permitted |
| Minimum cash buffer | 5% |
| Max trades per decision cycle | 5 |
| Trade frequency | Weekly decision cycle (avoids PDT rules on accounts < $25k) |
| Ticker validation | Every symbol verified against broker asset list before ordering |
 
## Rollout Phases
 
- **Phase 1 — Paper (4–8 weeks):** identical code, Alpaca paper-trading keys. Goal: shake out bugs, verify logging, watch behavior.
- **Phase 2 — Live (small):** swap to live API keys with a small, loss-tolerable amount of capital.
- **Phase 3 — Project layer:** performance dashboard vs. SPY, automated trade-reasoning writeups, optional auto-posting.
## Evaluation Metrics
 
**Performance:** Total Return (%), Annualized Return, Alpha vs. S&P 500, Sharpe Ratio, Maximum Drawdown
 
**Behavioral:** Turnover rate, consistency of decisions, reaction to volatility/news events, frequency of rejected (guardrail-violating) proposals
 
## Data Sources
 
- **Alpaca** — positions, account state, order execution, basic market data
- **yfinance / Alpha Vantage** — historical prices, supplemental data
- **News API or web search tool** — recent headlines for holdings/watchlist (Claude's training data is stale for markets; all market context is injected fresh each cycle)

## Tech Stack
 
- Python (Pandas / NumPy)
- **Anthropic API (Claude)** — decision engine, structured JSON output
- **Alpaca API** — brokerage: paper and live trading
- SQLite — decision and performance logging
- Cron / GitHub Actions — scheduling
## API Costs
 
- **Anthropic API:** pay-as-you-go per token (separate from any Claude.ai subscription). A weekly decision run costs cents; expected total well under $10/month. Prompt caching and the Batch API reduce costs further.
- **Alpaca:** free (commission-free trading, free paper environment)
- **Market data:** free tiers (yfinance, Alpha Vantage)

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
python agent.py --mode paper
```
 
## Disclaimer
 
This project is for research and educational purposes only. It does not constitute financial advice. Trading involves risk of loss; the autonomous agent trades only capital the operator can afford to lose. Past performance is not indicative of future results.
