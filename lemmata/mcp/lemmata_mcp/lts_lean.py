"""Translate a restricted, proof-free subset of a Left to Solve Lean statement."""

from __future__ import annotations

import re
import unicodedata

NAME = r"[A-Za-z_][A-Za-z_0-9']*(?:\.[A-Za-z_][A-Za-z_0-9']*)*"
DECLARATION = re.compile(rf"(?:theorem|lemma)\s+({NAME})(?=\s|[:({{\[])(.*)", re.S)
DEFINITION = re.compile(rf"(?:noncomputable )?(def|abbrev) +({NAME})(?=\s|[:({{\[])(.*)", re.S)
OPEN = re.compile(rf"open (?:scoped )?{NAME}(?: {NAME})*")
COMMENT_OR_STRING = re.compile(r'/-|-/|--[^\n]*|"(?:[^"\\]|\\.)*"')
COMMENT_DELIMITER = re.compile(r"/-|-/")
BLOCK = re.compile(r"(?m)^(?=[A-Za-z_@#])")
FORBIDDEN = re.compile(
    r"\b(?:by|sorry|admit|native_decide|run_elab|run_tac|include_str|unsafe|axiom|"
    r"elab|macro|syntax|set_option|implemented_by|extern|proof_wanted|decreasing_by|"
    r"termination_by|where|import|attribute|initialize|builtin_initialize)\b"
)
DELIMITERS = dict(zip("([{⟨", ")]}⟩", strict=True))
PUNCTUATION = ".,':@()[]{}⟨⟩|!?+-*/^=<>_~"
MAX_LEAN = 128 * 1024


class TextOnly(Exception):
    """No faithful translation into the supported Lean subset was available."""


def without_comments(source: str) -> str:
    """Blank comments without moving commands to different lines."""
    parts = []
    pos = 0
    while match := COMMENT_OR_STRING.search(source, pos):
        parts.append(source[pos : match.start()])
        pos = match.end()
        if match[0].startswith('"'):
            parts.append(match[0])
        elif match[0] == "/-":
            depth = 1
            while depth:
                inner = COMMENT_DELIMITER.search(source, pos)
                if inner is None:
                    raise TextOnly("unterminated block comment in upstream Lean")
                depth += 1 if inner[0] == "/-" else -1
                pos = inner.end()
            parts.append(re.sub(r"[^\n]", " ", source[match.start() : pos]))
        elif match[0] == "-/":
            raise TextOnly("unexpected block comment terminator in upstream Lean")
    parts.append(source[pos:])
    return "".join(parts)


def top_level(text: str, separator: str = "", *, binders: bool = False) -> tuple[str, str]:
    """Find a declaration separator outside binders, types and other nested terms."""
    closing = []
    for index, char in enumerate(text):
        if not closing:
            if separator and text.startswith(separator, index):
                return text[:index].strip(), text[index + len(separator) :].strip()
            if binders and char not in "([{ \n\t":
                raise TextOnly("expected bracketed Lean binders")
        if char in DELIMITERS:
            closing.append(DELIMITERS[char])
        elif char in DELIMITERS.values() and (not closing or closing.pop() != char):
            raise TextOnly("unbalanced delimiters in upstream Lean")
    if closing:
        raise TextOnly("unbalanced delimiters in upstream Lean")
    return text.strip(), ""


def math_term(term: str, role: str, *, binders: bool = False) -> str:
    # Reconstruct declarations from delimited terms. No upstream commands, tactics, string
    # literals, antiquotations, attributes, imports, proof bodies or parser extensions run.
    if (
        not term
        or len(term) > 8192
        or FORBIDDEN.search(term)
        or any(
            char not in PUNCTUATION
            and char not in " \n\t"
            and unicodedata.category(char)[0] not in "LNM"
            and unicodedata.category(char) != "Sm"
            for char in term
        )
        or any(marker in term for marker in ("/-", "-/", "--"))
    ):
        raise TextOnly(f"{role} is outside the supported proof-free Lean subset")
    try:
        top_level(term, binders=binders)
    except TextOnly as error:
        raise TextOnly(f"{role}: {error}") from error
    return term


