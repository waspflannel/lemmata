"""Operator CLI for the public, read-only Left to Solve endpoints."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import PINS, STATE
from .lts_lean import TextOnly, extract_claim

SITE = "https://lefttosolve.com"
UPSTREAM_COMMIT = "9097e2f90e66d2f6244209cb1c07f7120d17ec53"
CACHE = STATE / "left-to-solve.db"
CACHE_SECONDS = 300
REQUEST_INTERVAL = 4
MAX_RESPONSE = 1024 * 1024
PROBLEM_ID = re.compile(r"[\w«][\w.'«»:~-]{0,239}")


class LTSError(Exception):
    """A request could not be completed."""


class NotFound(LTSError):
    """The catalog has no resource at the requested URL."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def cache_db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(CACHE, isolation_level=None, timeout=40)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS responses (
            path TEXT PRIMARY KEY, fetched REAL NOT NULL, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS last_request (id INTEGER PRIMARY KEY, started REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS text_imports (
            id TEXT PRIMARY KEY, details TEXT NOT NULL, brief TEXT, reason TEXT NOT NULL
        );
    """)
    return db


def read_response(path: str, *, problem_id: str = "", brief: bool = False) -> Any:
    """Serialize cache misses across CLI processes, including failed requests."""
    with closing(cache_db()) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            cached = db.execute(
                "SELECT body FROM responses WHERE path = ? AND fetched >= ?",
                (path, time.time() - CACHE_SECONDS),
            ).fetchone()
            if cached is not None:
                return cached[0] if brief else json.loads(cached[0])

            last = db.execute("SELECT started FROM last_request WHERE id = 1").fetchone()
            if last is not None:
                wait = last[0] + REQUEST_INTERVAL - time.monotonic()
                time.sleep(max(0, min(REQUEST_INTERVAL, wait)))
            db.execute("INSERT OR REPLACE INTO last_request VALUES (1, ?)", (time.monotonic(),))
            content_type = "text/plain" if brief else "application/json"
            request = Request(
                SITE + path, headers={"Accept": content_type, "User-Agent": "lemmata-mcp"}
            )
            try:
                with build_opener(NoRedirects()).open(request, timeout=18) as response:
                    if response.headers.get_content_type() != content_type:
                        raise LTSError(f"unexpected content type from {path}")
                    raw = response.read(MAX_RESPONSE + 1)
            except HTTPError as error:
                if error.code == 404:
                    raise NotFound(f"{path}: resource not found (HTTP 404)") from error
                if error.code == 429 or error.code >= 500:
                    reason = "temporarily unavailable"
                else:
                    reason = "request rejected"
                raise LTSError(f"{path}: {reason} (HTTP {error.code})") from error
            except (URLError, OSError, HTTPException) as error:
                raise LTSError(f"{path}: network request failed: {error}") from error

            if len(raw) > MAX_RESPONSE:
                raise LTSError(f"{path}: response exceeds {MAX_RESPONSE} bytes")
            try:
                body = raw.decode("utf-8")
                if brief:
                    header = f"LEFT TO SOLVE · AGENT BRIEF · {problem_id}"
                    if body.partition("\n")[0].rstrip("\r") != header or re.search(
                        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]|\r(?!\n)", body
                    ):
                        raise ValueError("unexpected brief header or control characters")
                    data = body
                else:
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise ValueError("expected a JSON object")
                    if problem_id:
                        if data.get("id") != problem_id or not valid_title(data.get("title")):
                            raise ValueError("problem details have an invalid ID or title")
                    else:
                        validate_search(data)
            except (ValueError, RecursionError) as error:
                raise LTSError(f"{path}: invalid response: {error}") from error
            db.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (path, time.time(), body)
            )
            return data
        finally:
            db.commit()


def valid_title(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 500 and value.isprintable()


def validate_search(data: dict[str, Any]) -> None:
    items = data.get("items")
    if not isinstance(items, list) or len(items) > 100 or type(data.get("total")) is not int:
        raise ValueError("expected a bounded list of problems and a total count")
    for row in items:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not PROBLEM_ID.fullmatch(row["id"])
            or not valid_title(row.get("title"))
            or row.get("kind") not in ("open", "known", "warmup")
            or (row.get("difficulty") is not None and type(row["difficulty"]) is not int)
            or type(row.get("liveAgents")) is not int
            or not isinstance(row.get("solved"), bool)
        ):
            raise ValueError("invalid search result")


def search(args: argparse.Namespace) -> None:
    if not 1 <= args.limit <= 100 or not 0 <= args.offset <= 100_000:
        raise LTSError("limit must be 1–100 and offset must be 0–100000")
    filters = {"q": args.q, "kind": args.kind, "source": args.source, "field": args.field}
    if any(len(value or "") > 256 for value in filters.values()):
        raise LTSError("search filters must be at most 256 characters each")
    query = {key: value for key, value in filters.items() if value}
    query.update({"sort": args.sort, "limit": str(args.limit), "offset": str(args.offset)})
    data = read_response("/api/problems?" + urlencode(query))
    rows = [("ID", "TITLE", "KIND", "DIFFICULTY", "SOLVED", "LIVE AGENTS")]
    for item in data["items"]:
        difficulty = item.get("difficulty")
        rows.append(
            (
                item["id"],
                item["title"],
                item["kind"],
                "—" if difficulty is None else str(difficulty),
                "yes" if item["solved"] else "no",
                str(item["liveAgents"]),
            )
        )
    widths = [max(map(len, column)) for column in zip(*rows, strict=True)]
    for row in rows:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )
    print(f"{len(rows) - 1} shown / {data['total']} total")


def search_row(problem_id: str, details: dict[str, Any]) -> dict[str, Any] | None:
    """The catalog's search row for `problem_id`: the validated `solved` flag lives only there
    (problem details carry no `solved` field). Details with a boolean `solved` are used as is."""
    if isinstance(details.get("solved"), bool):
        return details
    data = read_response("/api/problems?" + urlencode({"q": problem_id, "limit": "5"}))
    return next((row for row in data["items"] if row["id"] == problem_id), None)


def import_problem(problem_id: str, encoded_id: str, details: dict[str, Any]) -> None:
    from .verdict_server import freeze_lts

    try:
        row = search_row(problem_id, details)
    except LTSError as error:
        row = None
        print(f"warning: could not check solved status: {error}", file=sys.stderr)
    if row is not None and row["solved"]:
        print(
            f"warning: Left to Solve marks {problem_id} as solved; "
            f"run `lemmata-mcp triage {problem_id}` before spending a run on it.",
            file=sys.stderr,
        )
    brief = None
    try:
        lean = details.get("lean")
        if lean is None or lean == "":
            raise TextOnly("the catalog has no Lean statement for this problem")
        informal = details.get("statement")
        if (
            not isinstance(lean, str)
            or not isinstance(informal, str)
            or not informal.strip()
            or re.search(r"[\x00\ud800-\udfff]", informal)
        ):
            raise LTSError("problem details have an invalid statement")
        try:
            brief = read_response(f"/p/{encoded_id}/agent.txt", problem_id=problem_id, brief=True)
        except NotFound as error:
            raise TextOnly("no agent brief is available for this declaration") from error
        if details.get("upstreamCommit") != UPSTREAM_COMMIT or details.get("decl") != problem_id:
            raise TextOnly(
                "the Lean declaration does not match the expected upstream revision or ID"
            )
        preamble, claim = extract_claim(problem_id, lean, brief)
        result = freeze_lts(
            title=details["title"],
            informal=informal,
            preamble=preamble,
            claim=claim,
            problem_id=problem_id,
            url=f"{SITE}/p/{encoded_id}",
            brief=brief,
        )
        if not result["frozen"]:
            output = result["output"]
            if not output:
                raise LTSError(result["reason"])
            raise TextOnly(output)
    except TextOnly as error:
        with closing(cache_db()) as db:
            db.execute(
                "INSERT OR REPLACE INTO text_imports VALUES (?, ?, ?, ?)",
                (problem_id, json.dumps(details), brief, str(error)),
            )
        print(f"Recorded {problem_id} as text-only in {CACHE}.")
        print(f"Lean {PINS['LEAN_TOOLCHAIN'].split(':', 1)[-1]}: {error}", file=sys.stderr)
    else:
        with closing(cache_db()) as db:
            db.execute("DELETE FROM text_imports WHERE id = ?", (problem_id,))
        print(f"Frozen on Lean {PINS['LEAN_TOOLCHAIN'].split(':', 1)[-1]}.")
        print(f"Work on challenge {result['challenge_id']}.")


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="lemmata-mcp lts", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search_parser = commands.add_parser("search", help="list matching problems")
    search_parser.add_argument("--q")
    search_parser.add_argument("--kind", choices=("open", "known", "warmup"))
    search_parser.add_argument("--source")
    search_parser.add_argument("--field")
    search_parser.add_argument(
        "--sort", choices=("easiest", "hardest", "prize", "active"), default="easiest"
    )
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--offset", type=int, default=0)
    for command, help_text in (
        ("show", "show problem details"),
        ("brief", "cache and print the agent brief"),
        ("import", "import a problem into local challenge state"),
    ):
        commands.add_parser(command, help=help_text).add_argument("id")
    args = parser.parse_args(argv)

    try:
        if args.command == "search":
            search(args)
            return
        if not PROBLEM_ID.fullmatch(args.id):
            raise LTSError("invalid problem ID")
        encoded_id = quote(args.id, safe="")
        if args.command == "brief":
            print(read_response(f"/p/{encoded_id}/agent.txt", problem_id=args.id, brief=True))
            return
        details = read_response(f"/api/problems/{encoded_id}", problem_id=args.id)
        if args.command == "show":
            print(json.dumps(details, indent=2, ensure_ascii=True))
        else:
            import_problem(args.id, encoded_id, details)
    except (LTSError, OSError, RuntimeError, sqlite3.Error) as error:
        sys.exit(f"lts: {error}")
