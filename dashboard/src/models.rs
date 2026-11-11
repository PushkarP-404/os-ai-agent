// ============================================================
// models.rs — Scan GGUF model files, query llama-server
// ============================================================
use serde::Deserialize;

pub const GGUF_SEARCH_PATHS: &[&str] = &[
    "/var/lib/ai-agent/models",
    "/var/lib/llama/models",
    "/opt/models",
    "/root/models",
];

#[derive(Debug, Clone)]
pub struct ModelEntry {
    pub name: String,
    pub path: String,
    pub size_gb: f32,
    pub active: bool,
}

/// Scan known directories for *.gguf files
pub fn scan_models() -> Vec<ModelEntry> {
    let mut models = Vec::new();
    let mut seen = std::collections::HashSet::new();

    for dir in GGUF_SEARCH_PATHS {
        let path = std::path::Path::new(dir);
        if !path.exists() { continue; }

        if let Ok(entries) = std::fs::read_dir(path) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.extension().and_then(|e| e.to_str()) == Some("gguf") {
                    let path_str = p.to_string_lossy().to_string();
                    if !seen.insert(path_str.clone()) { continue; }

                    let size_gb = entry.metadata().map(|m| {
                        m.len() as f32 / 1_073_741_824.0
                    }).unwrap_or(0.0);

                    models.push(ModelEntry {
                        name: p.file_name()
                            .and_then(|n| n.to_str())
                            .unwrap_or("unknown")
                            .to_string(),
                        path: path_str,
                        size_gb,
                        active: false,
                    });
                }
            }
        }
    }

    #[cfg(not(target_os = "linux"))]
    if models.is_empty() {
        models.push(ModelEntry {
            name: "os_planner_lora.gguf".to_string(),
            path: "/var/lib/ai-agent/models/os_planner_lora.gguf".to_string(),
            size_gb: 0.1,
            active: true,
        });
        models.push(ModelEntry {
            name: "smollm2-135m-instruct-q4_k_m.gguf".to_string(),
            path: "/var/lib/ai-agent/models/smollm2-135m-instruct-q4_k_m.gguf".to_string(),
            size_gb: 0.1,
            active: false,
        });
    }

    models
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct LlamaProps {
    status: Option<String>,
    model_path: Option<String>,
    model_alias: Option<String>,
}

fn read_conf_model_name() -> Option<String> {
    let raw = std::fs::read_to_string("/etc/conf.d/llama-server").ok()?;
    for line in raw.lines() {
        let line = line.trim();
        if let Some(val) = line.strip_prefix("LLAMA_MODEL=") {
            let trimmed = val.trim_matches('"').trim_matches('\'');
            if let Some(name) = trimmed.rsplit('/').next() {
                if !name.is_empty() {
                    return Some(name.to_string());
                }
            }
        }
    }
    None
}

/// Query llama-server /props (then /health, then /etc/conf.d/llama-server)
pub async fn active_model_name(endpoint: &str) -> String {
    let base = endpoint.trim_end_matches('/');
    let props_url = format!("{base}/props");
    if let Ok(resp) = reqwest::get(&props_url).await {
        if resp.status().is_success() {
            if let Ok(p) = resp.json::<LlamaProps>().await {
                if let Some(name) = p.model_path.as_deref()
                    .or(p.model_alias.as_deref())
                    .and_then(|s| s.rsplit('/').next())
                {
                    if !name.is_empty() {
                        return name.to_string();
                    }
                }
            }
        }
    }

    let health_url = format!("{base}/health");
    if let Ok(resp) = reqwest::get(&health_url).await {
        if resp.status().is_success() {
            if let Some(name) = read_conf_model_name() {
                return name;
            }
            return "Loaded".to_string();
        }
    }

    read_conf_model_name().unwrap_or_else(|| "Offline".to_string())
}
