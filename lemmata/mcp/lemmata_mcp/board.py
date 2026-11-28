"""Structured whiteboard shared by every worker thread: one SQLite file in WAL mode.

Three records:
- lemma DAG: statement, deps, owner, status, proof ref. Only the lean server may set
  `proved` (after checking the proof); workers cannot claim it through this server.
- dead ends: normalized goal, library pin, tactics/premises tried, budget, outcome.
- refutation coverage: generator + version, bounds, shard, symmetry assumptions, and an
  outcome that keeps `timed_out`, `no_witness_in_domain` and `lemma_refuted` apart.
- goal tree: the proof state, rooted at `Claim`. Goals change state only through the lean
  server's goal tools, each step backed by a Lean-checked lemma; workers only read it.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
import time
from contextlib import closing
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
CREATE TABLE IF NOT EXISTS evolve_runs (
  id TEXT PRIMARY KEY,
  problem TEXT,
  evaluator TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS evolve_runs_frozen BEFORE UPDATE ON evolve_runs
  BEGIN SELECT RAISE(ABORT, 'an evolve run keeps its evaluator'); END;
CREATE TABLE IF NOT EXISTS programs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES evolve_runs(id),
  parent_id INTEGER,
  program TEXT NOT NULL,
  program_sha TEXT NOT NULL,
  score REAL,
  object TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  UNIQUE (run_id, program_sha)
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  problem TEXT NOT NULL,
  parent_id INTEGER REFERENCES goals(id),
  split_id INTEGER REFERENCES goal_splits(id),
  lean_statement TEXT NOT NULL,
  stmt_hash TEXT NOT NULL,
  informal TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  lemma_id INTEGER REFERENCES lemmas(id),
  complexity INTEGER NOT NULL,
  flag TEXT,
  created_round INTEGER,
  updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS goals_root ON goals (problem) WHERE parent_id IS NULL;
CREATE TABLE IF NOT EXISTS goal_splits (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL REFERENCES goals(id),
  relation TEXT NOT NULL,
  lemma_id INTEGER NOT NULL REFERENCES lemmas(id),
  weaken_lemma_ids TEXT NOT NULL DEFAULT '[]',
  dead INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY,
  problem TEXT NOT NULL,
  round INTEGER NOT NULL,
  thread_id TEXT,
  phase TEXT NOT NULL,
  open_leaves INTEGER NOT NULL,
  closed INTEGER NOT NULL,
  refuted INTEGER NOT NULL,
  case_splits INTEGER NOT NULL,
  flagged INTEGER NOT NULL,
  complexity_sum INTEGER NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE (problem, round, phase)
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
    db: sqlite3.Connection | None = None,
) -> int:
    db = db or connect()
    _check_deps(db, problem, None, deps)
    cur = db.execute(
        "INSERT INTO lemmas (problem, name, statement, lean_statement, deps, owner, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (problem, name, statement, lean_statement, json.dumps(deps), owner, time.time()),
    )
    return int(cur.lastrowid or 0)


def update_lemma(lemma_id: int, db: sqlite3.Connection | None = None, **fields: Any) -> None:
    db = db or connect()
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


def record_proved_lemma(
    lemma_id: int,
    source: str,
    decl_types: dict[str, str],
    db: sqlite3.Connection | None = None,
) -> str:
    """Called by the lean server after it checked `source` (no sorry, standard axioms)."""
    db = db or connect()
    ref = sha256(source)
    update_lemma(lemma_id, db, status="proved", proof_ref=f"lean:{ref}", lean_source=source)
    for name, type_ in decl_types.items():
        db.execute(
            "INSERT OR REPLACE INTO project_decls VALUES (?, ?, ?, ?, ?)",
            (name, type_, lemma_id, ref, time.time()),
        )
    return ref


def split_prefix(source: str, prefix: str) -> tuple[str, str]:
    """(leading copy of `prefix`, rest) when `source` starts with `prefix` up to whitespace
    (both sides compared as `" ".join(text.split())`), else ("", source)."""
    wanted = prefix.split()
    tokens = list(itertools.islice(re.finditer(r"\S+", source), len(wanted)))
    if not wanted or [t.group() for t in tokens] != wanted:
        return "", source
    cut = tokens[-1].end()
    return source[:cut], source[cut:]


def strip_prefix(source: str, prefix: str) -> str:
    """`source` without a leading copy of the frozen challenge prefix. Workers register lemmas
    with the prefix pasted in; composing two such sources verbatim redefines `Claim`."""
    return split_prefix(source, prefix)[1]


def lemma_sources(lemma_ids: list[int], prefix: str | None = None) -> str:
    """Lean source of proved lemmas, in the given order, for use as a preamble. With `prefix`,
    each source loses its leading copy of it, so the caller can emit the prefix once."""
    db = connect()
    parts = []
    for lemma_id in lemma_ids:
        row = db.execute(
            "SELECT status, lean_source FROM lemmas WHERE id = ?", (lemma_id,)
        ).fetchone()
        if row is None or row["status"] != "proved":
            raise ValueError(f"lemma {lemma_id} is not proved")
        source = row["lean_source"]
        parts.append(strip_prefix(source, prefix).strip("\n") if prefix else source)
    return "\n\n".join(parts)


def lemma_problems(lemma_ids: list[int]) -> set[str]:
    marks = ",".join("?" * len(lemma_ids))
    rows = connect().execute(
        f"SELECT DISTINCT problem FROM lemmas WHERE id IN ({marks})", lemma_ids
    )
    return {row["problem"] for row in rows}


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
    if outcome == "lemma_refuted" and lemma_id is not None:
        refuse_goal_lemma(lemma_id)
        if lemma_row(lemma_id)["status"] == "proved":
            raise ValueError(f"lemma {lemma_id} is proved and cannot be refuted")
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


def evolve_run(run_id: str, evaluator: str | None, problem: str | None) -> str:
    """The run's frozen evaluator, creating the run on first use."""
    db = connect()
    row = db.execute("SELECT evaluator FROM evolve_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        if evaluator is None:
            raise ValueError(f"no evolve run {run_id!r}; start it with an evaluator")
        db.execute(
            "INSERT INTO evolve_runs VALUES (?, ?, ?, ?)", (run_id, problem, evaluator, time.time())
        )
        return evaluator
    if evaluator is not None and evaluator != row["evaluator"]:
        raise ValueError(f"evolve run {run_id!r} already has a different evaluator")
    return row["evaluator"]


def add_program(
    run_id: str,
    program: str,
    parent_id: int | None,
    score: float | None,
    obj: Any,
    error: str | None,
) -> int:
    cur = connect().execute(
        "INSERT OR IGNORE INTO programs (run_id, parent_id, program, program_sha, score, object,"
        " error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, parent_id, program, sha256(program), score, json.dumps(obj), error, time.time()),
    )
    return int(cur.lastrowid or 0)


def evolve_sample(run_id: str, k: int) -> dict[str, Any]:
    """Parents for the next mutation, OpenEvolve-style: the k best programs (exploit), plus
    the best of each program-length niche they miss (explore), plus run statistics."""
    db = connect()
    scored = "FROM programs WHERE run_id = ? AND score IS NOT NULL"
    best = _rows(
        db.execute(f"SELECT id, score, program {scored} ORDER BY score DESC LIMIT ?", (run_id, k))
    )
    niches = _rows(
        db.execute(
            "SELECT id, MAX(score) AS score, program FROM programs WHERE run_id = ?"
            " AND score IS NOT NULL GROUP BY LENGTH(program) / 500 ORDER BY score DESC LIMIT ?",
            (run_id, 2 * k),
        )
    )
    seen = {p["id"] for p in best}
    stats = db.execute(
        "SELECT COUNT(*) AS programs, COUNT(score) AS scored, MAX(score) AS best_score"
        " FROM programs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return {
        "parents": best,
        "inspirations": [p for p in niches if p["id"] not in seen][:k],
        **dict(stats),
    }


# --- goal tree ----------------------------------------------------------------------------
# Written only by the lean server, after the Lean check that justifies each change: a split
# lemma `C1 → … → Ck → P`, a close lemma `C`, a refute lemma `¬ C`, or the composite that
# propagates them upward. Every goal lemma's `deps` is the exact use_lemmas list it was
# checked with, so `deps + [lemma]` always elaborates after the challenge prefix.

GOAL_STATEMENT_CHARS = 300
GOAL_OWNER = "lean:goal-tree"  # owner of every goal lemma; workers' tools refuse to touch them
ROUND_PHASES = ("begin", "end")


def goal_complexity(statement: str) -> int:
    # ponytail: token count; replace with quantifier depth / free-var count if it misleads
    return len(re.findall(r"\w+|[^\w\s]", normalize_goal(statement)))


def goal_hash(statement: str) -> str:
    return sha256(normalize_goal(statement))


def _insert_goal(
    db: sqlite3.Connection,
    problem: str,
    parent_id: int | None,
    split_id: int | None,
    statement: str,
    informal: str | None,
    flag: str | None = None,
) -> int:
    round_ = db.execute(
        "SELECT MAX(round) AS r FROM rounds WHERE problem = ? AND phase = 'begin'", (problem,)
    ).fetchone()["r"]
    cur = db.execute(
        "INSERT INTO goals (problem, parent_id, split_id, lean_statement, stmt_hash, informal,"
        " complexity, flag, created_round, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            problem,
            parent_id,
            split_id,
            statement,
            goal_hash(statement),
            informal,
            goal_complexity(statement),
            flag,
            round_,
            time.time(),
        ),
    )
    return int(cur.lastrowid or 0)


