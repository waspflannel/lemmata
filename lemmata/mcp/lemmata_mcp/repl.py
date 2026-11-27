"""One warm leanprover-community/repl process with the Lean header loaded once, plus an
opt-in Kimina Lean Server pool (same REPL binary and project) for parallel batch checks."""

from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .common import (
    CHILD_PATH,
    HOME,
    OPTIONS,
    SCRATCH,
    WORK_PROJECT,
    base_env,
    extra_imports,
    log,
    run,
    sandboxed,
)

IMPORTS = ["Mathlib", *extra_imports(WORK_PROJECT)]
HEADER = "\n".join(f"import {module}" for module in IMPORTS)
# LeanHammer's default premise selector sends goals to leanpremise.net. Keep premise
# selection on the machine: Sine Qua Non over the imported library and the current file.
# The REPL binary cannot load lean-cvc5's native library (precompiled FFI), so hammer's
# SMT branch would abort the process: it is off by default here.
if "Hammer" in IMPORTS:
    HEADER += (
        "\n\nset_library_suggestions open Lean.LibrarySuggestions in"
        " sineQuaNonSelector.intersperse currentFile"
        "\nset_option hammer.disableSmtDefault true"
    )
REPL_BINARY = WORK_PROJECT / ".lake" / "packages" / "REPL" / ".lake" / "build" / "bin" / "repl"
# ponytail: the REPL keeps every environment it ever produced; restart to free memory.
# Upgrade path: pickle the base env once and unpickle on restart if import time matters.
RESTART_AFTER = 400


class ReplTimeout(Exception):
    pass


