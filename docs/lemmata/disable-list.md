# lemmata: disable / keep list (scoping pass)

Read-only scoping of this Codex fork (`codex-rs` at `d7ebabf7b7`) for **lemmata**, a math-only research harness.
lemmata runs Astra through Codex on a ChatGPT login. Its only tools are Lean/Mathlib, Mathlib search, sandboxed brute force, Z3, SymPy/Sage, OEIS, a shared whiteboard and `submit_verdict`. All of them are delivered as **our own MCP server(s)** (or app-server `dynamic_tools`).

Paths are relative to `codex-rs/` unless stated otherwise.

---

## 0. The three main control points

Most of the work goes through three places that already exist:

1. **`[features]` in `config.toml`.** Every `Feature` in `features/src/lib.rs` (about lines 1212–2690) can be switched off with `features.<key> = false`, including Stable ones. This needs no code changes and is fully rebase-safe.
2. **`ToolPolicy.allowed_tools`** in `ext/extension-api/src/tool_policy.rs`. It is a hard allowlist of tool names, checked in `core/src/tools/registry.rs:487` and `core/src/tools/spec_plan.rs:380`, and it also filters hosted tools such as web search. It is read from `thread_extension_init` in `core/src/session/session.rs:1073`. Guardian already uses it the same way (`ext/guardian-reviewer/src/settings.rs:44`, `ext/guardian-v3/src/conversation.rs:98`). This is **the** registry gate.
3. **`thread_extensions()`** in `app-server/src/extensions.rs:53-140`. This one function installs every thread extension (web search, image gen, git attribution, guardian v2/v3, memories, skills, plugins MCP, goals, queue, history notes, message board). One small patched function controls the whole extension surface.

---

## 1. DISABLE / REMOVE table

How-to legend: **cfg** = `config.toml` / `-c` override / app-server `thread/start.config` (no code). **policy** = left out of the `ToolPolicy` allowlist (small patch, see §3). **patch** = small edit in our fork. **remove** = delete the crate or code (not recommended).
Mergeable means how easy the change stays across upstream rebases: Easy / Med / Hard.

### 1a. Agent tools the model could call

