// ============================================================
// main.rs — Sentinel Control Center entry point
// Elenchus OS · Obsidian Lumina design system
// ============================================================
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod telemetry;
mod services;
mod markdown;
mod intent_client;
mod models;
mod llm;

use std::sync::{Arc, Mutex};
use anyhow::Result;
use chrono::Utc;
use slint::SharedString;
use tokio::sync::mpsc;

// Pull in the generated Slint types
slint::include_modules!();

// ── App state shared between async tasks and UI ─────────────
#[derive(Default)]
struct AppState {
    messages: Vec<ChatMessage>,
    intent_events: Vec<IntentEvent>,
    log_lines: Vec<LogLine>,
    models: Vec<ModelEntry>,
    is_thinking: bool,
    active_model: String,
}

type SharedState = Arc<Mutex<AppState>>;

fn now_ts() -> String {
    Utc::now().format("%H:%M:%S").to_string()
}

fn initial_dock_state() -> DockState {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--prompt" || a == "--console") {
        DockState::Console
    } else if args.iter().any(|a| a == "--dock" || a == "--collapsed") {
        DockState::Collapsed
    } else {
        DockState::Panel
    }
}

fn main() -> Result<()> {
    env_logger::init();

    // Build the Tokio runtime
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;

    // Create the Slint window on the main thread
    let window = SentinelDock::new()?;
    let start_state = initial_dock_state();
    window.set_state(start_state);
    if start_state == DockState::Console {
        window.set_active_tab(ConsoleTab::Chat);
    }
    #[cfg(target_os = "linux")]
    window.window().set_position(slint::LogicalPosition::new(360.0, 48.0));

    let weak = window.as_weak();

    let state: SharedState = Arc::new(Mutex::new(AppState {
        active_model: "Loading…".to_string(),
        ..Default::default()
    }));

    // ── Seed model list ─────────────────────────────────────
    {
        let raw = models::scan_models();
        let mut st = state.lock().unwrap();
        st.models = raw.iter().map(|m| ModelEntry {
            name:    m.name.clone().into(),
            path:    m.path.clone().into(),
            size_gb: format!("{:.1}", m.size_gb).into(),
            active:  m.active,
        }).collect();
    }
    push_models_to_ui(&weak, &state);

    // ── Seed welcome message ────────────────────────────────
    {
        let mut st = state.lock().unwrap();
        st.messages.push(ChatMessage {
            kind:      1, // Agent
            content:   "Sentinel ready. Ask a question, or prefix with `/do ` to dispatch an OS task via `agent-cli`.".into(),
            timestamp: now_ts().into(),
            streaming: false,
        });
    }
    push_messages_to_ui(&weak, &state);

    // ── Telemetry loop ───────────────────────────────────────
    {
        let weak2 = weak.clone();
        rt.spawn(async move {
            loop {
                if let Ok(t) = telemetry::read().await {
                    let w = weak2.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(win) = w.upgrade() {
                            win.set_cpu_usage(t.cpu_usage);
                            win.set_ram_usage(t.ram_usage);
                            win.set_disk_usage(t.disk_usage);
                        }
                    });
                }
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            }
        });
    }

    // ── Service status loop ──────────────────────────────────
    {
        let weak2 = weak.clone();
        rt.spawn(async move {
            loop {
                if let Ok((ai, llama, lp)) = services::check_all().await {
                    let w = weak2.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(win) = w.upgrade() {
                            win.set_ai_agent_status(slint_service_status(ai));
                            win.set_llama_status(slint_service_status(llama));
                            win.set_lightpanda_status(slint_service_status(lp));
                        }
                    });
                }
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            }
        });
    }

    // ── Active model query loop ──────────────────────────────
    {
        let weak2  = weak.clone();
        let state2 = state.clone();
        rt.spawn(async move {
            loop {
                let name = models::active_model_name(llm::LLAMA_ENDPOINT).await;
                {
                    let mut st = state2.lock().unwrap();
                    st.active_model = name.clone();
                    for m in &mut st.models {
                        m.active = m.name.as_str() == name;
                    }
                }
                let w = weak2.clone();
                let n = SharedString::from(name);
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(win) = w.upgrade() {
                        win.set_active_model(n);
                    }
                });
                push_models_to_ui(&weak2, &state2);
                tokio::time::sleep(std::time::Duration::from_secs(10)).await;
            }
        });
    }

    // ── Intent Bus subscription (inside runtime context!) ─────
    {
        let weak2  = weak.clone();
        let state2 = state.clone();
        let rt_handle = rt.handle().clone();
        rt.spawn(async move {
            let mut rx = {
                let _guard = rt_handle.enter();
                intent_client::subscribe()
            };
            while let Some(ev) = rx.recv().await {
                {
                    let mut st = state2.lock().unwrap();
                    st.intent_events.push(IntentEvent {
                        timestamp: ev.timestamp.clone().into(),
                        action:    ev.action.clone().into(),
                        source:    ev.source.clone().into(),
                        target:    ev.target.clone().into(),
                        status:    ev.status.clone().into(),
                    });
                    // Keep ring buffer <= 200 entries
                    if st.intent_events.len() > 200 {
                        st.intent_events.remove(0);
                    }
                }
                push_intents_to_ui(&weak2, &state2);
            }
        });
    }

    // ── System / agent log tail loop ─────────────────────────
    {
        let weak2  = weak.clone();
        let state2 = state.clone();
        rt.spawn(async move {
            loop {
                let lines = read_recent_logs();
                {
                    let mut st = state2.lock().unwrap();
                    st.log_lines = lines;
                }
                push_logs_to_ui(&weak2, &state2);
                tokio::time::sleep(std::time::Duration::from_secs(4)).await;
            }
        });
    }

    // ── Callbacks from UI ────────────────────────────────────

    // Chat message send
    {
        let weak2  = weak.clone();
        let state2 = state.clone();
        let rt_handle = rt.handle().clone();
        window.on_send_chat_message(move |text| {
            let text = text.to_string();
            if text.trim().is_empty() { return; }

            // Add user bubble
            {
                let mut st = state2.lock().unwrap();
                st.messages.push(ChatMessage {
                    kind:      0, // User
                    content:   text.clone().into(),
                    timestamp: now_ts().into(),
                    streaming: false,
                });
            }
            push_messages_to_ui(&weak2, &state2);
            set_thinking(&weak2, &state2, true);

            let state3 = state2.clone();
            let weak3  = weak2.clone();
            let prompt  = text.clone();

            rt_handle.spawn(async move {
                let trimmed = prompt.trim();
                let os_cmd = trimmed
                    .strip_prefix("/do ")
                    .or_else(|| trimmed.strip_prefix("/agent "));

                if let Some(task_query) = os_cmd {
                    let result = llm::dispatch_agent_cli(task_query).await;
                    let (kind, msg) = match result {
                        Ok(out) => (3, out), // ToolResult
                        Err(e)  => (3, format!("agent-cli error: {e}")),
                    };
                    {
                        let mut st = state3.lock().unwrap();
                        st.messages.push(ChatMessage {
                            kind,
                            content:   msg.into(),
                            timestamp: now_ts().into(),
                            streaming: false,
                        });
                    }
                    push_messages_to_ui(&weak3, &state3);
                    set_thinking(&weak3, &state3, false);
                    return;
                }

                let (tx, mut rx) = mpsc::channel::<String>(64);

                // Add empty agent bubble (streaming)
                let bubble_idx = {
                    let mut st = state3.lock().unwrap();
                    let idx = st.messages.len();
                    st.messages.push(ChatMessage {
                        kind:      1, // Agent
                        content:   "".into(),
                        timestamp: now_ts().into(),
                        streaming: true,
                    });
                    idx
                };

                // Stream tokens
                let stream_fut = llm::stream_completion(&prompt, tx);
                let collect_fut = async {
                    while let Some(token) = rx.recv().await {
                        {
                            let mut st = state3.lock().unwrap();
                            if let Some(bubble) = st.messages.get_mut(bubble_idx) {
                                let mut new_content = bubble.content.to_string();
                                new_content.push_str(&token);
                                bubble.content = new_content.into();
                            }
                        }
                        push_messages_to_ui(&weak3, &state3);
                    }
                };

                let (stream_res, _) = tokio::join!(stream_fut, collect_fut);

                // Finalise: stop streaming flag & format markdown
                {
                    let mut st = state3.lock().unwrap();
                    if let Some(bubble) = st.messages.get_mut(bubble_idx) {
                        if let Err(err) = stream_res {
                            if bubble.content.is_empty() {
                                bubble.content = format!("LLM error: {err}").into();
                            }
                        } else if !bubble.content.is_empty() {
                            let blocks = markdown::parse(bubble.content.as_str());
                            let rendered = markdown::to_plain(&blocks);
                            if !rendered.is_empty() {
                                bubble.content = rendered.into();
                            }
                        }
                        bubble.streaming = false;
                    }
                }
                push_messages_to_ui(&weak3, &state3);
                set_thinking(&weak3, &state3, false);
            });
        });
    }

    // Model load
    {
        let state2 = state.clone();
        let weak2  = weak.clone();
        window.on_load_model(move |path| {
            let path = path.to_string();
            log::info!("Loading model: {path}");
            {
                let mut st = state2.lock().unwrap();
                for m in &mut st.models {
                    m.active = m.path.as_str() == path;
                }
                if let Some(name) = path.rsplit('/').next() {
                    st.active_model = name.to_string();
                }
            }
            push_models_to_ui(&weak2, &state2);
        });
    }

    // Model unload
    {
        let state2 = state.clone();
        let weak2  = weak.clone();
        window.on_unload_model(move || {
            let mut st = state2.lock().unwrap();
            for m in &mut st.models {
                m.active = false;
            }
            push_models_to_ui(&weak2, &state2);
        });
    }

    // Clipboard
    window.on_copy_to_clipboard(move |text| {
        #[cfg(not(target_os = "linux"))]
        {
            use arboard::Clipboard;
            if let Ok(mut cb) = Clipboard::new() {
                let _ = cb.set_text(text.to_string());
            }
        }
        #[cfg(target_os = "linux")]
        {
            let _ = std::process::Command::new("xclip")
                .args(["-selection", "clipboard"])
                .stdin(std::process::Stdio::piped())
                .spawn()
                .and_then(|mut c| {
                    use std::io::Write;
                    if let Some(stdin) = c.stdin.as_mut() {
                        let _ = stdin.write_all(text.as_bytes());
                    }
                    c.wait()
                });
        }
    });

    // State change log
    window.on_state_changed(|state| {
        log::debug!("Dock state -> {:?}", state as i32);
    });

    // Close requested
    window.on_close_requested(|| {
        let _ = slint::quit_event_loop();
    });

    // Run the event loop (blocks until window closes)
    window.run()?;
    Ok(())
}

