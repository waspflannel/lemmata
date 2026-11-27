"""Domain CAS and exact solvers behind the compute server: GAP, PARI/GP and Macaulay2 as
sandboxed CLIs, Mace4 finite models (with Lean tables), SoPlex exact LP with an
independently re-checked dual certificate, and sum-of-squares certificates (CSDP in Sage,
rounded to exact rationals and re-expanded). Every output is a candidate or evidence; a
verdict still needs Lean (comparator) or the frozen witness replay."""

from __future__ import annotations

import json
import re
from fractions import Fraction
from typing import Any

from .cas import Sage, lean_to_sage
from .common import SCRATCH, base_env, limit_memory, run, sandboxed, sha256, tool_path

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAS_MEMORY = 8 << 30  # GAP's workspace (-o 4g) and gp's parisizemax (4G) fit below this


def _need(name: str, step: str) -> str:
    path = tool_path(name)
    if path is None:
        raise RuntimeError(f"{name} missing; run setup-wsl.sh {step}")
    return str(path)


def _work_file(kind: str, text: str, suffix: str):
    directory = SCRATCH / kind
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha256(text)[:16]}{suffix}"
    path.write_text(text)
    return path


# --- GAP, PARI/GP, Macaulay2 --------------------------------------------------------------


def cas_cli(system: str, code: str, timeout: int) -> dict[str, Any]:
    """Run `code` in a fresh sandboxed GAP, gp or M2 process; return its output."""
    env = base_env(HOME=str(SCRATCH))  # GAP and M2 keep per-user files under $HOME
    stdin = None
    if system == "gap":
        cmd = [_need("gap", "soplex"), "-q", "-b", "-o", "4g"]
        stdin = code + "\nQUIT;\n"
    elif system == "gp":
        cmd = [_need("gp", "soplex"), "-q", "-f", "-D", "parisizemax=4G"]
        stdin = code + "\n\\q\n"
    else:
        cmd = [_need("M2", "macaulay2"), "--script", str(_work_file("m2", code, ".m2"))]
    # Address-space cap so one runaway Groebner basis cannot take the whole WSL VM.
    code_, out, err = run(
        sandboxed(cmd, env, [SCRATCH]),
        timeout=timeout,
        env=env,
        stdin=stdin,
        preexec=limit_memory(CAS_MEMORY),
    )
    if system == "gap":  # conda's GAP misses its optional packagemanager; not an error
        out = re.sub(
            r"#I  packagemanager package is not available.*\n#I  and it is present.*\n", "", out
        )
    return {
        "ok": code_ == 0,
        "timed_out": code_ is None,
        "output": out[-20000:],
        "stderr": "\n".join(line for line in err.splitlines() if not line.startswith("[landrun"))[
            -4000:
        ],
    }


# --- Mace4 --------------------------------------------------------------------------------

_MODEL = re.compile(r"interpretation\(\s*(\d+),\s*\[[^\]]*\],\s*\[(.*?)\]\)\.", re.DOTALL)
_SYMBOL = re.compile(r"(function|relation)\((.+?)(?:\(([_,]+)\))?,\s*\[([^\]]*)\]\)", re.DOTALL)


def _table(values: list[int], arity: int, n: int) -> Any:
    if arity == 0:
        return values[0]
    if arity == 1:
        return values
    rows = [values[i : i + n] for i in range(0, len(values), n)]
    return rows if arity == 2 else values  # arity >= 3 stays flat (row-major)


def _lean_def(ident: str, kind: str, arity: int, n: int, table: Any) -> str | None:
    def vec(xs: list[Any]) -> str:
        return "![" + ", ".join(str(x).lower() for x in xs) + "]"

    relation = kind == "relation"
    out = "Bool" if relation else f"Fin {n}"
    if arity > 2:
        return None
    if relation:  # Mace4 prints relations as 0/1
        table = (
            bool(table) if arity == 0
            else [bool(x) for x in table] if arity == 1
            else [[bool(x) for x in row] for row in table]
        )  # fmt: skip
    if arity == 0:
        return f"def {ident} : {out} := {str(table).lower()}"
    if arity == 1:
        return f"def {ident} : Fin {n} → {out} := {vec(table)}"
    return f"def {ident} : Fin {n} → Fin {n} → {out} := ![{', '.join(vec(r) for r in table)}]"


def parse_mace4(output: str) -> list[dict[str, Any]]:
    models = []
    for size, body in _MODEL.findall(output):
        n = int(size)
        model: dict[str, Any] = {"size": n, "functions": {}, "relations": {}, "lean": []}
        for i, (kind, symbol, args, raw) in enumerate(_SYMBOL.findall(body)):
            arity = args.count("_") if args else 0
            values = [int(v) for v in re.findall(r"-?\d+", raw)]
            table = _table(values, arity, n)
            model["functions" if kind == "function" else "relations"][symbol.strip()] = table
            ident = symbol if NAME.fullmatch(symbol) else f"op{i}"
            lean = _lean_def(f"m_{ident}", kind, arity, n, table)
            if lean:
                model["lean"].append(f"-- {symbol.strip()}\n{lean}")
        models.append(model)
    return models