def extract_claim(problem_id: str, lean: str, brief: str) -> tuple[str, str]:
    if len(lean) > 8192:
        raise TextOnly("Lean target exceeds the import size limit")
    displayed = DECLARATION.fullmatch(without_comments(lean).strip())
    if not displayed:
        raise TextOnly("the catalog target is not a supported theorem declaration")
    name, target = displayed.groups()
    namespace = problem_id.removesuffix(name).removesuffix(".")
    if problem_id not in (name, f"{namespace}.{name}") or (
        namespace and not re.fullmatch(NAME, namespace)
    ):
        raise TextOnly("the target declaration name and problem ID differ")

    upstream = re.search(
        r"(?ms)^== THE FULL UPSTREAM FILE [^\n]*\n(.*?)(?=^== [A-Z]|\Z)",
        brief.replace("\r\n", "\n"),
    )
    if not upstream or len(upstream[1]) > MAX_LEAN:
        raise TextOnly("the brief has no complete upstream Lean file within the import size limit")
    source = without_comments(upstream[1]).strip()
    entries: list[tuple[str, re.Match[str] | None]] = []
    scopes: list[str] = []
    for block in BLOCK.split(source):
        block = block.strip()
        if not block:
            continue
        decl = DECLARATION.fullmatch(block)
        if decl and decl[1] == name and ".".join(scopes) == namespace:
            signature, _ = top_level(decl[2], ":=")
            if signature.split() != target.split():
                raise TextOnly(f"{name}: catalog target differs from the brief's upstream file")
            break
        if definition := DEFINITION.fullmatch(block):
            entries.append((block, definition))
        elif block.startswith("namespace "):
            scope = block.removeprefix("namespace ")
            qualified = ".".join([*scopes, scope])
            if not re.fullmatch(NAME, scope) or not (namespace + ".").startswith(qualified + "."):
                raise TextOnly(f"unsupported upstream namespace before {name}: {scope[:120]}")
            scopes.append(scope)
            entries.append((block, None))
        elif OPEN.fullmatch(block) and not {"in", "hiding", "renaming"} & set(block.split()):
            entries.append((block, None))
        elif (
            decl
            or block in ("module", "noncomputable section")
            or re.fullmatch(
                r"(?:public )?import [^\n]+|(?:@\[[^\]]*\] )?public section|"
                r"@\[[^\]]*\]|set_option linter\.[\w.]+ (?:true|false)(?: in)?",
                block,
            )
        ):
            continue
        else:
            line = block.splitlines()[0][:120]
            raise TextOnly(f"unsupported upstream context before {name}: {line}")
    else:
        raise TextOnly(f"{problem_id}: declaration absent from the brief's upstream file")

    binders, proposition = top_level(signature, ":")
    if not proposition:
        raise TextOnly(f"{problem_id}: declaration has no explicit proposition")
    proposition = math_term(proposition, f"target {name}")
    if binders:
        parameters = math_term(binders, "target binders", binders=True)
        proposition = f"∀ {parameters},\n    {proposition}"

    if len(entries) > 256:
        raise TextOnly("the upstream declaration context exceeds the import size limit")
    # Only definitions referenced by the target (or another selected definition) are ported.
    references = signature
    preamble = []
    for block, definition in reversed(entries):
        if definition is None:
            preamble.append(block)
            continue
        kind, def_name, rest = definition.groups()
        if not re.search(rf"(?<![\w']){re.escape(def_name)}(?![\w'])", references):
            continue
        binders_and_type, value = top_level(rest, ":=")
        binders, type_ = top_level(binders_and_type, ":")
        if def_name in ("Claim", "claim", "claim_neg", "lemmataLtsTarget"):
            raise TextOnly(f"definition {def_name} needs a manual Lean port")
        parameters = ""
        if binders:
            parameters = " " + math_term(binders, f"binders of {def_name}", binders=True)
        type_annotation = f" : ({math_term(type_, f'type of {def_name}')})" if type_ else ""
        value = math_term(value, f"definition {def_name}")
        preamble.append(
            f"set_option autoImplicit false in\n"
            f"noncomputable {kind} {def_name}{parameters}{type_annotation} := (\n  {value}\n)"
        )
        references += "\n" + rest

    preamble.reverse()
    preamble.append(
        f"set_option autoImplicit false in\n"
        f"noncomputable def lemmataLtsTarget : Prop := (\n  {proposition}\n)"
    )
    preamble.extend(f"end {scope}" for scope in reversed(scopes))
    claim = f"{namespace}.lemmataLtsTarget" if namespace else "lemmataLtsTarget"
    return "\n\n".join(preamble), claim
