"""Static configuration for the trading agent: guardrail thresholds, model choice,
and the tradable universe.

The starter WATCHLIST below is a small placeholder (mega-cap names + broad ETFs),
not the full S&P 1500 referenced in README.md. Expand it before running for real.
"""

# --- Guardrails (README.md#guardrails) ---------------------------------------
MAX_POSITION_PCT = 0.15        # max 15% of portfolio per ticker
MIN_CASH_BUFFER_PCT = 0.05     # keep at least 5% of equity in cash
MAX_TRADES_PER_CYCLE = 5       # reject proposed trades beyond this count

# --- Claude decision engine ---------------------------------------------------
CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_MAX_TOKENS = 2048

# --- Tradable universe (long-only, whitelisted US equities/ETFs) -------------
WATCHLIST = [
    "SPY", "QQQ",
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META",
    "JPM", "V", "UNH", "JNJ", "PG", "XOM",
]

# --- News lookup ---------------------------------------------------------------
NEWS_HEADLINES_PER_SYMBOL = 3

# --- Storage --------------------------------------------------------------------
DEFAULT_DB_PATH = "trading_agent.db"
