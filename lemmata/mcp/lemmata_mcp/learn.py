"""`lemmata-mcp learn <challenge> [--explanation FILE] --out FILE`: one self-contained HTML
Learn page (statement, verdict, explanation, goal tree with Lean sources). Operator side.

KaTeX (fonts included) and marked are fetched once from cdnjs at pinned versions, checked
against pinned sha256 digests, cached under ~/.lemmata/state/learn-vendor and inlined, so the
page opens offline. If the fetch fails the page links the CDN instead; without network the
explanation then stays readable as raw Markdown.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import os
import re
import sys
import time
from contextlib import closing
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import board, verdict_server
from .common import PACKAGE_DIR, STATE

CDN = "https://cdnjs.cloudflare.com/ajax/libs/"
KATEX = "KaTeX/0.16.9/"
MARKED = "marked/4.3.0/"
# Script and stylesheet digests are pinned; fonts are woff2 data and only reach @font-face.
SCRIPTS = {
    KATEX + "katex.min.js": "dc84b296ec3e884de093158f760fd9d45b6c7abe58b5381557f4e138f46a58ae",
    MARKED + "marked.min.js": "c68075672d976e4777390560baa112194855bd4404b13647da4855aae1f9360c",
}
KATEX_CSS = (
    KATEX + "katex.min.css",
    "505d5f829022bb7b4f24dfee0aa1141cd7bba67afe411d1240335f820960b5c3",
)
VENDOR = STATE / "learn-vendor"
VERDICT_TEXT = {
    "PROVED": ("v-proved", "Proof.", "Proved"),
    "DISPROVED": ("v-disproved", "Disproof.", "Disproved"),
}

STATUS_MARK = {"closed": "∎", "refuted": "⊥", "open": "○", "split": "∧"}
RELATION_TEXT = {
    "case": "case split",
    "reduction": "reduction",
    "restatement": "restatement",
    "strengthening": "strengthening",
}
FLAG_TEXT = {
    "restatement": "restatement: equivalent to its parent, no progress",
    "strengthening": "strengthening: may be harder than its parent",
}


# --- vendored assets ----------------------------------------------------------------------


def _fetch(path: str, digest: str | None = None) -> bytes:
    cached = VENDOR / path
    if cached.exists():
        data = cached.read_bytes()
    else:
        with urlopen(Request(CDN + path, headers={"User-Agent": "lemmata-mcp"}), timeout=30) as r:
            data = r.read()
    if digest and hashlib.sha256(data).hexdigest() != digest:
        cached.unlink(missing_ok=True)
        raise ValueError(f"{path}: sha256 mismatch")
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_name(f"{cached.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, cached)
    return data


def _katex_css() -> str:
    """KaTeX's stylesheet with every font as an inline woff2 (woff/ttf fallbacks dropped)."""

    def font(m: re.Match[str]) -> str:
        data = base64.b64encode(_fetch(f"{KATEX}fonts/{m[1]}.woff2")).decode()
        return f'src:url(data:font/woff2;base64,{data}) format("woff2")'

    css = _fetch(*KATEX_CSS).decode()
    return re.sub(r"src:url\(fonts/([\w-]+)\.woff2\)[^;}]*", font, css)


def _assets() -> str:
    try:
        css = _katex_css()
        scripts = [_fetch(path, digest).decode() for path, digest in SCRIPTS.items()]
    except (URLError, OSError, ValueError, HTTPException) as e:
        print(f"lemmata-mcp learn: vendoring failed ({e}); linking the CDN", file=sys.stderr)

        def sri(digest: str) -> str:
            b64 = base64.b64encode(bytes.fromhex(digest)).decode()
            return f'integrity="sha256-{b64}" crossorigin="anonymous"'

        tags = [f'<link rel="stylesheet" href="{CDN}{KATEX_CSS[0]}" {sri(KATEX_CSS[1])}>']
        tags += [f'<script src="{CDN}{p}" {sri(d)}></script>' for p, d in SCRIPTS.items()]
        return "\n".join(tags)
    inline = [f"<style>{css}</style>"]
    inline += [f"<script>{s.replace('</script', '<\\/script')}</script>" for s in scripts]
    return "\n".join(inline)


