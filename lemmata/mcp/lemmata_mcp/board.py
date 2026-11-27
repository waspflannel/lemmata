"""Structured whiteboard shared by every worker thread: one SQLite file in WAL mode.

Three records:
- lemma DAG: statement, deps, owner, status, proof ref. Only the lean server may set
  `proved` (after checking the proof); workers cannot claim it through this server.
- dead ends: normalized goal, library pin, tactics/premises tried, budget, outcome.
- refutation coverage: generator + version, bounds, shard, symmetry assumptions, and an
  outcome that keeps `timed_out`, `no_witness_in_domain` and `lemma_refuted` apart.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from .common import PINS, STATE, sha256

DB_PATH = STATE / "whiteboard.db"

LEMMA_STATUSES = ("open", "claimed", "proved", "refuted", "abandoned")
WORKER_LEMMA_STATUSES = ("open", "claimed", "abandoned")
DEAD_END_OUTCOMES = ("timed_out", "failed", "error")
COVERAGE_OUTCOMES = ("timed_out", "no_witness_in_domain", "lemma_refuted", "error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lemmas (
  id INTEGER PRIMARY KEY,
  problem TEXT NOT NULL,
  name TEXT NOT NULL,
  statement TEXT NOT NULL,
  lean_statement TEXT,
  deps TEXT NOT NULL DEFAULT '[]',
  owner TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  proof_ref TEXT,
  lean_source TEXT,
  updated_at REAL NOT NULL,
  UNIQUE (problem, name)
);
CREATE TABLE IF NOT EXISTS project_decls (
  name TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  lemma_id INTEGER NOT NULL REFERENCES lemmas(id),
  source_sha TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_ends (
  id INTEGER PRIMARY KEY,
  problem TEXT NOT NULL,
  lemma_id INTEGER,
  goal TEXT NOT NULL,
  goal_hash TEXT NOT NULL,
  library_pin TEXT NOT NULL,
  tactics TEXT NOT NULL,
  premises TEXT NOT NULL,
  budget TEXT NOT NULL,
  outcome TEXT NOT NULL,
  note TEXT,
  owner TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dead_ends_goal ON dead_ends (goal_hash);
CREATE TABLE IF NOT EXISTS coverage (
  id INTEGER PRIMARY KEY,
  problem TEXT NOT NULL,
  lemma_id INTEGER,
  generator TEXT NOT NULL,
  generator_version TEXT NOT NULL,
  bounds TEXT NOT NULL,
  shard TEXT NOT NULL,
  symmetry TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reached TEXT,
  witness TEXT,
  artifact TEXT,
  owner TEXT,
  created_at REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    return db


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    out = []
    for row in cursor.fetchall():
        item = dict(row)
        for key in (
            "deps",
            "tactics",
            "premises",
            "budget",
            "bounds",
            "shard",
            "symmetry",
            "reached",
            "witness",
        ):
            if isinstance(item.get(key), str):
                item[key] = json.loads(item[key])
        out.append(item)
    return out


def normalize_goal(goal: str) -> str:
    """Whitespace-insensitive, with inaccessible-name daggers (`x✝¹`) collapsed."""
    goal = re.sub(r"✝[⁰¹²³⁴⁵⁶⁷⁸⁹]*", "✝", goal)
    return " ".join(goal.split())


def library_pin() -> str:
    return f"lean={PINS['LEAN_TOOLCHAIN']} mathlib={PINS['MATHLIB_REV']}"


def _check_deps(db: sqlite3.Connection, problem: str, lemma_id: int | None, deps: list[int]):
    """Deps must exist in the same problem and keep the graph acyclic."""
    for dep in deps:
        row = db.execute("SELECT problem FROM lemmas WHERE id = ?", (dep,)).fetchone()
        if row is None or row["problem"] != problem:
            raise ValueError(f"dependency {dep} is not a lemma of problem {problem!r}")
    if lemma_id is None:
        return
    stack, seen = list(deps), set()
    while stack:
        current = stack.pop()
        if current == lemma_id:
            raise ValueError("dependency cycle")
        if current in seen:
            continue
        seen.add(current)
        row = db.execute("SELECT deps FROM lemmas WHERE id = ?", (current,)).fetchone()
        stack.extend(json.loads(row["deps"]))


def add_lemma(
    problem: str,
    name: str,
    statement: str,
    lean_statement: str | None,
    deps: list[int],
    owner: str | None,
) -> int:
    db = connect()
    _check_deps(db, problem, None, deps)
    cur = db.execute(
        "INSERT INTO lemmas (problem, name, statement, lean_statement, deps, owner, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (problem, name, statement, lean_statement, json.dumps(deps), owner, time.time()),
    )
    return int(cur.lastrowid or 0)


def update_lemma(lemma_id: int, **fields: Any) -> None:
    db = connect()
    row = db.execute("SELECT problem FROM lemmas WHERE id = ?", (lemma_id,)).fetchone()
    if row is None:
        raise ValueError(f"no lemma {lemma_id}")
    if "deps" in fields:
        _check_deps(db, row["problem"], lemma_id, fields["deps"])
        fields["deps"] = json.dumps(fields["deps"])
    if "status" in fields and fields["status"] not in LEMMA_STATUSES:
        raise ValueError(f"status must be one of {LEMMA_STATUSES}")
    sets = ", ".join(f"{key} = ?" for key in fields)
    db.execute(
        f"UPDATE lemmas SET {sets}, updated_at = ? WHERE id = ?",
        (*fields.values(), time.time(), lemma_id),
    )


def record_proved_lemma(lemma_id: int, source: str, decl_types: dict[str, str]) -> str:
    """Called by the lean server after it checked `source` (no sorry, standard axioms)."""
    ref = sha256(source)
    update_lemma(lemma_id, status="proved", proof_ref=f"lean:{ref}", lean_source=source)
    db = connect()
    for name, type_ in decl_types.items():
        db.execute(
            "INSERT OR REPLACE INTO project_decls VALUES (?, ?, ?, ?, ?)",
            (name, type_, lemma_id, ref, time.time()),
        )
    return ref


def lemma_sources(lemma_ids: list[int]) -> str:
    """Lean source of proved lemmas, in the given order, for use as a preamble."""
    db = connect()
    parts = []
    for lemma_id in lemma_ids:
        row = db.execute(
            "SELECT status, lean_source FROM lemmas WHERE id = ?", (lemma_id,)
        ).fetchone()
        if row is None or row["status"] != "proved":
            raise ValueError(f"lemma {lemma_id} is not proved")
        parts.append(row["lean_source"])
    return "\n\n".join(parts)


def search_project_decls(query: str, limit: int) -> list[dict[str, Any]]:
    like = f"%{query}%"
    cur = connect().execute(
        "SELECT d.name, d.type, d.source_sha, l.problem, l.id AS lemma_id FROM project_decls d"
        " JOIN lemmas l ON l.id = d.lemma_id WHERE d.name LIKE ? OR d.type LIKE ? LIMIT ?",
        (like, like, limit),
    )
    return _rows(cur)


def add_coverage(
    problem: str,
    lemma_id: int | None,
    generator: str,
    generator_version: str,
    bounds: dict,
    shard: dict,
    symmetry: list,
    outcome: str,
    reached: Any = None,
    witness: Any = None,
    artifact: str | None = None,
    owner: str | None = None,
) -> int:
    if outcome not in COVERAGE_OUTCOMES:
        raise ValueError(f"outcome must be one of {COVERAGE_OUTCOMES}")
    db = connect()
    cur = db.execute(
        "INSERT INTO coverage (problem, lemma_id, generator, generator_version, bounds, shard,"
        " symmetry, outcome, reached, witness, artifact, owner, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            problem,
            lemma_id,
            generator,
            generator_version,
            json.dumps(bounds),
            json.dumps(shard),
            json.dumps(symmetry),
            outcome,
            json.dumps(reached),
            json.dumps(witness),
            artifact,
            owner,
            time.time(),
        ),
    )
    if outcome == "lemma_refuted" and lemma_id is not None:
        update_lemma(lemma_id, status="refuted", proof_ref=f"coverage:{cur.lastrowid}")
    return int(cur.lastrowid or 0)


server = MCPServer(
    "lemmata-whiteboard",
    instructions="Shared state for all workers on a problem. `problem` is the challenge id. "
    "Read before starting; record lemmas, dead ends and search coverage as you go.",
)


@server.tool()
def read(problem: str, lemma_id: int | None = None) -> dict[str, Any]:
    """Everything recorded for a problem (or one lemma): lemma DAG, dead ends, coverage."""
    db = connect()
    args: tuple = (problem,)
    lemma_where = where = "problem = ?"
    if lemma_id is not None:
        args = (problem, lemma_id)
        lemma_where, where = "problem = ? AND id = ?", "problem = ? AND lemma_id = ?"
    lemmas = _rows(
        db.execute(
            "SELECT id, name, statement, lean_statement, deps, owner, status, proof_ref, updated_at"
            f" FROM lemmas WHERE {lemma_where}",
            args,
        )
    )
    dead_ends = _rows(db.execute(f"SELECT * FROM dead_ends WHERE {where}", args))
    coverage = _rows(db.execute(f"SELECT * FROM coverage WHERE {where}", args))
    return {"lemmas": lemmas, "dead_ends": dead_ends, "coverage": coverage}


@server.tool()
def lemma_add(
    problem: str,
    name: str,
    statement: str,
    lean_statement: str | None = None,
    deps: list[int] | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Add a lemma node. `deps` are ids of lemmas it uses (must keep the DAG acyclic)."""
    return {"id": add_lemma(problem, name, statement, lean_statement, deps or [], owner)}


