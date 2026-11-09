// ============================================================
// intent_client.rs — Intent Bus Unix socket client
// Subscribes to broadcast events from /var/run/ai-agent/intent.sock
// ============================================================
use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

#[allow(dead_code)]
pub const SOCKET_PATHS: &[&str] = &[
    "/var/run/ai-agent/intent.sock",
    "/var/ai-agent/intent.sock",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IntentEvent {
    pub timestamp: String,
    pub action: String,
    pub source: String,
    pub target: String,
    pub status: String,
    #[serde(flatten)]
    pub payload: serde_json::Value,
}

/// Returns a channel receiver that emits IntentEvents.
/// Spawns a background task; reconnects on socket errors.
pub fn subscribe() -> mpsc::Receiver<IntentEvent> {
    let (tx, rx) = mpsc::channel(64);
    tokio::spawn(async move {
        loop {
            if let Err(e) = connect_and_stream(&tx).await {
                log::warn!("Intent Bus disconnected: {e}. Retrying in 2s…");
            }
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        }
    });
    rx
}

#[cfg(target_os = "linux")]
async fn connect_and_stream(tx: &mpsc::Sender<IntentEvent>) -> Result<()> {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::net::UnixStream;

    let path = SOCKET_PATHS.iter().find(|p| std::path::Path::new(p).exists())
        .ok_or_else(|| anyhow::anyhow!("Intent socket not found"))?;

    let stream = UnixStream::connect(path).await?;
    let reader = BufReader::new(stream);
    let mut lines = reader.lines();

    while let Some(line) = lines.next_line().await? {
        if line.trim().is_empty() { continue; }
        match serde_json::from_str::<IntentEvent>(&line) {
            Ok(ev) => { let _ = tx.send(ev).await; }
            Err(e) => log::debug!("Intent Bus parse error: {e}: {line}"),
        }
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
async fn connect_and_stream(_tx: &mpsc::Sender<IntentEvent>) -> Result<()> {
    // On Windows host: emit synthetic events every 3s for UI development
    use chrono::Utc;
    let actions = ["browser.goto", "llm.query", "fs.read", "agent.spawn"];
    let apps    = ["ai-agent", "lightpanda", "llama-server", "file-manager"];
    let mut i = 0usize;
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        let ev = IntentEvent {
            timestamp: Utc::now().format("%H:%M:%S").to_string(),
            action:    actions[i % actions.len()].to_string(),
            source:    apps[i % apps.len()].to_string(),
            target:    apps[(i + 1) % apps.len()].to_string(),
            status:    if i % 4 == 3 { "err".to_string() } else { "ok".to_string() },
            payload:   serde_json::Value::Null,
        };
        let _ = _tx.send(ev).await;
        i += 1;
    }
}

