// ============================================================
// llm.rs — Streaming completions from llama-server + agent-cli
// ============================================================
use anyhow::{anyhow, Result};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

pub const LLAMA_ENDPOINT: &str = "http://127.0.0.1:11434";

#[derive(Serialize)]
struct CompletionRequest<'a> {
    prompt: &'a str,
    n_predict: i32,
    stream: bool,
    temperature: f32,
    stop: Vec<&'a str>,
}

#[derive(Deserialize)]
struct CompletionChunk {
    content: Option<String>,
    stop: Option<bool>,
}

fn format_chatml(prompt: &str) -> String {
    if prompt.contains("<|im_start|>") {
        prompt.to_string()
    } else {
        format!(
            "<|im_start|>system\nYou are Sentinel, the Elenchus OS assistant. Be concise and helpful.<|im_end|>\n<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
        )
    }
}

/// Fire a streaming completion. Sends token chunks to `tx`.
pub async fn stream_completion(
    prompt: &str,
    tx: mpsc::Sender<String>,
) -> Result<()> {
    let client = reqwest::Client::new();
    let url = format!("{}/completion", LLAMA_ENDPOINT);
    let formatted = format_chatml(prompt);

    let body = CompletionRequest {
        prompt: &formatted,
        n_predict: 512,
        stream: true,
        temperature: 0.7,
        stop: vec!["<|im_end|>", "</s>", "[/INST]", "Human:", "User:"],
    };

    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await?;

    if !resp.status().is_success() {
        return Err(anyhow!("llama-server HTTP {}", resp.status()));
    }

    let mut stream = resp.bytes_stream();

    while let Some(chunk) = stream.next().await {
        let bytes = chunk?;
        let text = String::from_utf8_lossy(&bytes);

        // llama.cpp streams SSE-like lines: "data: {...}\n\n"
        for line in text.lines() {
            let line = line.trim();
            let line = line.strip_prefix("data:").unwrap_or(line).trim();
            if line.is_empty() { continue; }

            if let Ok(parsed) = serde_json::from_str::<CompletionChunk>(line) {
                if let Some(content) = parsed.content {
                    if !content.is_empty() {
                        let _ = tx.send(content).await;
                    }
                }
                if parsed.stop.unwrap_or(false) {
                    return Ok(());
                }
            }
        }
    }

    Ok(())
}

/// Dispatch an OS command/intent through agent-cli when prefixed with /do or /agent.
pub async fn dispatch_agent_cli(query: &str) -> Result<String> {
    #[cfg(target_os = "linux")]
    {
        use tokio::process::Command;
        let bin = if std::path::Path::new("/usr/local/bin/agent-cli").exists() {
            "/usr/local/bin/agent-cli"
        } else {
            "/usr/bin/agent-cli"
        };
        let out = Command::new(bin).arg(query).output().await?;
        let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
        if !stdout.is_empty() {
            return Ok(stdout);
        }
        if !stderr.is_empty() {
            return Ok(stderr);
        }
        Ok(format!("Dispatched task to agent-cli (exit {})", out.status))
    }
    #[cfg(not(target_os = "linux"))]
    {
        Ok(format!("[dev] agent-cli mock dispatch: {query}"))
    }
}
