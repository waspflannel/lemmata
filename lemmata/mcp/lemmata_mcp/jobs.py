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
from fractions import Fraction
from typing import Any

ENUMERATOR_VERSION = "lemmata-enumerate/1"
GRAPH_VERSION = "lemmata-geng/1"
ANNEAL_VERSION = "lemmata-anneal/1"
MINE_VERSION = "lemmata-txgraffiti/1"
TABLE_VERSION = "lemmata-table/1"
TABLE_MAX_ROWS = 10**6
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
    if job.get("file"):  # a graph6 list downloaded by hand (e.g. from House of Graphs)
        cmd = ["cat", job["file"]]
    else:
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
        g6 = line.strip().removeprefix(">>graph6<<")
        if not g6:
            continue
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


# --- integer relations (mpmath PSLQ) -----------------------------------------------------


def pslq_job(job: dict[str, Any]) -> dict[str, Any]:
    import mpmath

    def values(dps: int) -> list[Any]:
        mpmath.mp.dps = dps
        namespace = {k: getattr(mpmath, k) for k in dir(mpmath) if not k.startswith("_")}
        exec(job.get("setup", ""), namespace)
        return [mpmath.mpf(eval(e, namespace)) for e in job["exprs"]]

    xs = values(job["dps"])
    relation = mpmath.pslq(xs, maxcoeff=job["max_coeff"], maxsteps=job["max_steps"])
    if relation is None:
        return {"relation": None, "note": f"no relation with |coefficients| <= {job['max_coeff']}"}
    residual = mpmath.fsum(c * x for c, x in zip(relation, xs, strict=True))
    # A true relation keeps vanishing as precision grows; a numerical accident does not.
    check = mpmath.fsum(c * x for c, x in zip(relation, values(2 * job["dps"]), strict=True))
    return {
        "relation": [int(c) for c in relation],
        "residual": mpmath.nstr(residual, 5),
        "residual_at_double_precision": mpmath.nstr(check, 5),
        "note": "numerical candidate only: prove the identity (Lean, exact algebra) before use",
    }


# --- local search / simulated annealing ---------------------------------------------------


class _GraphSpace:
    """Graphs on n vertices; a move toggles one edge."""

    def __init__(self, job: dict[str, Any], rng: random.Random) -> None:
        import networkx as nx

        self.nx, self.rng, self.job = nx, rng, job
        self.pairs = list(itertools.combinations(range(job["n"]), 2))

    def start(self) -> Any:
        return self.nx.gnp_random_graph(
            self.job["n"], self.job["edge_p"], seed=self.rng.randrange(2**32)
        )

    def move(self, g: Any) -> Any:
        h = g.copy()
        u, v = self.rng.choice(self.pairs)
        if h.has_edge(u, v):
            h.remove_edge(u, v)
        else:
            h.add_edge(u, v)
        return h

    def args(self, g: Any) -> dict[str, Any]:
        return {"G": g}

    def show(self, g: Any) -> dict[str, Any]:
        return {"graph6": self.nx.to_graph6_bytes(g, header=False).decode().strip()}