def ensure_root(problem: str, informal: str) -> int:
    """The root goal `Claim`, created on the first goal call for a problem."""
    db = connect()
    if row := db.execute(
        "SELECT id FROM goals WHERE problem = ? AND parent_id IS NULL", (problem,)
    ).fetchone():
        return row["id"]
    try:
        return _insert_goal(db, problem, None, None, "Claim", informal)
    except sqlite3.IntegrityError:  # another worker created it first (goals_root index)
        return ensure_root(problem, informal)


def live_goal(problem: str, goal_id: int, db: sqlite3.Connection | None = None) -> dict:
    """Goal `goal_id` of `problem`, refusing goals that hang under a dead split."""
    db = db or connect()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None or row["problem"] != problem:
        raise ValueError(f"goal {goal_id} is not a goal of problem {problem!r}")
    current = row
    while current["split_id"] is not None:
        split = db.execute(
            "SELECT dead FROM goal_splits WHERE id = ?", (current["split_id"],)
        ).fetchone()
        if split["dead"]:
            raise ValueError(f"goal {goal_id} hangs under dead split {current['split_id']}")
        current = db.execute("SELECT * FROM goals WHERE id = ?", (current["parent_id"],)).fetchone()
    return dict(row)


def ancestor_hashes(goal_id: int) -> set[str]:
    """Statement hashes of a goal and all its ancestors."""
    db, hashes, current = connect(), set(), goal_id
    while current is not None:
        row = db.execute(
            "SELECT parent_id, stmt_hash FROM goals WHERE id = ?", (current,)
        ).fetchone()
        hashes.add(row["stmt_hash"])
        current = row["parent_id"]
    return hashes


