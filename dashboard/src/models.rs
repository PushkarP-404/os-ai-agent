// ============================================================
// models.rs — Scan GGUF model files, query llama-server
// ============================================================
use serde::Deserialize;

pub const GGUF_SEARCH_PATHS: &[&str] = &[
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

    for dir in GGUF_SEARCH_PATHS {
        let path = std::path::Path::new(dir);
        if !path.exists() { continue; }

        if let Ok(entries) = std::fs::read_dir(path) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.extension().and_then(|e| e.to_str()) == Some("gguf") {
                    let size_gb = entry.metadata().map(|m| {
                        m.len() as f32 / 1_073_741_824.0
                    }).unwrap_or(0.0);

                    models.push(ModelEntry {
                        name: p.file_name()
                            .and_then(|n| n.to_str())
                            .unwrap_or("unknown")
                            .to_string(),
                        path: p.to_string_lossy().to_string(),
                        size_gb,
                        active: false,
                    });
                }
            }
        }
    }

    // Windows dev: return a mock model
    if models.is_empty() {
        models.push(ModelEntry {
            name: "llama-3.2-3b-q4.gguf".to_string(),
            path: "/opt/models/llama-3.2-3b-q4.gguf".to_string(),
            size_gb: 2.1,
            active: true,
        });
        models.push(ModelEntry {
            name: "mistral-7b-q4.gguf".to_string(),
            path: "/opt/models/mistral-7b-q4.gguf".to_string(),
            size_gb: 4.3,
            active: false,
        });
    }

    models
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct LlamaHealth {
    status: Option<String>,
    #[serde(rename = "model_path")]
    model_path: Option<String>,
}

/// Query llama-server /health to get the loaded model name
pub async fn active_model_name(endpoint: &str) -> String {
    let url = format!("{}/health", endpoint.trim_end_matches('/'));
    match reqwest::get(&url).await {
        Ok(resp) => {
            if let Ok(h) = resp.json::<LlamaHealth>().await {
                h.model_path
                    .as_deref()
                    .and_then(|p| p.rsplit('/').next())
                    .unwrap_or("Unknown")
                    .to_string()
            } else {
                "Unknown".to_string()
            }
        }
        Err(_) => "Offline".to_string(),
    }
}

