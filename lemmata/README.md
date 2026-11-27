# lemmata shell

This fork trims Codex into a math-only research harness. Almost everything is configuration; the
code changes are two small hunks behind one switch, so upstream rebases stay easy. The full
scoping notes are in `docs/lemmata/disable-list.md`.

## Files

| File | Purpose |
|---|---|
| `config/lemmata.config.toml` | Prover/refuter profile. Every non-math feature off, web search off, hard tool allowlist. |
| `config/lemmata-research.config.toml` | Research profile. Same, plus web search, limited by instructions to arXiv, MathOverflow, OEIS and zbMATH. Never issues verdicts. |
| `config/lemmata-math.md` | Base instructions for the prover: math only, refuse off-topic requests, verdicts PROVED / DISPROVED / UNRESOLVED with certificates, only Lean-checked proofs count as PROVED. |
| `config/lemmata-research.md` | Base instructions for the research role. |
| `config/requirements.toml` | Optional managed requirements so later config layers cannot turn the main features back on. |
| `mcp/` | MCP servers (Lean, library, compute, verdict, whiteboard) and the WSL2 setup script. See [Math tools](#math-tools-lemmatamcp). |

Each profile is self-contained, so the two files repeat the shared settings. Keep them in sync.

## The switch: `[lemmata] mode`

```toml
[lemmata]
mode = true          # the single fork switch
research = false     # research role: lets web search install
allowed_tools = ["mcp__lean/check", "web/run", "web_search"]
```

With `mode = true`:

1. **Extension gate** (`codex-rs/app-server/src/extensions.rs`, `thread_extensions()`): app-server skips
   history notes, the agent message board, goals, git attribution, guardian v2/v3 (including the
   shadow install), memories, plugin MCP, image generation and skills. MCP (our math servers), the
   queue and turn admission stay. Web search installs only when `research = true`.
2. **Hard allowlist** (`codex-rs/core/src/session/session.rs`): when the thread starts without an
   explicit `ToolPolicy`, core builds one from `allowed_tools`. Only those tools can be shown to
   the model or dispatched, hosted tools such as `web_search` included. A missing list means
   **no tools** (fail closed).

With `mode = false` (the default) both patches are inert and the fork behaves like upstream.

The allowlist is the real guarantee, not the feature flags. Some current models request code mode
(`exec`/`wait`) and `request_user_input` from the model catalog even with `features.code_mode = false`,
so with `mode = false` those tools still appear. The catalog also selects multi-agent v2 unless
`[agents] enabled = false`, which both profiles set.

Unknown keys in `[lemmata]` are a config error, so a typo cannot silently switch the guards off.

**Choose the profile when the process starts.** app-server builds its extension set once from the
process config, so `mode` and `research` must come from `--profile`, not from a per-thread
`thread/start.config`. A per-thread override only changes the allowlist. Run one app-server per role.

**Run threads in a scratch working directory that is not a repo.** A project `.codex/config.toml`
in the thread's working directory could override `[lemmata]`, and requirements cannot pin it.

### Allowlist entries

Write an entry as `name` for a plain tool (`web_search`, `update_plan`) or `namespace/name` for a
namespaced one. MCP tools use the namespace `mcp__<server>`, so the `check` tool of server `lean` is
`mcp__lean/check`; the standalone web search tool is `web/run`. Keep `allowed_tools` equal to the
union of the servers' `enabled_tools`; both profiles do.

## Running

Use a dedicated `CODEX_HOME` so the lemmata login and settings stay separate from a normal Codex setup:

```sh
export CODEX_HOME=~/.lemmata
mkdir -p "$CODEX_HOME"
cp lemmata/config/*.config.toml lemmata/config/*.md "$CODEX_HOME"/
codex login                                  # ChatGPT login (forced_login_method = "chatgpt")
codex --profile lemmata app-server           # prover / refuter workers
codex --profile lemmata-research app-server  # research workers
```

`--profile <name>` loads `$CODEX_HOME/<name>.config.toml` on top of `config.toml`. Relative paths
such as `model_instructions_file` resolve against that file's directory.

To check what the model will see:

```sh
codex --profile lemmata debug prompt-input "Prove that sqrt(2) is irrational."
```

The `tools` array should contain only allowlisted tools: no shell, `apply_patch`, image or web tools.
`debug prompt-input` builds its own small extension registry instead of the app-server one: it never
installs the standalone web search extension, so `web/run` will not show there even for the research
profile. MCP tools and hosted `web_search` do go through the core allowlist, so they show as they
would in app-server. To check `web/run`, start a thread through app-server.

## Math tools (`lemmata/mcp`)

Five Python MCP servers plus upstream [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp),
all running inside WSL2. Codex on Windows starts each one with
`wsl.exe -d Ubuntu --cd ~ --exec .lemmata/bin/lemmata-mcp <server>` (already in both profiles).

### Setup (WSL2, no sudo)

```sh
# inside WSL (Ubuntu 24.04): needs git, curl, gcc, make, python3 >= 3.12, uv
bash /mnt/c/<path to repo>/lemmata/mcp/setup-wsl.sh
```

Everything goes under `~/.lemmata` (`LEMMATA_HOME`): elan + the pinned Lean, a work Lake project
(Mathlib + REPL), a second read-only Mathlib for verification, comparator + lean4export, landrun
(built with a user-local Go), CaDiCaL, drat-trim, nauty `geng`, Miniforge + Sage, the
formal-conjectures checkout, OEIS `stripped.gz`/`names.gz`, the venv and the declaration index.
Steps are idempotent; rerun one with `bash setup-wsl.sh <step>` (`elan lean verify go landrun
comparator sat nauty venv sage formal_conjectures oeis index`). About 25 GB of disk and 20-30
minutes on a fast connection. `lemmata-mcp smoke` starts every server over stdio and lists its tools.

Operators freeze challenges with `lemmata-mcp freeze spec.json`, where the spec holds
`freeze_statement`'s arguments (`title`, `informal`, `claim`, optional `preamble`, `imports`,
`predicate`, `variables`, `require_equivalence`, `second_of`). Use the returned id as the thread's
problem id. `freeze_statement` is also an MCP tool, kept out of every worker allowlist.