| # | Name | Where it lives | How to disable | Risk / effort | Mergeable |
|---|---|---|---|---|---|
| 1 | **Shell / exec** (`shell`, `exec_command`, `write_stdin`, unified exec, PTY) | `core/src/tools/handlers/{shell_spec,unified_exec}*`, `core/src/unified_exec/`, `core/src/exec.rs`, `shell-command/`, `exec-server*/`; features `shell_tool`, `unified_exec`, `unified_exec_tty`, `shell_snapshot`, `exec_permission_approvals`, `write_stdin_approval` | **cfg**: `features.shell_tool=false`, `features.unified_exec=false`, `features.shell_snapshot=false`. **policy**: leave out of the allowlist (belt and braces). Brute-force search runs in *our* sandboxed MCP tool, not here. | Low / trivial | Easy |
| 2 | **User shell commands** (`!cmd` in the TUI, `UserShell` task) | `core/src/tasks/user_shell.rs`, `core/src/user_shell_command.rs` | Not exposed if we never ship the TUI. Otherwise **patch** the TUI composer. | Low | Easy |
| 3 | **apply_patch / file editing** | `apply-patch/`, `core/src/apply_patch.rs`, `core/src/tools/handlers/apply_patch*`; registered when `model_info.apply_patch_tool_type.is_some()` (`spec_plan.rs:1389`) | No feature flag. Use **policy** (leave out of the allowlist), or **cfg** `model_catalog_json` with `apply_patch_tool_type` unset. Also set `sandbox_mode="read-only"` so nothing can write. | Low | Easy |
| 4 | **Web search** (hosted `web_search` and the standalone `web.run` extension) | `ext/web-search/`, `core/src/web_search.rs`, `core/src/tools/hosted_spec.rs`; config `web_search`, `tools.web_search`; features `web_search_request`, `web_search_cached`, `web_search_indexed_default`, `standalone_web_search` | **cfg**: `web_search="disabled"`, `features.standalone_web_search=false`, `features.web_search_indexed_default=false`. **patch**: drop `codex_web_search_extension::install` in `app-server/src/extensions.rs:122`. **policy** also filters hosted specs. OEIS comes through our MCP. | Low | Easy |
| 5 | **Image generation** | `ext/image-generation/`, `spec_plan.rs:786`; features `image_generation`, `defer_image_generation`, `image_generation_animation` | **cfg**: `features.image_generation=false`. **patch**: drop the install at `extensions.rs:123`. | Low | Easy |
| 6 | **view_image** | `core/src/tools/handlers/view_image*`; feature `view_image` | **cfg**: `features.view_image=false` | Low | Easy |
| 7 | **Browser / computer use** | `config/src/{browser_use,computer_use,in_app_browser_requirements}.rs`; features `in_app_browser`, `browser_annotation_api`, `browser_use`, `browser_use_full_cdp_access`, `browser_use_external`, `computer_use` | **cfg**: set all six to `false`. Do not set the `[browser_use]` or `[computer_use]` tables. | Low | Easy |
| 8 | **Code mode** (JS `exec`/`wait` runtime, V8) | `code-mode*/`, `v8-poc/`, `core/src/tools/code_mode/`, `core/src/code_mode_host*.rs`; features `code_mode`, `code_mode_only`, `code_mode_host`, `code_mode_*` | **cfg**: `features.code_mode=false`, `features.code_mode_host=false`, `features.code_mode_only=false` | Med (check the MCP-only path after `code_mode_host` is off) / low | Easy |
| 9 | **Plugins / marketplace / remote plugins / tool suggest / plugin install tools** | `core-plugins/`, `plugin*/`, `core/src/plugins/`, handlers `request_plugin_install*`, `list_available_plugins_to_install*`; features `plugins`, `remote_plugin`, `plugin_sharing`, `recommended_plugins`, `tool_suggest`; config `[plugins]`, `[marketplaces]` | **cfg**: set those features to `false` and leave `marketplaces` empty. **patch**: drop `codex_mcp_extension::install_plugins` (`extensions.rs:121`). | Low | Easy |
| 10 | **Apps / connectors** (ChatGPT apps over MCP, `codex_apps`) | `connectors/`, `core/src/connectors.rs`, `core/src/apps/`, `cloud-config/`; features `apps`, `enable_mcp_apps`, `catalog_apps_instructions`, `apps_early_resource_discovery`, `codex_apps_mcp_2026_07_28`, `psp`; config `[apps]`, `include_apps_instructions` | **cfg**: `features.apps=false`, `include_apps_instructions=false`. Only our own `mcp_servers.*` entries stay. | Low | Easy |
| 11 | **Skills** (host, executor and cloud skill providers, skill MCP dependency install) | `skills/`, `ext/skills/`, `core/src/skills.rs`, `core/src/mcp_skill_dependencies.rs`; features `skill_mcp_dependency_install`; config `[skills]` | **cfg**: `features.skill_mcp_dependency_install=false`, `skills.*` off, `include_skill_instructions=false` if exposed. **patch**: skip `install_with_providers` in `extensions.rs`. | Low | Easy |
| 12 | **Code review** (`/review`, `codex review`, `review/start`, guardian auto-review, auto-approval reviewer) | `core/src/tasks/review.rs`, `prompts/templates/review`, `core/src/guardian/`, `ext/guardian-reviewer`, `guardian-v2`, `guardian-v3`, `guardian-context/`; features `guardian_approval`, `guardianv2`, `guardianv3`, `guardianv3.shadow_enabled`, `guardian_*`; config `approvals_reviewer`, `auto_review`, `review_model` | **cfg**: `features.guardian_approval=false`, `features.guardianv2=false`, `features.guardianv3=false`, `approval_policy="never"` (math tools are side-effect-free). **patch**: skip the guardian installs (`extensions.rs:113-118`; v3 *shadow* installs unconditionally) and refuse `review/start` at our gateway. | Med (guardian is wired deep into core; leave its code in place) | Easy (cfg+gate) / Hard (removal) |
| 13 | **Git integration** (git attribution, ghost commits, git info in context, `git/*` RPC, worktrees, git credential helper) | `ext/git-attribution/`, `git-utils/`, `worktree/`, `app-server/src/request_processors/git_processor.rs`, `cli/src/git_credential.rs`, `core-plugins/src/git_policy.rs`, `tui/src/get_git_diff.rs`; config `ghost_snapshot` | **patch**: drop `codex_git_attribution::install` (`extensions.rs:104`). **cfg**: no `ghost_snapshot`; run threads with `cwd` set to an empty scratch directory that is not a repo, so git context stays empty. Refuse `git/*` at the gateway. | Low | Easy |
| 14 | **Multi-agent tools exposed to the model** (spawn/wait/send agent, CSV fan-out, message board) | `core/src/tools/handlers/multi_agents*`, `core/src/agent/`, `ext/agent-message-board/`, `agent-message-board-*`, `message-board*`; features `multi_agent`, `multi_agent_v2`, `agent_message_board*`, `wake_on_agent_messages` | **cfg**: `features.multi_agent=false`, `features.multi_agent_v2=false`, `features.agent_message_board=false`. We do parallelism **outside** the model, with N app-server threads, and coordinate through the whiteboard MCP. | Low | Easy |
| 15 | **Memories** (cross-thread memory tool and extension) | `memories/`, `ext/memories/`; feature `memories`, `external_agent_memory_import`; config `[memories]` | **cfg**: `features.memories=false`. Stops cross-problem leakage and off-topic carry-over. | Low | Easy |
| 16 | **Goals / plan / request_user_input / send_message_to_user / sleep / current_time / test_sync / new_context_window** | `ext/goal/`, handlers `plan*`, `request_user_input*`, `send_message_to_user_async`, `sleep`, `current_time`, `test_sync*`; features `goals`, `sleep_tool`, `current_time_reminder`, `default_mode_request_user_input`, `send_message_to_user_async`; config `tools.update_plan`, `tools.experimental_request_user_input` | **cfg**: `features.goals=false`, `features.sleep_tool=false`, `features.current_time_reminder=false`. **policy** removes the rest. Keep `update_plan` only if we want to show proof plans; workers are headless, so drop `request_user_input`. | Low | Easy |
| 17 | **MCP resource tools / tool_search / dynamic tool discovery** | handlers `mcp_resource*`, `tool_search*`; `tool_suggest` | **policy**: allowlist only our MCP tool names. tool_search is not needed for about 10 tools. | Low | Easy |
| 18 | **Collaboration modes / personality / presets / persistent mode** | `collaboration-mode-templates/`, `prompts/templates/persistent_mode.md`; features `collaboration_modes`, `personality`; config `include_collaboration_mode_instructions`, `personality` | **cfg**: `include_collaboration_mode_instructions=false`, `features.personality=false` | Low | Easy |
| 19 | **Realtime / voice / dictation** | `realtime-webrtc/`, `voice-host/`, `core/src/realtime_*`; features `realtime_conversation`, `in_app_voice`, `in_app_dictation`, `realtime_frames_local`; config `[realtime]`, `[audio]`, `experimental_realtime_*` | **cfg**: set the features to `false`. Refuse `thread/realtime/*` at the gateway. | Low | Easy |
| 20 | **Chronicle / prediction / history notes** | `ext/history-notes/`; features `chronicle`; `thread/prediction/*` | **cfg** for `chronicle`. **patch**: drop `codex_history_notes_extension::install` (`extensions.rs:88`), which makes calls through `auth_manager`. | Low | Easy |
| 21 | **Multi-env copy file / environments / deferred executor** | `ext/multi-env-copy-file/`, `core/src/environment_selection*`; features `multi_env_copy_file`, `deferred_executor`, `per_environment_context`, `multi_environment_mcp` | Leave these features off (they default off). Pass `environments: []` on `thread/start` so no executor environment is attached. | Low | Easy |