// ── Helpers ─────────────────────────────────────────────────

fn slint_service_status(s: services::ServiceStatus) -> ServiceStatus {
    match s {
        services::ServiceStatus::Running  => ServiceStatus::Running,
        services::ServiceStatus::Stopped  => ServiceStatus::Stopped,
        services::ServiceStatus::Checking => ServiceStatus::Checking,
    }
}

fn read_recent_logs() -> Vec<LogLine> {
    #[cfg(target_os = "linux")]
    {
        let mut out = Vec::new();
        let date_str = Utc::now().format("%Y-%m-%d").to_string();
        let jsonl_path = format!("/var/ai-agent/logs/agent_responses/responses_{date_str}.jsonl");

        if let Ok(raw) = std::fs::read_to_string(&jsonl_path) {
            for line in raw.lines().rev().take(60).collect::<Vec<_>>().into_iter().rev() {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                    let ts = v.get("timestamp")
                        .and_then(|x| x.as_str())
                        .and_then(|s| s.split('T').nth(1))
                        .map(|s| s.chars().take(8).collect::<String>())
                        .unwrap_or_else(|| "00:00:00".to_string());
                    let comm = v.get("comm").and_then(|x| x.as_str()).unwrap_or("kernel");
                    let q = v.get("query").and_then(|x| x.as_str()).unwrap_or("");
                    let r = v.get("response").and_then(|x| x.as_str()).unwrap_or("");
                    if !q.is_empty() || !r.is_empty() {
                        let msg = if r.is_empty() { q.to_string() } else { format!("{q} -> {r}") };
                        out.push(LogLine {
                            timestamp: ts.into(),
                            level: "INFO".into(),
                            source: comm.into(),
                            message: msg.into(),
                        });
                    }
                }
            }
        }

        if out.is_empty() {
            if let Ok(raw) = std::fs::read_to_string("/var/log/task_runner.log") {
                for line in raw.lines().rev().take(60).collect::<Vec<_>>().into_iter().rev() {
                    let level = if line.contains("ERROR") {
                        "ERROR"
                    } else if line.contains("WARN") {
                        "WARN"
                    } else {
                        "INFO"
                    };
                    out.push(LogLine {
                        timestamp: now_ts().into(),
                        level: level.into(),
                        source: "task-runner".into(),
                        message: line.trim().into(),
                    });
                }
            }
        }

        if out.is_empty() {
            out.push(LogLine {
                timestamp: now_ts().into(),
                level: "INFO".into(),
                source: "sentinel".into(),
                message: "Listening for Elenchus syscall & task events...".into(),
            });
        }
        out
    }

    #[cfg(not(target_os = "linux"))]
    {
        vec![
            LogLine {
                timestamp: now_ts().into(),
                level: "INFO".into(),
                source: "ai-agent".into(),
                message: "Netlink syscall 548 listener active".into(),
            },
            LogLine {
                timestamp: now_ts().into(),
                level: "INFO".into(),
                source: "llama-srv".into(),
                message: "Loaded os_planner_lora.gguf (ctx=2048)".into(),
            },
        ]
    }
}

