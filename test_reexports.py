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