### 1b. Sandbox-for-code bits

| # | Name | Where | How | Risk / effort | Mergeable |
|---|---|---|---|---|---|
| 22 | Windows sandbox service / MXC / elevated sandbox setup | `windows-sandbox-rs/`, `windows-sandbox-service/`, `mxc-sandbox/`, `app-server/src/request_processors/windows_sandbox_processor.rs`, `cli/src/sandbox_setup.rs`; features `windows_sandbox_service`, `prefer_mxc` | **cfg**: keep the features off. Refuse `windowsSandbox/*` at the gateway. **Do not remove** the crates: core depends on them. | Low | Easy |
| 23 | Linux bwrap / landlock / seatbelt, network proxy, execpolicy | `linux-sandbox/`, `bwrap/`, `sandboxing/`, `network-proxy/`, `execpolicy*/`, `process-hardening/`; features `network_proxy`, `use_legacy_landlock`; config `sandbox_mode`, `[permissions]`, `shell_environment_policy` | **cfg**: `sandbox_mode="read-only"` with `features.network_proxy=false`. With no shell tool nothing executes through these paths. Keep them compiled. Our brute-force MCP server brings its **own** sandbox (container/nsjail/seccomp, time and memory limits). | Low | Easy |
| 24 | `codex sandbox`, `debug sandbox`, `execpolicy check` CLI | `cli/src/{debug_sandbox,sandbox_setup,sandbox_uninstall}.rs` | We only ship app-server, so these are not exposed. To hide them, **patch** the `Subcommand` enum in `cli/src/main.rs`. | Low | Med |

