"""Lean server: one warm REPL (`import Mathlib`) for checking, goal inspection, multi-attempt
proof repair and a bounded tactic portfolio. Nothing here issues a verdict; PROVED goes
through the verdict server's comparator check."""

from __future__ import annotations

import re
import secrets
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import board
from .cas import Sage, goal_equations, ideal_cofactors
from .common import STANDARD_AXIOMS, WORK_PROJECT
from .repl import HEADER, Repl, ReplTimeout

repl = Repl()
sage = Sage()
SESSION = secrets.token_hex(4)

# Order matters: cheap decision procedures first, searches last.
DEFAULT_PORTFOLIO = [
    "rfl",
    "decide",
    "norm_num",
    "simp",
    "omega",
    "ring",
    "linarith",
    "positivity",
    "nlinarith",
    "linear_combination",
    "aesop",
    "grind",
    "exact?",
    "apply?",
]
EXPLORATION_ONLY = {"native_decide"}  # never accepted in a PROVED certificate
FORBIDDEN = {"polyrith"}  # needs the network; not available
DECL = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t\n]*)?(?:(?:private|protected|noncomputable|nonrec)\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance)\s+([^\s:({\[⦃]+)",
    re.MULTILINE,
)
NAMESPACE = re.compile(r"^[ \t]*(namespace|section|end)\b[ \t]*([^\s-]*)", re.MULTILINE)

server = MCPServer(
    "lemmata-lean",
    instructions=(
        "Lean 4 + Mathlib (pinned) through one warm REPL. Code is elaborated after "
        "`import Mathlib`; do not write imports. Use `check` to see errors, goals at each "
        "`sorry` and the axioms of every theorem, `portfolio` to attack a `sorry`, `repair` "
        "to fix the first failing tactic, and `register_lemma` to share a finished lemma. "
        "native_decide is exploration only; polyrith is unavailable."
    ),
)


# --- helpers ----------------------------------------------------------------------------


def _pos(p: dict[str, int]) -> tuple[int, int]:
    return p["line"], p["column"]


def _offset(src: str, line: int, col: int) -> int:
    lines = src.split("\n")
    return sum(len(x) + 1 for x in lines[: line - 1]) + col


def _decl_names(code: str) -> list[str]:
    """Fully qualified names of declarations in `code` (namespaces tracked by line)."""
    events = sorted(
        [(m.start(), "decl", m.group(1)) for m in DECL.finditer(code)]
        + [(m.start(), m.group(1), m.group(2)) for m in NAMESPACE.finditer(code)]
    )
    stack: list[tuple[str, str]] = []
    names = []
    for _, kind, value in events:
        if kind == "decl":
            prefix = ".".join(v for k, v in stack if k == "namespace" and v)
            names.append(
                value.removeprefix("_root_.")
                if value.startswith("_root_.")
                else f"{prefix}.{value}"
                if prefix
                else value
            )
        elif kind in ("namespace", "section"):
            stack.append((kind, value))
        elif stack:
            stack.pop()
    return names


def _messages(response: dict[str, Any], line_offset: int) -> list[dict[str, Any]]:
    return [
        {
            "severity": m["severity"],
            "line": m["pos"]["line"] - line_offset,
            "col": m["pos"]["column"],
            "text": m["data"][:4000],
        }
        for m in response.get("messages", [])
    ]


def _with_preamble(code: str, use_lemmas: list[int] | None) -> tuple[str, int]:
    if not use_lemmas:
        return code, 0
    preamble = board.lemma_sources(use_lemmas) + "\n\n"
    return preamble + code, preamble.count("\n")


def _axioms(env: int, names: list[str], timeout: float) -> dict[str, list[str] | None]:
    """Axioms per declaration via `#print axioms` (None if the name did not resolve)."""
    result: dict[str, list[str] | None] = {}
    for name in names:
        response = repl.command(f"#print axioms {name}", timeout, env=env)
        text = " ".join(m["data"] for m in response.get("messages", []))
        if "does not depend on any axioms" in text:
            result[name] = []
        elif match := re.search(r"depends on axioms: \[([^\]]*)\]", text):
            result[name] = [a.strip() for a in match.group(1).split(",") if a.strip()]
        else:
            result[name] = None
    return result


def _check(
    code: str, use_lemmas: list[int] | None, timeout: float, all_tactics: bool = False
) -> dict[str, Any]:
    full, offset = _with_preamble(code, use_lemmas)
    started = time.monotonic()
    try:
        response = repl.command(full, timeout, all_tactics=all_tactics)
    except ReplTimeout as e:
        return {"ok": False, "timeout": True, "error": str(e)}
    if "message" in response and "env" not in response:
        return {"ok": False, "error": response["message"]}
    messages = _messages(response, offset)
    errors = [m for m in messages if m["severity"] == "error"]
    sorries = [
        {
            "line": s["pos"]["line"] - offset,
            "col": s["pos"]["column"],
            "goal": s["goal"],
            "proofState": s.get("proofState"),
        }
        for s in response.get("sorries", [])
    ]
    names = _decl_names(code)
    axioms = _axioms(response["env"], names, timeout) if not errors else {}
    bad_axioms = sorted({a for ax in axioms.values() if ax for a in ax} - set(STANDARD_AXIOMS))
    result: dict[str, Any] = {
        "ok": not errors and not sorries and not bad_axioms,
        "errors": errors,
        "warnings": [m for m in messages if m["severity"] == "warning"],
        "infos": [m for m in messages if m["severity"] == "info"],
        "sorries": sorries,
        "axioms": axioms,
        "nonstandard_axioms": bad_axioms,
        "seconds": round(time.monotonic() - started, 2),
        "_env": response["env"],
        "_offset": offset,
    }
    if all_tactics:
        result["_tactics"] = response.get("tactics", [])
    return result


def _public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def _closed(response: dict[str, Any]) -> bool:
    has_error = any(m["severity"] == "error" for m in response.get("messages", []))
    return "goals" in response and not response["goals"] and not has_error


def _suggestion(response: dict[str, Any]) -> str | None:
    for m in response.get("messages", []):
        # Lean 4.35 prints `Try this:\n  [apply] exact foo`; keep only the tactic.
        if match := re.search(r"Try this:\s*(?:\[\w+\]\s*)?(.+)", m["data"], re.DOTALL):
            return match.group(1).strip().split("\n")[0]
    return None


def _indent(text: str, col: int) -> str:
    return text.replace("\n", "\n" + " " * col)


def _block_end(src: str, line: int, col: int) -> int:
    """Offset where the tactic block containing a tactic at (line, col) ends."""
    lines = src.split("\n")
    end_line = len(lines)
    for i in range(line, len(lines)):  # lines after `line` (0-based index == line)
        text = lines[i]
        if text.strip() and len(text) - len(text.lstrip()) < col:
            end_line = i
            break
    return sum(len(x) + 1 for x in lines[:end_line]) - 1


def _tactic_candidates(tactics: list[str] | None, goal: str) -> list[tuple[str, str]]:
    """(label, tactic text) pairs; `linear_combination` gets CAS-proposed coefficients."""
    out = []
    for tactic in tactics or DEFAULT_PORTFOLIO:
        if tactic.split()[0] in FORBIDDEN:
            continue
        if tactic == "linear_combination":
            target, hyps = goal_equations(goal)
            if target is None:
                continue
            try:
                proposal = ideal_cofactors(sage, target, hyps, timeout=30)
            except RuntimeError:  # Sage not installed
                continue
            if proposal.get("tactic"):
                out.append(("linear_combination (CAS)", proposal["tactic"]))
            continue
        out.append((tactic, tactic))
    return out


# --- tools ------------------------------------------------------------------------------


@server.tool()
def check(
    code: str,
    use_lemmas: list[int] | None = None,
    show_tactics: bool = False,
    save_as: str | None = None,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Elaborate Lean code after `import Mathlib`.

    Returns errors/warnings, the goal at every `sorry`, axioms per declaration and `ok`
    (no errors, no sorry, only propext/Classical.choice/Quot.sound). `use_lemmas` prepends
    proved whiteboard lemmas (and their deps). `show_tactics` adds the goal before every
    tactic. `save_as` also writes the file under the work project so lean_lsp tools (goal at
    a line, hover, multi_attempt) can open it; the returned path is relative to that project.
    """
    result = _check(code, use_lemmas, timeout_s, all_tactics=show_tactics)
    if show_tactics and "_tactics" in result:
        result["tactics"] = [
            {
                "tactic": t["tactic"],
                "line": t["pos"]["line"] - result["_offset"],
                "col": t["pos"]["column"],
                "goals_before": t["goals"],
            }
            for t in result["_tactics"]
        ]
    if save_as:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", save_as):
            raise ValueError("save_as must be a Lean module name like Attempt1")
        relative = f"Scratch/S{SESSION}/{save_as}.lean"
        path = WORK_PROJECT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        full, _ = _with_preamble(code, use_lemmas)
        path.write_text(f"{HEADER}\n\n{full}\n")
        result["scratch_file"] = relative
        result["scratch_line_offset"] = 2 + result["_offset"]
    return _public(result)


@server.tool()
def portfolio(
    code: str,
    tactics: list[str] | None = None,
    sorry_index: int = 0,
    heartbeats: int = 200000,
    per_tactic_s: int = 20,
    explore: bool = False,
    use_lemmas: list[int] | None = None,
) -> dict[str, Any]:
    """Try a bounded tactic portfolio on one `sorry` in `code`.

    Default portfolio: rfl, decide, norm_num, simp, omega, ring, linarith, positivity,
    nlinarith, linear_combination (coefficients proposed by Sage via a Groebner basis),
    aesop, grind, exact?, apply?. Each try is capped by `heartbeats` and `per_tactic_s`.
    `explore=True` also tries native_decide, reported as exploration only: it is never
    spliced into `verified_code` and never valid in a PROVED certificate. The first closing
    tactic (or the term `exact?` found) is spliced in and the whole file is rechecked.
    """
    base = _check(code, use_lemmas, 120)
    sorries = base.get("sorries", [])
    if base.get("errors") or sorry_index >= len(sorries):
        return {
            "error": "need code that elaborates with a `sorry` at the target",
            "check": _public(base),
        }
    target = sorries[sorry_index]
    candidates = _tactic_candidates(tactics, target["goal"])
    if explore:
        candidates.append(("native_decide", "native_decide"))
    results, verified = [], None
    state = target["proofState"]
    for label, tactic in candidates:
        started = time.monotonic()
        try:
            response = repl.tactic(
                f"set_option maxHeartbeats {heartbeats} in\n{tactic}", state, per_tactic_s
            )
        except ReplTimeout:
            results.append({"tactic": label, "outcome": "timed_out", "budget_s": per_tactic_s})
            refreshed = _check(code, use_lemmas, 120)  # REPL restarted: new proof state ids
            state = refreshed["sorries"][sorry_index]["proofState"]
            continue
        entry: dict[str, Any] = {"tactic": label, "seconds": round(time.monotonic() - started, 2)}
        if label in EXPLORATION_ONLY:
            entry["exploration_only"] = True
        suggestion = _suggestion(response)
        if _closed(response):
            entry["outcome"] = "closed"
            entry["text"] = suggestion or tactic
        else:
            entry["outcome"] = "failed"
            entry["goals_after"] = response.get("goals")
            entry["messages"] = [m["data"][:1500] for m in response.get("messages", [])]
            if "message" in response:  # the REPL's own error (e.g. the tactic threw)
                entry["messages"].append(response["message"][:1500])
            if suggestion:
                entry["suggestion"] = suggestion
        results.append(entry)
        if entry["outcome"] == "closed" and label not in EXPLORATION_ONLY and verified is None:
            start = _offset(code, target["line"], target["col"])
            spliced = code[:start] + _indent(entry["text"], target["col"]) + code[start + 5 :]
            recheck = _check(spliced, use_lemmas, 120)
            if not recheck.get("errors"):
                verified = {
                    "code": spliced,
                    "ok": recheck["ok"],
                    "remaining_sorries": len(recheck["sorries"]),
                }
                break
    return {"goal": target["goal"], "results": results, "verified_code": verified}


@server.tool()
def repair(
    code: str,
    candidates: list[str] | None = None,
    rounds: int = 3,
    per_tactic_s: int = 20,
    use_lemmas: list[int] | None = None,
) -> dict[str, Any]:
    """Multi-attempt proof repair.

    Each round: find the first failing tactic, try replacement tactics from the proof state
    just before it (default: the tactic portfolio), splice in the first that makes progress
    and recheck the whole file. Returns the repaired code if it fully checks, otherwise the
    best attempt plus `working_prefix`: the proof cut at the failure with a `sorry`, which
    elaborates and keeps everything that already worked.
    """
    source, history = code, []
    for _ in range(rounds):
        result = _check(source, use_lemmas, 120, all_tactics=True)
        if result.get("timeout"):
            return {"fixed": False, "error": "check timed out", "code": source, "history": history}
        errors = result["errors"]
        if not errors:
            return {
                "fixed": result["ok"],
                "code": source,
                "history": history,
                "check": _public(result),
            }
        offset = result["_offset"]
        first = min(errors, key=lambda m: (m["line"], m["col"]))
        error_pos = (first["line"] + offset, first["col"])
        tactics = result["_tactics"]
        inside = [t for t in tactics if _pos(t["pos"]) <= error_pos <= _pos(t["endPos"])]
        if inside:  # innermost tactic containing the first error
            failed = max(inside, key=lambda t: _pos(t["pos"]))
            mode = "replace"
            state = failed["proofState"]
        else:  # e.g. "unsolved goals": extend after the last tactic of that block
            after = [t for t in tactics if _pos(t["pos"]) >= error_pos]
            if not after:
                return {"fixed": False, "code": source, "first_error": first, "history": history}
            failed = max(after, key=lambda t: _pos(t["endPos"]))
            mode = "append"
            response = repl.tactic(failed["tactic"], failed["proofState"], per_tactic_s)
            if not response.get("goals"):
                return {"fixed": False, "code": source, "first_error": first, "history": history}
            state = response["proofState"]
        line, col = failed["pos"]["line"] - offset, failed["pos"]["column"]
        start = _offset(source, line, col)
        end = _offset(source, failed["endPos"]["line"] - offset, failed["endPos"]["column"])
        block_end = _block_end(source, failed["endPos"]["line"] - offset, col)
        prefix = source[:start] + "sorry" + source[block_end:]
        round_log: dict[str, Any] = {
            "failed_tactic": failed["tactic"],
            "line": line,
            "error": first["text"][:1500],
            "mode": mode,
            "tried": [],
        }
        progress = None
        for label, tactic in _tactic_candidates(candidates, failed["goals"]):
            try:
                response = repl.tactic(tactic, state, per_tactic_s)
            except ReplTimeout:
                round_log["tried"].append({"tactic": label, "outcome": "timed_out"})
                break  # proof state ids are gone; the next round re-elaborates
            if any(m["severity"] == "error" for m in response.get("messages", [])):
                round_log["tried"].append({"tactic": label, "outcome": "failed"})
                continue
            text = _indent(_suggestion(response) or tactic, col)
            if mode == "replace":
                variants = [source[:start] + text + source[end:]]
                if not response.get("goals"):
                    variants.append(source[:start] + text + source[block_end:])
            else:
                variants = [source[:end] + "\n" + " " * col + text + source[end:]]
            for variant in variants:
                recheck = _check(variant, use_lemmas, 120)
                new_errors = recheck.get("errors", [{"line": 0, "col": 0}])
                if len(new_errors) < len(errors) or (
                    new_errors
                    and min((e["line"], e["col"]) for e in new_errors)
                    > (first["line"], first["col"])
                ):
                    progress = variant
                    round_log["tried"].append({"tactic": label, "outcome": "progress"})
                    break
            if progress:
                break
            round_log["tried"].append({"tactic": label, "outcome": "no_progress"})
        history.append(round_log)
        if progress is None:
            prefix_check = _check(prefix, use_lemmas, 120)
            return {
                "fixed": False,
                "code": source,
                "first_error": first,
                "goal_at_failure": failed["goals"],
                "history": history,
                "working_prefix": prefix,
                "working_prefix_elaborates": not prefix_check.get("errors"),
            }
        source = progress
    final = _check(source, use_lemmas, 120)
    return {"fixed": final["ok"], "code": source, "history": history, "check": _public(final)}


@server.tool()
def register_lemma(lemma_id: int, code: str, use_lemmas: list[int] | None = None) -> dict[str, Any]:
    """Check a finished lemma and mark whiteboard lemma `lemma_id` as proved.

    Requires `ok` from `check`: no errors, no sorry, standard axioms only, and no
    native_decide. The source and each declaration's type become searchable through the
    library server and reusable by everyone via `use_lemmas`.
    """
    if re.search(r"\bnative_decide\b", code):
        return {"registered": False, "reason": "native_decide is exploration only"}
    result = _check(code, use_lemmas, 300)
    if not result["ok"]:
        return {"registered": False, "reason": "does not fully check", "check": _public(result)}
    types = {}
    for name in result["axioms"]:
        response = repl.command(f"#check @{name}", 60, env=result["_env"])
        texts = [m["data"] for m in response.get("messages", []) if m["severity"] == "info"]
        if texts:
            types[name] = " ".join(texts[0].split())
    if use_lemmas:
        board.update_lemma(lemma_id, deps=use_lemmas)
    ref = board.record_proved_lemma(lemma_id, code, types)
    return {"registered": True, "proof_ref": f"lean:{ref}", "declarations": types}
