"""Library server: what is already known.

- declaration index over the pinned Mathlib (and its dependencies) plus lemmas proved in this
  project: full type, the import that provides it, and the revision it comes from.
  Loogle-style type-pattern search is lean_lsp's `lean_loogle` (local index, same toolchain);
  `decl_info` adds import and revision to its hits.
- Formal Conjectures (pinned commit), searched as source text.
- OEIS: local stripped/names data if downloaded, otherwise oeis.org over HTTP.
- pi-Base (pinned commit, local): topological spaces, properties and theorems, with
  forward-chaining deduction for counterexample lookup.
- LMFDB: the read-only HTTP API. Network, so allowlisted in the research profile only.
Everything here is a candidate source: a hit is a lead or a citation, never a certificate.
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

import yaml
from mcp.server.mcpserver import MCPServer

from . import board
from .common import HOME, INDEX_DB, PINS, WORK_PROJECT

FORMAL_CONJECTURES = HOME / "formal-conjectures"
OEIS_DIR = HOME / "oeis"
PI_BASE = HOME / "pi-base"
LMFDB_API = "https://www.lmfdb.org/api"

server = MCPServer(
    "lemmata-library",
    instructions="Search Mathlib declarations, project lemmas, Formal Conjectures, the OEIS, "
    "pi-Base and (research role) the LMFDB. For type-pattern queries use lean_lsp's "
    "lean_loogle, then decl_info here. Hits are leads and citations, not certificates.",
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


# --- pi-Base -------------------------------------------------------------------------------


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    _, head, body = path.read_text(encoding="utf-8").split("---", 2)
    return yaml.safe_load(head) or {}, body.strip()


@functools.cache
def _pi_base() -> dict[str, Any]:
    if not PI_BASE.exists():
        raise RuntimeError("pi-base data missing; run setup-wsl.sh pi_base")
    properties, spaces, traits = {}, {}, {}
    for path in sorted((PI_BASE / "properties").glob("*.md")):
        meta, body = _frontmatter(path)
        properties[meta["uid"]] = {
            "name": meta["name"],
            "aliases": meta.get("aliases") or [],
            "description": body[:1500],
        }
    for directory in sorted((PI_BASE / "spaces").iterdir()):
        meta, body = _frontmatter(directory / "README.md")
        uid = meta["uid"]
        spaces[uid] = {
            "name": meta["name"],
            "aliases": meta.get("aliases") or [],
            "description": body[:1500],
        }
        traits[uid] = {
            m["property"]: bool(m["value"])
            for m, _ in map(_frontmatter, (directory / "properties").glob("*.md"))
        }
    theorems = []
    for path in sorted((PI_BASE / "theorems").glob("*.md")):
        meta, body = _frontmatter(path)
        theorems.append(
            {"id": meta["uid"], "if": meta["if"], "then": meta["then"], "note": body[:500]}
        )
    revision = subprocess.run(
        ["git", "-C", str(PI_BASE), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    return {"properties": properties, "spaces": spaces, "traits": traits, "theorems": theorems,
            "revision": revision}  # fmt: skip


def _value(formula: dict[str, Any], known: dict[str, bool]) -> bool | None:
    """Three-valued truth of a pi-Base formula ({P: bool}, {and: [...]}, {or: [...]})."""
    if "and" in formula or "or" in formula:
        values = [_value(f, known) for f in formula.get("and") or formula["or"]]
        if "and" in formula:
            return False if False in values else True if all(values) else None
        return True if True in values else False if all(v is False for v in values) else None
    ((prop, value),) = formula.items()
    return None if prop not in known else known[prop] == value


def _literals(formula: dict[str, Any]) -> list[tuple[str, bool]] | None:
    if "or" in formula:
        return None
    if "and" in formula:
        parts = [_literals(f) for f in formula["and"]]
        return None if None in parts else [lit for part in parts if part for lit in part]
    return [next(iter(formula.items()))]


@functools.cache
def _deduced(space: str) -> dict[str, bool]:
    """Asserted traits closed under the theorems and their contrapositives."""
    data = _pi_base()
    known = dict(data["traits"][space])
    changed = True
    while changed:
        changed = False
        for theorem in data["theorems"]:
            if _value(theorem["if"], known) is True:
                new = [(p, v) for p, v in _literals(theorem["then"]) or [] if p not in known]
            elif _value(theorem["then"], known) is False and (lits := _literals(theorem["if"])):
                open_ = [(p, v) for p, v in lits if p not in known]
                rest_hold = all(known.get(p) == v for p, v in lits if p in known)
                new = [(open_[0][0], not open_[0][1])] if len(open_) == 1 and rest_hold else []
            else:
                new = []
            for prop, value in new:
                known[prop] = value
                changed = True
    return known


def _property_id(key: str, properties: dict[str, Any]) -> str:
    if key in properties:
        return key
    for uid, p in properties.items():
        if key.lower() in (n.lower() for n in [p["name"], *p["aliases"]]):
            return uid
    raise ValueError(f"unknown pi-Base property {key!r}; search for it with `query`")


@server.tool()
def pi_base(
    query: str | None = None, traits: dict[str, bool] | None = None, limit: int = 20
) -> dict[str, Any]:
    """pi-Base (topology), local at a pinned commit (CC BY 4.0: cite pi-Base and the refs).

    `query`: words that must all appear in a space's, property's or theorem's name, aliases
    or description. `traits`: property (id like P000016, name or alias) -> required value;
    returns spaces having all of them, asserted by pi-Base or deduced here by forward
    chaining over its theorems (both directions), e.g. {"Compact": true, "Metrizable":
    false} to look for a counterexample. Deduced traits are labelled; check the reasoning
    against pi-Base before citing it.
    """
    data = _pi_base()
    out: dict[str, Any] = {"revision": data["revision"]}
    if query:
        words = query.lower().split()

        def hit(entry: dict[str, Any]) -> bool:
            text = " ".join([entry["name"], *entry["aliases"], entry["description"]]).lower()
            return all(w in text for w in words)

        out["properties"] = [{"id": k, **v} for k, v in data["properties"].items() if hit(v)][
            :limit
        ]
        out["spaces"] = [{"id": k, **v} for k, v in data["spaces"].items() if hit(v)][:limit]
        out["theorems"] = [
            t for t in data["theorems"] if all(w in json.dumps(t).lower() for w in words)
        ][:limit]
    if traits:
        props = data["properties"]
        wanted = {_property_id(k, props): v for k, v in traits.items()}
        matches = []
        for uid, space in data["spaces"].items():
            known = _deduced(uid)
            if all(known.get(p) == v for p, v in wanted.items()):
                asserted = data["traits"][uid]
                matches.append({
                    "id": uid,
                    "name": space["name"],
                    "traits": {props[p]["name"]: {"value": v, "source": "asserted" if p in asserted
                               else "deduced"} for p, v in wanted.items()},
                })  # fmt: skip
        out["matching_spaces"] = matches[:limit]
        out["total_matches"] = len(matches)
    return out


# --- LMFDB (research profile only: network) --------------------------------------------------


@server.tool()
def lmfdb(
    collection: str,
    query: dict[str, str] | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Read-only lookup in the LMFDB API (www.lmfdb.org/api), one request per call.

    collection: an API table such as ec_curvedata, ec_classdata, nf_fields, mf_newforms,
    g2c_curves, lf_fields, artin_reps. query: field -> value in the API's typed syntax
    (`i11` integer, `s11.a` string, `li1,2` integer list, `py[0,1]` Python literal), e.g.
    {"conductor": "i11"}. fields: columns to return. Results carry LMFDB labels to cite;
    LMFDB data is evidence for the whiteboard, not a certificate.
    """
    if not re.fullmatch(r"[a-z0-9_]+", collection):
        raise ValueError("collection must be an LMFDB API table name")
    if not all(re.fullmatch(r"[A-Za-z0-9_]+", f) for f in [*(fields or []), *(query or {})]):
        raise ValueError("field names must be identifiers")
    params = {**(query or {}), "_format": "json", "_max_count": str(min(limit, 100))}
    if fields:
        params["_fields"] = ",".join(fields)
    url = f"{LMFDB_API}/{collection}/?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "lemmata-mcp"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
    rows = data.get("data", [])
    return {"source": url, "total_returned": len(rows), "results": rows[:limit]}
