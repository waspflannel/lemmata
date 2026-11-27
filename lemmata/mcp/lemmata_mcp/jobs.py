"""Sandboxed runner for worker-written Python (started by common.run_job under landrun).

Reads one JSON job on stdin, prints one JSON result line. Executing the caller's predicate
code is the purpose of this process; it never sees the state databases.

A predicate module defines `hypotheses(**vars)` (bool or list of bools, all must hold) and
`conclusion(**vars)` (bool). A counterexample satisfies every hypothesis and fails the
conclusion.
"""

from __future__ import annotations

import itertools
import json
import math
import random
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

ENUMERATOR_VERSION = "lemmata-enumerate/1"
GRAPH_VERSION = "lemmata-geng/1"
SPOT_CHECK_POINTS = 5000
SHRINK_EVALS = 2000

INF = math.inf


def _load(code: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(code, "<predicate>", "exec"), namespace)
    for name in ("hypotheses", "conclusion"):
        if not callable(namespace.get(name)):
            raise ValueError(f"predicate must define {name}(...)")
    return namespace


def _counterexample(ns: dict[str, Any], args: dict[str, Any]) -> bool:
    hyps = ns["hypotheses"](**args)
    held = all(bool(h) for h in hyps) if isinstance(hyps, list | tuple) else bool(hyps)
    return held and not bool(ns["conclusion"](**args))


# --- integer domains -----------------------------------------------------------------------


def _bounds(var: dict[str, Any]) -> tuple[float, float]:
    lo = {"int": -INF, "nat": 0, "pos": 1}[var.get("type", "int")]
    hi = INF
    if var.get("min") is not None:
        lo = max(lo, var["min"])
    if var.get("max") is not None:
        hi = min(hi, var["max"])
    return lo, hi


def _ring(level: int, lo: float, hi: float) -> list[int]:
    """Values with |v| == level inside [lo, hi], positive first."""
    return [v for v in dict.fromkeys((level, -level)) if lo <= v <= hi]


def _ball(level: int, lo: float, hi: float) -> list[int]:
    """Values with |v| < level inside [lo, hi], by increasing |v|."""
    return [v for k in range(level) for v in _ring(k, lo, hi)]


def _shell(level: int, bounds: list[tuple[float, float]]) -> Iterator[tuple[int, ...]]:
    """Every tuple with max |x_i| == level, partitioned by the first coordinate at |level|."""
    n = len(bounds)
    for i in range(n):
        parts = [
            _ball(level, *bounds[j])
            if j < i
            else _ring(level, *bounds[j])
            if j == i
            else _ball(level + 1, *bounds[j])
            for j in range(n)
        ]
        yield from itertools.product(*parts)


def _boundary_probe(bounds: list[tuple[float, float]]) -> list[tuple[int, ...]]:
    """Domain edges (min, max and their neighbours) that a small-level search would miss."""
    specials = []
    for lo, hi in bounds:
        values = {0, 1, -1}
        values |= {v for v in (lo, lo + 1, hi, hi - 1) if v not in (INF, -INF)}
        specials.append(sorted(int(v) for v in values if lo <= v <= hi))
    if math.prod(len(s) for s in specials) > 100_000:
        return []
    return list(itertools.product(*specials))


def _symmetry_plan(
    variables: list[dict[str, Any]],
    bounds: list[tuple[float, float]],
    symmetries: list[dict[str, Any]],
):
    """Validate declared symmetries; return (canonical test, generators)."""
    index = {v["name"]: i for i, v in enumerate(variables)}
    permuted: set[int] = set()
    groups: list[list[int]] = []
    negated: list[int] = []
    for sym in symmetries:
        if sym["type"] == "permute":
            group = [index[name] for name in sym["vars"]]
            if permuted & set(group):
                raise ValueError("permute groups must be disjoint")
            if len({bounds[i] for i in group}) != 1:
                raise ValueError(f"permute {sym['vars']}: variables have different domains")
            permuted |= set(group)
            groups.append(group)
        elif sym["type"] == "negate":
            i = index[sym["var"]]
            if bounds[i][0] != -bounds[i][1]:
                raise ValueError(f"negate {sym['var']}: domain is not symmetric about 0")
            negated.append(i)
        else:
            raise ValueError(f"unknown symmetry {sym['type']!r} (use permute or negate)")
    for group in groups:
        touched = [i for i in group if i in negated]
        if touched and len(touched) != len(group):
            raise ValueError("negate a permuted variable only if you negate its whole group")

    def canonical(x: tuple[int, ...]) -> bool:
        if any(x[i] < 0 for i in negated):
            return False
        return all(x[g[k]] <= x[g[k + 1]] for g in groups for k in range(len(g) - 1))

    generators: list[Callable[[tuple[int, ...]], tuple[int, ...]]] = []
    for group in groups:
        for a, b in itertools.pairwise(group):

            def swap(x: tuple[int, ...], a: int = a, b: int = b) -> tuple[int, ...]:
                y = list(x)
                y[a], y[b] = y[b], y[a]
                return tuple(y)

            generators.append(swap)
    for i in negated:

        def flip(x: tuple[int, ...], i: int = i) -> tuple[int, ...]:
            y = list(x)
            y[i] = -y[i]
            return tuple(y)

        generators.append(flip)
    return canonical, generators


