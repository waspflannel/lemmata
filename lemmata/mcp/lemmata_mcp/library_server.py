"""Library server: what is already known.

- declaration index over the pinned Mathlib (and its dependencies) plus lemmas proved in this
  project: full type, the import that provides it, and the revision it comes from.
  Loogle-style type-pattern search is lean_lsp's `lean_loogle` (local index, same toolchain);
  `decl_info` adds import and revision to its hits.
- Formal Conjectures (pinned commit), searched as source text.
- OEIS: local stripped/names data if downloaded, otherwise oeis.org over HTTP.
"""

from __future__ import annotations

import functools
import gzip
import json
import re
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import board
from .common import HOME, INDEX_DB, PINS, WORK_PROJECT

FORMAL_CONJECTURES = HOME / "formal-conjectures"
OEIS_DIR = HOME / "oeis"

server = MCPServer(
    "lemmata-library",
    instructions="Search Mathlib declarations, project lemmas, Formal Conjectures and the "
    "OEIS. For type-pattern queries use lean_lsp's lean_loogle, then decl_info here.",
)


# --- declaration index --------------------------------------------------------------------


def _package_revisions() -> dict[str, str]:
    """Module root (e.g. `Mathlib`, `Aesop`) -> `package@rev` from the work project's manifest."""
    manifest = json.loads((WORK_PROJECT / "lake-manifest.json").read_text())
    roots: dict[str, str] = {}
    for package in manifest["packages"]:
        directory = WORK_PROJECT / ".lake" / "packages" / package["name"]
        label = f"{package['name']}@{package['rev']}"
        for entry in directory.iterdir():
            if entry.suffix == ".lean" or (entry.is_dir() and not entry.name.startswith(".")):
                roots.setdefault(entry.stem, label)
    toolchain = PINS["LEAN_TOOLCHAIN"]
    for core in ("Init", "Std", "Lean", "Lake"):
        roots[core] = toolchain
    return roots


def build_index(jsonl: Path) -> None:
    """Build the SQLite FTS index from DumpDecls.lean output (setup-wsl.sh step `index`)."""
    INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
    INDEX_DB.unlink(missing_ok=True)
    roots = _package_revisions()
    db = sqlite3.connect(INDEX_DB)
    db.executescript(
        "CREATE VIRTUAL TABLE decls USING fts5(name, kind UNINDEXED, module UNINDEXED,"
        " type, revision UNINDEXED);"
        "CREATE TABLE names (name TEXT PRIMARY KEY, rowid_ INTEGER);"
    )
    with jsonl.open(encoding="utf-8") as lines:
        rows = []
        for line in lines:
            d = json.loads(line)
            revision = roots.get(d["m"].split(".")[0], "unknown")
            rows.append((d["n"], d["k"], d["m"], " ".join(d["t"].split()), revision))
    db.executemany("INSERT INTO decls VALUES (?, ?, ?, ?, ?)", rows)
    db.execute("INSERT OR IGNORE INTO names SELECT name, rowid FROM decls")
    db.commit()
    print(f"indexed {len(rows)} declarations into {INDEX_DB}")


@functools.cache
def _index() -> sqlite3.Connection:
    if not INDEX_DB.exists():
        raise RuntimeError("declaration index missing; run setup-wsl.sh index")
    db = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _decl(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": row["kind"],
        "type": row["type"],
        "import": f"import {row['module']}",
        "revision": row["revision"],
    }


def _project_decl(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "kind": "theorem",
        "type": row["type"],
        "import": f"use_lemmas=[{row['lemma_id']}]",
        "revision": f"lean:{row['source_sha']}",
        "problem": row["problem"],
    }


@server.tool()
def search_decls(query: str, limit: int = 20, include_project: bool = True) -> list[dict[str, Any]]:
    """Full-text search over declaration names and pretty-printed types.

    Words are prefix-matched and ANDed (`prime dvd mul`). Each hit has its full type, the
    import that provides it (or `use_lemmas` for project lemmas) and the pinned revision.
    """
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    if not words:
        return []
    fts = " ".join('"' + w.replace('"', '""') + '"*' for w in words)
    rows = (
        _index()
        .execute(
            "SELECT * FROM decls WHERE decls MATCH ? ORDER BY bm25(decls, 10.0, 0, 0, 1.0, 0)"
            " LIMIT ?",
            (fts, limit),
        )
        .fetchall()
    )
    hits = [_decl(r) for r in rows]
    if include_project:
        hits += [_project_decl(r) for r in board.search_project_decls(query.strip(), limit)]
    return hits