### 1c. Telemetry, updates, cloud, account extras

| # | Name | Where | How | Risk / effort | Mergeable |
|---|---|---|---|---|---|
| 25 | **Analytics / Statsig metrics** | `analytics/`, `statsig/`, `core/src/statsig.rs`, `core/src/tools/control_tool_analytics.rs`; config `[analytics] enabled`; features `statsig_gates`, `analytics_plan_history`, `runtime_metrics` | **cfg**: `analytics.enabled=false`, `otel.metrics_exporter="none"` (the default is `statsig`, which follows `analytics.enabled`) | Low | Easy |
| 26 | **OTel logs/traces** | `otel/`, `otel-trace-websocket/`, `core/src/otel_init.rs`; config `[otel]` | **cfg**: no `otel.exporter` or `trace_exporter`; set `log_user_prompt=false`. Point it at our own collector later if we want our own observability. | Low | Easy |
| 27 | **Feedback upload / doctor report** | `feedback/`, `app-server/.../feedback_*.rs`; config `[feedback] enabled`; `feedback/upload` RPC | **cfg**: `feedback.enabled=false`. Refuse `feedback/*` at the gateway. | Low | Easy |
| 28 | **Update checks / self-update** | `tui/src/update*.rs`, `cli/src/doctor/updates.rs`, `cli/src/daemon_install.rs`; config `check_for_update_on_startup`; features `in_app_updates`, `daemon_updates`, `daemon_auto_start` | **cfg**: `check_for_update_on_startup=false`, `features.in_app_updates=false`, `features.daemon_updates=false`, `features.daemon_auto_start=false`. We pin the binary version ourselves. | Low | Easy |
| 29 | **Cloud tasks / cloud config / cloud skills / remote thread store** | `cloud-tasks*/`, `cloud-client/`, `cloud-config/`, `backend-client/`; `codex cloud`; config `[cloud]`, `experimental_thread_store*` | We do not ship the CLI subcommand. Leave `[cloud]` unset. **patch** (optional): `cloud_skill_provider = None` in the app-server wiring. | Low | Easy |
| 30 | **Remote control / TCP tunnel / responses-api-proxy / app-server daemon proxy** | `cli/src/remote_control_cmd.rs`, `tcp-tunnel/`, `responses-api-proxy/`, `app-server-daemon/`, `app-server/.../remote_control_processor*` | Not shipped / not launched. Refuse `remoteControl/*` at the gateway. | Low | Easy |
| 31 | **Desktop "in_app_*" surfaces** (chat, local automation) | features `in_app_chat`, `in_app_local_automation`; config `desktop` | **cfg**: `false` | Low | Easy |
| 32 | **Bedrock / GCP / gateway setup wizards, OSS providers** (lmstudio, ollama), AWS auth | `aws-auth/`, `lmstudio/`, `ollama/`; features `bedrock_setup_wizard`, `gcp_setup_wizard`, `gateway_setup_wizard`; config `oss_provider` | **cfg**: features `false`. Pin `model_provider="openai"` and `forced_login_method="chatgpt"`. | Low | Easy |
| 33 | **Rate-limit nudges / credits email / workspace messages / daybreak & cyber-access programs** | `account/*` RPCs; features `cli_daybreak`, `api_key_cyber_access_programs`, `daybreak_program_propagation`, `workspace_owner_usage_nudge` | **cfg**: features `false`. Our UI never calls the nudge RPCs. | Low | Easy |
| 34 | **External agent migration / import, hooks import** | `external-agent-migration/`; feature `external_agent_memory_import`; TUI `/import` | **cfg** / do not ship the TUI | Low | Easy |
| 35 | **Project docs** (AGENTS.md ingestion), project trust, `notify`, `file_opener` | `core/src/agents_md*.rs`; config `project_doc_max_bytes`, `project_doc_fallback_filenames`, `projects`, `notify` | **cfg**: `project_doc_max_bytes=0`, with a non-repo scratch `cwd`. Stops repo instructions being injected into math threads. | Low | Easy |

