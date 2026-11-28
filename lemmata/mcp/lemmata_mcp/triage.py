"""`lemmata-mcp triage <challenge-id | lts-problem-id>`: operator check before a run.

Advisory only. Triage reads the upstream Left to Solve status and the local history, and runs
a short bounded counterexample search against the frozen predicate. It never submits or
records a verdict. A witness it finds is a DISPROVED candidate that a worker must still
certify through the verdict server. It is deliberately not an MCP tool: a worker that saw its
score could skip work.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from typing import Any
from urllib.parse import quote

from . import board, verdict_server
from .common import STATE, run_job, sha256
from .left_to_solve import PROBLEM_ID, LTSError, read_response, search_row

CHALLENGE_ID = re.compile(r"[0-9a-f]{16}")
LADDER = (8, 32, 128)
INTEGER_TYPES = {"int", "nat", "pos"}  # what the `enumerate` job searches
KIND_POINTS = {"warmup": 40, "known": 25, "open": 5}
ADVISORY = "Advisory only: not a verdict. Triage never submits or records one."


def resolve(target: str) -> tuple[dict[str, Any] | None, str | None]:
    """(frozen challenge or None, Left to Solve problem id or None)."""
    with closing(verdict_server.connect()) as db:
        if CHALLENGE_ID.fullmatch(target):
            c = verdict_server._challenge(target)
            row = db.execute(
                "SELECT problem_id FROM left_to_solve_briefs WHERE challenge_id = ?", (target,)
            ).fetchone()
            return c, row[0] if row else None
        if not PROBLEM_ID.fullmatch(target):
            raise ValueError("expected a 16-hex challenge id or a Left to Solve problem id")
        row = db.execute(
            "SELECT challenge_id FROM left_to_solve_briefs WHERE problem_id = ?"
            " ORDER BY rowid DESC",
            (target,),
        ).fetchone()
    return (verdict_server._challenge(row[0]) if row else None), target


def upstream(problem_id: str) -> dict[str, Any]:
    try:
        details = read_response(
            f"/api/problems/{quote(problem_id, safe='')}", problem_id=problem_id
        )
        row = search_row(problem_id, details)
    except LTSError as error:
        return {"error": str(error)}
    if row is None:
        return {"error": "problem not found in the catalog search"}
    return {key: row.get(key) for key in ("solved", "kind", "difficulty", "liveAgents")}


def history(challenge_id: str) -> dict[str, Any]:
    with closing(verdict_server.connect()) as db:
        verdicts = dict(
            db.execute(
                "SELECT verdict, count(*) FROM submissions WHERE challenge_id = ? AND accepted = 1"
                " GROUP BY 1",
                (challenge_id,),
            ).fetchall()
        )
    with closing(board.connect()) as db:

        def counts(sql: str) -> dict[str, int]:
            return dict(db.execute(sql, (challenge_id,)).fetchall())

        return {
            "accepted_submissions": verdicts,
            "lemmas": counts("SELECT status, count(*) FROM lemmas WHERE problem = ? GROUP BY 1"),
            "coverage": counts(
                "SELECT outcome, count(*) FROM coverage WHERE problem = ? GROUP BY 1"
            ),
            "dead_ends": db.execute(
                "SELECT count(*) FROM dead_ends WHERE problem = ?", (challenge_id,)
            ).fetchone()[0],
        }


def search(c: dict[str, Any], budget_s: float) -> dict[str, Any]:
    """Size ladder of `enumerate` jobs, exactly as compute.enumerate runs them, each recorded
    as coverage. The first witness is replayed by the verdict server's independent replay."""
    if not c["predicate"]:
        return {"skipped": "not computable from the frozen statement (no predicate)"}
    types = {v.get("type", "int") for v in c["variables"]}
    if not types <= INTEGER_TYPES:
        return {
            "skipped": f"enumerate searches int/nat/pos only, not {sorted(types - INTEGER_TYPES)}"
        }
    deadline = time.monotonic() + budget_s
    rungs: list[dict[str, Any]] = []
    for bound in LADDER:
        left = deadline - time.monotonic()
        if left < 1:
            break
        job = {"predicate": c["predicate"], "variables": c["variables"], "max_bound": bound}
        result = run_job(
            {**job, "kind": "enumerate", "symmetries": [], "max_witnesses": 1, "shrink": True},
            left,
        )
        outcome = result.get("outcome", "error")
        rung = {"max_bound": bound, "outcome": outcome, "evaluated": result.get("evaluated")}
        if outcome in board.COVERAGE_OUTCOMES:
            rung["coverage_id"] = board.add_coverage(
                c["id"],
                None,
                "triage-enumerate",
                result.get("generator_version", "triage-enumerate"),
                {
                    "max_bound": bound,
                    "variables": c["variables"],
                    "predicate_sha": sha256(c["predicate"]),
                },
                {"index": 0, "count": 1},
                [],
                outcome,
                {k: result[k] for k in ("completed_level", "evaluated") if k in result},
                result.get("witnesses"),
            )
        if "error" in result:
            rung["error"] = str(result["error"])[:500]
        rungs.append(rung)
        if result.get("witnesses"):
            witness = result["witnesses"][0]["shrunk"]
            return {
                "rungs": rungs,
                "witness": witness,
                "replay": verdict_server._replay(c, witness),
            }
        if outcome != "no_witness_in_domain":
            break
    return {"rungs": rungs}


def claim_tokens(c: dict[str, Any]) -> int:
    """Rough token count of the frozen Lean prefix (LTS claims point into the preamble)."""
    return len(re.findall(r"\w+|[^\w\s]", verdict_server._prefix(c)))