def split_count(goal_id: int) -> int:
    """Splits ever made of a goal (dead ones included): numbers the next split lemma."""
    return (
        connect()
        .execute("SELECT COUNT(*) AS n FROM goal_splits WHERE goal_id = ?", (goal_id,))
        .fetchone()["n"]
    )


def iff_declaration(problem: str, goal: str, child: str) -> str | None:
    """A proved project declaration of `problem` whose type is `child ↔ goal` or
    `goal ↔ child` after normalization (types as `#check` printed them)."""
    # ponytail: textual match only; pretty-printed types rarely equal worker text verbatim
    forms = {
        normalize_goal(f"{left} ↔ {right}")
        for a, b in ((goal, child), (child, goal))
        for left, right in ((a, b), (f"({a})", f"({b})"))
    }
    rows = connect().execute(
        "SELECT d.name, d.type FROM project_decls d JOIN lemmas l ON l.id = d.lemma_id"
        " WHERE l.problem = ?",
        (problem,),
    )
    for row in rows:
        if normalize_goal(row["type"].split(" : ", 1)[-1]) in forms:
            return row["name"]
    return None


def lemma_row(lemma_id: int) -> dict[str, Any]:
    row = connect().execute("SELECT * FROM lemmas WHERE id = ?", (lemma_id,)).fetchone()
    if row is None:
        raise ValueError(f"no lemma {lemma_id}")
    return {**dict(row), "deps": json.loads(row["deps"])}