@server.tool()
def decl_info(names: list[str]) -> list[dict[str, Any]]:
    """Exact lookup: full type, import and revision for each name (Mathlib or project)."""
    out = []
    db = _index()
    for name in names:
        row = db.execute(
            "SELECT d.* FROM names n JOIN decls d ON d.rowid = n.rowid_ WHERE n.name = ?", (name,)
        ).fetchone()
        if row is not None:
            out.append(_decl(row))
            continue
        project = [r for r in board.search_project_decls(name, 50) if r["name"] == name]
        out.append(_project_decl(project[0]) if project else {"name": name, "found": False})
    return out


# --- Formal Conjectures ----------------------------------------------------------------------

_FC_DECL = re.compile(
    r"^(?P<doc>/--(?:.|\n)*?-/\n)?(?P<attr>@\[[^\]]*\]\n)?(?:theorem|lemma)\s+(?P<name>\S+)",
    re.MULTILINE,
)
_FC_STOP = re.compile(
    r"^(?:/--|@\[|theorem |lemma |def |end |namespace |section |open )", re.MULTILINE
)


@functools.cache
def _formal_conjectures() -> tuple[list[dict[str, Any]], str]:
    if not FORMAL_CONJECTURES.exists():
        raise RuntimeError("formal-conjectures missing; run setup-wsl.sh formal_conjectures")
    revision = subprocess.run(
        ["git", "-C", str(FORMAL_CONJECTURES), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    entries = []
    for path in sorted((FORMAL_CONJECTURES / "FormalConjectures").rglob("*.lean")):
        text = path.read_text(encoding="utf-8")
        for match in _FC_DECL.finditer(text):
            header_end = match.end()
            stop = _FC_STOP.search(text, header_end)
            body = text[match.start() : stop.start() if stop else len(text)].strip()
            category = re.search(r"category\s+([\w ]+?)(?:,|\])", match.group("attr") or "")
            entries.append(
                {
                    "name": match.group("name"),
                    "file": str(path.relative_to(FORMAL_CONJECTURES)),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "category": category.group(1).strip() if category else None,
                    "source": body[:6000],
                }
            )
    return entries, revision


@server.tool()
def formal_conjectures(query: str, limit: int = 10, category: str | None = None) -> dict[str, Any]:
    """Search google-deepmind/formal-conjectures (pinned commit) for statements.

    All query words must appear (case-insensitive) in the name, docstring, statement or file
    path. `category` filters on the @[category ...] tag, e.g. `research open`. The repo pins
    an older Lean than ours, so statements are text to adapt, not importable modules.
    """
    entries, revision = _formal_conjectures()
    words = query.lower().split()
    hits = [
        e
        for e in entries
        if all(w in f"{e['name']} {e['file']} {e['source']}".lower() for w in words)
        and (category is None or (e["category"] or "").startswith(category))
    ]
    return {"revision": revision, "total": len(hits), "results": hits[:limit]}


# --- OEIS -----------------------------------------------------------------------------------


@functools.cache
def _oeis_local() -> tuple[dict[str, str], dict[str, str]]:
    def load(name: str) -> dict[str, str]:
        out = {}
        with gzip.open(OEIS_DIR / name, "rt", encoding="utf-8") as lines:
            for line in lines:
                if line.startswith("A"):
                    key, _, rest = line.rstrip("\n").partition(" ")
                    out[key] = rest
        return out

    return load("names.gz"), load("stripped.gz")


def _oeis_http(query: str, limit: int) -> list[dict[str, Any]]:
    url = "https://oeis.org/search?" + urllib.parse.urlencode({"q": query, "fmt": "json"})
    request = urllib.request.Request(url, headers={"User-Agent": "lemmata-mcp"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response) or []
    results = data.get("results", []) if isinstance(data, dict) else data
    return [
        {"id": f"A{r['number']:06d}", "name": r["name"], "terms": r["data"]}
        for r in (results or [])[:limit]
    ]


@server.tool()
def oeis(query: str, limit: int = 10) -> dict[str, Any]:
    """Look up the OEIS by A-number (`A000045`), by terms (`1, 1, 2, 3, 5, 8`; matched as a
    contiguous subsequence) or by words in the sequence name."""
    if not (OEIS_DIR / "stripped.gz").exists():
        return {"source": "oeis.org", "results": _oeis_http(query, limit)}
    names, terms = _oeis_local()
    query = query.strip()
    if re.fullmatch(r"A\d{6}", query):
        ids = [query] if query in terms else []
    elif re.fullmatch(r"-?\d+(?:[\s,]+-?\d+)*", query):
        needle = "," + ",".join(re.split(r"[\s,]+", query)) + ","
        ids = [k for k, v in terms.items() if needle in v]
    else:
        words = query.lower().split()
        ids = [k for k, v in names.items() if all(w in v.lower() for w in words)]
    results = [
        {"id": k, "name": names.get(k, ""), "terms": terms.get(k, "").strip(",")[:400]}
        for k in ids[:limit]
    ]
    return {"source": "local", "total": len(ids), "results": results}