def mace4(problem: str, start_size: int, end_size: int, max_models: int, timeout: int):
    path = _work_file("mace4", problem, ".in")
    cmd = [
        _need("mace4", "ladr"),
        "-n", str(start_size),
        "-N", str(end_size),
        "-m", str(max_models),
        "-t", str(timeout),
        "-f", str(path),
    ]  # fmt: skip
    env = base_env()
    code, out, err = run(sandboxed(cmd, env, [SCRATCH]), timeout=timeout + 30, env=env)
    models = parse_mace4(out)
    result: dict[str, Any] = {"models": models, "exit": code}
    if not models:
        tail = [line for line in (out + err).splitlines() if line.strip()][-15:]
        result["output_tail"] = "\n".join(tail)
    return result


# --- SoPlex exact LP ------------------------------------------------------------------------


def _q(value: Any) -> Fraction:
    return Fraction(str(value))


def _lp_terms(coefs: dict[str, Fraction], fallback: str) -> str:
    terms = " ".join(f"{'+' if c >= 0 else '-'} {abs(c)} {v}" for v, c in coefs.items())
    return terms or f"0 {fallback}"


def _holds(row: dict[str, Any], x: dict[str, Fraction]) -> bool:
    lhs = sum(a * x[v] for v, a in row["coefs"].items())
    return {"<=": lhs <= row["rhs"], ">=": lhs >= row["rhs"], "=": lhs == row["rhs"]}[row["op"]]


def _certified_bound(
    cost: dict[str, Fraction],
    rows: list[dict[str, Any]],
    bounds: dict[str, tuple[Fraction | None, Fraction | None]],
    y: dict[str, Fraction],
) -> Fraction | None:
    """Exact lower bound on min cost·x over the LP implied by row multipliers y, or None if
    y is not a valid dual certificate. cost·x = y·Ax + d·x with d = cost - Aᵀy, and each
    term is bounded below using the row sense (y ≥ 0 on ≥ rows, ≤ 0 on ≤ rows) and the
    variable bounds on the side d needs. Shares no code with the solver."""
    bound = Fraction(0)
    reduced = dict(cost)
    for row in rows:
        yi = y.get(row["name"], Fraction(0))
        if (row["op"] == ">=" and yi < 0) or (row["op"] == "<=" and yi > 0):
            return None
        bound += yi * row["rhs"]
        for v, a in row["coefs"].items():
            reduced[v] = reduced.get(v, Fraction(0)) - yi * a
    for v, d in reduced.items():
        lo, hi = bounds.get(v, (None, None))
        side = lo if d > 0 else hi if d < 0 else Fraction(0)
        if side is None:
            return None
        bound += d * side
    return bound


def _solution_block(output: str, header: str) -> dict[str, Fraction] | None:
    # The block may be empty (`...:\nAll other ...` when every entry is zero).
    match = re.search(rf"{header} \(name, value\):\n(.*?)^All other", output, re.S | re.M)
    if match is None:
        return None
    pairs = [line.split() for line in match.group(1).splitlines()]
    return {p[0]: Fraction(p[1]) for p in pairs if len(p) == 2}


