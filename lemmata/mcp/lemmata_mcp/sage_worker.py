"""Warm Sage worker. Runs under the Sage conda env's python, so it imports nothing from
lemmata_mcp. Protocol: one JSON request per stdin line, one JSON response per line on
the original stdout; fd 1 is pointed at stderr so stray C-level prints cannot corrupt it.

Executing worker-written code is the point of this process; cas.py starts it under
landrun with write access only to the scratch directory.
"""

import ast
import contextlib
import io
import json
import os
import sys
import traceback

PROTOCOL = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)

# Sage lives in its own conda env, not the venv the type checker sees.
from sage.all import alarm, cancel_alarm  # noqa: E402  # pyright: ignore[reportMissingImports]
from sage.repl.preparse import preparse  # noqa: E402  # pyright: ignore[reportMissingImports]

NAMESPACE: dict = {}
exec("from sage.all import *", NAMESPACE)


def evaluate(code: str):
    """Exec the code; if the last statement is an expression, return its value."""
    tree = ast.parse(preparse(code))
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    exec(compile(tree, "<sage>", "exec"), NAMESPACE)
    if last is None:
        return None
    return eval(compile(ast.Expression(last.value), "<sage>", "eval"), NAMESPACE)


for line in sys.stdin:
    request = json.loads(line)
    if request.get("reset"):
        NAMESPACE.clear()
        exec("from sage.all import *", NAMESPACE)
    captured = io.StringIO()
    try:
        alarm(request["timeout"])
        with contextlib.redirect_stdout(captured):
            value = evaluate(request["code"])
        cancel_alarm()
        response = {"ok": True, "value": None if value is None else str(value)}
    except BaseException:  # AlarmInterrupt is a KeyboardInterrupt
        cancel_alarm()
        response = {"ok": False, "error": traceback.format_exc(limit=4)[-4000:]}
    response["stdout"] = captured.getvalue()[-20000:]
    PROTOCOL.write(json.dumps(response) + "\n")
