"""Read the decision log and print a card per position: thesis, verdicts, trades.

Answers the question the raw tables make tedious — "why does the agent own
this, and does it still think so?" — by stitching together the three places
that knowledge lives:

    theses            the standing case: why we own it, target, invalidation
    position_reviews  the per-cycle verdict on whether that case still holds
    trades            what was actually proposed, and why, and if it survived
                      the guardrails

Read-only, database-only: no broker calls, no network, safe to run mid-cycle.

Run: python report.py [SYMBOL ...] [--closed] [--all] [--cycles N] [--db-path ...]
"""

from __future__ import annotations

import argparse
import sqlite3
import textwrap

import config
import markettime

WIDTH = 88
LABEL_WIDTH = 14
DEFAULT_VERDICT_HISTORY = 3


def _wrap(text: str, label: str = "", indent: int = 2) -> str:
    """Label in the left gutter, text wrapped in a hanging block beside it."""
    pad = " " * indent
    body = textwrap.wrap(" ".join((text or "—").split()), WIDTH - indent - LABEL_WIDTH)
    if not body:
        body = ["—"]
    head = f"{pad}{label:<{LABEL_WIDTH}}{body[0]}"
    rest = [f"{pad}{'':<{LABEL_WIDTH}}{line}" for line in body[1:]]
    return "\n".join([head, *rest])


def fetch_positions(
    conn: sqlite3.Connection, symbols: list[str], include_closed: bool
) -> list[dict]:
    """Theses to report on, open first, most recently opened first within each."""
    where, params = [], []
    if not include_closed:
        where.append("status = 'open'")
    if symbols:
        where.append(f"symbol IN ({','.join('?' * len(symbols))})")
        params.extend(symbols)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""SELECT symbol, opened_at, thesis, target, invalidation,
                   status, closed_at, exit_trigger
            FROM theses {clause}
            ORDER BY status = 'closed', opened_at DESC""",
        params,
    ).fetchall()
    keys = (
        "symbol", "opened_at", "thesis", "target", "invalidation",
        "status", "closed_at", "exit_trigger",
    )
    return [dict(zip(keys, r)) for r in rows]


def fetch_verdicts(conn: sqlite3.Connection, symbol: str, limit: int) -> list[dict]:
    """Most recent review verdicts for a symbol, oldest-first for reading."""
    rows = conn.execute(
        """SELECT c.timestamp, r.verdict, r.note
           FROM position_reviews r JOIN decision_cycles c ON c.id = r.cycle_id
           WHERE r.symbol = ? ORDER BY c.id DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    return [
        {"timestamp": t, "verdict": v, "note": n}
        for t, v, n in reversed(rows)
    ]


def fetch_trades(conn: sqlite3.Connection, symbol: str) -> list[dict]:
    """Every trade ever proposed for a symbol, including rejected ones.

    Rejections are the point of keeping these: a trade the guardrails stopped
    is a decision the agent made, and it should stay visible.
    """
    rows = conn.execute(
        """SELECT c.timestamp, t.side, t.notional_usd, t.is_valid,
                  t.rejection_reason, t.order_status, t.reasoning
           FROM trades t JOIN decision_cycles c ON c.id = t.cycle_id
           WHERE t.symbol = ? ORDER BY c.id""",
        (symbol,),
    ).fetchall()
    keys = (
        "timestamp", "side", "notional_usd", "is_valid",
        "rejection_reason", "order_status", "reasoning",
    )
    return [dict(zip(keys, r)) for r in rows]


def fetch_untracked(conn: sqlite3.Connection) -> list[str]:
    """Symbols reviewed in the latest cycle that have no open thesis.

    Should normally be empty — the agent is instructed to adopt or exit
    anything untracked — so a non-empty list is worth surfacing.
    """
    rows = conn.execute(
        """SELECT DISTINCT r.symbol FROM position_reviews r
           WHERE r.cycle_id = (SELECT MAX(id) FROM decision_cycles)
             AND r.symbol NOT IN (SELECT symbol FROM theses WHERE status = 'open')
           ORDER BY r.symbol"""
    ).fetchall()
    return [r[0] for r in rows]


def render_card(conn: sqlite3.Connection, position: dict, verdict_limit: int) -> str:
    """One position's full story: header, standing case, verdicts, trades."""
    symbol = position["symbol"]
    opened = markettime.fmt_et(position["opened_at"])
    lines = ["─" * WIDTH]

    if position["status"] == "open":
        held = markettime.to_eastern(position["opened_at"])
        days = (markettime.now_eastern() - held).days if held else "?"
        lines.append(f"{symbol}   open · opened {opened} · {days}d held")
    else:
        lines.append(
            f"{symbol}   CLOSED {markettime.fmt_et(position['closed_at'])} · "
            f"trigger: {position['exit_trigger'] or '—'} · opened {opened}"
        )

    lines.append("")
    lines.append(_wrap(position["thesis"], "Thesis"))
    lines.append(_wrap(position["target"], "Target"))
    lines.append(_wrap(position["invalidation"], "Invalidation"))

    verdicts = fetch_verdicts(conn, symbol, verdict_limit)
    if verdicts:
        lines.append("")
        lines.append(f"  Verdicts (last {len(verdicts)})")
        for v in verdicts:
            lines.append(
                f"    {markettime.fmt_et(v['timestamp'])}  {(v['verdict'] or '?').upper()}"
            )
            lines.append(_wrap(v["note"], indent=6))

    trades = fetch_trades(conn, symbol)
    if trades:
        lines.append("")
        lines.append("  Trades")
        for t in trades:
            if not t["is_valid"]:
                status = f"REJECTED — {t['rejection_reason']}"
            else:
                status = str(t["order_status"] or "no order").replace("OrderStatus.", "")
            lines.append(
                f"    {markettime.fmt_et(t['timestamp'])}  "
                f"{t['side'].upper():<4} ${t['notional_usd']:>10,.2f}  {status}"
            )
            lines.append(_wrap(t["reasoning"], indent=6))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-position report from the agent's decision log."
    )
    parser.add_argument("symbols", nargs="*", help="Limit to these tickers (default: all).")
    parser.add_argument(
        "--closed", action="store_true", help="Show closed positions instead of open ones."
    )
    parser.add_argument(
        "--all", action="store_true", help="Show both open and closed positions."
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=DEFAULT_VERDICT_HISTORY,
        help=f"Verdicts of history per position (default: {DEFAULT_VERDICT_HISTORY}).",
    )
    parser.add_argument("--db-path", default=config.DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    try:
        symbols = [s.upper() for s in args.symbols]
        positions = fetch_positions(conn, symbols, include_closed=args.closed or args.all)
        if args.closed and not args.all:
            positions = [p for p in positions if p["status"] == "closed"]

        if not positions:
            scope = "closed" if args.closed and not args.all else "open"
            print(f"No {scope} positions" + (f" matching {', '.join(symbols)}" if symbols else ""))
            return

        for p in positions:
            print(render_card(conn, p, args.cycles))

        print("─" * WIDTH)
        print(f"{len(positions)} position(s) · as of {markettime.fmt_et(markettime.now_utc())}")

        untracked = fetch_untracked(conn)
        if untracked and not symbols:
            print(f"Untracked (reviewed, no stored thesis): {', '.join(untracked)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