def exact_lp(
    objective: dict[str, Any],
    constraints: list[dict[str, Any]],
    bounds: dict[str, list[Any]] | None,
    maximize: bool,
    timeout: int,
) -> dict[str, Any]:
    rows = []
    for i, c in enumerate(constraints):
        name = c.get("name") or f"c{i}"
        if c["op"] not in ("<=", ">=", "=") or not NAME.fullmatch(name):
            raise ValueError(f"constraint {name!r}: op must be <=, >= or = and name an identifier")
        rows.append({"name": name, "op": c["op"], "rhs": _q(c["rhs"]),
                     "coefs": {v: _q(a) for v, a in c["coefs"].items()}})  # fmt: skip
    cost = {v: _q(a) * (-1 if maximize else 1) for v, a in objective.items()}
    names: set[str] = set(cost) | set(bounds or {})
    variables = sorted(names.union(*(r["coefs"] for r in rows)))
    if not all(NAME.fullmatch(v) for v in variables):
        raise ValueError("variable names must be identifiers")
    box: dict[str, tuple[Fraction | None, Fraction | None]] = {
        v: tuple(None if b is None else _q(b) for b in (bounds or {}).get(v, [None, None]))  # type: ignore[misc]
        for v in variables
    }
    if not variables:
        raise ValueError("the LP has no variables")
    lines = ["Minimize", f" obj: {_lp_terms(cost, variables[0])}", "Subject To"]
    lines += [
        f" {r['name']}: {_lp_terms(r['coefs'], variables[0])} {r['op']} {r['rhs']}" for r in rows
    ]
    lines.append("Bounds")
    for v, (lo, hi) in box.items():
        lines.append(f" {'-inf' if lo is None else lo} <= {v} <= {'+inf' if hi is None else hi}")
    lines.append("End")
    path = _work_file("lp", "\n".join(lines) + "\n", ".lp")
    cmd = [_need("soplex", "soplex"), "-X", "-Y", "--int:readmode=1", "--int:solvemode=2",
           "-f0", "-o0", f"-t{timeout}", str(path)]  # fmt: skip
    env = base_env()
    code, out, _ = run(sandboxed(cmd, env, [SCRATCH]), timeout=timeout + 30, env=env)
    status = re.search(r"SoPlex status\s*:\s*(.*)", out)
    result: dict[str, Any] = {"status": status.group(1).strip() if status else f"exit {code}",
                              "lp_file": str(path)}  # fmt: skip
    primal = _solution_block(out, "Primal solution")
    if "[optimal]" in result["status"] and primal is not None:
        primal = {v: primal.get(v, Fraction(0)) for v in variables}
        value = sum(c * primal[v] for v, c in cost.items())
        feasible = all(_holds(r, primal) for r in rows) and all(
            (lo is None or primal[v] >= lo) and (hi is None or primal[v] <= hi)
            for v, (lo, hi) in box.items()
        )
        dual = _solution_block(out, "Dual solution") or {}
        certified = feasible and any(
            _certified_bound(cost, rows, box, y) == value
            for y in (dual, {k: -x for k, x in dual.items()})
        )
        result |= {
            "objective": str(-value if maximize else value),
            "primal": {v: str(x) for v, x in primal.items()},
            "dual": {k: str(x) for k, x in dual.items()},
            "optimality_certified": certified,
        }
    elif "[infeasible]" in result["status"]:
        ray = _solution_block(out, "Dual ray") or {}
        zero = {v: Fraction(0) for v in variables}
        certified = any(
            (b := _certified_bound(zero, rows, box, y)) is not None and b > 0
            for y in (ray, {k: -x for k, x in ray.items()})
        )
        result |= {
            "farkas": {k: str(x) for k, x in ray.items()},
            "infeasibility_certified": certified,
        }
    if "dual" in result or "farkas" in result:
        used = [
            k for k, x in (result.get("dual") or result.get("farkas") or {}).items() if x != "0"
        ]
        result["lean_hint"] = f"linarith only [{', '.join(used)}]" if used else None
    if code is None:
        result["timed_out"] = True
    result["note"] = (
        "certificates are re-checked here in exact arithmetic, independently of SoPlex; "
        "the Lean proof (e.g. the linarith hint) is still what counts"
    )
    return result


# --- sum-of-squares certificates ------------------------------------------------------------