### 1d. User-facing surfaces (front ends)

| # | Name | Where | How | Risk / effort | Mergeable |
|---|---|---|---|---|---|
| 36 | **TUI** (slash commands `/review`, `/diff`, `/init`, `/apps`, `/plugins`, `/worktree`, `/voice`, `/agents`, `/mcp`, `/cd`, `/pets`, …) | `tui/`, `tui/src/slash_command.rs` | Do not ship it. Users use the lemmata front end over app-server. If a debug TUI is wanted, gate it in `slash_command.rs` with a fork-only `is_lemmata_allowed()` filter. | Low (not shipped) / Med (if patched) | Easy / Med |
| 37 | **codex CLI subcommands** (`exec`, `review`, `apply`, `cloud`, `mcp`, `plugin`, `marketplace`, `queue`, `remote-control`, `sandbox`, `execpolicy`, `git-credential`, `tcp-tunnel`, `responses-api-proxy`, `exec-server`, `app`, `resume`, `fork`) | `cli/src/main.rs:146-246`, `codex-cli/` (npm wrapper) | Ship only `codex app-server` (plus `login` for the ChatGPT device/browser flow). Wrap the binary; no edits to `main.rs`. | Low | Easy |
| 38 | **App-server client-callable RPCs we must not forward** | `app-server-protocol/src/protocol/common.rs`, `app-server/src/request_processors/*` | **gateway allowlist**, enforced in our orchestrator, *not* in the fork: refuse `command/exec*`, `process/*`, `fs/*` (write/remove/copy/watch), `git/*`, `review/start`, `plugin/*`, `marketplace/*`, `app/*`, `skills/*` (writes), `config/value/write`, `config/batchWrite`, `config/mcpServer/reload`, `feedback/*`, `thread/realtime/*`, `windowsSandbox/*`, `remoteControl/*`, `project/*`, `environment/add`, `attestation/*`. End users never talk to app-server directly. | Low | Easy (no fork change) |
| 39 | **SDKs**: TS `sdk/typescript`, Python `sdk/python`, `sdk/python-runtime` | as named | Keep them as **our orchestrator's** client libraries; they are not exposed to end users. Pin the version. No removal. | Low | Easy |

---

## 2. KEEP list

