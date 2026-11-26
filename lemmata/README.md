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
`mcp__lean/check`; the standalone web search tool is `web/run`. The math tool names in the profiles
are provisional until the servers exist. Confirm the real names with `codex debug prompt-input`.

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

## Future math tools

Both profiles carry commented `mcp_servers` placeholders for `lean`, `brute_force`, `smt`, `cas`,
`oeis`, `whiteboard` and `submit_verdict`. When a server exists, uncomment its entry, set
`enabled_tools`, and make sure its tool names match `allowed_tools`.