def score(report: dict[str, Any], c: dict[str, Any] | None) -> tuple[int, str, list[list[Any]]]:
    # ponytail: hand rubric; calibrate after ~20 triaged runs
    up, found = report["upstream"], report["search"]
    if up.get("solved") is True:
        return 0, "already-solved", [["Left to Solve marks it solved", "score 0"]]
    if found.get("replay", {}).get("replayed"):
        return 100, "disprove-now", [["search witness replayed", "score 100"]]
    factors: list[list[Any]] = [["base", 50]]
    if up.get("kind") in KIND_POINTS:
        factors.append([f"Left to Solve kind {up['kind']}", KIND_POINTS[up["kind"]]])
    if type(up.get("difficulty")) is int:
        # The catalog's difficulty runs roughly 0-100 (A100800: 52), so 0.3 per point.
        factors.append([f"difficulty {up['difficulty']}", -min(30, up["difficulty"] * 3 // 10)])
    if c is not None:
        if c["predicate"]:
            factors.append(["frozen predicate (computable)", 10])
        if (tokens := claim_tokens(c)) < 400:
            factors.append([f"Lean prefix ~{tokens} tokens (< 400)", 10])
        # ponytail: every UNRESOLVED counts until the goal tree can tell which ones made progress
        unresolved = report["history"]["accepted_submissions"].get("UNRESOLVED", 0)
        if unresolved:
            factors.append(
                [f"{unresolved} accepted UNRESOLVED round(s)", -min(30, 10 * unresolved)]
            )
    return max(0, min(100, sum(f[1] for f in factors))), "scored", factors


def triage(target: str, budget_s: float) -> dict[str, Any]:
    c, problem_id = resolve(target)
    report: dict[str, Any] = {
        "target": target,
        "advisory": ADVISORY,
        "challenge": None if c is None else {"id": c["id"], "title": c["title"]},
        "left_to_solve_id": problem_id,
        "upstream": upstream(problem_id)
        if problem_id
        else {"skipped": "not a Left to Solve challenge"},
    }
    report["history"] = history(c["id"]) if c else None
    if report["upstream"].get("solved") is True:
        report["search"] = {"skipped": "already solved upstream"}
    elif c is None:
        report["search"] = {"skipped": "not imported; run `lemmata-mcp lts import` first"}
    else:
        report["search"] = search(c, budget_s)
    report["score"], report["status"], report["factors"] = score(report, c)
    return report


def markdown(r: dict[str, Any]) -> str:
    lines = [f"# Triage: {r['target']}", "", r["advisory"], ""]
    lines.append(f"**Status: {r['status']}**, score {r['score']}/100")
    c = r["challenge"]
    lines.append(f"- Challenge: {c['id']} ({c['title']})" if c else "- Challenge: not imported")
    up = r["upstream"]
    if "error" in up:
        lines.append(f"- Upstream: unavailable ({up['error']})")
    elif "skipped" in up:
        lines.append(f"- Upstream: none, {up['skipped']}")
    else:
        lines.append(
            f"- Upstream ({r['left_to_solve_id']}): solved {'yes' if up['solved'] else 'no'},"
            f" kind {up['kind']}, difficulty {up['difficulty']}, live agents {up['liveAgents']}"
        )
    if h := r["history"]:
        subs = ", ".join(f"{v} x{n}" for v, n in h["accepted_submissions"].items()) or "none"
        lemmas = ", ".join(f"{s} {n}" for s, n in h["lemmas"].items()) or "none"
        coverage = ", ".join(f"{o} {n}" for o, n in h["coverage"].items()) or "none"
        lines.append(
            f"- History: accepted submissions {subs}; lemmas {lemmas}; coverage {coverage};"
            f" dead ends {h['dead_ends']}"
        )
    s = r["search"]
    if "skipped" in s:
        lines.append(f"- Search: skipped, {s['skipped']}")
    for rung in s.get("rungs", []):
        extra = f", coverage #{rung['coverage_id']}" if "coverage_id" in rung else ""
        extra += f", {rung['error']}" if "error" in rung else ""
        lines.append(
            f"- Search to bound {rung['max_bound']}: {rung['outcome']},"
            f" {rung['evaluated']} points{extra}"
        )
    if "witness" in s:
        witness = json.dumps(s["witness"])
        if s["replay"].get("replayed"):
            lines.append(
                f"- DISPROVED candidate, not a verdict: witness {witness} replayed against the"
                " frozen predicate. A worker must still certify it, with a comparator-checked"
                " Lean `claim_neg` or by submitting it through verdict.submit_verdict, which"
                " replays it independently."
            )
        else:
            reason = s["replay"].get("reason", "replay failed")
            lines.append(f"- Search witness {witness} did not replay ({reason}); not a candidate")
    lines += ["", "Score factors:", *(f"- {name}: {points}" for name, points in r["factors"])]
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="lemmata-mcp triage", description=__doc__)
    parser.add_argument("id", help="16-hex challenge id or Left to Solve problem id")
    parser.add_argument("--budget-s", type=int, default=120, help="search budget, 1-3600 s")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = parser.parse_args(argv)
    if not 1 <= args.budget_s <= 3600:
        sys.exit("triage: --budget-s must be 1-3600")
    try:
        report = triage(args.id, args.budget_s)
        out = STATE / "triage" / f"{quote(args.id, safe='')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        sys.exit(f"triage: {error}")
    print(json.dumps(report, indent=1, ensure_ascii=False) if args.json else markdown(report))
    print(f"Saved {out}", file=sys.stderr)