# Runs inside the warm Sage worker (preparsed). Gram matrix SDP over a Newton-box monomial
# basis, solved by CSDP; then rounded to rationals, projected exactly onto the coefficient
# constraints, factored LDLᵀ over QQ and re-expanded, so only exact arithmetic decides.
# Everything is local to one function so the worker's own names (R, p, ...) survive.
_SOS_SAGE = r"""
def _lemmata_sos(VARS, POLY, CSDP, PROB, SOL, TIMEOUT, MAX_BASIS):
    import subprocess
    R = PolynomialRing(QQ, len(VARS), VARS)
    p = R(POLY)
    d = p.total_degree()
    if p == 0 or d % 2:
        return {"found": False, "reason": "zero or odd degree"}
    exps = [m.exponents()[0] for m in p.monomials()]
    k = len(VARS)
    lo = [min(e[i] for e in exps) for i in range(k)]
    hi = [max(e[i] for e in exps) for i in range(k)]
    low = ceil(min(sum(e) for e in exps) / 2)
    basis = [m for t in range(low, d // 2 + 1) for m in R.monomials_of_degree(t)
             if all(lo[i] <= 2 * m.exponents()[0][i] <= hi[i] for i in range(k))]
    if len(basis) > MAX_BASIS:
        reason = f"monomial basis of size {len(basis)} exceeds {MAX_BASIS}"
        return {"found": False, "reason": reason}
    coeff = {m: p.monomial_coefficient(m) for m in p.monomials()}
    # Facial reduction: if m^2 has coefficient 0 and only Q[m,m] produces it, then Q[m,m] = 0,
    # so m's whole row is 0 in every Gram matrix; drop m (repeat until stable).
    while True:
        N = len(basis)
        classes = {}
        for i in range(N):
            for j in range(i, N):
                classes.setdefault(basis[i] * basis[j], []).append((i, j))
        keep = [m for i, m in enumerate(basis) if m * m in coeff or len(classes[m * m]) > 1]
        if len(keep) == N:
            break
        basis = keep
    if any(m not in classes for m in coeff):
        return {"found": False, "reason": "a monomial of p is outside the basis squared"}
    mons = list(classes)
    lines = [str(len(mons)), "1", str(N), " ".join(str(float(coeff.get(m, 0))) for m in mons)]
    for c, m in enumerate(mons, 1):
        lines += [f"{c} 1 {i + 1} {j + 1} 1" for i, j in classes[m]]
    open(PROB, "w").write("\n".join(lines) + "\n")
    run = subprocess.run([CSDP, PROB, SOL], capture_output=True, text=True, timeout=TIMEOUT)
    if run.returncode not in (0, 3, 4):  # 3/4: partial; the exact check below decides
        return {"found": False, "reason": f"csdp exit {run.returncode} (1/2 = infeasible: no "
                "SOS with this basis)", "csdp_tail": run.stdout[-1500:]}
    X = matrix(RDF, N, N)
    for line in open(SOL).read().splitlines()[1:]:
        mat, blk, i, j, v = line.split()
        if mat == "2":
            X[int(i) - 1, int(j) - 1] = X[int(j) - 1, int(i) - 1] = float(v)
    for bits in (10, 16, 24, 32, 48, 64):
        Q = matrix(QQ, N, N, [QQ(round(x * 2^bits)) / 2^bits for x in X.list()])
        for m, pairs in classes.items():
            w = sum(1 if i == j else 2 for i, j in pairs)
            t = (coeff.get(m, 0) - sum((1 if i == j else 2) * Q[i, j] for i, j in pairs)) / w
            for i, j in pairs:
                Q[i, j] += t
                if i != j:
                    Q[j, i] += t
        L, D = identity_matrix(QQ, N), [QQ(0)] * N
        good = True
        for c in range(N):
            D[c] = Q[c, c] - sum(L[c, j]^2 * D[j] for j in range(c))
            if D[c] < 0:
                good = False
                break
            for i in range(c + 1, N):
                v = Q[i, c] - sum(L[i, j] * L[c, j] * D[j] for j in range(c))
                if D[c] == 0 and v != 0:
                    good = False
                    break
                L[i, c] = v / D[c] if D[c] else 0
            if not good:
                break
        if not good:
            continue
        terms = [(D[c], sum(L[i, c] * basis[i] for i in range(N))) for c in range(N) if D[c]]
        if sum(dc * q^2 for dc, q in terms) == p:
            return {"found": True, "bits": int(bits), "basis_size": int(N),
                    "terms": [[str(dc), str(q)] for dc, q in terms]}
    return {"found": False, "reason": "numerical Gram matrix did not round to an exact PSD one"}
"""


def sos_certificate(sage: Sage, polynomial: str, timeout: int, max_basis: int = 120):
    """p = Σ dᵢ qᵢ² with rational dᵢ ≥ 0, so 0 ≤ p over any ordered field."""
    names: dict[str, str] = {}
    poly = lean_to_sage(polynomial, names)
    if poly is None:
        return {"found": False, "reason": "not a polynomial in Lean syntax (+ - * ^ and numerals)"}
    if not names:
        return {"found": False, "reason": "no variables"}
    work = str(_work_file("sos", polynomial, ""))
    args = [list(names.values()), poly, _need("csdp", "csdp"), work + ".dat-s", work + ".sol"]
    call = f"__import__('json').dumps(_lemmata_sos(*{args!r}, {timeout}, {max_basis}))"
    result = sage.run(_SOS_SAGE + call, timeout + 60)
    if not result.get("ok"):
        return {"found": False, "error": result.get("error")}
    out = json.loads(result["value"])
    if not out.get("found"):
        return out
    back = {v: k for k, v in names.items()}

    def lean(text: str) -> str:
        return re.sub(r"\bv\d+\b", lambda m: back[m.group(0)], text)

    terms = [(d, lean(q)) for d, q in out.pop("terms")]
    identity = " + ".join(f"({d}) * ({q})^2" for d, q in terms)
    return {
        **out,
        "terms": [{"coefficient": d, "square_of": q} for d, q in terms],
        "identity": f"{polynomial} = {identity}",
        "lean_tactic": "nlinarith ["
        + ", ".join(f"sq_nonneg ({q})" for _, q in terms if re.search(r"[^\W\d]", q))
        + "]",
        "note": "exact identity, re-expanded in Sage; check the Lean tactic with lean.check",
    }
