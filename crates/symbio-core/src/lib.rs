//! Shared contract for every Symbio crate: tool types, errors, path boundary, config.
//! (Python: voice_action/mcp_client.py types, conversion._inside, config.py, attachment.py)

pub mod attachment;
pub mod config;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};

pub const TRUNCATION_MARKER: &str = "\n[TRUNCATED]";

/// Python `MCPError` hierarchy. `Rejected`/`NotFound` are `ToolRejectedError` subclasses.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ToolError {
    #[error("{0}")]
    Rejected(String),
    #[error("{0}")]
    NotFound(String),
    #[error("{0}")]
    Startup(String),
    #[error("{0}")]
    Connection(String),
    #[error("{0}")]
    Timeout(String),
    #[error("{0}")]
    Other(String),
}

impl ToolError {
    pub fn rejected(msg: impl Into<String>) -> Self {
        Self::Rejected(msg.into())
    }
    /// True for Python `ToolRejectedError` and its subclasses.
    pub fn is_rejected(&self) -> bool {
        matches!(self, Self::Rejected(_) | Self::NotFound(_))
    }
}

pub type ToolOutcome<T> = Result<T, ToolError>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolDefinition {
    pub name: String,
    pub description: String,
    pub schema: Value,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolResult {
    pub ok: bool,
    pub text: String,
    pub structured: Option<Value>,
    pub error: Option<String>,
    pub truncated: bool,
}

impl ToolResult {
    pub fn ok(text: impl Into<String>, structured: Option<Value>) -> Self {
        Self { ok: true, text: text.into(), structured, error: None, truncated: false }
    }
}

/// Python `_bounded_join`: join without building an unbounded copy (limit in chars).
pub fn bounded_join<'a>(parts: impl IntoIterator<Item = &'a str>, limit: usize) -> (String, bool) {
    let mut out = String::new();
    let mut remaining = limit;
    for part in parts {
        let len = part.chars().count();
        out.extend(part.chars().take(remaining));
        if len > remaining {
            return (out, true);
        }
        remaining -= len;
    }
    (out, false)
}

/// JSON Schema (draft 2020-12) check of tool arguments; Python `_validate` helpers.
pub fn validate_arguments(definition: &ToolDefinition, arguments: &Value) -> ToolOutcome<()> {
    let ok = jsonschema::draft202012::is_valid(&definition.schema, arguments);
    if ok { Ok(()) } else { Err(ToolError::rejected(format!("잘못된 {} 인자입니다.", definition.name))) }
}

/// Python `conversion._inside`: absolute, canonical path under an allowed root, never via a symlink.
pub fn inside(raw: &Value, roots: &[PathBuf], exists: bool) -> ToolOutcome<PathBuf> {
    let Some(raw) = raw.as_str().filter(|s| !s.is_empty() && *s == s.trim()) else {
        return Err(ToolError::rejected("파일 경로가 올바르지 않습니다."));
    };
    let path = PathBuf::from(raw);
    let unreadable = || ToolError::rejected("파일 경로를 확인할 수 없습니다.");
    if !path.is_absolute() || !roots.iter().any(|root| path.starts_with(root)) {
        return Err(ToolError::rejected("허용 폴더 밖이거나 링크를 거치는 경로입니다."));
    }
    let is_link = std::fs::symlink_metadata(&path).map(|m| m.file_type().is_symlink()).unwrap_or(false);
    let escapes = is_link
        || if exists {
            path.canonicalize().map_err(|_| unreadable())? != path
        } else {
            let parent = path.parent().unwrap_or(Path::new("/"));
            parent.canonicalize().map_err(|_| unreadable())? != parent
        };
    if escapes {
        return Err(ToolError::rejected("허용 폴더 밖이거나 링크를 거치는 경로입니다."));
    }
    Ok(path)
}
