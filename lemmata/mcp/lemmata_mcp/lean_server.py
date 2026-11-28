"""Lean server: one warm REPL (`import Mathlib`, plus LeanHammer and LeanCert when installed)
for checking, goal inspection, proof-state branching, multi-attempt proof repair, a bounded
tactic portfolio (Duper, hammer, LeanCert included) and batch checks (optionally on a Kimina
pool). Nothing here issues a verdict; PROVED goes through the verdict server's comparator."""

from __future__ import annotations

import itertools
import re
import secrets
import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import board, verdict_server
from .cas import Sage, goal_equations, ideal_cofactors
from .common import _COMMENTS, _STRINGS, STANDARD_AXIOMS, WORK_PROJECT, scan_certificate
from .repl import HEADER, IMPORTS, Kimina, Repl, ReplTimeout

repl = Repl()
kimina = Kimina()
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
# v2 automation, offered when its package is in the header. `hammer` (Aesop, Duper via
# Zipperposition, grind, cvc5 in parallel; local premise selection only) is tried last and
# counts only through the tactic it suggests: `hammer` itself is never accepted in a
# certificate. LeanCert runs in its kernel trust mode, the only one valid for PROVED.
if "LeanCert.Tactic" in IMPORTS:
    DEFAULT_PORTFOLIO.insert(DEFAULT_PORTFOLIO.index("nlinarith"), "leancert (trust := kernel)")
if "Hammer" in IMPORTS:
    DEFAULT_PORTFOLIO.insert(DEFAULT_PORTFOLIO.index("exact?"), "duper [*]")
    DEFAULT_PORTFOLIO.append("hammer")
EXPLORATION_ONLY = {"native_decide"}  # never accepted in a PROVED certificate
FORBIDDEN = {"polyrith"}  # needs the network; not available
SUGGESTION_ONLY = {"hammer"}  # only its "Try this" suggestion may be spliced
# Re-enabling LeanHammer's cloud premise selector (or pointing it elsewhere) would send
# goals off the machine; the header pins the local Sine Qua Non selector instead.
CLOUD_PREMISES = re.compile(
    r"set_library_suggestions|premiseSelection\.apiBaseUrl|\bCloud\.premiseSelector"
)
# lean-smt calls cvc5 through a native library the REPL binary cannot load: the REPL aborts.
# The header turns hammer's SMT branch off; this keeps it off and refuses `smt` itself.
SMT = re.compile(r"\bsmt\b|disableSmt")
BULLET = re.compile(r"\s*\[\w+\]")  # starts the next alternative in a `Try these:` list
DECL = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t\n]*)?(?:(?:private|protected|noncomputable|nonrec)\s+)*"
    r"(?:theorem|lemma|def|abbrev|instance)\s+([^\s:({\[⦃]+)",
    re.MULTILINE,
)
NAMESPACE = re.compile(r"^[ \t]*(namespace|section|end)\b[ \t]*([^\s-]*)", re.MULTILINE)

