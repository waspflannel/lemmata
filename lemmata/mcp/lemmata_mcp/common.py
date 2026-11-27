"""Paths, pins, sandboxing and subprocess helpers shared by every lemmata server."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    return values


PINS = _load_env(PACKAGE_DIR / "pins.env")

HOME = Path(os.environ.get("LEMMATA_HOME", Path.home() / ".lemmata"))
# Runtime switches: package defaults, overridden by $LEMMATA_HOME/options.env (setup step).
OPTIONS = _load_env(PACKAGE_DIR / "options.env")
if (HOME / "options.env").exists():
    OPTIONS |= _load_env(HOME / "options.env")
BIN = HOME / "bin"
WORK_PROJECT = HOME / "lean"  # warm REPL, lean-lsp-mcp, scratch files
VERIFY_PROJECT = HOME / "verify"  # read-only Mathlib used only by comparator
STATE = HOME / "state"  # whiteboard.db, verdicts.db, artifacts
SCRATCH = HOME / "scratch"  # the only directory sandboxed code may write
INDEX_DB = HOME / "index" / "decls.db"
SAGE_PYTHON = HOME / "miniforge3" / "envs" / "sage" / "bin" / "python"

STANDARD_AXIOMS = ("propext", "Classical.choice", "Quot.sound")
# Lean packages imported next to Mathlib when a project's manifest has them: the work
# project's set goes into the REPL header, the verification project's into every solution,
# so what checks in the REPL is what comparator builds. setup-wsl.sh installs both together.
EXTRA_IMPORTS = {"Hammer": "Hammer", "LeanCert": "LeanCert.Tactic"}
# Child processes get this PATH, never the Windows PATH that WSL appends.
CHILD_PATH = f"{BIN}:{Path.home() / '.elan' / 'bin'}:/usr/local/bin:/usr/bin:/bin"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def log(message: str) -> None:
    """Servers speak MCP on stdout, so diagnostics go to stderr."""
    print(f"[lemmata] {message}", file=sys.stderr, flush=True)


# Certificate text rejected before comparator runs (and by lean.register_lemma, since proved
# lemmas end up in certificates). Comparator's axiom check catches the semantic cases too
# (native_decide adds Lean.ofReduceBool); this gives a clear early reason. v2: `hammer`,
# `premises` and library suggestions would run provers or premise selection (possibly over
# the network) inside comparator's build: a certificate carries the tactic hammer suggested.
FORBIDDEN_SOURCE = re.compile(
    r"\b(sorry|admit|native_decide|implemented_by|extern|unsafe|axiom|debug\.skipKernelTC"
    r"|hammer|Hammer|premises|suggestions|LibrarySuggestions|set_library_suggestions)\b"
)
# LeanCert's native and auto trust modes check with native_decide (compiler in the trusted
# base). Its default mode is native too, which comparator rejects through that axiom. Only
# `(trust := kernel)` can certify PROVED.
LEANCERT_NATIVE = re.compile(
    r"trust\s*:=\s*\.?(?:native|auto)\b|leancert\.trust\s+\"(?:native|auto)\""
)
_COMMENTS = re.compile(r"--.*|/-(?:.|\n)*?-/")
_STRINGS = re.compile(r'"(?:[^"\\\n]|\\.)*"')


def scan_certificate(source: str) -> str | None:
    """Reason a certificate is refused, or None. Two views (comments stripped; strings blanked
    then comments stripped), so neither `"--"` nor a quote inside a comment can hide a token."""
    for code in (_COMMENTS.sub("", source), _COMMENTS.sub("", _STRINGS.sub('""', source))):
        if match := FORBIDDEN_SOURCE.search(code):
            return f"forbidden in a certificate: `{match.group(1)}`"
        if LEANCERT_NATIVE.search(code):
            return "LeanCert native/auto trust is exploration only; use `(trust := kernel)`"
    return None


def extra_imports(project: Path) -> list[str]:
    """Modules of EXTRA_IMPORTS whose package is in `project`'s lake manifest."""
    try:
        manifest = json.loads((project / "lake-manifest.json").read_text())
    except FileNotFoundError:
        return []
    installed = {package["name"] for package in manifest["packages"]}
    return [module for package, module in EXTRA_IMPORTS.items() if package in installed]


def tool_path(name: str) -> Path | None:
    path = BIN / name
    return path if path.exists() else None


def sandboxed(cmd: list[str], env: dict[str, str], writable: list[Path]) -> list[str]:
    """Wrap `cmd` in landrun: read/exec everywhere, write only `writable` and /dev.

    Worker-written code (Lean `#eval`, Python predicates, Sage) runs through this so it
    cannot touch the verification Mathlib or the state databases. Landlock on the WSL2
    6.6 kernel (ABI 3) cannot restrict network; see README.
    """
    landrun = tool_path("landrun")
    if landrun is None:
        log("landrun missing: running worker code UNSANDBOXED")
        return cmd
    args = [str(landrun), "--rox", "/", "--rw", "/dev"]
    for path in writable:
        path.mkdir(parents=True, exist_ok=True)
        args += ["--rw", str(path)]
    # landrun passes no environment, and its --env splits values on commas, so set the
    # variables with env(1) inside the sandbox instead.
    return [*args, "--", "/usr/bin/env", *(f"{k}={v}" for k, v in env.items()), *cmd]


def limit_memory(max_bytes: int):
    """preexec_fn: cap address space (not for Lean, which reserves huge virtual ranges)."""

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))

    return apply


def run(
    cmd: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    preexec=None,
) -> tuple[int | None, str, str]:
    """Run to completion; on timeout kill the whole process group. Returns (code, out, err).

    `code` is None on timeout.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=preexec,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return None, out, err


def base_env(**extra: str) -> dict[str, str]:
    """Minimal child environment. TMPDIR is in scratch: sandboxed code cannot write /tmp."""
    tmp = SCRATCH / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": CHILD_PATH,
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "TMPDIR": str(tmp),
        **extra,
    }


def run_job(payload: dict[str, Any], timeout: float, memory_gb: int = 4) -> dict[str, Any]:
    """Run `lemmata_mcp.jobs` (worker-written Python) sandboxed, one JSON in, one JSON out."""
    env = base_env()
    cmd = sandboxed([sys.executable, "-I", "-m", "lemmata_mcp.jobs"], env, [SCRATCH])
    # The job enforces `timeout` itself and reports partial coverage; this is the backstop.
    code, out, err = run(
        cmd,
        timeout=timeout + 30,
        env=env,
        stdin=json.dumps({**payload, "timeout": timeout}),
        preexec=limit_memory(memory_gb << 30),
    )
    if code is None:
        return {"error": "job killed after hard timeout", "stderr": err[-2000:]}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": f"job failed (exit {code})", "stderr": err[-4000:]}