def lemma_chain(goal_lemma_ids: list[int]) -> list[int]:
    """Goal lemmas with the lemmas each was checked against, in an order that elaborates:
    each lemma's own `deps` (its checked use_lemmas list), then the lemma, first copy kept."""
    out: dict[int, None] = {}
    for lemma_id in goal_lemma_ids:
        out |= dict.fromkeys([*lemma_row(lemma_id)["deps"], lemma_id])
    return list(out)


def is_goal_lemma(lemma_id: int) -> bool:
    row = connect().execute("SELECT owner FROM lemmas WHERE id = ?", (lemma_id,)).fetchone()
    return row is not None and row["owner"] == GOAL_OWNER


def refuse_goal_lemma(lemma_id: int) -> None:
    """Goal lemmas justify goal states and certificates: no worker tool may change them."""
    if is_goal_lemma(lemma_id):
        raise ValueError(f"lemma {lemma_id} belongs to the goal tree and cannot be changed")


def _insert_checked_lemma(db: sqlite3.Connection, problem: str, lemma: dict[str, Any]) -> int:
    """A lemma the lean server has just checked: name, statement, source, deps, types."""
    lemma_id = add_lemma(
        problem,
        lemma["name"],
        lemma["statement"],
        lemma["statement"],
        lemma["deps"],
        GOAL_OWNER,
        db,
    )
    record_proved_lemma(lemma_id, lemma["source"], lemma["types"], db)
    return lemma_id


def _transaction(problem: str, goal_id: int, statuses: tuple[str, ...]):
    """Open a write transaction and re-read the goal: it may have changed during the Lean
    check (another worker), and the change must still apply."""
    db = connect()
    db.execute("BEGIN IMMEDIATE")
    try:
        goal = live_goal(problem, goal_id, db)
        if goal["status"] not in statuses:
            raise ValueError(f"goal {goal_id} is {goal['status']}; expected {'/'.join(statuses)}")
    except BaseException:
        db.execute("ROLLBACK")
        db.close()
        raise
    return db, goal


def add_split(
    problem: str,
    goal_id: int,
    relation: str,
    flag: str | None,
    children: list[dict[str, str]],
    split_lemma: dict[str, Any],
    weaken_lemmas: list[dict[str, Any] | None],
    n: int,
) -> dict[str, Any]:
    """Record a checked split of an open goal: its lemmas, the split row, the children.
    `n` is the split number the lemma names were built from."""
    db, _ = _transaction(problem, goal_id, ("open",))
    with closing(db), db:
        made = db.execute(
            "SELECT COUNT(*) AS n FROM goal_splits WHERE goal_id = ?", (goal_id,)
        ).fetchone()["n"]
        if made != n - 1:
            raise ValueError("goal changed during the check; retry")
        lemma_id = _insert_checked_lemma(db, problem, split_lemma)
        weak_ids = [_insert_checked_lemma(db, problem, w) if w else None for w in weaken_lemmas]
        split_id = int(
            db.execute(
                "INSERT INTO goal_splits (goal_id, relation, lemma_id, weaken_lemma_ids,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (goal_id, relation, lemma_id, json.dumps(weak_ids), time.time()),
            ).lastrowid
            or 0
        )
        child_ids = [
            _insert_goal(
                db, problem, goal_id, split_id, c["lean_statement"], c.get("informal"), flag
            )
            for c in children
        ]
        db.execute(
            "UPDATE goals SET status = 'split', updated_at = ? WHERE id = ?", (time.time(), goal_id)
        )
    return {"split_id": split_id, "lemma_id": lemma_id, "children": child_ids}


def resolve_goal(problem: str, goal_id: int, status: str, lemma: dict[str, Any]) -> int:
    """Mark an open or split goal `closed` or `refuted` with the checked lemma that shows it."""
    if status not in ("closed", "refuted"):
        raise ValueError("status must be closed or refuted")
    db, _ = _transaction(problem, goal_id, ("open", "split"))
    with closing(db), db:
        lemma_id = _insert_checked_lemma(db, problem, lemma)
        db.execute(
            "UPDATE goals SET status = ?, lemma_id = ?, updated_at = ? WHERE id = ?",
            (status, lemma_id, time.time(), goal_id),
        )
    return lemma_id


