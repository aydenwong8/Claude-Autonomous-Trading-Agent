# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository currently contains only `README.md` — no code has been written yet (no `agent.py`, `requirements.txt`, or `.env.example` exist). The README is the design doc for the project and should be treated as the source of truth for intended architecture until implementation begins. When scaffolding the project, follow the structure described below rather than inventing a different one.

## What this project is

An experiment testing whether Claude (Anthropic) can act as an autonomous algorithmic trading decision-maker, managing a real-money portfolio against Alpaca's brokerage API and being benchmarked against the S&P 500 (SPY). The agent runs with **zero human override** — every trade is proposed by Claude and then accepted or rejected by code-enforced guardrails, never by a human in the loop.

## Intended architecture (scheduled loop)

The system is designed as a five-stage pipeline run on a schedule (weekly decision cycle):

1. **Gather state** — pull current positions, cash, and P&L from the Alpaca API; fetch current prices and recent news for holdings and the watchlist.
2. **Decision** — send the full state to the Claude API (`/v1/messages`) with instructions to output trades as structured JSON, each with written reasoning.
3. **Validate** — code (not a human) checks every proposed trade against the guardrails below; invalid trades are rejected and logged.
4. **Execute** — place validated orders through the Alpaca trading API (paper first, then live).
5. **Log** — store the full prompt, Claude's response/reasoning, validation results, executed orders, and daily portfolio value in a local SQLite database.

Because Claude's training data is stale for markets, all market/news context must be injected fresh into the prompt each cycle rather than relied upon from model knowledge.

## Guardrails (must remain code-enforced, not advisory)

These are hard constraints the validation stage must check on every proposed trade before execution:

| Rule | Value |
|---|---|
| Max position size | 15% of portfolio per ticker |
| Universe | Long-only, whitelisted US equities/ETFs (e.g., S&P 1500) |
| Leverage / margin / options | Not permitted |
| Minimum cash buffer | 5% |
| Max trades per decision cycle | 5 |
| Trade frequency | Weekly (avoids PDT rules on accounts < $25k) |
| Ticker validation | Every symbol must be verified against the broker asset list before ordering |

When implementing or modifying the validation stage, treat these as invariants — a trade that fails any of them must be rejected and logged, never silently adjusted or passed through.

## Rollout phases

- **Phase 1 — Paper (4–8 weeks):** identical code path, Alpaca paper-trading keys. Goal: shake out bugs, verify logging, watch behavior before risking capital.
- **Phase 2 — Live (small):** swap to live API keys with a small, loss-tolerable amount of capital.
- **Phase 3 — Project layer:** performance dashboard vs. SPY, automated trade-reasoning writeups, optional auto-posting.

Code should generally support switching between paper and live via configuration/mode flag rather than via separate code paths (the README's example CLI is `python agent.py --mode paper`).

## Tech stack (planned)

- Python (Pandas / NumPy)
- Anthropic API (Claude) — decision engine, structured JSON output
- Alpaca API — brokerage: paper and live trading
- SQLite — decision and performance logging
- Cron / GitHub Actions — scheduling
- Market data: yfinance / Alpha Vantage (historical/supplemental prices), a news API or web search tool (recent headlines)

## Related project

A "companion stock-prediction project" is referenced as a future integration point (feeding quant model signals into Claude's context) — this is a separate repository, not part of this one.