class _IntSpace:
    """Integer tuples in the variables' domains (clipped to ±max_abs); a move changes one
    coordinate by a small or a tenth-of-range step."""

    def __init__(self, job: dict[str, Any], rng: random.Random) -> None:
        self.rng = rng
        self.names = [v["name"] for v in job["variables"]]
        self.bounds = []
        for var in job["variables"]:
            lo, hi = _bounds(var)
            self.bounds.append((int(max(lo, -job["max_abs"])), int(min(hi, job["max_abs"]))))

    def start(self) -> Any:
        return tuple(self.rng.randint(lo, hi) for lo, hi in self.bounds)

    def move(self, x: Any) -> Any:
        i = self.rng.randrange(len(x))
        lo, hi = self.bounds[i]
        jump = self.rng.choice([1, 1, 2, max(1, (hi - lo) // 10)]) * self.rng.choice([-1, 1])
        return (*x[:i], min(hi, max(lo, x[i] + jump)), *x[i + 1 :])

    def args(self, x: Any) -> dict[str, Any]:
        return dict(zip(self.names, x, strict=True))

    def show(self, x: Any) -> dict[str, Any]:
        return self.args(x)


def local_search_job(job: dict[str, Any]) -> dict[str, Any]:
    """Minimize the worker's score(...) by annealing; stop at the first counterexample."""
    ns = _load(job["predicate"])
    if not callable(ns.get("score")):
        raise ValueError("predicate must also define score(...): lower is closer to a witness")
    rng = random.Random(job["seed"])
    deadline = time.monotonic() + job["timeout"]
    space = _GraphSpace(job, rng) if job.get("n") else _IntSpace(job, rng)
    start, move, args, show = space.start, space.move, space.args, space.show

    evaluated = errors = 0

    def evaluate(x: Any) -> tuple[float, bool]:
        nonlocal evaluated, errors
        evaluated += 1
        try:
            return float(ns["score"](**args(x))), _counterexample(ns, args(x))
        except Exception:
            errors += 1
            return INF, False

    best, best_score, outcome = None, INF, "no_witness_found"
    for _ in range(job["restarts"]):
        x = start()
        score, hit = evaluate(x)
        for t in range(job["steps"]):
            if hit or time.monotonic() > deadline:
                break
            temperature = job["temperature"] * (1 - t / job["steps"]) + 1e-12
            y = move(x)
            y_score, y_hit = evaluate(y)
            if (
                y_hit
                or y_score <= score
                or rng.random() < math.exp((score - y_score) / temperature)
            ):
                x, score, hit = y, y_score, y_hit
            if score < best_score:
                best, best_score = x, score
        if hit:
            return {
                "outcome": "lemma_refuted",
                "witnesses": [show(x)],
                "evaluated": evaluated,
                "errors": errors,
                "generator_version": ANNEAL_VERSION,
            }
        if time.monotonic() > deadline:
            outcome = "timed_out"
            break
    return {
        "outcome": outcome,
        "witnesses": [],
        "best": {"point": show(best), "score": best_score} if best is not None else None,
        "evaluated": evaluated,
        "errors": errors,
        "generator_version": ANNEAL_VERSION,
        "note": "random search: finding nothing says nothing about the domain",
    }


# --- TxGraffiti-style conjecture mining ---------------------------------------------------


def _hull_lines(
    points: list[tuple[Fraction, Fraction]], upper: bool
) -> list[tuple[Fraction, Fraction]]:
    """(slope, intercept) of each edge of the lower (or upper) convex hull: every such line
    bounds all points from below (above) and touches at least two."""
    sign = -1 if upper else 1
    extreme: dict[Fraction, Fraction] = {}
    for x, y in points:
        y = sign * y
        extreme[x] = min(extreme.get(x, y), y)
    hull: list[tuple[Fraction, Fraction]] = []
    for p in sorted(extreme.items()):
        while len(hull) >= 2:
            (ax, ay), (bx, by) = hull[-2], hull[-1]
            if (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    lines = []
    for (ax, ay), (bx, by) in itertools.pairwise(hull):
        slope = (by - ay) / (bx - ax)
        lines.append((sign * slope, sign * (ay - slope * ax)))
    return lines


def _linear(slope: Fraction, intercept: Fraction, name: str) -> str:
    term = name if slope == 1 else f"{slope}*{name}"
    if slope == 0:
        return str(intercept)
    if intercept == 0:
        return term
    return f"{term} {'+' if intercept > 0 else '-'} {abs(intercept)}"


def mine_job(job: dict[str, Any]) -> dict[str, Any]:
    import inspect

    namespace: dict[str, Any] = {}
    exec(compile(job["invariants"], "<invariants>", "exec"), namespace)
    # Only functions defined in the invariants code itself (not imported ones, not _helpers).
    invariants = {
        k: f
        for k, f in namespace.items()
        if inspect.isfunction(f)
        and not k.startswith("_")
        and f.__code__.co_filename == "<invariants>"
    }
    target = job["target"]
    if target not in invariants:
        raise ValueError(f"target {target!r} is not one of the invariants {sorted(invariants)}")
    deadline = time.monotonic() + job["timeout"]

    def objects() -> Iterator[tuple[str, dict[str, Any]]]:
        if job["objects"] == "graphs":
            import networkx as nx

            for n in range(job["n_min"], job["n_max"] + 1):
                cmd = [job["geng"], "-q", *job["flags"], str(n)]
                with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        g6 = line.strip()
                        yield g6, {"G": nx.from_graph6_bytes(g6.encode())}
        else:
            for k in range(job["int_min"], job["int_max"] + 1):
                yield str(k), {"n": k}

    rows: list[tuple[str, dict[str, Fraction]]] = []
    skipped, first_error = 0, None
    for label, kwargs in objects():
        if len(rows) >= job["max_objects"] or time.monotonic() > deadline:
            break
        try:
            values = {name: Fraction(f(**kwargs)) for name, f in invariants.items()}
        except Exception as e:  # undefined or non-numeric somewhere: the object is left out
            skipped += 1
            first_error = first_error or {"object": label, "error": repr(e)}
            continue
        rows.append((label, values))

    candidates = []
    for feature in invariants:
        if feature == target:
            continue
        points = [(v[feature], v[target]) for _, v in rows]
        for upper in (False, True):
            for slope, intercept in _hull_lines(points, upper):
                bound = [slope * v[feature] + intercept for _, v in rows]
                sharp = [label for (label, v), b in zip(rows, bound, strict=True) if v[target] == b]
                relation = "<=" if upper else ">="
                candidates.append(
                    {
                        "statement": f"{target} {relation} {_linear(slope, intercept, feature)}",
                        "upper": upper,
                        "touch": len(sharp),
                        "sharp_examples": sharp[:3],
                        "_bound": bound,
                    }
                )
    # Dalmatian heuristic: keep a conjecture only if it is strictly sharper than every kept
    # one (same direction) on at least one object.
    candidates.sort(key=lambda c: -c["touch"])
    kept: list[dict[str, Any]] = []
    for c in candidates:
        rivals = [k["_bound"] for k in kept if k["upper"] == c["upper"]]
        better = (lambda a, b: a < b) if c["upper"] else (lambda a, b: a > b)
        if not rivals or any(
            all(better(b, r[i]) for r in rivals) for i, b in enumerate(c["_bound"])
        ):
            kept.append(c)
        if len(kept) >= job["top"]:
            break
    return {
        "objects": len(rows),
        "skipped_objects": skipped,
        "first_error": first_error,
        "conjectures": [{k: v for k, v in c.items() if k != "_bound"} for c in kept],
        "generator_version": MINE_VERSION,
        "note": "each conjecture holds on every object tested; it is a candidate, not a theorem",
    }


# --- evolutionary program search (one candidate) ---------------------------------------


def construct_job(job: dict[str, Any]) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(job["program"], "<program>", "exec"), namespace)
    if not callable(namespace.get("construct")):
        raise ValueError("program must define construct() returning a JSON value")
    text = json.dumps(namespace["construct"]())
    if len(text) > 1_000_000:
        raise ValueError("construction is larger than 1 MB of JSON")
    return {"object": json.loads(text)}


def score_job(job: dict[str, Any]) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(job["evaluator"], "<evaluator>", "exec"), namespace)
    value = float(namespace["score"](job["object"]))
    if math.isnan(value):
        raise ValueError("score is NaN")
    return {"score": value}


# --- certificate tables -------------------------------------------------------------------


def table_job(job: dict[str, Any]) -> dict[str, Any]:
    """Run the worker's rows() and validate every row against the fixed field widths."""
    namespace: dict[str, Any] = {}
    exec(compile(job["generator"], "<generator>", "exec"), namespace)
    if not callable(namespace.get("rows")):
        raise ValueError("generator must define rows() yielding tuples of non-negative ints")
    bits = job["field_bits"]
    deadline = time.monotonic() + job["timeout"]
    rows: list[list[int]] = []
    for row in namespace["rows"]():
        row = list(row)
        fits = len(row) == len(bits) and all(
            type(v) is int and 0 <= v < 1 << b for v, b in zip(row, bits, strict=True)
        )
        if not fits:
            raise ValueError(f"row {len(rows)} {row!r} does not fit field_bits {bits}")
        rows.append(row)
        if len(rows) > TABLE_MAX_ROWS:
            raise ValueError(f"more than {TABLE_MAX_ROWS} rows")
        if len(rows) % 1024 == 0 and time.monotonic() > deadline:
            raise TimeoutError(f"rows() still running after {len(rows)} rows")
    return {"rows": rows, "generator_version": TABLE_VERSION}


def main() -> None:
    job = json.loads(sys.stdin.read())
    kinds = {
        "enumerate": enumerate_job,
        "graphs": graph_job,
        "flint": flint_job,
        "pslq": pslq_job,
        "local_search": local_search_job,
        "mine": mine_job,
        "construct": construct_job,
        "score": score_job,
        "table": table_job,
    }
    try:
        result = kinds[job["kind"]](job)
    except Exception as e:
        result = {"outcome": "error", "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