def parent_edge(goal_id: int) -> dict[str, Any] | None:
    """The live split a goal came from: parent goal, split row, the goal's index in it and
    all its sibling goals (in split order). None for the root."""
    db = connect()
    goal = db.execute("SELECT parent_id, split_id FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal["parent_id"] is None:
        return None
    split = dict(
        db.execute("SELECT * FROM goal_splits WHERE id = ?", (goal["split_id"],)).fetchone()
    )
    split["weaken_lemma_ids"] = json.loads(split["weaken_lemma_ids"])
    children = [
        dict(r)
        for r in db.execute("SELECT * FROM goals WHERE split_id = ? ORDER BY id", (split["id"],))
    ]
    parent = dict(db.execute("SELECT * FROM goals WHERE id = ?", (goal["parent_id"],)).fetchone())
    index = [c["id"] for c in children].index(goal_id)
    return {"parent": parent, "split": split, "index": index, "children": children}


def kill_split(problem: str, split_id: int, parent_id: int) -> None:
    """A refuted child makes its split useless: mark it dead and reopen the parent (a no-op
    if the parent was closed in the meantime)."""
    try:
        db, _ = _transaction(problem, parent_id, ("split",))
    except ValueError:
        return
    with closing(db), db:
        db.execute("UPDATE goal_splits SET dead = 1 WHERE id = ?", (split_id,))
        db.execute(
            "UPDATE goals SET status = 'open', updated_at = ? WHERE id = ?",
            (time.time(), parent_id),
        )


def root_goal(problem: str) -> dict[str, Any] | None:
    row = (
        connect()
        .execute("SELECT * FROM goals WHERE problem = ? AND parent_id IS NULL", (problem,))
        .fetchone()
    )
    return dict(row) if row else None


def goal_tree(problem: str) -> dict[str, Any]:
    """The live tree from the root (statements truncated, dead splits as ids only), plus
    stats: open leaves and their complexity in the live tree; closed, refuted, flagged goals
    and case splits counted over all goals ever recorded (so they never go down). Only the
    root and children of `case` splits (live or dead) count as closed or refuted: resolving
    a goal under a `reduction` split (e.g. closing `True`) is not progress. `open_leaf_ids`
    lists the open leaves counted in the stats."""
    db = connect()
    goals = {
        r["id"]: dict(r) for r in db.execute("SELECT * FROM goals WHERE problem = ?", (problem,))
    }
    splits = [
        dict(r)
        for r in db.execute(
            "SELECT s.* FROM goal_splits s JOIN goals g ON g.id = s.goal_id WHERE g.problem = ?"
            " ORDER BY s.id",
            (problem,),
        )
    ]
    case_splits = {s["id"] for s in splits if s["relation"] == "case"}
    counted = [g for g in goals.values() if g["split_id"] is None or g["split_id"] in case_splits]
    open_leaf_ids: list[int] = []
    stats = {
        "open_leaves": 0,
        "closed": sum(g["status"] == "closed" for g in counted),
        "refuted": sum(g["status"] == "refuted" for g in counted),
        "case_splits": sum(s["relation"] == "case" for s in splits),
        "flagged": sum(g["flag"] is not None for g in goals.values()),
        "complexity_sum": 0,
    }
    children: dict[int, list[dict]] = {}
    for g in sorted(goals.values(), key=lambda g: g["id"]):
        if g["split_id"] is not None:
            children.setdefault(g["split_id"], []).append(g)

    def node(g: dict[str, Any], active: bool = True) -> dict[str, Any]:
        """`active`: no ancestor is closed or refuted yet, so open goals here still count."""
        statement = g["lean_statement"]
        if len(statement) > GOAL_STATEMENT_CHARS:
            statement = statement[:GOAL_STATEMENT_CHARS] + "…"
        item: dict[str, Any] = {"id": g["id"], "status": g["status"], "informal": g["informal"]}
        item["statement"] = statement
        for key in ("flag", "lemma_id"):
            if g[key] is not None:
                item[key] = g[key]
        if active and g["status"] == "open":
            stats["open_leaves"] += 1
            open_leaf_ids.append(g["id"])
            stats["complexity_sum"] += g["complexity"]
        own = [s for s in splits if s["goal_id"] == g["id"]]
        if dead := [s["id"] for s in own if s["dead"]]:
            item["dead_splits"] = dead
        for s in own:
            if not s["dead"]:
                item["split"] = {
                    "id": s["id"],
                    "relation": s["relation"],
                    "lemma_id": s["lemma_id"],
                    "weaken_lemma_ids": json.loads(s["weaken_lemma_ids"]),
                    "children": [
                        node(c, active and g["status"] == "split")
                        for c in children.get(s["id"], [])
                    ],
                }
        return item

    root = next((g for g in goals.values() if g["parent_id"] is None), None)
    return {
        "problem": problem,
        "root": node(root) if root else None,
        "stats": stats,
        "open_leaf_ids": open_leaf_ids,
    }


def snapshot(problem: str, round_: int, phase: str, thread_id: str | None) -> dict[str, Any]:
    """Record the tree stats at the begin or end of a round. At `end`, `progress` compares
    with the same round's `begin` row (None without one): a goal closed or refuted, or a
    new case split. `flagged` lists the goals flagged by splits made during the round."""
    if phase not in ROUND_PHASES:
        raise ValueError(f"phase must be one of {ROUND_PHASES}")
    stats = goal_tree(problem)["stats"]
    db = connect()
    db.execute(
        "INSERT OR REPLACE INTO rounds (problem, round, thread_id, phase, open_leaves, closed,"
        " refuted, case_splits, flagged, complexity_sum, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (problem, round_, thread_id, phase, *stats.values(), time.time()),
    )
    result: dict[str, Any] = {
        "problem": problem,
        "round": round_,
        "phase": phase,
        "stats": stats,
        "progress": None,
        "flagged": [],
    }
    if phase == "end":
        begin = db.execute(
            "SELECT * FROM rounds WHERE problem = ? AND round = ? AND phase = 'begin'",
            (problem, round_),
        ).fetchone()
        if begin is not None:
            resolved = stats["closed"] + stats["refuted"] - begin["closed"] - begin["refuted"]
            result["progress"] = resolved > 0 or stats["case_splits"] > begin["case_splits"]
        result["flagged"] = flagged_goals(problem, round_)
    return result


def flagged_goals(problem: str, round_: int) -> list[dict[str, Any]]:
    return [
        {"goal_id": r["id"], "flag": r["flag"], "informal": r["informal"]}
        for r in connect().execute(
            "SELECT id, flag, informal FROM goals WHERE problem = ? AND created_round = ?"
            " AND flag IS NOT NULL ORDER BY id",
            (problem, round_),
        )
    ]


server = MCPServer(
    "lemmata-whiteboard",
    instructions="Shared state for all workers on a problem. `problem` is the challenge id. "
    "Read `goal_tree` first (compact proof state), `read` for details; record lemmas, dead "
    "ends and search coverage as you go.",
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


@server.tool(name="goal_tree")
def goal_tree_tool(problem: str) -> dict[str, Any]:
    """The proof state of a problem, compact: read this first, before `read`.

    A tree of goals rooted at `Claim` (id, status open/split/closed/refuted, informal text,
    statement truncated, flag, lemma ids) plus stats. Goals change only through the lean
    server's goal_split / goal_close / goal_refute, each backed by a Lean check. A flag
    `restatement` or `strengthening` marks a single-child reduction: no progress.
    """
    from .verdict_server import _challenge

    ensure_root(problem, _challenge(problem)["title"])
    return goal_tree(problem)


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
    if owner == GOAL_OWNER:
        raise ValueError(f"owner {GOAL_OWNER!r} is reserved for the lean server")
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
    if owner == GOAL_OWNER:
        raise ValueError(f"owner {GOAL_OWNER!r} is reserved for the lean server")
    refuse_goal_lemma(lemma_id)
    if status is not None and lemma_row(lemma_id)["status"] == "proved":
        raise ValueError(f"lemma {lemma_id} is proved; its status cannot change")
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