fn push_messages_to_ui(weak: &slint::Weak<SentinelDock>, state: &SharedState) {
    let msgs: Vec<ChatMessage> = state.lock().unwrap().messages.clone();
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(win) = w.upgrade() {
            let model = std::rc::Rc::new(slint::VecModel::from(msgs));
            win.set_messages(slint::ModelRc::new(model));
        }
    });
}

fn push_intents_to_ui(weak: &slint::Weak<SentinelDock>, state: &SharedState) {
    let evs: Vec<IntentEvent> = state.lock().unwrap().intent_events.clone();
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(win) = w.upgrade() {
            let model = std::rc::Rc::new(slint::VecModel::from(evs));
            win.set_intent_events(slint::ModelRc::new(model));
        }
    });
}

fn push_models_to_ui(weak: &slint::Weak<SentinelDock>, state: &SharedState) {
    let ms: Vec<ModelEntry> = state.lock().unwrap().models.clone();
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(win) = w.upgrade() {
            let model = std::rc::Rc::new(slint::VecModel::from(ms));
            win.set_models(slint::ModelRc::new(model));
        }
    });
}

fn push_logs_to_ui(weak: &slint::Weak<SentinelDock>, state: &SharedState) {
    let ls: Vec<LogLine> = state.lock().unwrap().log_lines.clone();
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(win) = w.upgrade() {
            let model = std::rc::Rc::new(slint::VecModel::from(ls));
            win.set_log_lines(slint::ModelRc::new(model));
        }
    });
}

fn set_thinking(weak: &slint::Weak<SentinelDock>, state: &SharedState, thinking: bool) {
    {
        state.lock().unwrap().is_thinking = thinking;
    }
    let w = weak.clone();
    let _ = slint::invoke_from_event_loop(move || {
        if let Some(win) = w.upgrade() {
            win.set_is_thinking(thinking);
        }
    });
}
