// ============================================================
// intent_client.rs — Intent Bus Unix socket client + log reader
// Registers with /var/ai-agent/intent.sock and polls health
// ============================================================
use anyhow::Result;
use chrono::Utc;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

#[allow(dead_code)]
pub const SOCKET_PATHS: &[&str] = &[
    "/var/ai-agent/intent.sock",
    "/var/run/ai-agent/intent.sock",
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
                log::warn!("Intent Bus disconnected: {e}. Retrying in 3s…");
            }
            tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        }
    });
    rx
}

#[cfg(target_os = "linux")]
async fn connect_and_stream(tx: &mpsc::Sender<IntentEvent>) -> Result<()> {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixStream;

    let path = SOCKET_PATHS
        .iter()
        .find(|p| std::path::Path::new(p).exists())
        .ok_or_else(|| anyhow::anyhow!("Intent socket not found"))?;

    let stream = UnixStream::connect(path).await?;
    let (reader, mut writer) = stream.into_split();

    // Register sentinel-ui on the Intent Bus
    let reg = r#"{"type":"register","app_name":"sentinel-ui","capabilities":["ui.status"]}"#;
    writer.write_all(reg.as_bytes()).await?;
    writer.write_all(b"\n").await?;

    let mut lines = BufReader::new(reader).lines();

    // Read registration ACK
    if let Some(ack_line) = lines.next_line().await? {
        let ok = ack_line.contains("\"OK\"");
        let _ = tx
            .send(IntentEvent {
                timestamp: Utc::now().format("%H:%M:%S").to_string(),
                action: "bus.register".to_string(),
                source: "sentinel-ui".to_string(),
                target: "intent-bus".to_string(),
                status: if ok { "ok" } else { "err" }.to_string(),
                payload: serde_json::Value::Null,
            })
            .await;
    }

    // Periodically ping the bus and read any unsolicited intent messages
    let mut ping_interval = tokio::time::interval(std::time::Duration::from_secs(15));
    ping_interval.tick().await; // consume initial tick

    loop {
        tokio::select! {
            res = lines.next_line() => {
                match res? {
                    Some(line) if !line.trim().is_empty() => {
                        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&line) {
                            let status_str = val.get("status").and_then(|v| v.as_str()).unwrap_or("");
                            if status_str == "PONG" {
                                continue; // routine keepalive
                            }
                            let action = val.get("intent_type")
                                .or_else(|| val.get("action"))
                                .or_else(|| val.get("type"))
                                .and_then(|v| v.as_str())
                                .unwrap_or("bus.event")
                                .to_string();
                            let source = val.get("source_app")
                                .or_else(|| val.get("source"))
                                .and_then(|v| v.as_str())
                                .unwrap_or("intent-bus")
                                .to_string();
                            let target = val.get("target_app")
                                .or_else(|| val.get("target"))
                                .and_then(|v| v.as_str())
                                .unwrap_or("sentinel-ui")
                                .to_string();
                            let ev_status = if status_str.eq_ignore_ascii_case("error")
                                || status_str.eq_ignore_ascii_case("denied")
                                || status_str.eq_ignore_ascii_case("failed")
                            {
                                "err"
                            } else {
                                "ok"
                            };
                            let _ = tx.send(IntentEvent {
                                timestamp: Utc::now().format("%H:%M:%S").to_string(),
                                action,
                                source,
                                target,
                                status: ev_status.to_string(),
                                payload: val,
                            }).await;
                        }
                    }
                    Some(_) => {}
                    None => break,
                }
            }
            _ = ping_interval.tick() => {
                if writer.write_all(b"{\"type\":\"ping\"}\n").await.is_err() {
                    break;
                }
            }
        }
    }

    Ok(())
}

#[cfg(not(target_os = "linux"))]
async fn connect_and_stream(tx: &mpsc::Sender<IntentEvent>) -> Result<()> {
    // On Windows host: emit synthetic events every 3s for UI development
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
        let _ = tx.send(ev).await;
        i += 1;
    }
}
