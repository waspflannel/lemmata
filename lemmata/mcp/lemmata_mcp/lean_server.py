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

from . import board
from .cas import Sage, goal_equations, ideal_cofactors
from .common import STANDARD_AXIOMS, WORK_PROJECT, scan_certificate
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
        "tactic, `batch_check` for many independent snippets, and `register_lemma` to share "
        "a finished lemma. native_decide and LeanCert's native/auto trust modes are "
        "exploration only; `hammer` counts only through the tactic it suggests; polyrith is "
        "unavailable."
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
    code: str, use_lemmas: list[int] | None, timeout: float, all_tactics: bool = False
) -> dict[str, Any]:
    _guard(code)
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


@server.tool()
def register_lemma(lemma_id: int, code: str, use_lemmas: list[int] | None = None) -> dict[str, Any]:
    """Check a finished lemma and mark whiteboard lemma `lemma_id` as proved.

    Requires `ok` from `check` (no errors, no sorry, standard axioms only) and the same text
    rules as a verdict certificate (no native_decide, hammer, LeanCert native trust, ...),
    since registered lemmas end up inside certificates. The source and each declaration's
    type become searchable through the library server and reusable by everyone via
    `use_lemmas`.
    """
    if reason := scan_certificate(code):
        return {"registered": False, "reason": reason}
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
