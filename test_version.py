"""Tests for per-cycle agent versioning — in-memory SQLite, no broker, no API."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import agent
import config


NOW = datetime(2026, 9, 22, 20, 35, tzinfo=timezone.utc)


def _portfolio() -> agent.PortfolioState:
    return agent.PortfolioState(cash=5_000.0, equity=100_000.0, positions={}, timestamp=NOW)


def test_current_version_describes_this_checkout():
    v = agent.current_agent_version()
    # Run from the repo, so git is available and the commit resolves.
    assert v.code_commit and len(v.code_commit) == 12
    assert v.code_dirty in (True, False)
    assert len(v.system_prompt_sha) == 12
    assert (v.model, v.effort) == (config.CLAUDE_MODEL, config.CLAUDE_EFFORT)


def test_prompt_hash_tracks_the_system_prompt(monkeypatch):
    before = agent.current_agent_version().system_prompt_sha
    monkeypatch.setattr(agent, "SYSTEM_PROMPT", agent.SYSTEM_PROMPT + " ")
    assert agent.current_agent_version().system_prompt_sha != before


def test_version_is_recorded_on_the_cycle():
    conn = agent.init_db(":memory:")
    version = agent.AgentVersion("abc123def456", True, "0123456789ab", "m", "high")
    cycle_id = agent.log_decision_cycle(
        conn, "p", "r", [], [], _portfolio(), NOW, "paper", (1, 2), version,
    )
    row = conn.execute(
        """SELECT code_commit, code_dirty, system_prompt_sha, model, effort
           FROM decision_cycles WHERE id = ?""",
        (cycle_id,),
    ).fetchone()
    assert row == ("abc123def456", 1, "0123456789ab", "m", "high")


def test_unknown_version_is_null_not_guessed():
    conn = agent.init_db(":memory:")
    cycle_id = agent.log_decision_cycle(conn, "p", "r", [], [], _portfolio(), NOW, "paper")
    row = conn.execute(
        "SELECT code_commit, code_dirty, model FROM decision_cycles WHERE id = ?",
        (cycle_id,),
    ).fetchone()
    assert row == (None, None, None)


def test_migration_adds_columns_to_an_old_table_and_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE decision_cycles (
               id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
               mode TEXT NOT NULL, prompt TEXT NOT NULL, claude_response TEXT NOT NULL,
               portfolio_cash REAL NOT NULL, portfolio_equity REAL NOT NULL)"""
    )
    agent._migrate_cycle_usage(conn)
    agent._migrate_cycle_version(conn)
    agent._migrate_cycle_version(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_cycles)")}
    assert {"code_commit", "code_dirty", "system_prompt_sha", "model", "effort"} <= columns


def test_prompt_log_header_names_the_version(tmp_path):
    version = agent.AgentVersion("abc123def456", True, "0123456789ab", "m", "high")
    path = agent.write_prompt_log(7, "user prompt", "paper", NOW, version, str(tmp_path))
    text = open(path).read()
    assert "code      abc123def456 (uncommitted edits)" in text
    assert "sysprompt 0123456789ab" in text
    assert "=== SYSTEM PROMPT ===" in text and text.endswith("user prompt\n")