# --- Lean and tree rendering --------------------------------------------------------------

LEAN_TOKEN = re.compile(
    r"(?P<comment>--[^\n]*|/-.*?-/)"
    r'|(?P<string>"(?:[^"\\\n]|\\.)*")'
    r"|\b(?P<keyword>theorem|lemma|def|abbrev|example|instance|structure|inductive|namespace"
    r"|section|end|open|variable|by|fun|have|show|from|let|in|if|then|else|match|with|at"
    r"|calc|do|where|using|Type|Prop|Sort)\b"
    r"|\b(?P<tactic>intro|intros|exact|refine|apply|simp|simpa|simp_all|omega|decide|rfl"
    r"|rcases|obtain|cases|induction|constructor|use|rw|norm_num|linarith|nlinarith|positivity"
    r"|aesop|tauto|unfold|subst|contradiction|exfalso|trivial|first|done|ring|gcongr|ext)\b"
    r"|(?P<symbol>[∀∃→↔¬∧∨λ⟨⟩←↦≤≥≠∣])",
    re.S,
)


def lean_html(source: str) -> str:
    """Escaped Lean source with a light token highlight (no JS needed)."""
    out, last = [], 0
    for m in LEAN_TOKEN.finditer(source):
        out.append(html.escape(source[last : m.start()]))
        out.append(f'<span class="lk-{m.lastgroup}">{html.escape(m[0])}</span>')
        last = m.end()
    out.append(html.escape(source[last:]))
    return "".join(out)


def _lemma(lemma_id: int | None, label: str) -> str:
    if lemma_id is None:
        return ""
    try:
        row = board.lemma_row(lemma_id)
    except ValueError:
        return f'<p class="cite">{label} lemma {lemma_id} (missing from the whiteboard)</p>'
    head = f"{label} lemma {lemma_id}, <code>{html.escape(row['name'])}</code>"
    source = row["lean_source"] or row["lean_statement"] or row["statement"]
    return (
        f'<details class="source"><summary>{head}</summary>'
        f'<pre class="lean">{lean_html(source.strip())}</pre></details>'
    )


def _goal(node: dict[str, Any], full: dict[int, str], depth: int = 0) -> str:
    status = node["status"]
    tags = []
    split = node.get("split")
    if split:
        tags.append(
            f'<span class="tag rel-{split["relation"]}">'
            f"{RELATION_TEXT.get(split['relation'], split['relation'])}"
            f" into {len(split['children'])}</span>"
        )
    if node.get("flag"):
        tags.append(f'<span class="tag flag">{html.escape(node["flag"])}</span>')
    informal = html.escape(node["informal"] or "(no informal text)")
    parts = [
        f'<details class="goal st-{status}"{" open" if depth < 3 else ""}>',
        f'<summary><span class="mark" title="{status}">{STATUS_MARK.get(status, "?")}</span>'
        f'<span class="gid">Goal {node["id"]}</span>'
        f'<span class="line"><span class="informal tex">{informal}</span>{"".join(tags)}</span>'
        "</summary>",
        '<div class="goal-body">',
        f'<pre class="lean statement">{lean_html(full.get(node["id"], node["statement"]))}</pre>',
    ]
    if node.get("flag"):
        parts.append(f'<p class="flag-note">{FLAG_TEXT.get(node["flag"], node["flag"])}</p>')
    if status in ("closed", "refuted") and node.get("lemma_id"):
        parts.append(_lemma(node["lemma_id"], "Closed by" if status == "closed" else "Refuted by"))
    if split:
        parts.append(_lemma(split["lemma_id"], "Split justified by"))
        weak = [i for i in split["weaken_lemma_ids"] if i is not None]
        if weak:
            ids = ", ".join(str(i) for i in weak)
            parts.append(f'<p class="cite">Each case implied by the goal: lemmas {ids}</p>')
    if node.get("dead_splits"):
        ids = ", ".join(str(i) for i in node["dead_splits"])
        parts.append(f'<p class="cite">Abandoned splits (a case was refuted): {ids}</p>')
    if split and split["children"]:
        parts.append('<div class="children">')
        parts += [_goal(child, full, depth + 1) for child in split["children"]]
        parts.append("</div>")
    parts.append("</div></details>")
    return "\n".join(parts)