def _shrink(
    test: Callable[[tuple[int, ...]], bool], x: tuple[int, ...], bounds: list[tuple[float, float]]
) -> tuple[int, ...]:
    """Greedy Plausible-style integer shrinking: try 0, |v|, v/2 and v -/+ 1 toward zero."""
    evals = 0
    improved = True
    while improved and evals < SHRINK_EVALS:
        improved = False
        for i, v in enumerate(x):
            step = 1 if v > 0 else -1
            half = step * (abs(v) // 2)  # exact for big ints, rounds toward zero
            for cand in dict.fromkeys((0, abs(v), half, v - step)):
                lo, hi = bounds[i]
                if cand == v or not lo <= cand <= hi or abs(cand) > abs(v):
                    continue
                y = (*x[:i], cand, *x[i + 1 :])
                evals += 1
                try:
                    if test(y):
                        x, improved = y, True
                        break
                except Exception:
                    continue
            if improved:
                break
    return x


def enumerate_job(job: dict[str, Any]) -> dict[str, Any]:
    ns = _load(job["predicate"])
    variables = job["variables"]
    names = [v["name"] for v in variables]
    bounds = [_bounds(v) for v in variables]
    canonical, generators = _symmetry_plan(variables, bounds, job.get("symmetries", []))
    shard, shards = job.get("shard_index", 0), job.get("shard_count", 1)
    max_level, deadline = job["max_bound"], time.monotonic() + job["timeout"]

    def test(x: tuple[int, ...]) -> bool:
        return _counterexample(ns, dict(zip(names, x, strict=True)))

    # A reduction is used only if the whole counterexample predicate is invariant under
    # every generator on the first SPOT_CHECK_POINTS tuples (a check, not a proof).
    spot_checked = 0
    if generators:
        for x in itertools.islice(
            (x for k in range(max_level + 1) for x in _shell(k, bounds)), SPOT_CHECK_POINTS
        ):
            for g in generators:
                try:
                    same = test(x) == test(g(x))
                except Exception:
                    continue  # undefined at this point: counted as an error by the search
                if not same:
                    return {
                        "outcome": "error",
                        "error": "declared symmetry does not hold",
                        "point": dict(zip(names, x, strict=True)),
                        "image": dict(zip(names, g(x), strict=True)),
                    }
            spot_checked += 1

    witnesses: list[dict[str, Any]] = []
    evaluated = errors = position = 0
    first_error = None
    completed_level = -1
    probe = _boundary_probe(bounds)
    levels: list[tuple[str, Any]] = [("boundary", probe)] + [
        (str(k), None) for k in range(max_level + 1)
    ]
    outcome = "no_witness_in_domain"
    for label, points in levels:
        stream = points if points is not None else _shell(int(label), bounds)
        for x in stream:
            if not canonical(x):
                continue
            position += 1
            if position % shards != shard:
                continue
            if evaluated % 256 == 0 and time.monotonic() > deadline:
                outcome = "timed_out"
                break
            evaluated += 1
            try:
                hit = test(x)
            except Exception as e:
                errors += 1
                first_error = first_error or {
                    "point": dict(zip(names, x, strict=True)),
                    "error": repr(e),
                }
                continue
            if hit:
                small = _shrink(test, x, bounds) if job.get("shrink", True) else x
                witnesses.append(
                    {
                        "found": dict(zip(names, x, strict=True)),
                        "shrunk": dict(zip(names, small, strict=True)),
                    }
                )
                if len(witnesses) >= job.get("max_witnesses", 1):
                    outcome = "lemma_refuted"
                    break
        if outcome != "no_witness_in_domain":
            break
        if label != "boundary":
            completed_level = int(label)
    if witnesses:
        outcome = "lemma_refuted"
    elif errors and outcome == "no_witness_in_domain":
        outcome = "error"
    return {
        "outcome": outcome,
        "witnesses": witnesses,
        "evaluated": evaluated,
        "errors": errors,
        "first_error": first_error,
        "completed_level": completed_level,
        "boundary_probe": probe[:50],
        "symmetry": job.get("symmetries", []),
        "symmetry_spot_checked_points": spot_checked,
        "generator_version": ENUMERATOR_VERSION,
    }


# --- graphs (nauty geng) ----------------------------------------------------------------------


def graph_job(job: dict[str, Any]) -> dict[str, Any]:
    import networkx as nx

    ns = _load(job["predicate"])
    deadline = time.monotonic() + job["timeout"]
    cmd = [job["geng"], "-q", *job["flags"], str(job["n"]), f"{job['res']}/{job['mod']}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None

    def test(g: Any) -> bool:
        return _counterexample(ns, {"G": g})

    rng = random.Random(0)
    witnesses, evaluated, errors, first_error = [], 0, 0, None
    outcome = "no_witness_in_domain"
    for line in proc.stdout:
        if evaluated % 64 == 0 and time.monotonic() > deadline:
            outcome = "timed_out"
            break
        g6 = line.strip()
        graph = nx.from_graph6_bytes(g6.encode())
        evaluated += 1
        try:
            hit = test(graph)
            if evaluated <= 200:  # geng is up to isomorphism: the predicate must be too
                perm = list(graph.nodes)
                rng.shuffle(perm)
                if test(nx.relabel_nodes(graph, dict(zip(graph.nodes, perm, strict=True)))) != hit:
                    proc.kill()
                    return {
                        "outcome": "error",
                        "graph6": g6,
                        "error": "predicate is not isomorphism-invariant",
                    }
        except Exception as e:
            errors += 1
            first_error = first_error or {"graph6": g6, "error": repr(e)}
            continue
        # No shrinking: geng already goes by vertex count, and dropping edges could leave the
        # flag-defined domain (-c, -d2, ...), which the predicate does not know about.
        if hit:
            witnesses.append({"graph6": g6, "edges": sorted(graph.edges)})
            if len(witnesses) >= job.get("max_witnesses", 1):
                outcome = "lemma_refuted"
                break
    proc.kill()
    proc.wait()
    if witnesses:
        outcome = "lemma_refuted"
    elif errors and outcome == "no_witness_in_domain":
        outcome = "error"
    return {
        "outcome": outcome,
        "witnesses": witnesses,
        "evaluated": evaluated,
        "errors": errors,
        "first_error": first_error,
        "generator_version": GRAPH_VERSION,
    }


# --- rigorous intervals (python-flint / Arb) ------------------------------------------------


def flint_job(job: dict[str, Any]) -> dict[str, Any]:
    import flint

    prec, enclosure = job["prec"], None
    while prec <= job["max_prec"]:
        flint.ctx.prec = prec
        namespace = {k: getattr(flint, k) for k in dir(flint) if not k.startswith("_")}
        exec(job.get("setup", ""), namespace)
        value = eval(job["expr"], namespace)
        if isinstance(value, int | flint.fmpz | flint.fmpq):
            sign = "0" if value == 0 else "+" if value > 0 else "-"
            return {"sign": sign, "exact": str(value)}
        if not isinstance(value, flint.arb):
            raise TypeError(f"expression must give an integer, fmpq or arb, not {type(value)}")
        enclosure = value.str(radius=True)
        if value > 0:
            return {"sign": "+", "enclosure": enclosure, "prec": prec}
        if value < 0:
            return {"sign": "-", "enclosure": enclosure, "prec": prec}
        if value.is_exact() and value == 0:
            return {"sign": "0", "enclosure": enclosure, "prec": prec}
        prec *= 2  # the ball straddles 0: refine
    return {
        "sign": "unresolved",
        "enclosure": enclosure,
        "prec": prec // 2,
        "note": "enclosure still contains 0 at max_prec",
    }


def main() -> None:
    job = json.loads(sys.stdin.read())
    kinds = {"enumerate": enumerate_job, "graphs": graph_job, "flint": flint_job}
    try:
        result = kinds[job["kind"]](job)
    except Exception as e:
        result = {"outcome": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
