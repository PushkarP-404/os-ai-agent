// ============================================================
// llm.rs — Streaming completions from llama-server
// ============================================================
use anyhow::Result;
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

/// Fire a streaming completion. Sends token chunks to `tx`.
/// Returns when stream ends or errors.
pub async fn stream_completion(
    prompt: &str,
    tx: mpsc::Sender<String>,
) -> Result<()> {
    let client = reqwest::Client::new();
    let url = format!("{}/completion", LLAMA_ENDPOINT);

    let body = CompletionRequest {
        prompt,
        n_predict: 512,
        stream: true,
        temperature: 0.7,
        stop: vec!["</s>", "[/INST]", "Human:", "User:"],
    };

    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await?;

    let mut stream = resp.bytes_stream();

    while let Some(chunk) = stream.next().await {
        let bytes = chunk?;
        let text = String::from_utf8_lossy(&bytes);

        // llama.cpp streams SSE-like lines: "data: {...}\n\n"
        for line in text.lines() {
            let line = line.trim_start_matches("data: ").trim();
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