server = MCPServer(
    "lemmata-lean",
    instructions=(
        f"Lean 4 + Mathlib (pinned) through one warm REPL. Code is elaborated after "
        f"`import {', '.join(IMPORTS)}`; do not write imports. Use `check` to see errors, "
        "goals at each `sorry` and the axioms of every theorem, `portfolio` to attack a "
        "`sorry`, `step` to branch from any proof state, `repair` to fix the first failing "
        "tactic, `batch_check` for many independent snippets, `register_lemma` to share "
        "a finished lemma, and `goal_split` / `goal_close` / `goal_refute` / `goal_certificate` "
        "to move the goal tree (see whiteboard.goal_tree) by checked steps. native_decide "
        "and LeanCert's native/auto trust modes are exploration only; `hammer` counts only "
        "through the tactic it suggests; polyrith is unavailable."
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


def _challenge_prefix(problem: str) -> str:
    return verdict_server._prefix(verdict_server._challenge(problem))


def _lemmas_prefix(use_lemmas: list[int]) -> str | None:
    """The frozen prefix of the one challenge all `use_lemmas` belong to, if there is one."""
    problems = board.lemma_problems(use_lemmas)
    if len(problems) != 1:
        return None
    try:
        return _challenge_prefix(problems.pop())
    except ValueError:  # not a frozen challenge (e.g. smoke tests)
        return None


def _with_preamble(
    code: str, use_lemmas: list[int] | None, prefix: str | None = None
) -> tuple[str, int]:
    """`code` after the sources of `use_lemmas`, and the line offset of `code` in the result.

    With a challenge prefix (given, or that of the lemmas' frozen challenge) the prefix is
    emitted once, then the lemma sources and `code`, each without a pasted copy of it: two
    sources that both start with the prefix would otherwise redefine `Claim`.
    """
    if prefix is None and use_lemmas:
        prefix = _lemmas_prefix(use_lemmas)
    if prefix is None:
        if not use_lemmas:
            return code, 0
        preamble = board.lemma_sources(use_lemmas) + "\n\n"
        return preamble + code, preamble.count("\n")
    head, rest = board.split_prefix(code, prefix)
    lemmas = board.lemma_sources(use_lemmas or [], prefix)
    preamble = prefix + "\n" + (lemmas + "\n\n" if lemmas else "")
    return preamble + rest, preamble.count("\n") - head.count("\n")


def _parse_axioms(text: str) -> list[str] | None:
    """Axioms from `#print axioms` output (None if the name did not resolve)."""
    if "does not depend on any axioms" in text:
        return []
    if match := re.search(r"depends on axioms: \[([^\]]*)\]", text):
        return [a.strip() for a in match.group(1).split(",") if a.strip()]
    return None


def _axioms(env: int, names: list[str], timeout: float) -> dict[str, list[str] | None]:
    """Axioms per declaration via `#print axioms`."""
    result: dict[str, list[str] | None] = {}
    for name in names:
        response = repl.command(f"#print axioms {name}", timeout, env=env)
        result[name] = _parse_axioms(" ".join(m["data"] for m in response.get("messages", [])))
    return result


def _guard(code: str) -> None:
    if CLOUD_PREMISES.search(code):
        raise ValueError("premise selection is local only; do not change the selector")
    if SMT.search(code):
        raise ValueError("lean-smt (cvc5) is unavailable: its native library cannot load here")


def _check(
    code: str,
    use_lemmas: list[int] | None,
    timeout: float,
    all_tactics: bool = False,
    prefix: str | None = None,
) -> dict[str, Any]:
    _guard(code)
    full, offset = _with_preamble(code, use_lemmas, prefix)
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
        # A declaration whose axioms could not be read is not known to be clean.
        "ok": not errors and not sorries and not bad_axioms and None not in axioms.values(),
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
    """The first suggested tactic block. Lean 4.35 prints `Try this:\n  [apply] exact foo`;
    hammer prints `Try these:` with several `[apply]` alternatives, each possibly a
    multi-line tactic sequence (`apply h` then `simp_all only`)."""
    for m in response.get("messages", []):
        match = re.search(r"Try th(?:is|ese):\s*(?:\[\w+\]\s*)?(.+)", m["data"], re.DOTALL)
        if match:
            first, *rest = match.group(1).split("\n")
            block = list(itertools.takewhile(lambda line: not BULLET.match(line), rest))
            block = [line for line in block if line.strip()]
            # Lean indents continuation lines two past the line the suggestion starts on;
            # strip only that, so deeper (term-continuation) indentation survives.
            opener = m["data"][: match.start(1)].rsplit("\n", 1)[-1]
            pad = len(opener) - len(opener.lstrip()) + 2
            pad = min([pad, *(len(line) - len(line.lstrip()) for line in block)])
            return "\n".join([first.strip(), *(line[pad:].rstrip() for line in block)])
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
        _guard(tactic)
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
    """Elaborate Lean code after the header (`import Mathlib`, plus Hammer and LeanCert.Tactic
    when installed).

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
    `leancert (trust := kernel)`, nlinarith, linear_combination (coefficients proposed by
    Sage via a Groebner basis), aesop, grind, `duper [*]`, exact?, apply?, hammer (LeanCert,
    Duper and hammer only when installed). Each try is capped by `heartbeats` and
    `per_tactic_s`; give hammer 60 s or more. `explore=True` also tries native_decide,
    reported as exploration only: it is never spliced into `verified_code` and never valid
    in a PROVED certificate. The first closing tactic (or the term `exact?` found, or the
    tactic `hammer` suggests) is spliced in and the whole file is rechecked.
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
            if tactic.split()[0] in SUGGESTION_ONLY and not suggestion:
                entry["exploration_only"] = True  # nothing certificate-safe to splice
        else:
            entry["outcome"] = "failed"
            entry["goals_after"] = response.get("goals")
            entry["messages"] = [m["data"][:1500] for m in response.get("messages", [])]
            if "message" in response:  # the REPL's own error (e.g. the tactic threw)
                entry["messages"].append(response["message"][:1500])
            if suggestion:
                entry["suggestion"] = suggestion
        results.append(entry)
        usable = entry["outcome"] == "closed" and not entry.get("exploration_only")
        if usable and verified is None:
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
            if tactic.split()[0] in SUGGESTION_ONLY and not _suggestion(response):
                round_log["tried"].append({"tactic": label, "outcome": "no_suggestion"})
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


def _decl_types(result: dict[str, Any]) -> dict[str, str]:
    """Type of each declaration a successful `_check` elaborated, as `#check @name` prints."""
    types = {}
    for name in result["axioms"]:
        response = repl.command(f"#check @{name}", 60, env=result["_env"])
        texts = [m["data"] for m in response.get("messages", []) if m["severity"] == "info"]
        if texts:
            types[name] = " ".join(texts[0].split())
    return types


@server.tool()
def register_lemma(lemma_id: int, code: str, use_lemmas: list[int] | None = None) -> dict[str, Any]:
    """Check a finished lemma and mark whiteboard lemma `lemma_id` as proved.

    Requires `ok` from `check` (no errors, no sorry, standard axioms only) and the same text
    rules as a verdict certificate (no native_decide, hammer, LeanCert native trust, ...),
    since registered lemmas end up inside certificates. The source and each declaration's
    type become searchable through the library server and reusable by everyone via
    `use_lemmas`.
    """
    board.refuse_goal_lemma(lemma_id)
    if reason := scan_certificate(code):
        return {"registered": False, "reason": reason}
    try:  # lemmas registered without the pasted prefix still elaborate
        prefix = _challenge_prefix(board.lemma_row(lemma_id)["problem"])
    except ValueError:  # not a frozen challenge
        prefix = None
    result = _check(code, use_lemmas, 300, prefix=prefix)
    if not result["ok"]:
        return {"registered": False, "reason": "does not fully check", "check": _public(result)}
    types = _decl_types(result)
    if use_lemmas:
        board.update_lemma(lemma_id, deps=use_lemmas)
    ref = board.record_proved_lemma(lemma_id, code, types)
    return {"registered": True, "proof_ref": f"lean:{ref}", "declarations": types}


@server.tool()
def step(proof_state: int, tactic: str, timeout_s: int = 60) -> dict[str, Any]:
    """Proof-state branching: run one tactic on a proof state and get the next one.

    `proof_state` comes from a `sorry` reported by `check` (its `proofState`) or from an
    earlier `step`. A state can be extended any number of times, so search trees branch
    freely without re-elaborating the file. States are lost when the REPL restarts (after a
    timeout, or every 400 commands): then `check` again. Exploration only: the finished
    proof still has to be written out and checked.
    """
    _guard(tactic)
    try:
        response = repl.tactic(tactic, proof_state, timeout_s)
    except ReplTimeout as e:
        return {"closed": False, "error": str(e)}
    return {
        "closed": _closed(response),
        "proof_state": response.get("proofState"),
        "goals": response.get("goals"),
        "messages": [m["data"][:1500] for m in response.get("messages", [])],
        "suggestion": _suggestion(response),
        "error": response.get("message"),
    }


def _kimina_result(code: str, result: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """Kimina's REPL response in `check`'s shape (lines relative to `code`)."""
    response = result.get("response") or {}
    if result.get("error") or "message" in response:
        return {"ok": False, "error": result.get("error") or response.get("message")}
    offset = HEADER.count("\n") + 2
    messages = _messages(response, offset)
    errors = [m for m in messages if m["severity"] == "error"]
    code_lines = code.count("\n") + 1
    printed = [m["text"] for m in messages if m["line"] > code_lines]
    axioms = {n: _parse_axioms(" ".join(t for t in printed if f"'{n}'" in t)) for n in names}
    sorries = [
        {"line": s["pos"]["line"] - offset, "col": s["pos"]["column"], "goal": s["goal"]}
        for s in response.get("sorries", [])
    ]
    bad = sorted({a for ax in axioms.values() if ax for a in ax} - set(STANDARD_AXIOMS))
    return {
        "ok": not errors and not sorries and not bad and None not in axioms.values(),
        "errors": errors,
        "warnings": [m for m in messages if m["severity"] == "warning" and m["line"] <= code_lines],
        "sorries": sorries,
        "axioms": axioms,
        "nonstandard_axioms": bad,
        "seconds": round(result.get("time") or 0, 2),
    }


@server.tool()
def batch_check(snippets: list[str], timeout_s: int = 120) -> dict[str, Any]:
    """Check many independent snippets (no `use_lemmas`), each like `check`: errors, the goal
    at each `sorry`, axioms per declaration and `ok`.

    With KIMINA_REPLS > 0 in $LEMMATA_HOME/options.env the snippets run in parallel on a
    Kimina Lean Server pool; otherwise one after another on the warm REPL. Use it to test
    many candidate proofs or lemma variants at once.
    """
    for code in snippets:
        _guard(code)
    if kimina.repls <= 0:
        return {
            "backend": "warm REPL (sequential)",
            "results": [_public(_check(code, None, timeout_s)) for code in snippets],
        }
    names = [_decl_names(code) for code in snippets]
    sources = [
        f"{HEADER}\n\n{code}\n\n" + "\n".join(f"#print axioms {n}" for n in decls)
        for code, decls in zip(snippets, names, strict=True)
    ]
    results = kimina.check(sources, timeout_s)
    return {
        "backend": f"Kimina Lean Server ({kimina.repls} REPLs)",
        "results": [
            _kimina_result(code, r, decls)
            for code, r, decls in zip(snippets, results, names, strict=True)
        ],
    }


# --- goal tree --------------------------------------------------------------------------
# Every change of goal state is backed by a Lean check run here: the server composes each
# theorem statement from stored goal text, the worker supplies proofs only, and the check is
# `challenge prefix + use_lemmas (prefix stripped) + theorem`, through `scan_certificate` and
# `_check(...)["ok"]`. Upward propagation (a split goal closed by its children, a refutation
# carried through a weaken edge) is checked the same way, as its own composite theorem.

PROBE_TACTICS = "first | exact h | simpa using h | tauto | omega | (simp_all; done) | aesop"
DELIMITER_PAIRS = {")": "(", "]": "[", "}": "{", "⟩": "⟨"}


def _statement_guard(statement: str) -> None:
    """A goal statement is spliced into `(…) → (…)`: one balanced term, nothing that could
    end it early or comment out the rest of the composed theorem."""
    if not statement.strip() or re.search(r"--|/-|:=", statement):
        raise ValueError(f"goal statement must be one Lean term, no comments: {statement!r}")
    if not _balanced(statement):
        raise ValueError(f"unbalanced delimiters in goal statement: {statement!r}")


def _balanced(text: str) -> bool:
    stack = []
    for char in text:
        if char in DELIMITER_PAIRS.values():
            stack.append(char)
        elif char in DELIMITER_PAIRS and (not stack or stack.pop() != DELIMITER_PAIRS[char]):
            return False
    return not stack


def _codes(text: str) -> list[str]:
    """`text` with comments and strings blanked, in both orders (as `scan_certificate`)."""
    return [
        _COMMENTS.sub(" ", _STRINGS.sub('""', text)),
        _STRINGS.sub('""', _COMMENTS.sub(" ", text)),
    ]


# Commands that change what later text means: `variable (h : False)` + `include h` would add a
# false hypothesis to the composed theorem. Matched only at a word start (after whitespace),
# so `List.prefix` and the like pass.
# ponytail: instances and `open` in lemma sources can still change an intermediate goal's
# meaning; the root `Claim` and comparator stay fixed, so a certificate stays honest.
CONTEXT_COMMANDS = re.compile(
    r"(?:^|\s)(variable|include|omit|notation3?|infix[lr]?|prefix|postfix|macro(?:_rules)?"
    r"|syntax|elab(?:_rules)?)(?=[\s(\"]|$)",
    re.MULTILINE,
)


def _goal_context(problem: str, goal_id: int) -> tuple[str, dict[str, Any]]:
    """The challenge prefix and the goal (the root is created on the first goal call)."""
    challenge = verdict_server._challenge(problem)
    board.ensure_root(problem, challenge["title"])
    return verdict_server._prefix(challenge), board.live_goal(problem, goal_id)


def _goal_lemma(
    prefix: str,
    name: str,
    statement: str,
    proof: str,
    use_lemmas: list[int] | None,
    timeout: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Check `theorem name : statement := proof` after the prefix and `use_lemmas`.

    Returns (lemma record for the board, None) when it fully checks, else (None, rejection).
    The proof must add no declaration of its own: only `name` may be declared, and it is
    wrapped in parentheses so it cannot end the term early.
    """
    if any("/-" in c or not _balanced(c) for c in _codes(proof)):
        return None, {"reason": "the proof must be one term with balanced delimiters"}
    lemma_text = board.lemma_sources(use_lemmas, prefix) if use_lemmas else ""
    for code in _codes(f"{proof}\n{lemma_text}"):
        if found := CONTEXT_COMMANDS.search(code):
            return None, {"reason": f"`{found.group(1)}` is not allowed in goal proofs or lemmas"}
    source = f"theorem {name} : {statement} := (\n{proof.strip(chr(10))}\n)\n"
    if reason := scan_certificate(source):
        return None, {"reason": reason}
    if _decl_names(source) != [name] or NAMESPACE.search(source):
        return None, {"reason": "the proof may not declare anything or open/close scopes"}
    result = _check(source, use_lemmas, timeout, prefix=prefix)
    if not result["ok"]:
        return None, {"reason": f"`{name}` does not fully check", "check": _public(result)}
    lemma = {
        "name": name,
        "statement": statement,
        "source": source,
        "deps": list(use_lemmas or []),
        "types": _decl_types(result),
    }
    return lemma, None


def _probe(
    prefix: str,
    goal: dict[str, Any],
    child: str,
    use_lemmas: list[int] | None,
    assume: str | None = None,
) -> bool:
    """Does a cheap tactic close `(assume) → (child)` (or `child` alone without `assume`)?"""
    # ponytail: cheap probe; upgrade to portfolio(explore=false) if restatements slip through
    if assume is None:
        source = f"theorem goal_{goal['id']}_probe : ({child}) := by\n  {PROBE_TACTICS}\n"
    else:
        source = (
            f"theorem goal_{goal['id']}_probe : ({assume}) → ({child}) := by\n"
            f"  intro h\n  {PROBE_TACTICS}\n"
        )
    return bool(_check(source, use_lemmas, 30, prefix=prefix).get("ok"))


def _classify(
    prefix: str,
    goal: dict[str, Any],
    statements: list[str],
    weak_lemmas: list[dict[str, Any] | None],
    use_lemmas: list[int] | None,
) -> tuple[str, str]:
    """(relation, reason) for a checked split; only `case` counts as progress. A would-be
    `case` split runs the cheap probe on each child (up to 2k Lean checks of 30 s each)."""
    known = board.ancestor_hashes(goal["id"])
    if len(statements) >= 2:
        if any(board.goal_hash(s) in known for s in statements):
            return "restatement", "a child repeats the goal or an ancestor"
        if all(weak_lemmas):
            # one probe per child (up to 2k cheap checks), only for would-be case splits
            # every child already follows from P (weaken proofs); one that also implies P
            # cheaply is P restated
            target = goal["lean_statement"]
            if any(_probe(prefix, goal, target, use_lemmas, s) for s in statements):
                return "restatement", "a child implies the goal by a cheap tactic"
            if any(_probe(prefix, goal, s, use_lemmas) for s in statements):
                return "reduction", "a child is closed by a cheap tactic: it is not a real case"
            return "case", "every child has a weaken proof: a genuine case split"
        return "reduction", "some child lacks a weaken proof, so it may be harder than the goal"
    child = statements[0]
    if weak_lemmas[0]:
        return "restatement", "the weaken proof makes the single child equivalent to the goal"
    if board.goal_hash(child) in known:
        return "restatement", "the child repeats the goal or an ancestor"
    if decl := board.iff_declaration(goal["problem"], goal["lean_statement"], child):
        return "restatement", f"project declaration `{decl}` states the equivalence"
    if _probe(prefix, goal, child, use_lemmas, goal["lean_statement"]):
        return "restatement", "the equivalence probe proved goal → child"
    return "strengthening", "single child, no equivalence found: it may be strictly harder"


def _propagate_closed(
    problem: str, prefix: str, goal_id: int, timeout: float
) -> tuple[list[int], dict[str, Any] | None]:
    """Close split ancestors whose live split now has every child closed, each through a
    checked composite `theorem goal_<P>_closed : P := split child_1_closed …`."""
    closed: list[int] = []
    edge = board.parent_edge(goal_id)
    while (
        edge
        and not edge["split"]["dead"]
        and edge["parent"]["status"] == "split"
        and all(c["status"] == "closed" for c in edge["children"])
    ):
        parent, split_lemma_id = edge["parent"], edge["split"]["lemma_id"]
        parts = [split_lemma_id, *(c["lemma_id"] for c in edge["children"])]
        proof = " ".join(board.lemma_row(lemma_id)["name"] for lemma_id in parts)
        lemma, rejection = _goal_lemma(
            prefix,
            f"goal_{parent['id']}_closed",
            parent["lean_statement"],
            proof,
            board.lemma_chain(parts),
            timeout,
        )
        if rejection:
            return closed, {"goal_id": parent["id"], **rejection}
        board.resolve_goal(problem, parent["id"], "closed", lemma)
        closed.append(parent["id"])
        edge = board.parent_edge(parent["id"])
    return closed, None


def _propagate_refuted(
    problem: str, prefix: str, goal_id: int, lemma_id: int, timeout: float
) -> dict[str, Any]:
    """Carry `¬ C` up through weaken edges (`P → C` gives `¬ P`, checked as a composite).
    Without a weaken edge the parent's split is useless: it is marked dead and the parent
    reopened."""
    refuted: list[int] = []
    edge = board.parent_edge(goal_id)
    while edge and not edge["split"]["dead"] and edge["parent"]["status"] == "split":
        parent = edge["parent"]
        weak_id = edge["split"]["weaken_lemma_ids"][edge["index"]]
        if weak_id is not None:
            weak, child = board.lemma_row(weak_id)["name"], board.lemma_row(lemma_id)["name"]
            lemma, rejection = _goal_lemma(
                prefix,
                f"goal_{parent['id']}_refuted",
                f"¬ ({parent['lean_statement']})",
                f"fun h => {child} ({weak} h)",
                board.lemma_chain([weak_id, lemma_id]),
                timeout,
            )
            if lemma is not None:
                lemma_id = board.resolve_goal(problem, parent["id"], "refuted", lemma)
                refuted.append(parent["id"])
                edge = board.parent_edge(parent["id"])
                continue
            return {
                "refuted": refuted,
                "reopened": None,
                "dead_split": None,
                "propagation_failed": {"goal_id": parent["id"], **rejection},
            }
        board.kill_split(problem, edge["split"]["id"], parent["id"])
        return {"refuted": refuted, "reopened": parent["id"], "dead_split": edge["split"]["id"]}
    return {"refuted": refuted, "reopened": None, "dead_split": None}


@server.tool()
def goal_split(
    problem: str,
    goal_id: int,
    children: list[dict[str, str]],
    proof: str,
    weaken_proofs: list[str | None] | None = None,
    use_lemmas: list[int] | None = None,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Split open goal P (ids from `whiteboard.goal_tree`) into children C1..Ck by a
    Lean-checked reduction.

    children: [{"lean_statement": "<Prop, elaborated after the challenge prefix>",
    "informal": "..."}]. proof: proves `(C1) → … → (Ck) → (P)`. weaken_proofs, one per child
    (null where absent): proves `(P) → (Ci)`. Relation: `case` (k ≥ 2, every child has a
    weaken proof: progress); `reduction` (k ≥ 2, some child without one); `restatement` (a
    single child equivalent to P, or a child repeating an ancestor: flagged, no progress);
    `strengthening` (a single child, no equivalence found: flagged, no progress). Nothing
    is written unless every proof checks.
    """
    prefix, goal = _goal_context(problem, goal_id)
    if goal["status"] != "open":
        raise ValueError(f"goal {goal_id} is {goal['status']}; only open goals split")
    statements = [c["lean_statement"] for c in children]
    if not statements:
        raise ValueError("a split needs at least one child")
    for statement in statements:
        _statement_guard(statement)
    if len({board.goal_hash(s) for s in statements}) < len(statements):
        raise ValueError("two children have the same statement")
    weaken = weaken_proofs or [None] * len(statements)
    if len(weaken) != len(statements):
        raise ValueError("weaken_proofs needs one entry (or null) per child")
    n = board.split_count(goal_id) + 1
    target = goal["lean_statement"]
    split_lemma, rejection = _goal_lemma(
        prefix,
        f"goal_{goal_id}_split{n}",
        " → ".join(f"({s})" for s in [*statements, target]),
        proof,
        use_lemmas,
        timeout_s,
    )
    if rejection:
        return {"split": False, **rejection}
    weak_lemmas: list[dict[str, Any] | None] = []
    for i, (statement, weak_proof) in enumerate(zip(statements, weaken, strict=True), 1):
        if weak_proof is None:
            weak_lemmas.append(None)
            continue
        lemma, rejection = _goal_lemma(
            prefix,
            f"goal_{goal_id}_weak{n}_{i}",
            f"({target}) → ({statement})",
            weak_proof,
            use_lemmas,
            timeout_s,
        )
        if rejection:
            return {"split": False, "child": i, **rejection}
        weak_lemmas.append(lemma)
    relation, reason = _classify(prefix, goal, statements, weak_lemmas, use_lemmas)
    flag = relation if relation in ("restatement", "strengthening") else None
    rows = board.add_split(problem, goal_id, relation, flag, children, split_lemma, weak_lemmas, n)
    return {
        "split": True,
        "split_id": rows["split_id"],
        "relation": relation,
        "flag": flag,
        "children": rows["children"],
        "progress": relation == "case",
        "reason": reason,
    }


@server.tool()
def goal_close(
    problem: str,
    goal_id: int,
    proof: str,
    use_lemmas: list[int] | None = None,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Close goal C with a Lean proof of `theorem goal_<id>_closed : C`.

    A split goal whose live split then has every child closed closes too (checked as a
    composite of the split lemma and the children's lemmas), recursively up to the root.
    """
    prefix, goal = _goal_context(problem, goal_id)
    lemma, rejection = _goal_lemma(
        prefix, f"goal_{goal_id}_closed", goal["lean_statement"], proof, use_lemmas, timeout_s
    )
    if rejection:
        return {"closed": [], **rejection}
    board.resolve_goal(problem, goal_id, "closed", lemma)
    above, stuck = _propagate_closed(problem, prefix, goal_id, timeout_s)
    result: dict[str, Any] = {
        "closed": [goal_id, *above],
        "root_closed": board.root_goal(problem)["status"] == "closed",
    }
    if stuck:  # e.g. a timeout: the worker can close that parent with goal_close directly
        result["propagation_failed"] = stuck
    return result


@server.tool()
def goal_refute(
    problem: str,
    goal_id: int,
    proof: str,
    use_lemmas: list[int] | None = None,
    timeout_s: int = 300,
) -> dict[str, Any]:
    """Refute goal C with a Lean proof of `theorem goal_<id>_refuted : ¬ (C)`.

    Through a weaken edge (`P → C`) the parent is refuted too (checked composite), and so
    on upward. Otherwise the parent's split is marked dead and the parent reopened. A
    coverage row with `lemma_refuted` never refutes a goal: it is not a Lean proof.
    """
    prefix, goal = _goal_context(problem, goal_id)
    lemma, rejection = _goal_lemma(
        prefix,
        f"goal_{goal_id}_refuted",
        f"¬ ({goal['lean_statement']})",
        proof,
        use_lemmas,
        timeout_s,
    )
    if rejection:
        return {"refuted": [], **rejection}
    lemma_id = board.resolve_goal(problem, goal_id, "refuted", lemma)
    above = _propagate_refuted(problem, prefix, goal_id, lemma_id, timeout_s)
    return {
        **above,
        "refuted": [goal_id, *above["refuted"]],
        "root_refuted": board.root_goal(problem)["status"] == "refuted",
    }


@server.tool()
def goal_certificate(problem: str) -> dict[str, Any]:
    """The verdict certificate once the root goal is closed (PROVED) or refuted (DISPROVED):
    the goal lemmas and everything they were checked against, in dependency order and
    without the challenge prefix, ending in `theorem claim : Claim := …` (or `claim_neg`).
    Pass `proof` to `verdict.submit_verdict` unchanged: comparator is the judge.
    """
    root = board.root_goal(problem)
    if root is None or root["status"] not in ("closed", "refuted"):
        status = root["status"] if root else "not created yet"
        return {"verdict": None, "reason": f"the root goal is {status}"}
    name = board.lemma_row(root["lemma_id"])["name"]
    body = board.lemma_sources(board.lemma_chain([root["lemma_id"]]), _challenge_prefix(problem))
    if root["status"] == "closed":
        verdict, final = "PROVED", f"theorem claim : Claim := {name}"
    else:
        verdict, final = "DISPROVED", f"theorem claim_neg : ¬ Claim := {name}"
    return {"verdict": verdict, "proof": f"{body}\n\n{final}\n"}
