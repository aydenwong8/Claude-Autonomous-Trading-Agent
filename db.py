"""SQLite schema, migrations, and cycle/run logging."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

import config
import markettime
from models import AgentVersion, PortfolioState, PositionReview, ValidatedTrade
from prompt import SYSTEM_PROMPT


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