@server.tool()
def lemma_update(
    lemma_id: int,
    status: str | None = None,
    owner: str | None = None,
    deps: list[int] | None = None,
    lean_statement: str | None = None,
) -> dict[str, Any]:
    """Claim, release or abandon a lemma, or change its deps/owner/Lean statement.

    `proved` is set only by the lean server's `register_lemma` (after a Lean check) and
    `refuted` only by recorded coverage with outcome `lemma_refuted`.
    """
    if status is not None and status not in WORKER_LEMMA_STATUSES:
        raise ValueError(f"workers may set status to {WORKER_LEMMA_STATUSES} only")
    fields = {
        k: v
        for k, v in {
            "status": status,
            "owner": owner,
            "deps": deps,
            "lean_statement": lean_statement,
        }.items()
        if v is not None
    }
    update_lemma(lemma_id, **fields)
    return {"ok": True}


@server.tool()
def dead_end_add(
    problem: str,
    goal: str,
    tactics: list[str],
    outcome: str,
    budget: dict[str, Any],
    premises: list[str] | None = None,
    lemma_id: int | None = None,
    note: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Record a goal that did not yield: what was tried, with what budget, and how it ended.

    outcome: `timed_out` (budget ran out), `failed` (tactics finished without closing it)
    or `error`.
    """
    if outcome not in DEAD_END_OUTCOMES:
        raise ValueError(f"outcome must be one of {DEAD_END_OUTCOMES}")
    norm = normalize_goal(goal)
    cur = connect().execute(
        "INSERT INTO dead_ends (problem, lemma_id, goal, goal_hash, library_pin, tactics,"
        " premises, budget, outcome, note, owner, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            problem,
            lemma_id,
            norm,
            sha256(norm),
            library_pin(),
            json.dumps(tactics),
            json.dumps(premises or []),
            json.dumps(budget),
            outcome,
            note,
            owner,
            time.time(),
        ),
    )
    return {"id": cur.lastrowid}


@server.tool()
def dead_end_find(goal: str) -> list[dict[str, Any]]:
    """Dead ends recorded for this exact goal (after normalization), across all problems."""
    norm = normalize_goal(goal)
    return _rows(
        connect().execute(
            "SELECT * FROM dead_ends WHERE goal_hash = ? ORDER BY created_at", (sha256(norm),)
        )
    )


@server.tool()
def coverage_add(
    problem: str,
    generator: str,
    generator_version: str,
    bounds: dict[str, Any],
    outcome: str,
    shard: dict[str, Any] | None = None,
    symmetry: list[Any] | None = None,
    reached: Any = None,
    witness: Any = None,
    lemma_id: int | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Record a search done outside the compute server (compute records its own runs).

    outcome: `timed_out` (stopped early; give `reached`), `no_witness_in_domain` (the
    whole bounded domain was searched), `lemma_refuted` (a witness was found) or `error`.
    """
    row_id = add_coverage(
        problem,
        lemma_id,
        generator,
        generator_version,
        bounds,
        shard or {"index": 0, "count": 1},
        symmetry or [],
        outcome,
        reached,
        witness,
        None,
        owner,
    )
    return {"id": row_id}
