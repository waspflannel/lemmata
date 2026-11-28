"""`lemmata-mcp goals ...`: goal-tree state for a round runner (operator side, read-mostly).

goals show <problem>                                    goal_tree JSON
goals snapshot <problem> --round N --phase begin|end [--thread ID]
                                                        one JSON line (stats, progress, verdict)
goals prompt <problem> --round N [--template FILE]      the next round's prompt (UTF-8)
goals prompt <problem> --learn                          the Learn-page explanation prompt
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from . import board, verdict_server

DEFAULT_TEMPLATE = """\
Round {round} on challenge {problem}. Frozen statement unchanged; same certificate rules.

Open goals (`whiteboard.goal_tree` has the full tree):
{open_leaves}

Flagged in the previous round:
{flagged}

Stats: {stats}

This round must close or refute an open goal, or split one into ≥2 cases each with a weaken \
proof (`goal_split` with `weaken_proofs`). A single-child equivalent reduction counts as no \
progress, and the run stops after repeated no-progress rounds.

End with `submit_verdict` (UNRESOLVED citing goal ids, or PROVED/DISPROVED from \
`goal_certificate`).
"""

LEARN_PROMPT = """\
The run on challenge {problem} has stopped. Write the explanation for its Learn page.

No tool calls except `whiteboard.goal_tree` (problem {problem}). Do not call `submit_verdict`.

Write for a strong undergraduate, in Markdown with LaTeX math between `$...$` (inline) and \
`$$...$$` (display); use `lean` code blocks for Lean. Sections, in order:
1. Background and definitions the statement needs.
2. Why the problem is hard (or, if it fell quickly, what made it tractable).
3. A top-down walk through the goal tree. For each goal, give the idea of the Lean-checked step \
that closed, refuted or split it, and cite its goal id and lemma ids. Say which splits were \
flagged as restatements or strengthenings and why they did not count as progress.
4. What remains open, stated plainly. If the verdict is PROVED or DISPROVED, say what the \
certificate consists of.

Your final message is the page text itself: the explanation only, no preface.
"""

FLAG_ADVICE = {
    "restatement": "this was a restatement: do not reduce this goal to an equivalent single "
    "leaf again",
    "strengthening": "this single child may be harder than its parent: prefer a case split "
    "with weaken proofs",
}


def latest_verdict(problem: str) -> dict[str, Any] | None:
    """The latest accepted PROVED/DISPROVED submission for the challenge, if any."""
    with closing(verdict_server.connect()) as db:
        row = db.execute(
            "SELECT verdict, accepted, reason FROM submissions WHERE challenge_id = ?"
            " AND accepted = 1 AND verdict IN ('PROVED', 'DISPROVED') ORDER BY id DESC LIMIT 1",
            (problem,),
        ).fetchone()
    return {**dict(row), "accepted": bool(row["accepted"])} if row else None


def snapshot(problem: str, round_: int, phase: str, thread_id: str | None) -> dict[str, Any]:
    verdict_server._challenge(problem)  # a frozen challenge, or ValueError
    return {**board.snapshot(problem, round_, phase, thread_id), "verdict": latest_verdict(problem)}


def prompt(problem: str, round_: int, template: str) -> str:
    verdict_server._challenge(problem)
    tree = board.goal_tree(problem)
    nodes: dict[int, dict[str, Any]] = {}

    def index(node: dict[str, Any]) -> None:
        nodes[node["id"]] = node
        for child in node.get("split", {}).get("children", []):
            index(child)

    if tree["root"]:
        index(tree["root"])
    leaves = [
        f"- goal {g['id']}: {g['informal'] or '(no informal text)'} :: `{g['statement']}`"
        for g in (nodes[i] for i in tree["open_leaf_ids"])
    ]
    flagged = [
        f"- goal {f['goal_id']} ({f['flag']}): {f['informal'] or ''} :: "
        f"{FLAG_ADVICE.get(f['flag'], f['flag'])}"
        for f in board.flagged_goals(problem, round_ - 1)
    ]
    if tree["root"] is None:
        leaves = ["- the root `Claim` (created by the first goal_tree or goal_* call)"]
    stats = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in tree["stats"].items())
    return template.format(
        problem=problem,
        round=round_,
        tree=json.dumps(tree, ensure_ascii=False),
        open_leaves="\n".join(leaves) or "(none)",
        flagged="\n".join(flagged) or "(none)",
        stats=stats,
    )


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="lemmata-mcp goals")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("problem")
    snap = commands.add_parser("snapshot")
    snap.add_argument("problem")
    snap.add_argument("--round", type=int, required=True)
    snap.add_argument("--phase", choices=board.ROUND_PHASES, required=True)
    snap.add_argument("--thread")
    text = commands.add_parser("prompt")
    text.add_argument("problem")
    text.add_argument("--round", type=int)
    text.add_argument("--template", type=Path)
    text.add_argument("--learn", action="store_true", help="the Learn-page explanation prompt")
    args = parser.parse_args(argv)
    if args.command == "prompt" and not args.learn and args.round is None:
        parser.error("prompt needs --round N (or --learn)")
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        if args.command == "show":
            verdict_server._challenge(args.problem)
            print(json.dumps(board.goal_tree(args.problem), ensure_ascii=False, indent=1))
        elif args.command == "snapshot":
            result = snapshot(args.problem, args.round, args.phase, args.thread)
            print(json.dumps(result, ensure_ascii=False))
        elif args.learn:
            verdict_server._challenge(args.problem)
            sys.stdout.write(LEARN_PROMPT.format(problem=args.problem))
        else:
            template = (
                args.template.read_text(encoding="utf-8") if args.template else DEFAULT_TEMPLATE
            )
            sys.stdout.write(prompt(args.problem, args.round, template))
    except ValueError as e:
        sys.exit(f"lemmata-mcp goals: {e}")
