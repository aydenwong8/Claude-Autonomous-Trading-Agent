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
import html
import json
import math
import hashlib
import os
import sqlite3
import statistics
import subprocess
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from collections.abc import Iterable
from typing import Any

from dotenv import load_dotenv

import config
import markettime
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

load_dotenv()

# Prompts are large and belong in files, not in the console or a `sqlite3`
# one-liner's output. One plain-text file per cycle, named by cycle id.
PROMPT_LOG_DIR = os.path.join("logs", "prompts")


# ---------------------------------------------------------------------------
# Stage 5: Log
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the local SQLite database and ensure schema exists."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            prompt TEXT NOT NULL,
            claude_response TEXT NOT NULL,
            portfolio_cash REAL NOT NULL,
            portfolio_equity REAL NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            code_commit TEXT,
            code_dirty INTEGER,
            system_prompt_sha TEXT,
            model TEXT,
            effort TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL REFERENCES decision_cycles(id),
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            notional_usd REAL NOT NULL,
            reasoning TEXT,
            is_valid INTEGER NOT NULL,
            rejection_reason TEXT,
            order_id TEXT,
            order_status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS theses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            thesis TEXT NOT NULL,
            target TEXT NOT NULL,
            invalidation TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            closed_at TEXT,
            exit_trigger TEXT,
            updated_at TEXT,
            revision INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    # Every version a case has ever had, including the one it was opened with.
    # `theses` holds only the current wording; without this table a re-anchor
    # would quietly rewrite history and the record would show a position that
    # had always been owned for whatever reason it is owned for today.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thesis_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            cycle_id INTEGER REFERENCES decision_cycles(id),
            revised_at TEXT NOT NULL,
            revision INTEGER NOT NULL,
            -- 'open' | 'buy_refresh' | 'adopt' | 'review_reanchor'
            source TEXT NOT NULL,
            thesis TEXT NOT NULL,
            target TEXT NOT NULL,
            invalidation TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL REFERENCES decision_cycles(id),
            symbol TEXT NOT NULL,
            verdict TEXT NOT NULL,
            note TEXT
        )
        """
    )
    # Every invocation writes exactly one row here, success or failure, so a run
    # that dies before logging a decision cycle still leaves a trace — silence
    # from a scheduled run is otherwise indistinguishable from a no-trade day.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            outcome TEXT NOT NULL,
            error_text TEXT,
            cycles_completed INTEGER NOT NULL
        )
        """
    )
    # Every news article the agent has been shown or could have been, keyed by
    # Alpaca's article id so a story fetched on ten days is one row. The prompt
    # reads a holding's news back from here, not from the day's fetch — this is
    # what lets the model tell a story it already reacted to from a new one.
    # `first_seen_at` is audit only; "new" is decided by publication time.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY,
            published_at TEXT NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT,
            source TEXT,
            url TEXT,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_symbols (
            article_id INTEGER NOT NULL REFERENCES news_articles(id),
            symbol TEXT NOT NULL,
            PRIMARY KEY (symbol, article_id)
        )
        """
    )
    # One open thesis per symbol is an invariant the lifecycle below relies on.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS theses_one_open_per_symbol
        ON theses (symbol) WHERE status = 'open'
        """
    )
    _migrate_thesis_revisions(conn)
    _migrate_cycle_usage(conn)
    _migrate_cycle_version(conn)
    conn.commit()
    return conn


