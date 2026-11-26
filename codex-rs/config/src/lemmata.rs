//! Fork-only `[lemmata]` settings for the math-only lemmata shell.
//! Kept in its own module so upstream rebases only touch one field and one re-export.

use codex_protocol::ToolName;
use schemars::JsonSchema;
use serde::Deserialize;
use serde::Serialize;

/// Separates a tool namespace from its name in `allowed_tools` entries.
const NAMESPACE_SEPARATOR: char = '/';

/// `[lemmata]` table in `config.toml`.
#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq, Eq, JsonSchema)]
// A typo here must fail loudly: an ignored `mode` key would silently disable every lemmata guard.
#[serde(deny_unknown_fields)]
#[schemars(deny_unknown_fields)]
pub struct LemmataToml {
    /// The single fork switch. When true, app-server skips non-math extensions
    /// and every thread gets a hard tool allowlist.
    #[serde(default)]
    pub mode: bool,
    /// Research role: lets the web search extension install. Only honored in lemmata mode.
    #[serde(default)]
    pub research: bool,
    /// Hard tool allowlist used in lemmata mode. Entries are `name` or `namespace/name`
    /// (for example `mcp__lemmata/lean_check` or `web/run`). Missing means no tools.
    pub allowed_tools: Option<Vec<String>>,
}

impl LemmataToml {
    /// Whether the research role may install web search.
    pub fn allows_web_search(&self) -> bool {
        !self.mode || self.research
    }

    /// The hard allowlist when lemmata mode is on; `None` leaves tool selection alone.
    pub fn tool_allowlist(&self) -> Option<Vec<ToolName>> {
        if !self.mode {
            return None;
        }
        let entries = self.allowed_tools.as_deref().unwrap_or_default();
        Some(entries.iter().map(|entry| parse_tool_name(entry)).collect())
    }
}

fn parse_tool_name(entry: &str) -> ToolName {
    match entry.split_once(NAMESPACE_SEPARATOR) {
        Some((namespace, name)) => ToolName::namespaced(namespace, name),
        None => ToolName::plain(entry),
    }
}
