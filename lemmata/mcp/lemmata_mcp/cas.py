"""Warm SageMath worker client, plus Lean <-> Sage polynomial translation for
CAS-proposed `linear_combination` coefficients (Groebner-basis ideal membership)."""

from __future__ import annotations

import ast
import json
import os
import re
import select
import signal
import subprocess
import threading
from typing import Any

from .common import CHILD_PATH, PACKAGE_DIR, SAGE_PYTHON, SCRATCH, base_env, sandboxed


class Sage:
    """One sandboxed Sage process, started on first use and kept warm."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        if not SAGE_PYTHON.exists():
            raise RuntimeError(f"Sage not installed at {SAGE_PYTHON}; run setup-wsl.sh sage")
        dot_sage = SCRATCH / "dot_sage"
        # Sage shells out to gap, maxima, ... from its env's bin.
        env = base_env(
            DOT_SAGE=str(dot_sage),
            MPLCONFIGDIR=str(dot_sage / "mpl"),
            HOME=str(SCRATCH),
            PATH=f"{SAGE_PYTHON.parent}:{CHILD_PATH}",
        )
        self.proc = subprocess.Popen(
            sandboxed(
                [str(SAGE_PYTHON), "-u", str(PACKAGE_DIR / "sage_worker.py")], env, [SCRATCH]
            ),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None

    def run(self, code: str, timeout: int = 60, reset: bool = False) -> dict[str, Any]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            assert self.proc is not None and self.proc.stdin and self.proc.stdout
            self.proc.stdin.write(
                json.dumps({"code": code, "timeout": timeout, "reset": reset}) + "\n"
            )
            self.proc.stdin.flush()
            # Sage's alarm() interrupts at `timeout`; the extra margin covers import time and
            # code that ignores signals (then the worker is killed and restarted next call).
            ready, _, _ = select.select([self.proc.stdout], [], [], timeout + 120)
            line = self.proc.stdout.readline() if ready else ""
            if not line:
                self._kill()
                return {"ok": False, "error": "Sage worker died or hung; restarted on next call"}
            return json.loads(line)


_TOKEN = re.compile(r"\s*(?:(\d+)|([^\W\d][\w'₀-₉!?.]*)|([-+*/^()]))")


def lean_to_sage(expr: str, names: dict[str, str]) -> str | None:
    """Translate a Lean polynomial expression; None if it uses anything else.

    Atoms (variables, constants such as `Real.pi`, casts `↑x`) become indeterminates, which
    is sound for ideal membership. Juxtaposition (function application) is refused.
    """
    expr = expr.replace("↑", "").strip()
    out: list[str] = []
    pos, prev_atom = 0, False
    while pos < len(expr):
        match = _TOKEN.match(expr, pos)
        if match is None or match.end() == pos:
            return None
        number, ident, op = match.groups()
        if number or ident:
            if prev_atom:
                return None
            out.append(number or names.setdefault(ident, f"v{len(names)}"))
            prev_atom = True
        else:
            if op == "(" and prev_atom:
                return None
            out.append(op)
            prev_atom = op == ")"
        pos = match.end()
    return " ".join(out) or None


def _equation(text: str, names: dict[str, str]) -> tuple[str, str] | None:
    lhs, sep, rhs = text.partition("=")
    if not sep or "=" in rhs or any(c in text for c in "≠≤≥<>↔∀∃"):
        return None
    left, right = lean_to_sage(lhs, names), lean_to_sage(rhs, names)
    return (left, right) if left and right else None


def ideal_cofactors(
    sage: Sage, target: str, hypotheses: dict[str, str], timeout: int = 60
) -> dict[str, Any]:
    """Find c_i with target_lhs - target_rhs = sum c_i (h_i.lhs - h_i.rhs) over QQ.

    Returns a ready `linear_combination` tactic; Lean still has to check it.
    """
    names: dict[str, str] = {}
    goal = _equation(target, names)
    if goal is None:
        return {"error": "target is not a polynomial equation"}
    hyps = {h: eq for h, text in hypotheses.items() if (eq := _equation(text, names))}
    variables = list(names.values()) or ["v0"]
    gens = ", ".join(f"R('{lhs}') - R('{rhs}')" for lhs, rhs in hyps.values()) or "R(0)"
    code = (
        f"R = PolynomialRing(QQ, {variables!r}, order='degrevlex')\n"
        f"I = R.ideal([{gens}])\n"
        f"f = R('{goal[0]}') - R('{goal[1]}')\n"
        "[str(c) for c in f.lift(I)] if f in I else None"
    )
    result = sage.run(code, timeout)
    if not result.get("ok"):
        return {"error": result.get("error")}
    coefficients = ast.literal_eval(result["value"])
    if coefficients is None:
        return {"in_ideal": False, "used_hypotheses": list(hyps)}
    back = {v: k for k, v in names.items()}

    def to_lean(coefficient: str) -> str:
        return re.sub(r"\bv\d+\b", lambda m: back[m.group(0)], coefficient)

    terms = [f"({to_lean(c)}) * {h}" for h, c in zip(hyps, coefficients, strict=False) if c != "0"]
    tactic = "linear_combination " + " + ".join(terms) if terms else "linear_combination"
    return {"in_ideal": True, "tactic": tactic, "used_hypotheses": list(hyps)}


def goal_equations(goal: str) -> tuple[str | None, dict[str, str]]:
    """Split a pretty-printed goal into its target and its equational hypotheses."""
    lines: list[str] = []
    for line in goal.split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += " " + line.strip()
        else:
            lines.append(line)
    target, hyps = None, {}
    for line in lines:
        if line.startswith("⊢"):
            target = line[1:].strip()
        elif " : " in line:
            binders, _, type_ = line.partition(" : ")
            for name in binders.split():
                hyps[name] = type_
    return target, {h: t for h, t in hyps.items() if "=" in t}
