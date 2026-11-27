"""One warm leanprover-community/repl process with `import Mathlib` loaded once."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from .common import CHILD_PATH, SCRATCH, WORK_PROJECT, base_env, log, run, sandboxed

HEADER = "import Mathlib"
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
        binary = self.project / ".lake" / "packages" / "REPL" / ".lake" / "build" / "bin" / "repl"
        if not binary.exists():
            raise RuntimeError(f"REPL binary missing at {binary}; run setup-wsl.sh lean")
        env = self._lean_env()
        self.proc = subprocess.Popen(
            sandboxed([str(binary)], env, [SCRATCH]),
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
        log("starting Lean REPL (import Mathlib)")
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
