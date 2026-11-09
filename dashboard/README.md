# Sentinel Control Center — Rust/Slint Dashboard

Collapsible desktop dock UI for Elenchus OS, built with [Slint](https://slint.dev/) and Tokio.

## Design System: Obsidian Lumina

| Token | Value | Usage |
|---|---|---|
| `bg-root` | `#0b0d16` | Deepest background |
| `bg-surface` | `#111422` | Cards, panels |
| `accent-start` | `#6366f1` | Indigo gradient start |
| `accent-end` | `#8b5cf6` | Violet gradient end |

## Dock States

| State | Height | Content |
|---|---|---|
| Collapsed | 46 px | Logo · CPU bar · RAM bar · Service pills · ▲ |
| Panel | 320 px | Nav sidebar · System gauges · Quick prompt |
| Console | 580 px | Tab strip · Markdown chat · Model Manager · Intent Monitor · Live Log |

## Building (Development — Windows host)

```powershell
cd dashboard
cargo build
cargo run
```

The Windows host build uses mock data (no VM required). Service status shows "RUNNING", telemetry returns fixed values, and the Intent Bus emits synthetic events every 3 seconds.

## Building (Production — Alpine Linux musl)

```bash
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl
```

Output binary: `target/x86_64-unknown-linux-musl/release/elenchus-dashboard`
Expected RSS: **~14 MB** (vs ~65 MB for the Python GTK3 original).

## Directory Structure

```
dashboard/
├── Cargo.toml               # Rust manifest
├── build.rs                 # Slint compile step
├── elenchus-dashboard.desktop
├── ui/
│   ├── main.slint           # Root SentinelDock window
│   ├── theme.slint          # Obsidian Lumina design tokens
│   ├── components/
│   │   ├── metric_gauge.slint
│   │   ├── service_badge.slint
│   │   ├── chat_bubble.slint
│   │   └── inline_bar.slint
│   └── views/
│       ├── dock_bar.slint   # Collapsed 46px strip
│       ├── panel.slint      # 320px panel
│       ├── console.slint    # 580px full console
│       ├── model_manager.slint
│       ├── intent_monitor.slint
│       └── live_log.slint
└── src/
    ├── main.rs              # Entry point, Slint↔Rust wiring
    ├── telemetry.rs         # /proc/stat CPU + RAM + disk
    ├── services.rs          # OpenRC service queries
    ├── markdown.rs          # pulldown-cmark parser
    ├── intent_client.rs     # Unix socket Intent Bus client
    ├── models.rs            # GGUF scanner + llama-server health
    └── llm.rs               # Streaming completions
```

## Running Tests

```bash
cargo test                           # Rust unit tests (markdown parser etc.)
cd .. && python -m pytest tests/ -q  # Python harness tests (must stay 83/83)
```

