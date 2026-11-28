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
| `start.ps1`, `start.cmd` | One-command start: checks, health check, then the TUI. See [How to test](#how-to-test). |
| `rounds.ps1`, `rounds.cmd` | Headless round runner: chains `codex exec` rounds on one challenge until a stop rule fires. See [Round runner](#round-runner). |
| `examples/` | Sample challenge specs for `lemmata-mcp freeze`. |
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

## How to test

Once `setup-wsl.sh` has finished and `codex-rs` is release-built
(`cargo build --release -j 4 -p codex-cli --bin codex`), one command starts everything:

```bat
C:\lemmata\lemmata\start.cmd            &rem prover/refuter shell
C:\lemmata\lemmata\start.cmd -Research  &rem research profile
C:\lemmata\lemmata\start.cmd -Check     &rem health check only
```

`start.ps1` checks WSL, the `~/.lemmata` install and the release binary; points the WSL venv
back at the main checkout if it still runs another worktree (and reports Lean projects that do
not match `pins.env`); copies the profiles into `%USERPROFILE%\.lemmata` (the lemmata
`CODEX_HOME`); starts every MCP server once and lists its tools; then opens the Codex TUI under
the chosen profile in `%USERPROFILE%\.lemmata\work`. On the first run it opens the ChatGPT
browser login. It always uses the main checkout, even when started from a worktree.

Verdicts are submitted against a frozen challenge, so freeze a claim first and give the shell
its id. Two sample claims are in `lemmata/examples`:

```bat
wsl -d Ubuntu --cd ~ --exec .lemmata/bin/lemmata-mcp freeze /mnt/c/lemmata/lemmata/examples/prove-even.json
```

| Claim | Spec | Type in the shell | Expected |
|---|---|---|---|
| n(n+1) is even for every natural n | `prove-even.json` | `Work on challenge <id>.` | PROVED (e.g. `Nat.even_mul_succ_self`), comparator-checked |
| 2^n + 1 is prime for every n > 0 | `disprove-prime.json` | `Work on challenge <id>.` | DISPROVED by the witness n = 3 (9 = 3 * 3), replayed |
| (off-topic) | none | `Write a Python script that renames the files in my Downloads folder.` | Refused in one sentence, no tool calls |

## Round runner

`rounds.cmd` runs headless rounds on one frozen challenge. Each round is one
`codex exec --profile lemmata` that forks the previous round's thread (`-Fresh`: a new session
per round). Run `start.cmd -Check` once first; the runner needs the ChatGPT login and never uses
an API key.

```bat
C:\lemmata\lemmata\rounds.cmd -Challenge 5f8c55c0733a5f9e -Name giuga -Fork <thread id> -MaxRounds 5
C:\lemmata\lemmata\rounds.cmd -Challenge <id> -Name even -DryRun   &rem write round 1 files, launch nothing
C:\lemmata\lemmata\rounds.cmd -Challenge <id> -Name giuga -Resume  &rem continue from giuga-run.jsonl
```

Per round N it writes `<Name>-r<N>-prompt.txt`, `-launch.cmd` (rerunnable by hand), `.log` and
`-last-message.txt` to `-OutDir` (default `%USERPROFILE%\lemmata-run`), and appends a record to
`<Name>-run.jsonl`. It stops on the first of:

| Status | Rule |
|---|---|
| `verdict` | an accepted PROVED or DISPROVED |
| `no-progress` | `-NoProgressRounds` (2) consecutive rounds without goal-tree progress |
| `max-rounds` | round `-MaxRounds` (10) finished |
| `time-cap` | `-TimeCapHours` (12) used up; one round is killed after `-RoundCapHours` (4) |
| `failed` | the same round exits non-zero without `turn.completed` twice |

Progress, the verdict and the round prompts come from `lemmata-mcp goals snapshot` and
`goals prompt` (the goal tree). Where that CLI is missing, progress is unknown (it never counts
toward `no-progress`), the verdict is read from the round log's accepted `submit_verdict`
result, and the prompt is a built-in one (or `-PromptTemplate` with `{problem}` and `{round}`).
After every round, and at the stop, it posts `{threadId: <Name>-r<N>, status}` to the webhook in
`-WebhookEnv`; the key is never logged.

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
comparator sat nauty venv sage formal_conjectures oeis index ladr csdp soplex macaulay2 snappy
pi_base kimina options`). About 30 GB of disk and 30-40 minutes on a fast connection.
`lemmata-mcp smoke` starts every server over stdio and lists its tools.

Upgrading a v1 install: `bash setup-wsl.sh lean verify venv ladr csdp soplex macaulay2 snappy
pi_base kimina options` (explicit steps always rerun). `lean` and `verify` rewrite both
lakefiles with LeanHammer and LeanCert ahead of Mathlib; Mathlib's cache stays valid because
the shared dependencies (batteries, aesop, Qq) resolve to the same revisions.

Runtime switches live in `~/.lemmata/options.env` (written once by the `options` step from
`lemmata_mcp/options.env`, never overwritten): `KIMINA_REPLS` (default 0) and `LEANSEARCH`
(default 0). Restart the MCP servers after editing it.

Operators freeze challenges with `lemmata-mcp freeze spec.json`, where the spec holds
`freeze_statement`'s arguments (`title`, `informal`, `claim`, optional `preamble`, `imports`,
`predicate`, `variables`, `require_equivalence`, `second_of`). Use the returned id as the thread's
problem id. `freeze_statement` is also an MCP tool, kept out of every worker allowlist.

### Left to Solve

Run these commands in WSL with `~/.lemmata/bin` on your PATH:

```sh
lemmata-mcp lts search --sort easiest --kind warmup --limit 10
lemmata-mcp lts show Erdos1074.erdos_1074.variants.EHSNumbers_init
lemmata-mcp lts brief Erdos1074.erdos_1074.variants.EHSNumbers_init
lemmata-mcp lts import Erdos1074.erdos_1074.variants.EHSNumbers_init
```

Search also accepts `--q`, `--source`, `--field`, and `--offset`. The CLI reads public
catalog endpoints, spaces requests at least four seconds apart, and caches responses for
five minutes in `~/.lemmata/state/left-to-solve.db`.

Import extracts the target and supported definitions from the pinned Formal Conjectures
snapshot and checks them with the verification project's Lean and Mathlib. If it freezes
successfully, paste the printed `Work on challenge <id>.` into lemmata. The existing
`get_challenge` tool returns the cached brief under `left_to_solve` as reference data.
Problems without a supported Lean translation are recorded in the local catalog cache as
text-only, with the blocker printed by the CLI.

### Servers

| Server | Tools | What it does |
|---|---|---|
| `verdict` | `get_challenge`, `replay_witness`, `check_equivalence`, `submit_verdict` | Frozen challenges (SQLite triggers refuse UPDATE/DELETE). PROVED = `theorem claim : Claim` judged by [comparator](https://github.com/leanprover/comparator) against the frozen `Challenge.lean`, axioms limited to propext/Classical.choice/Quot.sound. DISPROVED = comparator-checked `claim_neg : ¬ Claim`, or a witness replayed against the frozen Python predicate. UNRESOLVED needs a summary. Anything else is rejected. |
| `lean` | `check`, `portfolio`, `repair`, `register_lemma`, `step`, `batch_check` | One warm leanprover-community/repl with `import Mathlib` (+ `Hammer`, `LeanCert.Tactic`) loaded once. Goals at each `sorry` and before each tactic, axioms per theorem, proof-state branching (`step`), bounded tactic portfolio (Duper, hammer, LeanCert kernel mode included), multi-attempt repair, batch checks (opt-in Kimina pool), sharing proved lemmas. |
| `lean_lsp` | `lean_goal`, `lean_multi_attempt`, `lean_loogle`, ... (15 allowlisted) | lean-lsp-mcp on the work project, for files written by `check(save_as=...)`. Local Loogle only; network tools and `lean_build` are disabled; its REPL mode is off (the one warm REPL is `lean`'s). |
| `library` | `search_decls`, `decl_info`, `formal_conjectures`, `oeis`, `pi_base`, `lmfdb` | FTS index over every Mathlib/Batteries/core declaration (full type, `import`, package revision) plus project-proved lemmas; Formal Conjectures at a pinned commit; OEIS from local data (HTTP fallback); pi-Base at a pinned commit with forward-chaining deduction; LMFDB read-only API (research profile only). |
| `compute` | `enumerate`, `graph_search`, `local_search`, `sat_solve`, `sage`, `gap`, `pari`, `macaulay2`, `snappy`, `mace4`, `ideal_cofactors`, `interval_sign`, `exact_lp`, `sos_certificate`, `pslq`, `conjecture_mine`, `evolve` | Size-ordered symmetry-aware integer search with Plausible-style shrinking; geng graph search (or a saved graph6 list); simulated annealing; CaDiCaL + DRAT-trim; warm Sage worker (also SnapPy); GAP + SmallGrp, PARI/GP, Macaulay2 and Mace4 CLIs; Groebner cofactors for `linear_combination`; python-flint Arb signs; SoPlex exact LP; CSDP sum-of-squares certificates; mpmath PSLQ; TxGraffiti-style conjecture mining; OpenEvolve-style program evolution. |
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
  `list[list[int]]`, `graph6`), and strict booleans (every hypothesis must be `True`, the conclusion `False`). The
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
| LeanHammer / LeanCert | `v4.35.0` / `v4.35.0` (Duper, lean-auto `v4.35.0`, lean-smt `6b26285`, cvc5 1.3.2, Zipperposition 2.1 via LeanHammer's manifest) |
| Mace4 (LADR) / CSDP / SoPlex | `laitep/ladr@8317cb2` / `releases/6.2.0` / `8.1.0` (conda-forge) |
| Macaulay2 / SnapPy | `1.22+ds-6build2` (Ubuntu 24.04 .debs, unpacked) / `3.3.2` (pip, in the Sage env) |
| GAP / PARI/GP | as shipped in the Sage env (GAP with SmallGrp, PARI 2.17) |
| pi-Base data / Kimina Lean Server | `c50a33c` / `fb2393d` |
| mpmath / PyYAML | `1.4.1` / `6.0.3` |

- v4.35.0 is the newest release that Mathlib, REPL, comparator and lean4export all tag. Mathlib
  is at v4.36.0, but comparator's newest tag is v4.35.0.
- Formal Conjectures pins Lean v4.33.1, so it is searched as text and never imported; statements
  must be adapted to our Mathlib.
- Loogle's own repository targets an older Lean; lean-lsp-mcp rebuilds it for v4.35.0. The first
  index build peaks around 13 GB RAM (about 7 GB warm), close to the 15 GB WSL default.
- nanoda (comparator's optional second kernel) is not installed; only Lean's kernel replays.

### v2 toolkit: what counts and what does not

The verdict rule is unchanged: PROVED only through a comparator-checked Lean proof, DISPROVED
only through a comparator-checked `claim_neg` or the independent witness replay. Every v2 tool
produces candidates or evidence that must go through one of those two doors.

- **Hammers.** The REPL header and every solution import `Hammer` (LeanHammer, which brings
  Duper, lean-auto with Zipperposition, lean-smt) and `LeanCert.Tactic`; comparator builds
  solutions against the same packages in the read-only verification project. `duper` and the
  tactics hammer suggests (`grind only [...]`, `apply ... ; simp_all`, `aesop ...`, `duper
  [...]`) are ordinary kernel-checked proofs. `hammer`, `premises`, library suggestions and
  `Hammer.*` names are rejected in certificates and by `register_lemma` (they would run provers
  and premise selection inside comparator's build); `portfolio` splices hammer's suggestion,
  never `hammer`. The text scan (`common.scan_certificate`) reads the source twice, comments
  stripped and strings blanked, so neither view can hide a token.
- **Premise selection is local.** LeanHammer's default selector sends goals to
  leanpremise.net. The header sets the Sine Qua Non selector instead, and `lean` refuses code
  that changes the selector.
- **No lean-smt in the REPL.** The REPL binary cannot load lean-cvc5's native library, so a
  cvc5 call aborts the process. The header sets `hammer.disableSmtDefault true` and `lean`
  refuses `smt`. Comparator's Lake build does load native libraries.
- **LeanCert** has three trust modes: `kernel` (`decide +kernel`, kernel only), `native`
  (default, `native_decide`) and `auto` (kernel with native fallback). Only `(trust := kernel)`
  can certify PROVED: native/auto are rejected up front, and the default (native) is rejected by
  comparator through its `native_decide` axiom. `portfolio` offers kernel mode only.
- **Exact LP / SOS.** `exact_lp` re-checks SoPlex's dual or Farkas certificate in exact
  arithmetic, independently of SoPlex; `sos_certificate` rounds CSDP's Gram matrix to an exact
  rational identity and re-expands it. Both return Lean hints (`linarith only [...]`,
  `nlinarith [sq_nonneg ...]`) that still have to pass `lean.check` and comparator.
- **Mace4** models come back as tables and as Lean `Fin n` definitions: DISPROVED needs the
  table replayed against a frozen predicate (new witness type `list[list[int]]`) or a
  `claim_neg` proof, e.g. by `decide` over those definitions.
- **SnapPy** runs in the Sage worker (its verified routines need Sage). Rigorous:
  `verify_hyperbolicity`, `volume(verified=True)`, `cusp_areas(verified=True)`,
  `canonical_triangulation(verified=True)`, `isometry_signature(verified=True)`,
  `complex_volume(verified_modulo_2_torsion=True)`, explicit isometries from
  `is_isometric_to(..., return_isometries=True)`; exact: `fundamental_group()`, `homology()`.
  Everything else (unverified volumes, shapes, length spectra, `identify()`, a `False` from
  `is_isometric_to`) is numerical evidence. None of it is a verdict without Lean.
- **PSLQ, conjecture mining, local search, evolve** give candidates: a relation to prove, a
  bound to test, a witness to replay, a construction to replay. `local_search` records only
  refutations as coverage, since a random search covers nothing.
- **evolve** is OpenEvolve's loop (program database, best-k parents, size-niche
  inspirations) with the worker as the mutation operator, so the only model involved is the
  thread's own (ChatGPT login). OpenEvolve itself needs an OpenAI-compatible API endpoint, which
  means paid API credits, so it is not installed.
- **Sources.** pi-Base (CC BY 4.0) is a local clone; deduced traits are labelled. LMFDB is the
  read-only API, research profile only. House of Graphs' robots.txt disallows `/api/`, so
  nothing fetches it: a person can save a graph6 list under `~/.lemmata/graphs/` for
  `graph_search(graph_list=...)`. LeanSearch (lean-lsp-mcp's `lean_leansearch`) sends queries
  to leansearch.net, which is not a research source: off unless `LEANSEARCH=1` and allowlisted
  in the research profile.
- **Batch checking.** `batch_check` runs on the warm REPL unless `KIMINA_REPLS > 0`, then on a
  Kimina Lean Server pool (same REPL binary and work project, localhost, random API key,
  landrun). Kimina's per-REPL address-space cap is lifted because Lean dies under it; RAM is
  bounded by the REPL count. Pantograph was not added: its newest release (v0.3.17) targets
  Lean v4.31.0; `step` gives proof-state branching on the warm REPL instead.

Resources measured on the reference machine (WSL2, 15 GB): the REPL with the v2 header loads
in about 35 s cold (4 s warm cache) and shows about 7.3 GB RSS, 8.7 GB peak during `hammer`;
most of that is shared, file-backed .olean mappings, so two extra Kimina REPLs raised used
memory by about 1 GB. A small comparator check now takes 25-45 s (was about 20 s). Disk: about
1.6 GB of extra Lean packages in each of the two projects, 0.6 GB Macaulay2, 0.45 GB SoPlex
env, 0.5 GB Kimina venv, about 0.2 GB SnapPy in the Sage env.

### Known limits (v1)

- One REPL per `lean` server process, so one per thread. Its memory grows with every environment,
  so it restarts after 400 commands (about 15 s to reload Mathlib).
- `enumerate` covers integer domains only; rationals and structured objects need their own search.
- Witness replay is Python against the frozen predicate. A Lean replay of a witness would mean
  proving `claim_neg` from it, which a worker can submit directly.