| Component | Path | Why it stays |
|---|---|---|
| Core agent loop / session / turn / compaction | `core/` (`session/`, `tasks/regular.rs`, `compact*.rs`, `context_manager/`), `core-api/` | This is the agent. Long proofs need compaction. |
| Responses client and model provider plumbing | `codex-api/`, `codex-client/`, `model-provider*/`, `models-manager/`, `http-client/`, `http-headers-helper/`, `client` | Talks to Astra via the ChatGPT backend. |
| ChatGPT login and auth storage | `login/`, `chatgpt/`, `keyring-store/`, `secrets/`, `codex-home/`, `agent-identity/`, `websocket-auth/` | The ChatGPT login is the product requirement. Pin `forced_login_method="chatgpt"`. |
| MCP client | `codex-mcp/`, `rmcp-client/`, `ext/mcp/` (`codex_mcp_extension::install`, **not** `install_plugins`), `core/src/mcp*.rs` | All math tools come in through this. `mcp_lazy_stdio_startup` and `mcp_connection_sharing` are useful with many workers. |
| App-server, transport, protocol | `app-server/`, `app-server-protocol/`, `app-server-transport/`, `app-server-client/` | How the orchestrator runs many parallel worker threads. |
| Dynamic tools (`thread/start.dynamic_tools`, `item/tool/call`) | `core/src/tools/handlers/dynamic.rs`, `tools/src/dynamic_tool.rs` | An alternative to MCP for `submit_verdict` and the whiteboard, called back into the orchestrator. Certificate validation then happens server-side before a verdict is accepted. |
| Tool registry / router / ToolPolicy / spec plan | `core/src/tools/{registry,router,spec_plan,orchestrator,parallel}.rs`, `tools/`, `ext/extension-api/` | Needed to run MCP tools. ToolPolicy is our allowlist. |
| Config loader, features, schema | `config/`, `features/`, `config-schema/` | Everything in §1 is configuration. |
| Hooks engine | `hooks/` (`UserPromptSubmit`, `PreToolUse`, `Stop`) | Lets us run the intake check and tool deny rules from config. |
| Rollout / thread store / state DB / history | `rollout/`, `thread-store/`, `state/`, `history/`, `message-history/` | Thread persistence gives auditable proof attempts and resume. Can run `ephemeral` for throwaway workers. |
| Prompts crate | `prompts/` (`compact`, `model_messages`) | Compaction prompt and base-instruction plumbing. We override the base text (see §3). |
| Protocol / utils / arg0 / process hardening | `protocol/`, `utils/`, `async-utils/`, `arg0/`, `process-hardening/`, `build-info/` | Shared foundations, deeply depended on. |
| Sandboxing crates (compiled, unused) | `sandboxing/`, `linux-sandbox/`, `windows-sandbox-rs/`, `execpolicy/` | Core links against them, and removing them makes rebases very painful. With `read-only` and no shell they are inert. |
| `update_plan` (optional) | `handlers/plan.rs` | Useful as a visible proof plan. Leave it out of the allowlist if not wanted. |
| Agent roles (optional) | `agent-roles/`, config `[agents]` | A cheap way to define a "math worker" role preset if we want one. |

---

## 3. Where to enforce math-only

Defense in depth, from outermost to innermost:

**A. Intake check (before any model call)**
- **Primary: the orchestrator.** Every user conjecture goes through *our* gateway before `turn/start`. A cheap classifier (or a narrow Astra call with no tools) decides math or not math. Non-math requests are rejected and Codex never sees them. No fork change.
- **Secondary, inside Codex: the `UserPromptSubmit` hook.** `hooks/src/events/user_prompt_submit.rs` supports `{"decision":"block","reason":…}` or exit code 2 (see line 200). Configure it in `config.toml` `[hooks]` / `hooks.json` (feature `hooks`, Stable, on by default). The hook script runs a fast topic check and blocks off-topic follow-ups inside a worker thread. Config only, rebase-safe.
- Not usable for this: `TurnStartAdmission` (`ext/extension-api/src/turn_admission.rs`) is a capacity gate and does not see prompt text.

**B. System prompt / base instructions**
- Resolution order in `core/src/config/mod.rs:4439-4451`: `base_instructions` from the request, then `model_instructions_file`, then `instructions` in config, then otherwise the model catalog template (`prompts/src/model_instructions.rs` via `ResolvedModelMessages`).
- Set the lemmata math-only prompt with **`thread/start.base_instructions`** (`app-server-protocol/src/protocol/v2/thread.rs:108`) from the orchestrator, or with `model_instructions_file = "<lemmata>/prompts/astra_math.md"` in config. Use `developer_instructions` for per-problem context (the statement, prior whiteboard state). Both are config/RPC only.
- Strip the other injected instruction blocks: `include_permissions_instructions=false`, `include_apps_instructions=false`, `include_collaboration_mode_instructions=false`, `include_environment_context=false`, `project_doc_max_bytes=0`.

