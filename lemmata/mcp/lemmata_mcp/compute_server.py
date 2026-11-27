"""Compute server: bounded counterexample search, SAT with checked proofs, graph search, CAS
and rigorous intervals. Nothing here is a verdict: a witness must still pass the verdict
server's independent replay, and UNSAT is evidence, never a proof by itself."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from pysat.card import CardEnc
from pysat.formula import CNF, IDPool

from . import board
from .cas import Sage
from .cas import ideal_cofactors as cas_ideal_cofactors
from .common import PINS, STATE, run, run_job, sha256, tool_path

sage_worker = Sage()
GENG_FLAG = re.compile(r"-[a-zA-Z]+|-[dD]\d+|\d+:\d+")

server = MCPServer(
    "lemmata-compute",
    instructions=(
        "Search and computation. Predicates are Python defining hypotheses(**vars) and "
        "conclusion(**vars); a counterexample satisfies all hypotheses and fails the "
        "conclusion. Pass `problem` (the challenge id) to record coverage on the whiteboard. "
        "Witnesses still need verdict.replay_witness; UNSAT is never a verdict."
    ),
)


def _record(
    problem: str | None,
    lemma_id: int | None,
    generator: str,
    result: dict[str, Any],
    bounds: dict[str, Any],
    shard: dict[str, Any],
    symmetry: list[Any],
    artifact: str | None = None,
) -> None:
    if problem is None or result.get("outcome") not in board.COVERAGE_OUTCOMES:
        return
    witness = result.get("witnesses") or result.get("true")
    reached = {k: result[k] for k in ("completed_level", "evaluated") if k in result}
    result["coverage_id"] = board.add_coverage(
        problem,
        lemma_id,
        generator,
        result.get("generator_version", generator),
        bounds,
        shard,
        symmetry,
        result["outcome"],
        reached,
        witness,
        artifact,
    )


@server.tool()
def enumerate(
    predicate: str,
    variables: list[dict[str, Any]],
    max_bound: int,
    symmetries: list[dict[str, Any]] | None = None,
    timeout_s: int = 60,
    shard_index: int = 0,
    shard_count: int = 1,
    max_witnesses: int = 1,
    shrink: bool = True,
    problem: str | None = None,
    lemma_id: int | None = None,
) -> dict[str, Any]:
    """Size-ordered integer search for a counterexample.

    variables: [{"name": "a", "type": "int"|"nat"|"pos", "min"?: int, "max"?: int}].
    Order: domain boundary probe (0, ±1, min, max and neighbours), then shells of increasing
    max |x_i| = 0, 1, ..., max_bound, so zero, negatives, equal values and edges come early.
    symmetries: [{"type": "permute", "vars": [...]}, {"type": "negate", "var": "a"}]; each is
    spot-checked against the whole predicate first and refused if it fails. Shards split the
    canonical sequence by position mod shard_count. Witnesses are shrunk Plausible-style.
    outcome: lemma_refuted | no_witness_in_domain (whole box searched) | timed_out | error.
    """
    result = run_job(
        {
            "kind": "enumerate",
            "predicate": predicate,
            "variables": variables,
            "max_bound": max_bound,
            "symmetries": symmetries or [],
            "shard_index": shard_index,
            "shard_count": shard_count,
            "max_witnesses": max_witnesses,
            "shrink": shrink,
        },
        timeout_s,
    )
    bounds = {"max_bound": max_bound, "variables": variables, "predicate_sha": sha256(predicate)}
    _record(
        problem,
        lemma_id,
        "enumerate",
        result,
        bounds,
        {"index": shard_index, "count": shard_count},
        symmetries or [],
    )
    return result


@server.tool()
def graph_search(
    predicate: str,
    n: int,
    geng_flags: list[str] | None = None,
    res: int = 0,
    mod: int = 1,
    timeout_s: int = 120,
    max_witnesses: int = 1,
    problem: str | None = None,
    lemma_id: int | None = None,
) -> dict[str, Any]:
    """Run predicate over all graphs on n vertices from nauty geng (one per isomorphism class).

    The predicate gets G (a networkx Graph): hypotheses(G), conclusion(G). geng_flags such
    as -c (connected), -t (triangle-free), -b (bipartite), -d2, -D4, or an edge range like
    5:9. res/mod is geng's own sharding. The predicate is checked for isomorphism invariance
    on the first 200 graphs. Witnesses come as graph6 and edge lists, smallest order first.
    """
    flags = geng_flags or []
    if not all(GENG_FLAG.fullmatch(f) for f in flags):
        raise ValueError("geng flags must look like -c, -t, -d2, -D4 or 5:9")
    geng = tool_path("geng")
    if geng is None:
        raise RuntimeError("geng missing; run setup-wsl.sh nauty")
    result = run_job(
        {
            "kind": "graphs",
            "predicate": predicate,
            "n": n,
            "flags": flags,
            "res": res,
            "mod": mod,
            "geng": str(geng),
            "max_witnesses": max_witnesses,
        },
        timeout_s,
    )
    bounds = {"n": n, "geng_flags": flags, "predicate_sha": sha256(predicate)}
    _record(
        problem,
        lemma_id,
        "geng",
        result,
        bounds,
        {"index": res, "count": mod},
        ["isomorphism (geng canonical forms)"],
    )
    return result


@server.tool()
def sat_solve(
    clauses: list[list[str]] | None = None,
    dimacs: str | None = None,
    cardinality: list[dict[str, Any]] | None = None,
    timeout_s: int = 300,
    check_proof: bool = True,
    problem: str | None = None,
    lemma_id: int | None = None,
) -> dict[str, Any]:
    """CaDiCaL on a CNF, with a DRAT proof checked by drat-trim when UNSAT.

    clauses use named literals: [["x1", "-x2"], ...]. cardinality: [{"lits": [...], "op":
    "atmost"|"atleast"|"equals", "k": 2}] (PySAT encodings). Or pass raw `dimacs`. A SAT model
    is decoded back to the named variables that are true. The CNF and proof trace are kept
    under the artifact directory. UNSAT never counts as a verdict by itself: cite the
    artifact as evidence, or formalize the claim in Lean.
    """
    cnf = CNF(from_string=dimacs) if dimacs else CNF()
    raw_vars = cnf.nv or 0
    pool = IDPool(start_from=raw_vars + 1)  # named variables never collide with DIMACS ones

    def lit(text: str) -> int:
        return -pool.id(text[1:]) if text.startswith("-") else pool.id(text)

    for clause in clauses or []:
        cnf.append([lit(x) for x in clause])
    for card in cardinality or []:
        encode = {"atmost": CardEnc.atmost, "atleast": CardEnc.atleast, "equals": CardEnc.equals}[
            card["op"]
        ]
        lits = [lit(x) for x in card["lits"]]
        cnf.extend(encode(lits=lits, bound=card["k"], vpool=pool).clauses)
    named = dict(pool.obj2id)  # encoding auxiliaries are not in obj2id
    cnf_text = cnf.to_dimacs()
    directory = STATE / "artifacts" / "sat" / sha256(cnf_text)[:16]
    directory.mkdir(parents=True, exist_ok=True)
    formula, proof = directory / "formula.cnf", directory / "proof.drat"
    formula.write_text(cnf_text)
    cadical = tool_path("cadical")
    if cadical is None:
        raise RuntimeError("cadical missing; run setup-wsl.sh sat")
    started = time.monotonic()
    code, out, _ = run(
        [str(cadical), "-q", "--no-binary", "-t", str(timeout_s), str(formula), str(proof)],
        timeout=timeout_s + 30,
    )
    result: dict[str, Any] = {
        "artifact": str(directory),
        "variables": cnf.nv,
        "clauses": len(cnf.clauses),
        "seconds": round(time.monotonic() - started, 2),
        "generator_version": f"cadical {PINS['CADICAL_REV']}",
    }
    if code == 10:
        values = {
            int(v) for line in out.splitlines() if line.startswith("v ") for v in line[2:].split()
        }
        result["status"] = "SAT"
        # Named variables that are true; raw DIMACS variables (if any) by number.
        result["true"] = sorted(str(k) for k, v in named.items() if v in values)
        if raw_vars:
            result["true_dimacs"] = sorted(v for v in values if 0 < v <= raw_vars)
        result["outcome"] = "lemma_refuted"
    elif code == 20:
        result["status"] = "UNSAT"
        result["note"] = "UNSAT is not a verdict. Keep the CNF and proof as evidence."
        result["outcome"] = "no_witness_in_domain"
        if check_proof:
            drat = tool_path("drat-trim")
            if drat is None:
                raise RuntimeError("drat-trim missing; run setup-wsl.sh sat")
            dcode, dout, _ = run(
                [str(drat), str(formula), str(proof), "-t", str(timeout_s)], timeout=timeout_s + 30
            )
            verified = dcode is not None and "s VERIFIED" in dout
            result["drat"] = "verified" if verified else "not verified"
            if not verified:
                result["outcome"] = "error"
                result["drat_output"] = dout[-2000:]
    else:
        result["status"] = "UNKNOWN"
        result["outcome"] = "timed_out"
    (directory / "result.json").write_text(json.dumps(result, indent=1))
    bounds = {"cnf_sha": sha256(cnf_text), "vars": cnf.nv, "clauses": len(cnf.clauses)}
    _record(problem, lemma_id, "sat", result, bounds, {"index": 0, "count": 1}, [], str(directory))
    return result


@server.tool()
def sage(code: str, timeout_s: int = 60, reset: bool = False) -> dict[str, Any]:
    """Evaluate SageMath code in a warm worker (state persists between calls; `reset` clears
    it). Returns printed output and the value of a final expression."""
    return sage_worker.run(code, timeout_s, reset)


@server.tool()
def ideal_cofactors(target: str, hypotheses: dict[str, str], timeout_s: int = 60) -> dict[str, Any]:
    """Propose `linear_combination` coefficients for a polynomial equation goal.

    target: "lhs = rhs" in Lean syntax; hypotheses: {"h1": "a * b = 1", ...}. Uses a Groebner
    basis over QQ (Sage `lift`). The returned tactic must still be checked in Lean.
    """
    return cas_ideal_cofactors(sage_worker, target, hypotheses, timeout_s)


@server.tool()
def interval_sign(
    expr: str, setup: str = "", prec: int = 64, max_prec: int = 8192, timeout_s: int = 60
) -> dict[str, Any]:
    """Rigorous sign of an exact (fmpz/fmpq) or ball (arb) value with python-flint.

    `expr` sees python-flint's names (arb, fmpq, fmpz, arb.pi(), ...); `setup` runs first at
    each precision. If the enclosure straddles 0, precision doubles up to max_prec, then the
    answer is `unresolved`, never a guess.
    """
    return run_job(
        {"kind": "flint", "expr": expr, "setup": setup, "prec": prec, "max_prec": max_prec},
        timeout_s,
    )