class Repl:
    def __init__(self, project: Path = WORK_PROJECT) -> None:
        self.project = project
        self.proc: subprocess.Popen[str] | None = None
        self.responses: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.base_env = 0
        self.commands = 0
        self.lock = threading.Lock()
        self._env: dict[str, str] | None = None

    def _lean_env(self) -> dict[str, str]:
        """LEAN_PATH / LEAN_SYSROOT as `lake env` sets them (computed once, unsandboxed)."""
        if self._env is None:
            code, out, err = run(
                ["lake", "env", "printenv"], cwd=self.project, timeout=300, env=base_env()
            )
            if code != 0:
                raise RuntimeError(f"`lake env` failed in {self.project}: {err[-1000:]}")
            lake = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            sysroot = lake.get("LEAN_SYSROOT", "")
            self._env = base_env(
                LEAN_PATH=lake.get("LEAN_PATH", ""),
                LEAN_SYSROOT=sysroot,
                PATH=f"{sysroot}/bin:{CHILD_PATH}",
            )
        return self._env

    def _start(self) -> None:
        if not REPL_BINARY.exists():
            raise RuntimeError(f"REPL binary missing at {REPL_BINARY}; run setup-wsl.sh lean")
        env = self._lean_env()
        self.proc = subprocess.Popen(
            sandboxed([str(REPL_BINARY)], env, [SCRATCH]),
            cwd=self.project,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        self.responses = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self.responses), daemon=True).start()
        self.commands = 0
        log(f"starting Lean REPL ({', '.join(IMPORTS)})")
        self.base_env = self._send({"cmd": HEADER}, timeout=900)["env"]

    @staticmethod
    def _read(proc: subprocess.Popen[str], out: queue.Queue[dict[str, Any] | None]) -> None:
        """Responses are JSON objects separated by blank lines."""
        assert proc.stdout is not None
        buffer: list[str] = []
        for line in proc.stdout:
            if line.strip():
                buffer.append(line)
                continue
            if buffer:
                out.put(json.loads("".join(buffer)))
                buffer = []
        out.put(None)

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc = None

    def _send(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(request) + "\n\n")
        self.proc.stdin.flush()
        try:
            response = self.responses.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            raise ReplTimeout(f"no REPL response within {timeout}s") from None
        if response is None:
            self._kill()
            raise RuntimeError("REPL exited")
        self.commands += 1
        return response

    def _ensure(self) -> None:
        if self.proc is None or self.proc.poll() is not None or self.commands > RESTART_AFTER:
            self._kill()
            self._start()

    def command(
        self, code: str, timeout: float, all_tactics: bool = False, env: int | None = None
    ) -> dict[str, Any]:
        """Elaborate `code` on top of `import Mathlib` (or on top of `env`).

        On timeout the process is killed and the next call restarts it, so env and
        proof-state ids from before a timeout are invalid afterwards.
        """
        request: dict[str, Any] = {"cmd": code}
        if all_tactics:
            request["allTactics"] = True
        with self.lock:
            self._ensure()
            request["env"] = self.base_env if env is None else env
            return self._send(request, timeout)

    def tactic(self, tactic: str, proof_state: int, timeout: float) -> dict[str, Any]:
        """Run a tactic on a proof state from an earlier `command` (never restarts)."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                raise ReplTimeout("REPL restarted since that proof state was made")
            return self._send({"tactic": tactic, "proofState": proof_state}, timeout)


class Kimina:
    """Kimina Lean Server: a pool of KIMINA_REPLS leanprover-community/repl processes (the
    work project's binary and Mathlib) that checks independent snippets in parallel. Started
    on first use, sandboxed like the warm REPL, bound to localhost with a random API key."""

    def __init__(self) -> None:
        self.repls = int(OPTIONS.get("KIMINA_REPLS") or 0)
        self.proc: subprocess.Popen[bytes] | None = None
        self.url = ""
        self.key = secrets.token_hex(16)
        self.lock = threading.Lock()

    def _start(self) -> None:
        python = HOME / "kimina-venv" / "bin" / "python"
        if not python.exists():
            raise RuntimeError("Kimina not installed; run setup-wsl.sh kimina")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = base_env(
            LEAN_SERVER_HOST="127.0.0.1",
            LEAN_SERVER_PORT=str(port),
            LEAN_SERVER_API_KEY=self.key,
            LEAN_SERVER_MAX_REPLS=str(self.repls),
            LEAN_SERVER_REPL_PATH=str(REPL_BINARY),
            LEAN_SERVER_PROJECT_DIR=str(WORK_PROJECT),
            LEAN_SERVER_LOG_LEVEL="ERROR",
            # Kimina caps each REPL's address space (RLIMIT_AS), but Lean reserves far more
            # virtual memory than it uses and dies under any realistic cap. RAM is bounded
            # by KIMINA_REPLS instead (see options.env).
            LEAN_SERVER_MAX_REPL_MEM="1024G",
        )
        self.proc = subprocess.Popen(
            sandboxed([str(python), "-m", "server"], env, [SCRATCH]),
            cwd=HOME / "src" / "kimina",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Kimina server exited during startup")
            try:
                urllib.request.urlopen(f"{self.url}/health", timeout=2).close()
                log(f"Kimina Lean Server up with {self.repls} REPLs")
                return
            except OSError:  # URLError is an OSError: not listening yet
                time.sleep(0.5)
        os.killpg(self.proc.pid, signal.SIGKILL)
        raise RuntimeError("Kimina server did not become healthy within 120 s")

    def check(self, codes: list[str], timeout: float) -> list[dict[str, Any]]:
        """One Kimina result per code (same order): {time, error?, response?}."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
        snippets = [{"id": str(i), "code": code} for i, code in enumerate(codes)]
        request = urllib.request.Request(
            f"{self.url}/api/check",
            data=json.dumps({"snippets": snippets, "timeout": int(timeout)}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"},
        )
        # Kimina queues snippets beyond its REPL count, so allow every one its timeout.
        budget = timeout * (len(codes) // max(self.repls, 1) + 1) + 900
        with urllib.request.urlopen(request, timeout=budget) as response:
            results = json.load(response)["results"]
        return sorted(results, key=lambda r: int(r["id"]))