**C. Tool registry allowlist (hard guarantee)**
- `ToolPolicy.allowed_tools` (`ext/extension-api/src/tool_policy.rs`). It is enforced at `core/src/tools/registry.rs:487` (model-visible tools) and `core/src/tools/spec_plan.rs:380` (hosted specs such as web_search). If the model hallucinates a tool name, the router gets nothing it can dispatch.
- Today it is only injected through `thread_extension_init` (by guardian). **Small patch**: in `core/src/session/session.rs:1073`, fall back to a config-driven policy, e.g. a new fork-only key `lemmata.allowed_tools = ["mcp__lemmata__lean_check", …, "submit_verdict"]`, instead of `Arc::default()`. That is about 15 lines in one place. Another option keeps core untouched: insert `ToolPolicy` from the app-server thread-start path (where `thread_extension_init` is built), like `ext/guardian-v3/src/conversation.rs:98` does.
- Back it up with **`PreToolUse` hooks** (config only) that deny any tool name outside the allowlist, and with per-server `enabled_tools` on our `mcp_servers.*` entries.

**D. Verdict gate**
- `submit_verdict` must be the only way to finish. Implement it as a dynamic tool or MCP tool that **validates the certificate server-side**: a Lean proof term that type-checks, a counterexample that re-evaluates, a Z3 `unsat` core. Use a `Stop` hook to block thread completion without a verdict (the `Stop` hook can block the stop).

---

## 4. Recommended approach

**Config-first, plus a very thin patch layer, with no hard removals.**

1. **Config profile (most of the work, zero merge cost).** Ship one `lemmata.config.toml` (or a `[profiles.lemmata]`) with every `features.* = false` from §1, plus `web_search="disabled"`, `sandbox_mode="read-only"`, `approval_policy="never"`, `analytics.enabled=false`, `feedback.enabled=false`, `check_for_update_on_startup=false`, `otel.metrics_exporter="none"`, `forced_login_method="chatgpt"`, `model_provider="openai"`, `project_doc_max_bytes=0`, the `include_*_instructions=false` keys, `model_instructions_file`, our `mcp_servers`, and `[hooks]`. The orchestrator also passes the per-thread values through `thread/start` (`base_instructions`, `config`, `dynamic_tools`, `environments: []`, `ephemeral`). Consider `requirements.toml` / managed config so a later override cannot turn features back on.
2. **Thin patch layer (about 3 small hunks, fork-only, easy to rebase).**
   - `app-server/src/extensions.rs` `thread_extensions()`: wrap the web-search, image-gen, git-attribution, guardian v2/v3(+shadow), history-notes, skills and `install_plugins` installs in a single `if !lemmata_mode(config)` check. This is the one place upstream adds extensions, so conflicts are rare and easy to see.
   - `core/src/session/session.rs:1073`: config-driven `ToolPolicy` fallback (hard allowlist).
   - Optional: `cli/src/main.rs` to hide subcommands. Wrapping the binary is better.
   - Put the patches behind one fork-only config key or feature (e.g. `features.lemmata_mode`), so the diff is additive and `git rebase` conflicts stay small. Keep them in a few commits that are rebased on top of upstream.
3. **Gateway allowlist outside the fork.** The orchestrator only forwards the app-server RPCs it needs (`initialize`, `account/login/*`, `account/read`, `thread/start|resume|read|fork`, `turn/start|interrupt`, `item/tool/call` responses, `model/list`). Everything in row 38 is refused there.
4. **No hard removal.** Deleting crates (cloud-tasks, tui, guardian, sandboxing) touches `Cargo.toml`, `BUILD.bazel`, `MODULE.bazel.lock` and many `use` sites, and conflicts on almost every upstream sync. Binary size and attack surface are already handled by never exposing those code paths. Revisit only if we need a smaller binary, and then use Cargo features rather than deletion.

**Order of work:**
1. Write the config profile and the MCP math servers.
2. Patch the ToolPolicy fallback.
3. Gate the extensions in `thread_extensions()`.
4. Add the UserPromptSubmit and Stop hooks.
5. Add the gateway RPC allowlist.

**Verify each step** by checking that `thread/start` → model request `tools[]` contains only the lemmata tools (`codex debug prompt-input` can dump it).

---

*Scoping only. No code was changed, built or tested. Line numbers are as of `d7ebabf7b7` and will drift.*
