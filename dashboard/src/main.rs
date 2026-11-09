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
    #[allow(dead_code)]
    log_lines: Vec<LogLine>,
    models: Vec<ModelEntry>,
    is_thinking: bool,
    active_model: String,
}

type SharedState = Arc<Mutex<AppState>>;

fn now_ts() -> String {
    Utc::now().format("%H:%M:%S").to_string()
}

fn main() -> Result<()> {
    env_logger::init();

    // Build the Tokio runtime
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;

    // Create the Slint window on the main thread
    let window = SentinelDock::new()?;
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
                }
                let w = weak2.clone();
                let n = SharedString::from(name);
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(win) = w.upgrade() {
                        win.set_active_model(n);
                    }
                });
                tokio::time::sleep(std::time::Duration::from_secs(10)).await;
            }
        });
    }

    // ── Intent Bus subscription ──────────────────────────────
    {
        let weak2  = weak.clone();
        let state2 = state.clone();
        let mut rx = intent_client::subscribe();
        rt.spawn(async move {
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
                    // Keep ring buffer ≤ 200 entries
                    if st.intent_events.len() > 200 {
                        st.intent_events.remove(0);
                    }
                }
                push_intents_to_ui(&weak2, &state2);
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

            // Set thinking indicator
            set_thinking(&weak2, &state2, true);

            // Spawn LLM streaming task
            let state3 = state2.clone();
            let weak3  = weak2.clone();
            let prompt  = text.clone();
            rt_handle.spawn(async move {
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

                let _ = tokio::join!(stream_fut, collect_fut);

                // Finalise: stop streaming flag
                {
                    let mut st = state3.lock().unwrap();
                    if let Some(bubble) = st.messages.get_mut(bubble_idx) {
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
        window.on_load_model(move |path| {
            let path = path.to_string();
            log::info!("Loading model: {path}");
            let mut st = state2.lock().unwrap();
            for m in &mut st.models {
                m.active = m.path.to_string() == path;
            }
        });
    }

    // Model unload
    {
        let state2 = state.clone();
        window.on_unload_model(move || {
            let mut st = state2.lock().unwrap();
            for m in &mut st.models {
                m.active = false;
            }
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
            // On Linux use xclip as fallback (arboard needs X11 running)
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
        log::debug!("Dock state → {:?}", state as i32);
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

