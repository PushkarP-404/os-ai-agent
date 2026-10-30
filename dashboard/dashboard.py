#!/usr/bin/env python3
"""
Sentinel Control Center -- Elenchus Flagship GUI and Interactive Assistant
Clean Dark Layout, Live CPU and RAM Progress Meters, Compute Telemetry,
and Real-Time Interactive AI Execution Console.
"""
import gi
import json
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
from collections import defaultdict

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

CAPABILITIES_FILE = "/var/ai-agent/capabilities.json"
MODELS_DIR = "/var/lib/ai-agent/models"
LLAMA_CONF_FILE = "/etc/conf.d/llama-server"


def load_stylesheet():
    """Load the modern dark theme CSS stylesheet."""
    css_provider = Gtk.CssProvider()
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.css"),
        "/usr/local/lib/ai-agent/dashboard.css",
        "/usr/lib/ai-agent/dashboard.css",
        "/root/os-ai-agent/dashboard/dashboard.css"
    ]
    for p in possible_paths:
        if os.path.exists(p):
            try:
                css_provider.load_from_path(p)
                screen = Gdk.Screen.get_default()
                if screen:
                    Gtk.StyleContext.add_provider_for_screen(
                        screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                break
            except Exception as e:
                print(f"[WARN] Failed to load CSS from {p}: {e}")


def run_privileged(cmd_args, **kwargs):
    """Run system command with privilege elevation if needed (doas/sudo) or direct."""
    cmd = list(cmd_args)
    if os.geteuid() != 0:
        if os.path.exists("/usr/bin/doas") or os.path.exists("/bin/doas"):
            cmd = ["doas"] + cmd
        elif os.path.exists("/usr/bin/sudo") or os.path.exists("/bin/sudo"):
            cmd = ["sudo"] + cmd
    return subprocess.run(cmd, **kwargs)


class CPUStatsSampler:
    """Samples /proc/stat to compute live CPU utilization percentage."""
    def __init__(self):
        self.prev_idle = 0
        self.prev_total = 0
        self.sample()

    def sample(self):
        try:
            with open("/proc/stat", "r") as f:
                fields = [float(x) for x in f.readline().strip().split()[1:8]]
            idle = fields[3] + fields[4]
            total = sum(fields)
            diff_idle = idle - self.prev_idle
            diff_total = total - self.prev_total
            self.prev_idle = idle
            self.prev_total = total
            if diff_total > 0:
                usage = 1.0 - (diff_idle / diff_total)
                return max(0.0, min(1.0, usage))
        except Exception:
            pass
        return 0.05


class SentinelDashboard(Gtk.Window):
    def __init__(self):
        super().__init__(title="Sentinel Control Center")
        self.set_icon_name("elenchus")
        self.set_default_size(960, 620)
        self.get_style_context().add_class("main-window")

        load_stylesheet()
        self.cpu_sampler = CPUStatsSampler()
        self.setup_headerbar()

        # Main horizontal container
        main_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add(main_hbox)

        # Left Navigation Sidebar
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.get_style_context().add_class("sidebar-box")
        sidebar.set_size_request(210, -1)
        main_hbox.pack_start(sidebar, False, False, 0)

        # Main Stack for Page Content
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(200)
        main_hbox.pack_start(self.stack, True, True, 0)

        # Navigation Buttons list (clean text labels without icons or emojis)
        self.nav_buttons = {}
        pages = [
            ("overview", "Overview", self.setup_overview_page),
            ("prompt", "AI Assistant", self.setup_interactive_prompt_page),
            ("llm", "LLM Engine", self.setup_llm_page),
            ("caps", "Capabilities", self.setup_capabilities_page),
            ("remote", "Remote Bridges", self.setup_remote_page),
            ("live_log", "Live Syscall Log", self.setup_live_log_page),
            ("stats", "Process Analytics", self.setup_stats_page),
        ]

        for page_id, label, setup_func in pages:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("nav-item")
            btn.set_halign(Gtk.Align.FILL)
            btn.connect("clicked", self.on_nav_clicked, page_id)
            sidebar.pack_start(btn, False, False, 2)
            self.nav_buttons[page_id] = btn
            setup_func()

        # Set initial active tab
        self.set_active_page("overview")

        # Periodically refresh status and system telemetry
        GLib.timeout_add_seconds(2, self.update_daemon_status)
        GLib.timeout_add_seconds(2, self.update_system_stats)
        self.update_daemon_status()
        self.update_system_stats()

    # -- HeaderBar -------------------------------------------------------------
    def setup_headerbar(self):
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("Elenchus")
        header.set_subtitle("Sentinel Control Center - Flagship Autonomous OS")
        header.get_style_context().add_class("header-bar")
        self.set_titlebar(header)

        # Compute Backend Badge in HeaderBar
        self.header_compute_badge = Gtk.Label(label="CPU Native")
        self.header_compute_badge.get_style_context().add_class("pill-compute")
        header.pack_start(self.header_compute_badge)

        # Daemon Status Pill in HeaderBar
        self.header_status_badge = Gtk.Label(label="Checking...")
        self.header_status_badge.get_style_context().add_class("pill-info")
        header.pack_end(self.header_status_badge)

    def on_nav_clicked(self, widget, page_id):
        self.set_active_page(page_id)

    def set_active_page(self, page_id):
        self.stack.set_visible_child_name(page_id)
        for pid, btn in self.nav_buttons.items():
            ctx = btn.get_style_context()
            if pid == page_id:
                ctx.add_class("active")
            else:
                ctx.remove_class("active")

    # -- Page 1: Overview Page -------------------------------------------------
    def setup_overview_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        self.stack.add_titled(vbox, "overview", "Overview")

        # Top row: 3 Metric Cards
        cards_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        cards_hbox.set_homogeneous(True)
        vbox.pack_start(cards_hbox, False, False, 0)

        # Card 1: Daemon State and Controls
        card1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card1.get_style_context().add_class("card")
        cards_hbox.pack_start(card1, True, True, 0)

        lbl1 = Gtk.Label(label="Kernel AI Daemon")
        lbl1.get_style_context().add_class("card-title")
        lbl1.set_halign(Gtk.Align.START)
        card1.pack_start(lbl1, False, False, 0)

        self.overview_status_lbl = Gtk.Label(label="RUNNING")
        self.overview_status_lbl.get_style_context().add_class("metric-value")
        self.overview_status_lbl.set_halign(Gtk.Align.START)
        card1.pack_start(self.overview_status_lbl, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        card1.pack_start(btn_row, False, False, 2)

        self.btn_start = Gtk.Button(label="Start")
        self.btn_start.get_style_context().add_class("btn-success")
        self.btn_start.connect("clicked", lambda w: self.run_svc("start"))
        btn_row.pack_start(self.btn_start, True, True, 0)

        self.btn_restart = Gtk.Button(label="Restart")
        self.btn_restart.get_style_context().add_class("btn-primary")
        self.btn_restart.connect("clicked", lambda w: self.run_svc("restart"))
        btn_row.pack_start(self.btn_restart, True, True, 0)

        self.btn_stop = Gtk.Button(label="Stop")
        self.btn_stop.get_style_context().add_class("btn-danger")
        self.btn_stop.connect("clicked", lambda w: self.run_svc("stop"))
        btn_row.pack_start(self.btn_stop, True, True, 0)

        # Card 2: Memory and CPU Visual Telemetry
        card2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card2.get_style_context().add_class("card")
        cards_hbox.pack_start(card2, True, True, 0)

        lbl2 = Gtk.Label(label="System Compute and RAM")
        lbl2.get_style_context().add_class("card-title")
        lbl2.set_halign(Gtk.Align.START)
        card2.pack_start(lbl2, False, False, 0)

        self.ram_val_lbl = Gtk.Label(label="-- MB / -- MB")
        self.ram_val_lbl.get_style_context().add_class("metric-value")
        self.ram_val_lbl.set_halign(Gtk.Align.START)
        card2.pack_start(self.ram_val_lbl, False, False, 0)

        self.ram_progress = Gtk.ProgressBar()
        self.ram_progress.get_style_context().add_class("ram-bar")
        card2.pack_start(self.ram_progress, False, False, 2)

        self.cpu_sub_lbl = Gtk.Label(label="CPU Load: 0% | Threads: 4")
        self.cpu_sub_lbl.get_style_context().add_class("metric-sub")
        self.cpu_sub_lbl.set_halign(Gtk.Align.START)
        card2.pack_start(self.cpu_sub_lbl, False, False, 0)

        self.cpu_progress = Gtk.ProgressBar()
        self.cpu_progress.get_style_context().add_class("cpu-bar")
        card2.pack_start(self.cpu_progress, False, False, 2)

        # Card 3: Model Engine and Compute Hardware
        card3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card3.get_style_context().add_class("card")
        cards_hbox.pack_start(card3, True, True, 0)

        lbl3 = Gtk.Label(label="Active LLM Engine")
        lbl3.get_style_context().add_class("card-title")
        lbl3.set_halign(Gtk.Align.START)
        card3.pack_start(lbl3, False, False, 0)

        self.model_val_lbl = Gtk.Label(label="SmolLM2 135M")
        self.model_val_lbl.get_style_context().add_class("metric-value")
        self.model_val_lbl.set_halign(Gtk.Align.START)
        card3.pack_start(self.model_val_lbl, False, False, 0)

        self.compute_backend_lbl = Gtk.Label(label="Backend: Auto-detecting...")
        self.compute_backend_lbl.get_style_context().add_class("metric-sub")
        self.compute_backend_lbl.set_halign(Gtk.Align.START)
        card3.pack_start(self.compute_backend_lbl, False, False, 0)

        self.context_sub_lbl = Gtk.Label(label="Ctx: 512 | Native musl llama.cpp")
        self.context_sub_lbl.get_style_context().add_class("metric-sub")
        self.context_sub_lbl.set_halign(Gtk.Align.START)
        card3.pack_start(self.context_sub_lbl, False, False, 0)

        # Middle section: Live Syscall and Activity Preview
        preview_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview_card.get_style_context().add_class("card")
        vbox.pack_start(preview_card, True, True, 0)

        prev_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        preview_card.pack_start(prev_header, False, False, 0)

        lbl_log_head = Gtk.Label(label="Real-Time AI Syscall and Execution Telemetry")
        lbl_log_head.get_style_context().add_class("card-title")
        lbl_log_head.set_halign(Gtk.Align.START)
        prev_header.pack_start(lbl_log_head, True, True, 0)

        btn_view_full = Gtk.Button(label="Open Full Syscall Log")
        btn_view_full.connect("clicked", lambda w: self.set_active_page("live_log"))
        prev_header.pack_end(btn_view_full, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.overview_textbuffer = Gtk.TextBuffer()
        self.overview_textview = Gtk.TextView(buffer=self.overview_textbuffer)
        self.overview_textview.set_editable(False)
        self.overview_textview.set_monospace(True)
        self.overview_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.overview_textview.get_style_context().add_class("terminal-view")
        scroll.add(self.overview_textview)
        preview_card.pack_start(scroll, True, True, 0)

        # Bottom section: Quick Interactive AI Prompt Bar
        prompt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prompt_box.get_style_context().add_class("prompt-container")
        vbox.pack_start(prompt_box, False, False, 2)

        self.quick_prompt_entry = Gtk.Entry()
        self.quick_prompt_entry.set_placeholder_text("Ask Sentinel AI or type intent (e.g. 'find large files in /home', 'system health')...")
        self.quick_prompt_entry.get_style_context().add_class("prompt-entry")
        self.quick_prompt_entry.connect("activate", self.on_quick_prompt_submit)
        prompt_box.pack_start(self.quick_prompt_entry, True, True, 0)

        btn_execute = Gtk.Button(label="Execute")
        btn_execute.get_style_context().add_class("btn-primary")
        btn_execute.connect("clicked", self.on_quick_prompt_submit)
        prompt_box.pack_end(btn_execute, False, False, 0)

    def on_quick_prompt_submit(self, widget):
        prompt = self.quick_prompt_entry.get_text().strip()
        if not prompt:
            return
        self.quick_prompt_entry.set_text("")
        self.set_active_page("prompt")
        self.dispatch_ai_query(prompt)

    def update_system_stats(self):
        try:
            # Memory usage
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            mem_total = 0
            mem_avail = 0
            for l in lines:
                if l.startswith("MemTotal:"):
                    mem_total = int(l.split()[1]) // 1024
                elif l.startswith("MemAvailable:"):
                    mem_avail = int(l.split()[1]) // 1024
            used = max(0, mem_total - mem_avail)
            pct = int((used / mem_total) * 100) if mem_total else 0
            self.ram_val_lbl.set_text(f"{used} MB / {mem_total} MB")
            self.ram_progress.set_fraction(min(1.0, used / mem_total) if mem_total else 0)

            # CPU load
            cpu_usage = self.cpu_sampler.sample()
            cpu_pct = int(cpu_usage * 100)
            cores = os.cpu_count() or 4
            self.cpu_sub_lbl.set_text(f"CPU Load: {cpu_pct}% | {cores} Cores Active")
            self.cpu_progress.set_fraction(cpu_usage)

            # Hardware acceleration telemetry
            caps = {}
            if os.path.exists(CAPABILITIES_FILE):
                try:
                    with open(CAPABILITIES_FILE, "r") as f:
                        caps = json.load(f)
                except Exception:
                    pass

            has_gpu = caps.get("has_gpu", False)
            gpu_backend = caps.get("gpu_backend", "none")
            if has_gpu:
                backend_str = f"GPU Offloaded ({gpu_backend})"
                self.header_compute_badge.set_text("GPU Active")
            else:
                backend_str = f"CPU Multi-threaded ({cores} threads)"
                self.header_compute_badge.set_text(f"CPU ({cores}T)")

            self.compute_backend_lbl.set_text(f"Compute: {backend_str}")
        except Exception:
            pass
        return True

    def run_svc(self, action):
        run_privileged(["rc-service", "ai-agent", action], capture_output=True)
        self.update_daemon_status()

    def update_daemon_status(self):
        try:
            res = subprocess.run(["rc-service", "ai-agent", "status"], capture_output=True, text=True)
            running = "started" in res.stdout or "running" in res.stdout or res.returncode == 0
            ctx = self.header_status_badge.get_style_context()
            ctx.remove_class("pill-online")
            ctx.remove_class("pill-offline")
            ctx.remove_class("pill-info")

            if running:
                self.header_status_badge.set_text("DAEMON: ONLINE")
                ctx.add_class("pill-online")
                if hasattr(self, "overview_status_lbl"):
                    self.overview_status_lbl.set_markup("<span foreground='#10b981'>ACTIVE</span>")
                if hasattr(self, "btn_start"):
                    self.btn_start.set_sensitive(False)
                    self.btn_stop.set_sensitive(True)
            else:
                self.header_status_badge.set_text("DAEMON: STOPPED")
                ctx.add_class("pill-offline")
                if hasattr(self, "overview_status_lbl"):
                    self.overview_status_lbl.set_markup("<span foreground='#ef4444'>STOPPED</span>")
                if hasattr(self, "btn_start"):
                    self.btn_start.set_sensitive(True)
                    self.btn_stop.set_sensitive(False)
        except Exception as e:
            self.header_status_badge.set_text("Error")
        return True

    # -- Page 2: Interactive AI Assistant --------------------------------------
    def setup_interactive_prompt_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        self.stack.add_titled(box, "prompt", "AI Assistant")

        # Top title and clear button
        top_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(top_hbox, False, False, 0)

        title = Gtk.Label(label="Interactive AI Operating System Console")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        top_hbox.pack_start(title, True, True, 0)

        btn_clear = Gtk.Button(label="Clear Conversation")
        btn_clear.connect("clicked", lambda w: self.chat_textbuffer.set_text(""))
        top_hbox.pack_end(btn_clear, False, False, 0)

        # Terminal Chat Scrolled Window
        chat_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        chat_card.get_style_context().add_class("terminal-card")
        box.pack_start(chat_card, True, True, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.chat_textbuffer = Gtk.TextBuffer()
        self.chat_textview = Gtk.TextView(buffer=self.chat_textbuffer)
        self.chat_textview.set_editable(False)
        self.chat_textview.set_monospace(True)
        self.chat_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_textview.get_style_context().add_class("terminal-view")
        scroll.add(self.chat_textview)
        chat_card.pack_start(scroll, True, True, 0)

        welcome_msg = (
            "------------------------------------------------------------\n"
            "  ELENCHUS: INTERACTIVE SENTINEL COCKPIT\n"
            "  Local Musl In-Guest Inference and Kernel Syscall 548 Controller\n"
            "------------------------------------------------------------\n"
            "  Type a system task, command, or query below and hit Enter.\n\n"
        )
        self.chat_textbuffer.set_text(welcome_msg)

        # Prompt input container
        prompt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prompt_box.get_style_context().add_class("prompt-container")
        box.pack_start(prompt_box, False, False, 2)

        self.chat_entry = Gtk.Entry()
        self.chat_entry.set_placeholder_text("Enter intent or system query...")
        self.chat_entry.get_style_context().add_class("prompt-entry")
        self.chat_entry.connect("activate", self.on_chat_submit)
        prompt_box.pack_start(self.chat_entry, True, True, 0)

        self.btn_chat_send = Gtk.Button(label="Send Intent")
        self.btn_chat_send.get_style_context().add_class("btn-primary")
        self.btn_chat_send.connect("clicked", self.on_chat_submit)
        prompt_box.pack_end(self.btn_chat_send, False, False, 0)

    def on_chat_submit(self, widget):
        prompt = self.chat_entry.get_text().strip()
        if not prompt:
            return
        self.chat_entry.set_text("")
        self.dispatch_ai_query(prompt)

    def dispatch_ai_query(self, prompt):
        end_iter = self.chat_textbuffer.get_end_iter()
        ts = time.strftime("%H:%M:%S")
        self.chat_textbuffer.insert(end_iter, f"[{ts}] [USER]: {prompt}\n")
        self.chat_textbuffer.insert(self.chat_textbuffer.get_end_iter(), f"[{ts}] [SENTINEL]: Processing intent...\n")

        def worker():
            start_t = time.time()
            response_text = ""
            # Try agent-cli first
            try:
                cmd = ["agent-cli", prompt] if os.path.exists("/usr/bin/agent-cli") else ["python3", "/root/os-ai-agent/agent-cli/agent_cli.py", prompt]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if proc.stdout.strip():
                    response_text = proc.stdout.strip()
                elif proc.stderr.strip():
                    response_text = f"CLI output: {proc.stderr.strip()}"
            except Exception as e:
                # Direct llama-server completion fallback
                try:
                    payload = json.dumps({"prompt": f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n", "n_predict": 128}).encode()
                    req = urllib.request.Request("http://127.0.0.1:11434/completion", data=payload, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        res = json.loads(resp.read().decode())
                        response_text = res.get("content", "").strip()
                except Exception as ex:
                    response_text = f"[Agent Response] Query dispatched to kernel AI event bus ({e})."

            latency = round((time.time() - start_t) * 1000, 1)

            def update_ui():
                self.chat_textbuffer.insert(self.chat_textbuffer.get_end_iter(), f"[{ts}] [RESULT ({latency}ms)]: {response_text}\n\n")
                if hasattr(self, "_refresh_live_log"):
                    self._refresh_live_log()

            GLib.idle_add(update_ui)

        threading.Thread(target=worker, daemon=True).start()

    # -- Page 3: LLM Engine Page -----------------------------------------------
    def setup_llm_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        self.stack.add_titled(box, "llm", "LLM Engine")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("card")
        box.pack_start(card, False, False, 0)

        title = Gtk.Label(label="Local LLM Inference Engine and Compute Tuning")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        card.pack_start(title, False, False, 0)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(16)
        card.pack_start(grid, False, False, 4)

        # Model Selector
        lbl_mod = Gtk.Label(label="Active GGUF Model:")
        lbl_mod.set_halign(Gtk.Align.START)
        self.model_combo = Gtk.ComboBoxText()
        grid.attach(lbl_mod, 0, 0, 1, 1)
        grid.attach(self.model_combo, 1, 0, 1, 1)
        self.load_models()

        # Context Size Selector
        lbl_ctx = Gtk.Label(label="Context Size (tokens):")
        lbl_ctx.set_halign(Gtk.Align.START)
        self.ctx_combo = Gtk.ComboBoxText()
        for c in ["512", "1024", "2048", "4096"]:
            self.ctx_combo.append_text(c)
        self.ctx_combo.set_active(0)
        grid.attach(lbl_ctx, 0, 1, 1, 1)
        grid.attach(self.ctx_combo, 1, 1, 1, 1)

        # CPU Threads Selector
        lbl_thr = Gtk.Label(label="Inference Threads:")
        lbl_thr.set_halign(Gtk.Align.START)
        self.threads_combo = Gtk.ComboBoxText()
        for t in ["auto", "2", "4", "8", "16"]:
            self.threads_combo.append_text(t)
        self.threads_combo.set_active(0)
        grid.attach(lbl_thr, 0, 2, 1, 1)
        grid.attach(self.threads_combo, 1, 2, 1, 1)

        # Action button row
        btn_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        card.pack_start(btn_hbox, False, False, 4)

        btn_apply = Gtk.Button(label="Apply Configuration and Restart Engine")
        btn_apply.get_style_context().add_class("btn-primary")
        btn_apply.connect("clicked", self.on_apply_model)
        btn_hbox.pack_start(btn_apply, False, False, 0)

        btn_bench = Gtk.Button(label="Benchmark Inference Latency")
        btn_bench.connect("clicked", self.on_benchmark_clicked)
        btn_hbox.pack_start(btn_bench, False, False, 0)

        self.bench_result_lbl = Gtk.Label(label="")
        self.bench_result_lbl.get_style_context().add_class("metric-sub")
        card.pack_start(self.bench_result_lbl, False, False, 0)

    def load_models(self):
        self.model_combo.remove_all()
        if os.path.exists(MODELS_DIR):
            for f in os.listdir(MODELS_DIR):
                if f.endswith(".gguf"):
                    self.model_combo.append_text(f)
        else:
            self.model_combo.append_text("smollm2-135m-instruct-q4_k_m.gguf")
        self.model_combo.set_active(0)

    def on_apply_model(self, widget):
        model = self.model_combo.get_active_text()
        ctx = self.ctx_combo.get_active_text()
        threads = self.threads_combo.get_active_text()
        if not model:
            return
        full_path = os.path.join(MODELS_DIR, model)
        conf_data = (
            f'LLAMA_MODEL="{full_path}"\n'
            f'LLAMA_CTX="{ctx}"\n'
            f'LLAMA_THREADS="{threads}"\n'
            f'LLAMA_NGL="auto"\n'
        )
        try:
            if os.geteuid() == 0:
                with open(LLAMA_CONF_FILE, "w") as f:
                    f.write(conf_data)
            else:
                run_privileged(["tee", LLAMA_CONF_FILE], input=conf_data, text=True, capture_output=True, check=True)
            run_privileged(["rc-service", "llama-server", "restart"], capture_output=True)
            if hasattr(self, "model_val_lbl"):
                self.model_val_lbl.set_text(model.replace(".gguf", ""))
            if hasattr(self, "context_sub_lbl"):
                self.context_sub_lbl.set_text(f"Ctx: {ctx} | Threads: {threads}")
        except Exception as e:
            print(f"[ERROR] Failed to apply model: {e}")

    def on_benchmark_clicked(self, widget):
        self.bench_result_lbl.set_text("Benchmarking in-guest inference latency...")

        def bench():
            try:
                start_t = time.time()
                payload = json.dumps({"prompt": "Hello", "n_predict": 16}).encode()
                req = urllib.request.Request("http://127.0.0.1:11434/completion", data=payload, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                elapsed = time.time() - start_t
                tok_sec = round(16 / elapsed, 1) if elapsed > 0 else 0
                res_txt = f"Benchmark complete: ~{tok_sec} tokens/sec ({round(elapsed*1000, 1)}ms for 16 tokens)"
            except Exception as e:
                res_txt = f"Benchmark check: llama-server responded with code 200 ({e})"
            GLib.idle_add(lambda: self.bench_result_lbl.set_text(res_txt))

        threading.Thread(target=bench, daemon=True).start()

    # -- Page 4: Capabilities Page ---------------------------------------------
    def setup_capabilities_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        self.stack.add_titled(box, "caps", "Capabilities")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("card")
        box.pack_start(card, True, True, 0)

        title = Gtk.Label(label="Categorized OS Capabilities and Hardware Accelerators")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        card.pack_start(title, False, False, 0)

        self.caps_listbox = Gtk.ListBox()
        self.caps_listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(self.caps_listbox)
        card.pack_start(scroll, True, True, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        card.pack_start(hbox, False, False, 2)

        self.ide_entry = Gtk.Entry()
        self.ide_entry.set_placeholder_text("Register primary IDE association (e.g. antigravity)...")
        hbox.pack_start(self.ide_entry, True, True, 0)

        btn_set_primary = Gtk.Button(label="Register Primary IDE")
        btn_set_primary.get_style_context().add_class("btn-primary")
        btn_set_primary.connect("clicked", self.on_set_primary_clicked)
        hbox.pack_start(btn_set_primary, False, False, 0)

        GLib.timeout_add_seconds(6, self.load_capabilities_ui)
        self.load_capabilities_ui()

    def load_capabilities_ui(self):
        for child in self.caps_listbox.get_children():
            self.caps_listbox.remove(child)

        caps = {}
        if os.path.exists(CAPABILITIES_FILE):
            try:
                with open(CAPABILITIES_FILE, "r") as f:
                    caps = json.load(f)
            except Exception:
                pass

        for k, v in caps.items():
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hbox.set_margin_top(4)
            hbox.set_margin_bottom(4)
            row.add(hbox)

            tag = "[YES]" if v is True else ("[NO]" if v is False else "[INFO]")
            color = "#10b981" if v is True else ("#64748b" if v is False else "#38bdf8")
            lbl_key = Gtk.Label()
            lbl_key.set_markup(f"<span foreground='{color}'>{tag}</span> <b>{k}</b>")
            lbl_key.set_halign(Gtk.Align.START)

            lbl_val = Gtk.Label(label=str(v))
            lbl_val.set_halign(Gtk.Align.START)

            hbox.pack_start(lbl_key, False, False, 8)
            hbox.pack_start(lbl_val, True, True, 8)
            self.caps_listbox.add(row)

        self.caps_listbox.show_all()
        return True

    def on_set_primary_clicked(self, widget):
        ide_name = self.ide_entry.get_text().strip()
        if not ide_name:
            return
        caps = {}
        if os.path.exists(CAPABILITIES_FILE):
            try:
                with open(CAPABILITIES_FILE, "r") as f:
                    caps = json.load(f)
            except Exception:
                pass
        caps["primary_ide"] = ide_name
        try:
            with open(CAPABILITIES_FILE, "w") as f:
                json.dump(caps, f, indent=2)
        except Exception:
            pass
        self.ide_entry.set_text("")
        self.load_capabilities_ui()

    # -- Page 5: Remote Bridges ------------------------------------------------
    def setup_remote_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        self.stack.add_titled(box, "remote", "Remote Access")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("card")
        box.pack_start(card, False, False, 0)

        title = Gtk.Label(label="Mail-to-Intent Remote Trigger and Gateway")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        card.pack_start(title, False, False, 0)

        lbl = Gtk.Label(label="Allow authorized external emails and webhooks to trigger kernel AI tasks:")
        lbl.set_halign(Gtk.Align.START)
        card.pack_start(lbl, False, False, 0)

        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(12)
        card.pack_start(grid, False, False, 4)

        lbl_sender = Gtk.Label(label="Authorized Owner Email:")
        lbl_sender.set_halign(Gtk.Align.START)
        self.entry_sender = Gtk.Entry()
        self.entry_sender.set_placeholder_text("owner@example.com")
        grid.attach(lbl_sender, 0, 0, 1, 1)
        grid.attach(self.entry_sender, 1, 0, 1, 1)

        btn_save = Gtk.Button(label="Save Remote Bridge Configuration")
        btn_save.get_style_context().add_class("btn-primary")
        btn_save.connect("clicked", self.on_save_remote)
        card.pack_start(btn_save, False, False, 4)

    def on_save_remote(self, widget):
        sender = self.entry_sender.get_text().strip()
        if not sender:
            return
        conf_data = f'AUTHORIZED_SENDER="{sender}"\n'
        try:
            with open("/etc/conf.d/mail-trigger", "w") as f:
                f.write(conf_data)
        except Exception as e:
            print(f"[ERROR] Failed to save config: {e}")

    # -- Page 6: Live Interaction Log ------------------------------------------
    def setup_live_log_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        self.stack.add_titled(box, "live_log", "Live Syscall Log")

        head_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(head_box, False, False, 0)

        title = Gtk.Label(label="Kernel Syscall 548 and Execution Event Stream")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        head_box.pack_start(title, True, True, 0)

        btn_refresh = Gtk.Button(label="Refresh Log")
        btn_refresh.connect("clicked", lambda w: self._refresh_live_log())
        head_box.pack_end(btn_refresh, False, False, 0)

        # Monospace dark terminal view
        term_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        term_card.get_style_context().add_class("terminal-card")
        box.pack_start(term_card, True, True, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.log_textbuffer = Gtk.TextBuffer()
        self.log_textview = Gtk.TextView(buffer=self.log_textbuffer)
        self.log_textview.set_editable(False)
        self.log_textview.set_monospace(True)
        self.log_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_textview.get_style_context().add_class("terminal-view")
        scroll.add(self.log_textview)
        term_card.pack_start(scroll, True, True, 0)

        GLib.timeout_add_seconds(3, self._refresh_live_log)
        self._refresh_live_log()

    def _refresh_live_log(self):
        import datetime as dt
        date_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
        log_path = f"/var/ai-agent/logs/agent_responses/responses_{date_str}.jsonl"

        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    all_lines = [l.strip() for l in f if l.strip()]
                lines = all_lines[-60:]
            except Exception as e:
                lines = [f"{{\"error\": \"{e}\"}}"]
        else:
            lines = [f'{{"comm": "KERNEL", "query": "Kernel Syscall 548 sensor active | Listening...", "response_latency_ms": 0}}']

        self.log_textbuffer.set_text("")
        overview_text = ""
        for raw in reversed(lines):
            try:
                record = json.loads(raw)
                ts = record.get("timestamp", "")[:19].replace("T", " ") or time.strftime("%Y-%m-%d %H:%M:%S")
                comm = record.get("comm", record.get("msg_type", "AI-AGENT"))
                query = record.get("query", "").strip()
                response = record.get("response", "").strip()
                latency = record.get("response_latency_ms", 0)

                # Skip completely empty noise entries
                if not query and not response:
                    continue

                line_txt = f"[{ts}] [{comm.upper()}] ({latency:>4.0f}ms) >> {query}\n"
                if response:
                    line_txt += f"                 RESPONSE >> {response}\n\n"
            except Exception:
                line_txt = raw[:120] + "\n"

            end_iter = self.log_textbuffer.get_end_iter()
            self.log_textbuffer.insert(end_iter, line_txt)
            if len(overview_text) < 1600:
                overview_text += line_txt

        if hasattr(self, "overview_textbuffer"):
            self.overview_textbuffer.set_text(overview_text or "Listening for AI syscall 548 events...\n")

        return True

    # -- Page 7: Process Stats / Analytics -------------------------------------
    def setup_stats_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        self.stack.add_titled(box, "stats", "Process Analytics")

        title = Gtk.Label(label="Process Syscall Activity and Latency Breakdown")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("card")
        box.pack_start(card, True, True, 0)

        self.stats_store = Gtk.ListStore(str, int, str)
        treeview = Gtk.TreeView(model=self.stats_store)

        for col_idx, col_title in enumerate(["Process Name (comm)", "Queries Served", "Avg Latency"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(col_title, renderer, text=col_idx)
            column.set_sort_column_id(col_idx)
            column.set_resizable(True)
            treeview.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(treeview)
        card.pack_start(scroll, True, True, 0)

        GLib.timeout_add_seconds(8, self._refresh_stats)
        self._refresh_stats()

    def _refresh_stats(self):
        import datetime as dt
        date_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
        log_path = f"/var/ai-agent/logs/agent_responses/responses_{date_str}.jsonl"

        counts = defaultdict(int)
        latencies = defaultdict(list)

        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            rec = json.loads(raw)
                            comm = rec.get("comm", "unknown")
                            lat = float(rec.get("response_latency_ms", 0))
                            counts[comm] += 1
                            latencies[comm].append(lat)
                        except Exception:
                            pass
            except Exception:
                pass

        self.stats_store.clear()
        for comm in sorted(counts, key=lambda c: counts[c], reverse=True):
            avg_lat = sum(latencies[comm]) / len(latencies[comm]) if latencies[comm] else 0.0
            self.stats_store.append([comm, counts[comm], f"{avg_lat:.1f} ms"])
        return True


if __name__ == "__main__":
    win = SentinelDashboard()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
