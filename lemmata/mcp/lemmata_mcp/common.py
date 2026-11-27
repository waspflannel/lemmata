"""Paths, pins, sandboxing and subprocess helpers shared by every lemmata server."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent


def _load_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in (PACKAGE_DIR / "pins.env").read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            pins[key] = value
    return pins


PINS = _load_pins()

HOME = Path(os.environ.get("LEMMATA_HOME", Path.home() / ".lemmata"))
BIN = HOME / "bin"
WORK_PROJECT = HOME / "lean"  # warm REPL, lean-lsp-mcp, scratch files
VERIFY_PROJECT = HOME / "verify"  # read-only Mathlib used only by comparator
STATE = HOME / "state"  # whiteboard.db, verdicts.db, artifacts
SCRATCH = HOME / "scratch"  # the only directory sandboxed code may write
INDEX_DB = HOME / "index" / "decls.db"
SAGE_PYTHON = HOME / "miniforge3" / "envs" / "sage" / "bin" / "python"

STANDARD_AXIOMS = ("propext", "Classical.choice", "Quot.sound")
# Child processes get this PATH, never the Windows PATH that WSL appends.
CHILD_PATH = f"{BIN}:{Path.home() / '.elan' / 'bin'}:/usr/local/bin:/usr/bin:/bin"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def log(message: str) -> None:
    """Servers speak MCP on stdout, so diagnostics go to stderr."""
    print(f"[lemmata] {message}", file=sys.stderr, flush=True)


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
