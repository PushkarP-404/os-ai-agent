# Sentinel Control Center — Rust/Slint Dashboard

Collapsible desktop dock UI for **Elenchus OS**, built with [Slint](https://slint.dev/) and Tokio (`13.4 MB` Alpine binary, `~15.6 MB` live VM RSS vs `~65 MB` for the original Python GTK3 UI).

---

## Design System: Obsidian Lumina

| Token | Value | Usage |
|---|---|---|
| `bg-root` | `#0b0d16` | Deepest background |
| `bg-surface` | `#111422` | Cards, sidebar, tab strip |
| `bg-elevated` | `#181b2e` | Service card, prompt bar |
| `accent-start` | `#6366f1` | Indigo gradient start |
| `accent-end` | `#8b5cf6` | Violet gradient end |

## Dock States (`560 px` wide)

| State | Size | Trigger Flag | Content |
|---|---|---|---|
| **Collapsed** | `560 × 46 px` | `--dock` / `--collapsed` | Logo · CPU bar · RAM bar · Service pills (`ai-agent`, `llama`) · ▲ · × |
| **Panel** *(default)* | `560 × 360 px` | *(default)* | Nav sidebar · SVG arc gauges (CPU, RAM, Disk) · Services (`ai-agent`, `llama-server`, `lightpanda`) · Quick prompt |
| **Console** | `560 × 580 px` | `--prompt` / `--console` | Tab strip (`Chat`, `Models`, `Intents`, `Live Log`) · ChatML/markdown streaming LLM chat · `/do <task>` OS dispatch · GGUF model list · Live Intent Bus monitor · Log stream |

---

## Starting the GUI VM & Launching Sentinel

From `c:\qemu-alpine`:

```powershell
# Recommended GUI boot (4 GB RAM, 4 vCPUs, SDL virtio-vga display, SSH on port 2222)
powershell -ExecutionPolicy Bypass -File c:\qemu-alpine\boot_gui.ps1

# Or via master launcher inside either c:\qemu-alpine or c:\qemu-alpine\os-ai-agent:
.\boot.ps1 -Gui

# Low-resource GUI boot (2 GB RAM, 2 vCPUs, QXL display):
powershell -ExecutionPolicy Bypass -File c:\qemu-alpine\boot_lowres.ps1
```

On **Linux / macOS**:

```bash
cd os-ai-agent
GUI=1 ./boot.sh
# or:
./boot.sh --gui
```

Once the XFCE4 desktop autologins as `aiuser`:
- The **Collapsed Dock** (`--dock`) starts automatically via `~/.config/autostart/elenchus-dashboard.desktop`.
- Launch manually or via shortcut (`<Super>Space`, `<Alt>Space`, Desktop icon, or `agent-cli "open dashboard"`):
  ```bash
  ai-agent-dashboard            # Opens Panel state (360 px)
  ai-agent-dashboard --dock     # Opens Collapsed Dock strip (46 px)
  ai-agent-dashboard --prompt   # Opens Full AI Console (580 px)
  ```
- Prefix chat messages with `/do ` or `/agent ` to dispatch OS automation intents through `agent-cli` instead of plain LLM chat.

> Note: Inside headless/TCG QEMU guests, `ai-agent-dashboard` defaults to `SLINT_BACKEND=winit-software` for reliable CPU framebuffer rendering over X11. On bare-metal GPU desktops you can set `SLINT_BACKEND=winit-femtovg` for OpenGL acceleration.

---

## Building (Development — Windows host)

```powershell
cd dashboard
cargo build
cargo run
```

The Windows host build provides fallback telemetry, service status, and synthetic Intent Bus events every 3 seconds when Linux `/proc` and `/var/ai-agent/intent.sock` are unavailable.

## Building (Production — Alpine Linux 3.20 musl)

Because `winit` / `xkbcommon-dl` / OpenGL load X11 libraries via `dlopen` at runtime, compile on Alpine with dynamic `musl` linking (`-C target-feature=-crt-static`) and Rust `1.92+` (e.g. via `rustup`):

```sh
# Inside Alpine 3.20 container / chroot (or native Alpine host with rustup):
apk add --no-cache build-base pkgconf libx11-dev libxcb-dev libxkbcommon-dev fontconfig-dev mesa-dev
RUSTFLAGS="-C target-feature=-crt-static" cargo build --release
```

Runtime dependencies on the Alpine target:
`libgcc`, `mesa-gl`, `libxkbcommon-x11`, `libx11`, `libxcb`, `fontconfig`, `xclip`.

Installed target paths in the VM:
- Binary: `/usr/local/bin/elenchus-dashboard`
- Wrapper: `/usr/bin/ai-agent-dashboard` (legacy Python dashboard remains preserved at `/usr/lib/ai-agent/dashboard.py` and `/usr/bin/ai-agent-dashboard.py-bak`)
- Desktop & autostart: `/usr/share/applications/elenchus-dashboard.desktop`, `/home/aiuser/.config/autostart/elenchus-dashboard.desktop`

---

## Directory Structure

```
dashboard/
├── Cargo.toml                  # Rust manifest (Slint 1.8, Tokio, pulldown-cmark, reqwest w/o OpenSSL)
├── Cargo.lock                  # Locked crate versions
├── build.rs                    # Slint compile hook
├── elenchus-dashboard.desktop  # XDG desktop entry
├── dashboard.py                # Legacy Python/GTK3 fallback (preserved intact)
├── dashboard.css               # Legacy GTK3 stylesheet
├── ui/
│   ├── main.slint              # Root SentinelDock window & state machine
│   ├── theme.slint             # Obsidian Lumina design tokens
│   ├── components/
│   │   ├── metric_gauge.slint  # SVG ArcTo circular gauges
│   │   ├── service_badge.slint # Service status pill
│   │   ├── chat_bubble.slint   # Role-styled chat bubbles
│   │   └── inline_bar.slint    # Compact resource bars for collapsed dock
│   └── views/
│       ├── dock_bar.slint      # Collapsed 46px strip
│       ├── panel.slint         # 360px overview & quick-prompt panel
│       ├── console.slint       # 580px tabbed full console
│       ├── model_manager.slint # GGUF model list & active switcher
│       ├── intent_monitor.slint# Intent Bus table
│       └── live_log.slint      # Tail view for agent responses & task runner logs
└── src/
    ├── main.rs                 # Entry point, CLI flags, Slint↔Tokio wiring
    ├── telemetry.rs            # /proc/stat CPU, /proc/meminfo RAM, statvfs Disk
    ├── services.rs             # OpenRC rc-service status checks
    ├── markdown.rs             # pulldown-cmark block parser
    ├── intent_client.rs        # /var/ai-agent/intent.sock register & ping client
    ├── models.rs               # /var/lib/ai-agent/models GGUF scanner + /props query
    └── llm.rs                  # ChatML streaming completions + /do agent-cli dispatch
```

## Running Tests

```powershell
cargo test --manifest-path dashboard/Cargo.toml   # Rust unit tests (markdown parser etc.)
python -m pytest tests/ -q                        # Python harness suite (83/83)
```