### Servers

| Server | Tools | What it does |
|---|---|---|
| `verdict` | `get_challenge`, `replay_witness`, `check_equivalence`, `submit_verdict` | Frozen challenges (SQLite triggers refuse UPDATE/DELETE). PROVED = `theorem claim : Claim` judged by [comparator](https://github.com/leanprover/comparator) against the frozen `Challenge.lean`, axioms limited to propext/Classical.choice/Quot.sound. DISPROVED = comparator-checked `claim_neg : ¬ Claim`, or a witness replayed against the frozen Python predicate. UNRESOLVED needs a summary. Anything else is rejected. |
| `lean` | `check`, `portfolio`, `repair`, `register_lemma` | One warm leanprover-community/repl with `import Mathlib` loaded once. Goals at each `sorry` and before each tactic, axioms per theorem, bounded tactic portfolio, multi-attempt repair, sharing proved lemmas. |
| `lean_lsp` | `lean_goal`, `lean_multi_attempt`, `lean_loogle`, ... (15 allowlisted) | lean-lsp-mcp on the work project, for files written by `check(save_as=...)`. Local Loogle only; network tools and `lean_build` are disabled; its REPL mode is off (the one warm REPL is `lean`'s). |
| `library` | `search_decls`, `decl_info`, `formal_conjectures`, `oeis` | FTS index over every Mathlib/Batteries/core declaration (full type, `import`, package revision) plus project-proved lemmas; Formal Conjectures at a pinned commit; OEIS from local data (HTTP fallback). |
| `compute` | `enumerate`, `graph_search`, `sat_solve`, `sage`, `ideal_cofactors`, `interval_sign` | Size-ordered symmetry-aware integer search with Plausible-style shrinking; geng graph search; CaDiCaL + DRAT-trim; warm Sage worker; Groebner cofactors for `linear_combination`; python-flint Arb signs. |
| `whiteboard` | `read`, `lemma_add`, `lemma_update`, `dead_end_add`, `dead_end_find`, `coverage_add` | Shared SQLite (WAL): lemma DAG, dead-end memory, refutation coverage. |

Details that matter for trust:

- **Frozen statement.** A challenge is Lean imports + preamble + `def Claim : Prop := ...`, with
  `theorem claim : Claim` and `theorem claim_neg : ¬ Claim` as the two targets. Workers submit
  only the text after that prefix; the server prepends the frozen prefix, so definitions cannot drift.
  Comparator still checks every declaration the statement uses.
- **Comparator** runs as its README requires: `systemd-run --user --property=RestrictAddressFamilies=~AF_UNIX`
  around `lake env comparator`, with landrun sandboxing each build. Every submission gets a fresh
  Lake project that symlinks the read-only verification Mathlib (`~/.lemmata/verify`), so no
  solution is ever compiled where an earlier one was. A small proof checks in about 20 s.
- **Sandbox.** Everything that runs worker-written code (the REPL, lean-lsp-mcp, Python predicates,
  Sage, the witness replay) runs under landrun with write access only to `~/.lemmata/scratch`
  (lean-lsp-mcp: also the work project and its caches). It cannot touch the verification
  Mathlib, `~/.elan` or the state databases. The WSL2 6.6 kernel has Landlock ABI 3, which cannot
  restrict network access, so sandboxed code can still reach the network.
- **Witness replay** is independent of the search: a fresh `python -I` interpreter, the frozen
  predicate (not the worker's), exact types (`int`, `nat`, `pos`, `rat`, `bool`, `list[int]`,
  `graph6`), and strict booleans (every hypothesis must be `True`, the conclusion `False`). The
  Python predicate is part of the frozen statement: review it as carefully as the Lean claim.
- **Second formalization.** Freeze another formalization with `second_of=<id>`; `check_equivalence`
  comparator-checks `theorem claim_equiv : F1.Claim ↔ F2.Claim`. A challenge frozen with
  `require_equivalence=true` refuses PROVED until such an equivalence is recorded.
- **native_decide** is exploration only: `portfolio(explore=true)` may report it, but it is never
  spliced into verified code, `register_lemma` refuses it, and certificates containing it are
  rejected (comparator would also reject the `Lean.ofReduceBool` axiom). polyrith is not offered.
- **UNSAT is never a verdict.** `sat_solve` keeps `formula.cnf`, `proof.drat` and `result.json`
  under `~/.lemmata/state/artifacts/sat/<hash>/` and reports whether drat-trim verified the proof.
- **Symmetry reductions** (`permute`, `negate`) are used only after the whole counterexample
  predicate is checked invariant under each generator on the first 5000 points; otherwise the
  search refuses. This is a spot check, not a proof, and coverage records the assumption.
  `graph_search` checks isomorphism invariance on the first 200 graphs.
- **Coverage outcomes** keep `timed_out` (with how far it got), `no_witness_in_domain` (the whole
  bounded domain was searched) and `lemma_refuted` (a witness was found) apart; `error` means some
  points raised and the domain is not fully covered. `lemma_refuted` marks the lemma refuted.
- Only `register_lemma` (after a full check) can mark a whiteboard lemma `proved`.

### Pins and known incompatibilities

All pins live in `lemmata/mcp/lemmata_mcp/pins.env`, read by both the setup script and the servers.

| Component | Pin |
|---|---|
| Lean | `leanprover/lean4:v4.35.0` |
| Mathlib, REPL, comparator | `v4.35.0` (lean4export at the commit comparator's manifest pins) |
| lean-lsp-mcp | `0.33.0`; it builds local Loogle for our toolchain (Loogle commit chosen by lean-lsp-mcp) |
| landrun | `main@573aa72`, built with `go1.26.11` |
| CaDiCaL / drat-trim / nauty | `rel-3.1.0` / `2e3b2dc` / `2.9.3` |
| Sage | `10.10` (conda-forge) |
| Formal Conjectures | `ea8e3e1` |

- v4.35.0 is the newest release that Mathlib, REPL, comparator and lean4export all tag. Mathlib
  is at v4.36.0, but comparator's newest tag is v4.35.0.
- Formal Conjectures pins Lean v4.33.1, so it is searched as text and never imported; statements
  must be adapted to our Mathlib.
- Loogle's own repository targets an older Lean; lean-lsp-mcp rebuilds it for v4.35.0. The first
  index build peaks around 13 GB RAM (about 7 GB warm), close to the 15 GB WSL default.
- nanoda (comparator's optional second kernel) is not installed; only Lean's kernel replays.

### Known limits (v1)

- One REPL per `lean` server process, so one per thread. Its memory grows with every environment,
  so it restarts after 400 commands (about 15 s to reload Mathlib).
- `enumerate` covers integer domains only; rationals and structured objects need their own search.
- Witness replay is Python against the frozen predicate. A Lean replay of a witness would mean
  proving `claim_neg` from it, which a worker can submit directly.
