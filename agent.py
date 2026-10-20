"""
Claude Autonomous Trading Agent — decision loop.

Pipeline (see README.md#architecture):
    1. Gather state   — positions, cash, prices, news
    2. Decision        — Claude proposes trades as structured JSON
    3. Validate         — code-enforced guardrails (README.md#guardrails)
    4. Execute          — place validated orders via Alpaca (paper or live)
    5. Log              — persist prompt, response, validation, orders, P&L to SQLite
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import traceback
from datetime import datetime

from dotenv import load_dotenv

import config
import markettime
from fundamentals import fetch_fundamentals
from performance import fetch_performance
from models import (  # re-exported: agent.X must keep working
    Position,
    PendingOrder,
    PortfolioState,
    NewsArticle,
    MarketContext,
    Thesis,
    PositionReview,
    ProposedTrade,
    ValidatedTrade,
    AgentVersion,
)
from broker import (  # re-exported: agent.X must keep working
    _trading_client,
    _data_client,
    _news_client,
    fetch_pending_orders,
    price_pending_orders,
    fetch_portfolio_state,
    _client_order_id,
    execute_trades,
)
from news import (  # re-exported: agent.X must keep working
    _news_symbol,
    _fetch_articles,
    store_news,
    _spread_by_day,
    _clip,
    _archived_news,
    fetch_candidate_news,
    fetch_drawdown_news,
    fetch_news_history,
)
from screen import (  # re-exported: agent.X must keep working
    fetch_market_context,
    _pct_change,
    compute_screen_stats,
    select_candidates,
)
from prompt import (  # re-exported: agent.X must keep working
    SYSTEM_PROMPT,
    build_decision_prompt,
)
from decide import (  # re-exported: agent.X must keep working
    _parse_trades_response,
    get_claude_decision,
)
from guardrails import (  # re-exported: agent.X must keep working
    validate_trades,
)
from theses import (  # re-exported: agent.X must keep working
    fetch_open_theses,
    fetch_action_history,
    CASE_FIELDS,
    _full_case,
    apply_thesis_lifecycle,
    ORPHAN_EXIT_TRIGGER,
    _close_orphaned_theses,
)
from db import (  # re-exported: agent.X must keep working
    PROMPT_LOG_DIR,
    init_db,
    _migrate_cycle_usage,
    _migrate_cycle_version,
    _migrate_thesis_revisions,
    log_run,
    log_decision_cycle,
    write_prompt_log,
)

load_dotenv()


def current_agent_version() -> AgentVersion:
    """Best-effort: a missing git binary or repo yields None, never a failure."""
    repo = os.path.dirname(os.path.abspath(__file__))

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True, text=True, timeout=10, check=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip()

    commit = git("rev-parse", "--short=12", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no", "--", "*.py")
    return AgentVersion(
        code_commit=commit,
        code_dirty=None if status is None else bool(status),
        system_prompt_sha=hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12],
        model=config.CLAUDE_MODEL,
        effort=config.CLAUDE_EFFORT,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fundamentals_for(conn, portfolio, market, now: datetime) -> dict[str, dict]:
    """Fundamentals for every name in the prompt: holdings and candidates.

    Never fails the cycle. fetch_fundamentals already turns data and network
    problems into per-name `lookup_failed`; this also covers a bug in it, so
    the agent decides on prices and news alone rather than not at all.
    """
    symbols = sorted(set(portfolio.positions) | set(market.candidates))
    (last,) = conn.execute("SELECT MAX(timestamp) FROM decision_cycles").fetchone()
    last_cycle_at = datetime.fromisoformat(last) if last else None
    try:
        blocks = fetch_fundamentals(symbols, last_cycle_at, now,
                                    held=set(portfolio.positions))
    except Exception:
        traceback.print_exc()
        return {s: {"lookup_failed": {"fundamentals": "internal error"}} for s in symbols}
    flagged = sum("lookup_failed" in b for b in blocks.values())
    print(f"fundamentals: {len(blocks)} names, {flagged} with a failed lookup")
    return blocks


def run_decision_cycle(mode: str, watchlist: list[str], db_path: str) -> None:
    """Run one full gather -> decide -> validate -> execute -> log cycle."""
    conn = init_db(db_path)
    cycles_completed = 0
    # Taken before anything runs, so an edit made mid-cycle cannot relabel it.
    version = current_agent_version()
    try:
        now = markettime.now_utc()
        portfolio = fetch_portfolio_state(mode)
        market = fetch_market_context(watchlist, portfolio)
        # Needs prices, so it runs here rather than in fetch_portfolio_state.
        # Must precede both the prompt and validation — they read the result.
        price_pending_orders(portfolio, market.prices)
        news_added = store_news(conn, market.articles, now)
        news_history = fetch_news_history(
            conn, portfolio.positions, now, market.news_lookup_failed,
        )
        candidate_news = fetch_candidate_news(
            conn, market.candidates, now, market.news_lookup_failed,
        )
        drawdown_news = fetch_drawdown_news(
            conn, market.drawdown_windows, market.drawdown_lookup_failed,
        )
        theses = fetch_open_theses(conn)
        actions = fetch_action_history(conn)
        fundamentals = fundamentals_for(conn, portfolio, market, now)
        performance = fetch_performance(conn, mode, now)
        prompt = build_decision_prompt(
            portfolio, market, theses, actions, news_history, candidate_news, now,
            fundamentals, drawdown_news, performance,
        )
        trades, reviews, raw_response, usage = get_claude_decision(prompt)
        validated = validate_trades(trades, portfolio, mode)
        execute_trades(validated, mode, markettime.fmt_et_date(now))
        # Logged before the lifecycle runs so the audit record of what was
        # decided exists before any state derived from it, and so revisions can
        # name the cycle that made them.
        cycle_id = log_decision_cycle(
            conn, prompt, raw_response, validated, reviews, portfolio, now, mode,
            usage, version,
        )
        thesis_counts = apply_thesis_lifecycle(
            conn, validated, reviews, portfolio, now, cycle_id,
        )
        cycles_completed = 1
        prompt_path = write_prompt_log(cycle_id, prompt, mode, now, version)
        _print_cycle_summary(
            cycle_id, now, validated, reviews, thesis_counts, portfolio, prompt_path,
            news_added,
        )
    except Exception:
        # Record the failure before the exception propagates and exits non-zero.
        log_run(conn, "failed", traceback.format_exc(), cycles_completed)
        raise
    else:
        log_run(conn, "success", None, cycles_completed)
    finally:
        conn.close()


def _print_cycle_summary(
    cycle_id: int,
    timestamp: datetime,
    validated: list[ValidatedTrade],
    reviews: list[PositionReview],
    thesis_counts: dict[str, int],
    portfolio: PortfolioState,
    prompt_path: str,
    news_added: int,
) -> None:
    """Five-line cycle summary — this is what lands in the launchd log.

    A count, not a payload. The reasoning behind these numbers lives in the
    database (`python report.py`) and the prompt file; a scheduled run's log
    only needs to answer "did it run, and did anything happen".
    """
    rejected = sum(1 for v in validated if not v.is_valid)
    failed = sum(1 for v in validated if v.submit_failed)
    print(f"cycle {cycle_id}  {markettime.fmt_et(timestamp)}")
    print(f"  positions reviewed      {len(reviews)}")
    print(f"  trades proposed         {len(validated)}")
    print(f"  rejected by guardrails  {rejected}")
    # A validated trade the broker refused is not a no-op — surface it here or
    # it is only visible by reading the trades table.
    if failed:
        print(f"  FAILED AT BROKER        {failed}")
    in_flight = sum(len(v) for v in portfolio.pending_orders.values())
    if in_flight:
        # Normally zero: the previous cycle's orders fill before this one runs.
        print(f"  orders in flight        {in_flight} (unfilled, from an earlier run)")
    changed = {
        k: n for k, n in thesis_counts.items()
        if n and k not in ("incomplete", "orphaned")
    }
    if changed:
        print("  theses                  "
              + ", ".join(f"{n} {k}" for k, n in changed.items()))
    # A case for something the account does not own, with nothing in flight to
    # explain it — closed here, but worth seeing rather than inferring.
    if thesis_counts.get("orphaned"):
        print(f"  ORPHANED CASES CLOSED   {thesis_counts['orphaned']}")
    # A case the model tried to rewrite and couldn't is a prompt problem, not a
    # quiet no-op: it will keep reasoning against wording it thinks it changed.
    if thesis_counts.get("incomplete"):
        print(f"  INCOMPLETE CASE IGNORED {thesis_counts['incomplete']}")
    print(f"  news archived           {news_added} new article{'s' if news_added != 1 else ''}")
    print(f"  prompt                  {prompt_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Autonomous Trading Agent")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode: paper (default) or live.",
    )
    parser.add_argument(
        "--db-path",
        default=config.DEFAULT_DB_PATH,
        help=f"Path to the SQLite log database (default: {config.DEFAULT_DB_PATH}).",
    )
    args = parser.parse_args()

    # Fail before any broker or Claude call, so a mistaken `--mode live` costs
    # nothing and prints the reason rather than a client error mid-cycle.
    config.assert_mode_allowed(args.mode)

    run_decision_cycle(mode=args.mode, watchlist=config.WATCHLIST, db_path=args.db_path)


if __name__ == "__main__":
    main()
