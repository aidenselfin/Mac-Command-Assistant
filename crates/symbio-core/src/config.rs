//! Port of voice_action/config.py. TODO(agent): full load_config.
use std::path::PathBuf;

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

#[derive(Clone, PartialEq)]
pub struct Config {
    pub provider: String,
    pub model: String,
    pub api_key: String,
    pub allowed_directories: Vec<PathBuf>,
    pub base_url: Option<String>,
    pub max_tool_rounds: u32,
    pub max_run_seconds: f64,
    pub llm_timeout_seconds: f64,
    pub mcp_timeout_seconds: f64,
    pub max_tool_result_chars: usize,
    pub feedback_enabled: bool,
    pub feedback_api_key: Option<String>,
    pub voice_api_key: Option<String>,
    pub fallback_api_key: Option<String>,
    pub fallback_model: String,
    pub feedback_model: String,
    pub feedback_daily_limit: u32,
    pub feedback_autonomy: bool,
    pub finder_reveal: bool,
    pub finder_dock: bool,
    pub escalation_model: Option<String>,
}

impl std::fmt::Debug for Config {
    // Keys never appear in logs (Python: field(repr=False)).
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("provider", &self.provider)
            .field("model", &self.model)
            .field("allowed_directories", &self.allowed_directories)
            .field("base_url", &self.base_url)
            .finish_non_exhaustive()
    }
}