def _migrate_cycle_usage(conn: sqlite3.Connection) -> None:
    """Add token-usage columns to a pre-usage `decision_cycles` table.

    Left NULL for cycles that ran before usage was recorded — those costs are
    not recoverable, and a zero would read as "this cycle was free" rather than
    "we were not counting yet".
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_cycles)")}
    if "input_tokens" in columns:
        return
    conn.execute("ALTER TABLE decision_cycles ADD COLUMN input_tokens INTEGER")
    conn.execute("ALTER TABLE decision_cycles ADD COLUMN output_tokens INTEGER")


def _migrate_cycle_version(conn: sqlite3.Connection) -> None:
    """Add agent-version columns to a pre-versioning `decision_cycles` table.

    Left NULL for earlier cycles rather than backfilled from git history: a
    cycle's timestamp says which commit was checked out only if the tree was
    clean, and it was not always — cycle 43 ran on the candidate screen before
    that code was committed. A guessed version is worse than an honest gap.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_cycles)")}
    for name, kind in (
        ("code_commit", "TEXT"),
        ("code_dirty", "INTEGER"),
        ("system_prompt_sha", "TEXT"),
        ("model", "TEXT"),
        ("effort", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE decision_cycles ADD COLUMN {name} {kind}")


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


def _migrate_thesis_revisions(conn: sqlite3.Connection) -> None:
    """Bring a pre-revision `theses` table up to the current schema.

    CREATE TABLE IF NOT EXISTS is a no-op on an existing table, so databases
    created before case re-anchoring existed need the new columns added and
    their current wording backfilled as revision 1 — otherwise the first
    re-anchor would look like the second version of a case with no first.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(theses)")}
    if "revision" in columns:
        return
    conn.execute("ALTER TABLE theses ADD COLUMN updated_at TEXT")
    conn.execute("ALTER TABLE theses ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    # cycle_id is left null: these predate the trail, and the cycle that opened
    # them is recoverable from opened_at if it is ever needed.
    conn.execute(
        """
        INSERT INTO thesis_revisions
            (symbol, cycle_id, revised_at, revision, source, thesis, target, invalidation)
        SELECT symbol, NULL, opened_at, 1, 'open', thesis, target, invalidation
        FROM theses
        """
    )


def log_run(
    conn: sqlite3.Connection,
    outcome: str,
    error_text: str | None,
    cycles_completed: int,
    timestamp: datetime | None = None,
) -> None:
    """Write one run_log row recording how this invocation ended.

    Committed immediately: on the failure path this must be durable before the
    process exits non-zero.
    """
    conn.execute(
        """
        INSERT INTO run_log (timestamp, outcome, error_text, cycles_completed)
        VALUES (?, ?, ?, ?)
        """,
        (
            (timestamp or markettime.now_utc()).isoformat(),
            outcome,
            error_text,
            cycles_completed,
        ),
    )
    conn.commit()


def log_decision_cycle(
    conn: sqlite3.Connection,
    prompt: str,
    claude_response: str,
    validated_trades: list[ValidatedTrade],
    reviews: list[PositionReview],
    portfolio: PortfolioState,
    timestamp: datetime,
    mode: str,
    usage: tuple[int | None, int | None] = (None, None),
    version: AgentVersion | None = None,
) -> int:
    """Persist the full prompt, Claude's response/reasoning, validation results,
    executed orders, and portfolio value for this cycle.

    `usage` is (input_tokens, output_tokens) as reported by the API. Output
    includes thinking tokens, which are billed but never appear in
    `claude_response` — without recording them here the cycle's real cost can
    only be guessed at, and effort tuning has nothing to measure against.

    Returns the new cycle id, which names this cycle's prompt log file.
    """
    cursor = conn.execute(
        """
        INSERT INTO decision_cycles (
            timestamp, mode, prompt, claude_response, portfolio_cash,
            portfolio_equity, input_tokens, output_tokens,
            code_commit, code_dirty, system_prompt_sha, model, effort
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp.isoformat(), mode, prompt, claude_response,
            portfolio.cash, portfolio.equity, usage[0], usage[1],
            version.code_commit if version else None,
            (None if version is None or version.code_dirty is None
             else int(version.code_dirty)),
            version.system_prompt_sha if version else None,
            version.model if version else None,
            version.effort if version else None,
        ),
    )
    cycle_id = cursor.lastrowid

    for v in validated_trades:
        conn.execute(
            """
            INSERT INTO trades (
                cycle_id, symbol, side, notional_usd, reasoning,
                is_valid, rejection_reason, order_id, order_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                v.trade.symbol,
                v.trade.side,
                v.trade.notional_usd,
                v.trade.reasoning,
                int(v.is_valid),
                v.rejection_reason,
                v.order["order_id"] if v.order else None,
                v.order["status"] if v.order else None,
            ),
        )

    for r in reviews:
        conn.execute(
            "INSERT INTO position_reviews (cycle_id, symbol, verdict, note) VALUES (?, ?, ?, ?)",
            (cycle_id, r.symbol, r.verdict, r.note),
        )
    conn.commit()
    return cycle_id


def write_prompt_log(
    cycle_id: int,
    prompt: str,
    mode: str,
    timestamp: datetime,
    version: AgentVersion | None = None,
    log_dir: str = PROMPT_LOG_DIR,
) -> str:
    """Write this cycle's prompt to logs/prompts/{cycle_id}.txt and return the path.

    Plain text, no ANSI — this is meant to be opened, diffed, and grepped long
    after the run, where escape codes would be noise. The database still holds
    the authoritative copy (README.md#architecture); this is the readable one.
    """
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{cycle_id}.txt")
    with open(path, "w") as f:
        f.write(
            f"cycle     {cycle_id}\n"
            f"timestamp {markettime.fmt_et(timestamp)}\n"
            f"mode      {mode}\n"
            f"model     {config.CLAUDE_MODEL}\n"
            f"effort    {config.CLAUDE_EFFORT}\n"
            + (
                f"code      {version.code_commit}"
                f"{' (uncommitted edits)' if version.code_dirty else ''}\n"
                f"sysprompt {version.system_prompt_sha}\n"
                if version else ""
            )
            + "\n"
            "=== SYSTEM PROMPT ===\n"
            f"{SYSTEM_PROMPT}\n"
            "\n"
            "=== USER PROMPT ===\n"
            f"{prompt}\n"
        )
    return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

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
        theses = fetch_open_theses(conn)
        actions = fetch_action_history(conn)
        prompt = build_decision_prompt(
            portfolio, market, theses, actions, news_history, candidate_news, now,
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
