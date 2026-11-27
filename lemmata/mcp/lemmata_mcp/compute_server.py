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

from . import board, solvers
from .cas import Sage
from .cas import ideal_cofactors as cas_ideal_cofactors
from .common import HOME, PINS, STATE, run, run_job, sha256, tool_path

sage_worker = Sage()
GENG_FLAG = re.compile(r"-[a-zA-Z]+|-[dD]\d+|\d+:\d+")
GRAPH_LISTS = HOME / "graphs"  # graph6 lists saved by hand, e.g. from House of Graphs

server = MCPServer(
    "lemmata-compute",
    instructions=(
        "Search and computation. Predicates are Python defining hypotheses(**vars) and "
        "conclusion(**vars); a counterexample satisfies all hypotheses and fails the "
        "conclusion. Pass `problem` (the challenge id) to record coverage on the whiteboard. "
        "Nothing here is a verdict: witnesses (search, Mace4 models, evolved constructions) "
        "need verdict.replay_witness or a Lean `claim_neg`; UNSAT, LP/SOS certificates, PSLQ "
        "relations and mined conjectures are evidence until Lean checks them."
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


def _geng() -> str:
    geng = tool_path("geng")
    if geng is None:
        raise RuntimeError("geng missing; run setup-wsl.sh nauty")
    return str(geng)


def _geng_flags(flags: list[str] | None) -> list[str]:
    if not all(GENG_FLAG.fullmatch(f) for f in flags or []):
        raise ValueError("geng flags must look like -c, -t, -d2, -D4 or 5:9")
    return flags or []


@server.tool()
def graph_search(
    predicate: str,
    n: int | None = None,
    geng_flags: list[str] | None = None,
    res: int = 0,
    mod: int = 1,
    graph_list: str | None = None,
    timeout_s: int = 120,
    max_witnesses: int = 1,
    problem: str | None = None,
    lemma_id: int | None = None,
) -> dict[str, Any]:
    """Run predicate over all graphs on n vertices from nauty geng (one per isomorphism class),
    or over a saved graph6 list.

    The predicate gets G (a networkx Graph): hypotheses(G), conclusion(G). geng_flags such
    as -c (connected), -t (triangle-free), -b (bipartite), -d2, -D4, or an edge range like
    5:9. res/mod is geng's own sharding. `graph_list` instead names a graph6 file in
    ~/.lemmata/graphs (e.g. a House of Graphs list a person downloaded; its terms do not
    allow automated access, so nothing here fetches it). The predicate is checked for
    isomorphism invariance on the first 200 graphs. Witnesses come as graph6 and edge lists.
    """
    flags = _geng_flags(geng_flags)
    if (n is None) == (graph_list is None):
        raise ValueError("give exactly one of n (geng) or graph_list")
    job: dict[str, Any] = {"kind": "graphs", "predicate": predicate, "max_witnesses": max_witnesses}
    if graph_list is not None:
        path = GRAPH_LISTS / graph_list
        if not re.fullmatch(r"[\w.-]+", graph_list) or not path.is_file():
            lists = sorted(p.name for p in GRAPH_LISTS.glob("*")) if GRAPH_LISTS.exists() else []
            raise ValueError(f"no graph list {graph_list!r} in {GRAPH_LISTS}; have {lists}")
        job["file"] = str(path)
        bounds: dict[str, Any] = {"graph_list": graph_list, "list_sha": sha256(path.read_text())}
        generator, symmetry = "graph_list", ["isomorphism (one graph per listed entry)"]
    else:
        job |= {"n": n, "flags": flags, "res": res, "mod": mod, "geng": _geng()}
        bounds = {"n": n, "geng_flags": flags}
        generator, symmetry = "geng", ["isomorphism (geng canonical forms)"]
    result = run_job(job, timeout_s)
    bounds["predicate_sha"] = sha256(predicate)
    shard = {"index": res, "count": mod} if graph_list is None else {"index": 0, "count": 1}
    _record(problem, lemma_id, generator, result, bounds, shard, symmetry)
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


# --- v2: domain CAS -----------------------------------------------------------------------


@server.tool()
def gap(code: str, timeout_s: int = 60) -> dict[str, Any]:
    """GAP 4 in a fresh process, with SmallGrp, TransGrp, PrimGrp, CTblLib and the other
    Sage-bundled packages. Statements end in `;`; print explicitly, e.g.
    `G := SmallGroup(16, 3);; Print(StructureDescription(G), "\\n");`."""
    return solvers.cas_cli("gap", code, timeout_s)


@server.tool()
def pari(code: str, timeout_s: int = 60) -> dict[str, Any]:
    """PARI/GP in a fresh process (parisizemax 4 GB): number fields, L-functions, factoring,
    class groups, elliptic curves. Print explicitly, e.g. `print(factor(2^64+1))`."""
    return solvers.cas_cli("gp", code, timeout_s)


@server.tool()
def macaulay2(code: str, timeout_s: int = 120) -> dict[str, Any]:
    """Macaulay2 script in a fresh process: Groebner bases, resolutions, Hilbert series,
    primary decomposition. Print explicitly, e.g.
    `R = QQ[x,y]; print toString gens gb ideal(x^2-y, y^2-x)`."""
    return solvers.cas_cli("M2", code, timeout_s)


@server.tool()
def mace4(
    problem: str,
    start_size: int = 2,
    end_size: int = 10,
    max_models: int = 1,
    timeout_s: int = 60,
) -> dict[str, Any]:
    """Mace4 finite model search on LADR input (`formulas(assumptions). ... end_of_list.`;
    put the negated conjecture among the assumptions to look for a counterexample).

    Models come back as tables (`functions` / `relations`, binary ones as row lists) and as
    Lean `Fin n` definitions. A model is a candidate: DISPROVED needs it replayed by
    verdict.replay_witness against the frozen predicate (tables as list[list[int]]) or a
    Lean proof of `claim_neg`, e.g. by `decide` on those definitions.
    """
    return solvers.mace4(problem, start_size, end_size, max_models, timeout_s)


@server.tool()
def snappy(code: str, timeout_s: int = 120) -> dict[str, Any]:
    """SnapPy (3-manifolds, knots, hyperbolic structures) in the warm Sage worker, with
    `snappy` imported (write `snappy.Manifold('m004')`); the value of a final expression is
    returned and state persists like `sage`.

    Rigorous (interval or exact) routines: M.verify_hyperbolicity(), M.volume(verified=
    True), M.cusp_areas(verified=True), M.canonical_triangulation(verified=True),
    M.isometry_signature(verified=True), M.complex_volume(verified_modulo_2_torsion=True),
    and an explicit isometry from M.is_isometric_to(N, return_isometries=True). Exact
    combinatorics: fundamental_group(), homology(). Numerical evidence only: unverified
    volume, shapes, length_spectrum, identify(), and a False from is_isometric_to. None of
    it is a verdict without Lean.
    """
    return sage_worker.run("import snappy\n" + code, timeout_s)


# --- v2: exact inequalities -----------------------------------------------------------------


@server.tool()
def exact_lp(
    objective: dict[str, Any],
    constraints: list[dict[str, Any]],
    bounds: dict[str, list[Any]] | None = None,
    maximize: bool = False,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Exact rational LP with SoPlex (rational read and solve, zero tolerances).

    objective: {"x": "1", "y": "1/2"}; constraints: [{"name": "h1", "coefs": {"x": 1,
    "y": 2}, "op": "<=" | ">=" | "=", "rhs": "3"}]; bounds: {"x": [lo, hi]} with null for
    infinite (default: free). Numbers are ints or rational strings. Returns the exact
    optimum with primal and dual, or a Farkas ray if infeasible; both certificates are
    re-checked here in exact arithmetic (`optimality_certified`, `infeasibility_certified`).
    `lean_hint` names the rows with nonzero multipliers (name rows like your hypotheses).
    """
    return solvers.exact_lp(objective, constraints, bounds, maximize, timeout_s)


@server.tool()
def sos_certificate(polynomial: str, timeout_s: int = 120, max_basis: int = 120) -> dict[str, Any]:
    """Sum-of-squares certificate for 0 <= p, p a polynomial in Lean syntax
    (`x^4 - 2*x^2*y + y^2 + 1`).

    CSDP solves the Gram-matrix SDP; the result is rounded to rationals, projected exactly
    onto p's coefficients, factored LDLᵀ over QQ and re-expanded, so a returned `identity`
    p = Σ dᵢ·qᵢ² (dᵢ ≥ 0) holds exactly. `lean_tactic` (nlinarith with the squares) still
    has to pass lean.check. No certificate does not mean p can be negative: not every
    nonnegative polynomial is a sum of squares.
    """
    return solvers.sos_certificate(sage_worker, polynomial, timeout_s, max_basis)


# --- v2: candidate sources ------------------------------------------------------------------


@server.tool()
def pslq(
    exprs: list[str],
    setup: str = "",
    dps: int = 60,
    max_coeff: int = 10**6,
    max_steps: int = 10**5,
    timeout_s: int = 60,
) -> dict[str, Any]:
    """Integer relation search (mpmath PSLQ): integers c with Σ cᵢ·xᵢ ≈ 0.

    exprs are mpmath expressions (`pi`, `log(2)`, `zeta(3)`, `mpf(1)` for a constant term);
    `setup` runs first. The relation is re-evaluated at double precision as a stability
    check. A relation is a numerical candidate, never a proof.
    """
    job = {"kind": "pslq", "exprs": exprs, "setup": setup, "dps": dps}
    return run_job(job | {"max_coeff": max_coeff, "max_steps": max_steps}, timeout_s)


@server.tool()
def local_search(
    predicate: str,
    variables: list[dict[str, Any]] | None = None,
    n: int | None = None,
    steps: int = 20000,
    restarts: int = 20,
    temperature: float = 1.0,
    max_abs: int = 10**6,
    edge_p: float = 0.5,
    seed: int = 0,
    timeout_s: int = 60,
    problem: str | None = None,
    lemma_id: int | None = None,
) -> dict[str, Any]:
    """Simulated annealing for a counterexample where exhaustive search is hopeless.

    The predicate defines hypotheses, conclusion and score(...) (lower = closer to a
    counterexample, e.g. the conclusion's slack). Integer mode: `variables` as in
    `enumerate` (unbounded ones clipped to ±max_abs). Graph mode: `n` vertices, each move
    toggles one edge, the functions get G. Stops at the first counterexample; replay it with
    verdict.replay_witness. Finding none covers nothing, so only refutations are recorded.
    """
    if (variables is None) == (n is None):
        raise ValueError("give exactly one of variables (integers) or n (graphs)")
    job = {"kind": "local_search", "predicate": predicate, "variables": variables, "n": n}
    job |= {"steps": steps, "restarts": restarts, "temperature": temperature}
    job |= {"max_abs": max_abs, "edge_p": edge_p, "seed": seed}
    result = run_job(job, timeout_s)
    if result.get("outcome") == "lemma_refuted":
        bounds = {"variables": variables, "n": n, "predicate_sha": sha256(predicate)}
        _record(problem, lemma_id, "anneal", result, bounds, {"index": seed, "count": 1}, [])
    return result


@server.tool()
def conjecture_mine(
    invariants: str,
    target: str,
    objects: str = "graphs",
    n_min: int = 1,
    n_max: int = 8,
    geng_flags: list[str] | None = None,
    int_min: int = 1,
    int_max: int = 1000,
    max_objects: int = 20000,
    top: int = 15,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """TxGraffiti-style conjectures: linear bounds `target >= a*f + b` (or `<=`).

    `invariants` is Python defining one function per invariant (prefix helpers with `_`),
    of G (objects="graphs", from geng on n_min..n_max vertices with geng_flags) or of n
    (objects="integers", int_min..int_max); return exact values (ints, Fractions). For
    each other invariant f, the edges of the convex hull of the (f, target) points give
    bounds that hold on every object and are sharp on at least two; they are ranked by how
    many objects they are sharp on and filtered Dalmatian-style. Conjectures are leads:
    test them with enumerate/graph_search on larger families before trying to prove one.
    """
    if objects not in ("graphs", "integers"):
        raise ValueError("objects must be graphs or integers")
    job = {"kind": "mine", "invariants": invariants, "target": target, "objects": objects}
    job |= {"n_min": n_min, "n_max": n_max, "flags": _geng_flags(geng_flags)}
    job |= {"geng": _geng() if objects == "graphs" else "", "int_min": int_min}
    job |= {"int_max": int_max, "max_objects": max_objects, "top": top}
    return run_job(job, timeout_s)


@server.tool()
def evolve(
    run_id: str,
    program: str | None = None,
    evaluator: str | None = None,
    parent_id: int | None = None,
    k: int = 3,
    problem: str | None = None,
    timeout_s: int = 60,
) -> dict[str, Any]:
    """OpenEvolve-style evolutionary search for constructions, with you as the mutation
    operator: no external model or API is called.

    Start a run with `evaluator`: Python defining score(obj) -> float (higher is better),
    frozen for the run. Then submit `program`s defining construct() -> JSON value (a
    coloring, a point set, a matrix, ...), each a mutation of a parent from the previous
    reply (`parent_id`). construct() and score() run in separate sandboxed processes. Every
    reply lists parents to mutate next (best k) and inspirations (best of other program-
    size niches). The population lives on the whiteboard, so several workers can evolve one
    run. A construction becomes a result only through verdict.replay_witness or Lean.
    """
    frozen = board.evolve_run(run_id, evaluator, problem)
    reply: dict[str, Any] = {}
    if program is not None:
        built = run_job({"kind": "construct", "program": program}, timeout_s)
        score, error = None, built.get("error")
        if error is None:
            job = {"kind": "score", "evaluator": frozen, "object": built["object"]}
            scored = run_job(job, timeout_s)
            score, error = scored.get("score"), scored.get("error")
        program_id = board.add_program(
            run_id, program, parent_id, score, built.get("object"), error
        )
        reply = {"program_id": program_id or "duplicate", "score": score, "error": error}
    return reply | board.evolve_sample(run_id, k)
