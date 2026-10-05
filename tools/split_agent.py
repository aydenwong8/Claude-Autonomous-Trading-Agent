"""Move named top-level definitions out of agent.py into a new module, verbatim.

One-off tool for the 2026-10 module split (docs/superpowers/specs/
2026-10-04-agent-refactor-design.md). Each definition is copied byte-for-byte,
together with the comments and blank lines directly above it; only imports are
generated. agent.py then imports every moved name back, so `agent.X` keeps
working for dashboard.py, the tests and pickled fixtures.

Usage: python tools/split_agent.py <module> <name> [<name> ...]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent.py"

STDLIB_MODULES = [
    "argparse", "html", "json", "math", "hashlib", "os", "sqlite3",
    "statistics", "subprocess", "traceback",
]
FROM_IMPORTS = {
    "dataclasses": ["dataclass", "field", "replace"],
    "datetime": ["datetime", "timedelta", "timezone"],
    "collections.abc": ["Iterable"],
    "typing": ["Any"],
}
PROJECT_MODULES = ["config", "markettime"]
SPLIT_MODULES = [
    "models", "broker", "news", "screen", "prompt", "decide", "guardrails",
    "theses", "db",
]
DOCSTRINGS = {
    "models": "Data model shared across the pipeline: portfolio, market, theses, trades.",
    "broker": "Alpaca clients, account and pending-order state, and order execution.",
    "news": "Alpaca news: fetching, the news archive, and per-symbol news history.",
    "screen": "Market context and the candidate screen.",
    "prompt": "The system prompt and the per-cycle decision prompt.",
    "decide": "The Claude call and parsing of its response.",
    "guardrails": "Code-enforced guardrails: every proposed trade is validated here.",
    "theses": "Investment cases: open theses, action history, and the thesis lifecycle.",
    "db": "SQLite schema, migrations, and cycle/run logging.",
}


def defined_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def module_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {n for node in tree.body for n in defined_names(node)}


def main(module: str, names: list[str]) -> None:
    if module not in SPLIT_MODULES:
        sys.exit(f"unknown module {module!r}")
    target = ROOT / f"{module}.py"
    if target.exists():
        sys.exit(f"{target.name} already exists")

    src = AGENT.read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    # Each definition's chunk runs from the line after the previous top-level
    # statement to its own last line, so leading comments travel with it.
    spans: dict[str, tuple[int, int, ast.stmt]] = {}
    prev_end = 0
    for node in tree.body:
        for name in defined_names(node):
            spans[name] = (prev_end + 1, node.end_lineno, node)
        prev_end = node.end_lineno

    missing = [n for n in names if n not in spans]
    if missing:
        sys.exit(f"not top-level in agent.py: {missing}")
    names = sorted(names, key=lambda n: spans[n][0])  # keep file order

    used: set[str] = set()
    for n in names:
        used |= {x.id for x in ast.walk(spans[n][2]) if isinstance(x, ast.Name)}

    # Where each referenced agent-level name now lives.
    homes = {m: module_defs(ROOT / f"{m}.py") for m in SPLIT_MODULES
             if (ROOT / f"{m}.py").exists()}
    cross: dict[str, list[str]] = {}
    for name in sorted(used - set(names)):
        home = next((m for m, defs in homes.items() if name in defs), None)
        if home is not None:
            cross.setdefault(home, []).append(name)
        elif name in spans:
            sys.exit(f"{name!r} is still in agent.py; move its module first")

    header = [f'"""{DOCSTRINGS[module]}"""\n', "\n", "from __future__ import annotations\n", "\n"]
    std = [m for m in STDLIB_MODULES if m in used]
    frm = [(mod, [n for n in ns if n in used]) for mod, ns in FROM_IMPORTS.items()]
    frm = [(mod, ns) for mod, ns in frm if ns]
    header += [f"import {m}\n" for m in sorted(std)]
    header += [f"from {mod} import {', '.join(ns)}\n" for mod, ns in frm]
    proj = [m for m in PROJECT_MODULES if m in used]
    if proj or cross:
        if std or frm:
            header.append("\n")
        header += [f"import {m}\n" for m in proj]
        for home in SPLIT_MODULES:
            if home in cross:
                header.append(f"from {home} import {', '.join(cross[home])}\n")

    body: list[str] = []
    for n in names:
        start, end, _ = spans[n]
        body += lines[start - 1:end]
    while body and not body[0].strip():
        body.pop(0)
    target.write_text("".join(header) + "\n\n" + "".join(body).rstrip("\n") + "\n")

    # Remove the chunks from agent.py and import the names back.
    drop = {i for n in names for i in range(spans[n][0] - 1, spans[n][1])}
    kept = [line for i, line in enumerate(lines) if i not in drop]
    anchor = kept.index("import markettime\n") + 1
    while kept[anchor].startswith("from ") and " import (" in kept[anchor]:
        while kept[anchor] != ")\n":
            anchor += 1
        anchor += 1
    block = [f"from {module} import (  # re-exported: agent.X must keep working\n"]
    block += [f"    {n},\n" for n in names]
    block.append(")\n")
    kept[anchor:anchor] = block
    AGENT.write_text("".join(kept))
    print(f"moved {len(names)} names to {target.name}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])