# --- page ---------------------------------------------------------------------------------


def _verdict(problem: str) -> str:
    with closing(verdict_server.connect()) as db:
        row = db.execute(
            "SELECT verdict, reason FROM submissions WHERE challenge_id = ? AND accepted = 1"
            " ORDER BY verdict IN ('PROVED', 'DISPROVED') DESC, id DESC LIMIT 1",
            (problem,),
        ).fetchone()
    if row is None:
        return '<p class="verdict v-none"><b>Status.</b> No verdict submitted yet.</p>'
    if row["verdict"] not in VERDICT_TEXT:
        return '<p class="verdict v-open"><b>Status.</b> Unresolved: no accepted verdict yet.</p>'
    kind, label, word = VERDICT_TEXT[row["verdict"]]
    reason = re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(row["reason"]))
    return f'<p class="verdict {kind}"><b>{label}</b> {word}; {reason}. <span>∎</span></p>'


def render(problem: str, explanation: str | None) -> str:
    c = verdict_server._challenge(problem)
    tree = board.goal_tree(problem)
    with closing(board.connect()) as db:
        full = {
            r["id"]: r["lean_statement"]
            for r in db.execute(
                "SELECT id, lean_statement FROM goals WHERE problem = ?", (problem,)
            )
        }
    stats = tree["stats"]
    stat_items = [
        ("closed", "closed"),
        ("refuted", "refuted"),
        ("open_leaves", "open"),
        ("case_splits", "case splits"),
        ("flagged", "flagged"),
    ]
    stats_html = "".join(
        f'<li class="stat-{key}"><b>{stats[key]}</b> {label}</li>' for key, label in stat_items
    )
    if tree["root"]:
        tree_html = _goal(tree["root"], full)
    else:
        tree_html = '<p class="empty">No goal tree yet. Workers create it with the goal tools.</p>'
    if explanation:
        explain_html = f'<pre class="md md-raw">{html.escape(explanation)}</pre>'
    else:
        explain_html = (
            '<p class="empty">No explanation was written for this run. Generate one with '
            "<code>rounds.cmd -Learn</code> or pass <code>--explanation</code>.</p>"
        )
    template = (PACKAGE_DIR / "learn.html").read_text(encoding="utf-8")
    fields = {
        "title": html.escape(c["title"]),
        "informal": html.escape(c["informal"]),
        "claim": lean_html(c["claim"].strip()),
        "preamble": (
            '<details class="source preamble"><summary>Frozen preamble</summary>'
            f'<pre class="lean">{lean_html(c["preamble"].strip())}</pre></details>'
            if c["preamble"].strip()
            else ""
        ),
        "verdict": _verdict(problem),
        "stats": stats_html,
        "explanation": explain_html,
        "tree": tree_html,
        "problem": html.escape(problem),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "assets": _assets(),
    }
    # One pass, so substituted text containing `{{...}}` is never expanded again.
    return re.sub(r"\{\{(\w+)\}\}", lambda m: fields[m[1]], template)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="lemmata-mcp learn", description=__doc__)
    parser.add_argument("problem", help="challenge id")
    parser.add_argument("--explanation", type=Path, help="Markdown explanation (UTF-8)")
    parser.add_argument("--out", type=Path, required=True, help="HTML file to write")
    args = parser.parse_args(argv)
    try:
        text = args.explanation.read_text(encoding="utf-8") if args.explanation else None
        page = render(args.problem, text and text.strip())
    except (ValueError, OSError) as e:
        sys.exit(f"lemmata-mcp learn: {e}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(page.encode("utf-8"))
    print(f"Learn page: {args.out}")
