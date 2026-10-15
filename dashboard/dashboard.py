#!/usr/bin/env python3
"""
Sentinel Control Center — AI-Agent OS Flagship GUI
Modern Dark Mode with Rounded Card Layout and Live Telemetry
"""
import gi
import json
import os
import subprocess
import time
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


class SentinelDashboard(Gtk.Window):
    def __init__(self):
        super().__init__(title="Sentinel Control Center")
        self.set_default_size(900, 580)
        self.get_style_context().add_class("main-window")

        load_stylesheet()
        self.setup_headerbar()

        # Main horizontal container
        main_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add(main_hbox)

        # Left Navigation Sidebar
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.get_style_context().add_class("sidebar-box")
        sidebar.set_size_request(200, -1)
        main_hbox.pack_start(sidebar, False, False, 0)

        # Main Stack for Page Content
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(200)
        main_hbox.pack_start(self.stack, True, True, 0)

        # Navigation Buttons list
        self.nav_buttons = {}
        pages = [
            ("overview", "Overview", self.setup_overview_page),
            ("llm", "LLM Engine", self.setup_llm_page),
            ("caps", "Capabilities", self.setup_capabilities_page),
            ("remote", "Remote Bridges", self.setup_remote_page),
            ("live_log", "Live Agent Log", self.setup_live_log_page),
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

        # Periodically refresh status
        GLib.timeout_add_seconds(2, self.update_daemon_status)
        self.update_daemon_status()

    # ── HeaderBar ────────────────────────────────────────────────────────────
    def setup_headerbar(self):
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("AI-Agent OS")
        header.set_subtitle("Sentinel Control Center")
        header.get_style_context().add_class("header-bar")
        self.set_titlebar(header)

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

    # ── Page 1: Overview Page ────────────────────────────────────────────────
    def setup_overview_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)
        vbox.set_margin_top(18)
        vbox.set_margin_bottom(18)
        self.stack.add_titled(vbox, "overview", "Overview")

        # Top row: 3 Metric Cards
        cards_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        cards_hbox.set_homogeneous(True)
        vbox.pack_start(cards_hbox, False, False, 0)

        # Card 1: Daemon State
        card1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card1.get_style_context().add_class("card")
        cards_hbox.pack_start(card1, True, True, 0)

        lbl1 = Gtk.Label(label="Kernel Daemon")
        lbl1.get_style_context().add_class("card-title")
        lbl1.set_halign(Gtk.Align.START)
        card1.pack_start(lbl1, False, False, 0)

        self.overview_status_lbl = Gtk.Label(label="● RUNNING")
        self.overview_status_lbl.get_style_context().add_class("metric-value")
        self.overview_status_lbl.set_halign(Gtk.Align.START)
        card1.pack_start(self.overview_status_lbl, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card1.pack_start(btn_row, False, False, 4)

        btn_start = Gtk.Button(label="Start")
        btn_start.get_style_context().add_class("btn-success")
        btn_start.connect("clicked", lambda w: self.run_svc("start"))
        btn_row.pack_start(btn_start, True, True, 0)

        btn_restart = Gtk.Button(label="Restart")
        btn_restart.get_style_context().add_class("btn-primary")
        btn_restart.connect("clicked", lambda w: self.run_svc("restart"))
        btn_row.pack_start(btn_restart, True, True, 0)

        btn_stop = Gtk.Button(label="Stop")
        btn_stop.get_style_context().add_class("btn-danger")
        btn_stop.connect("clicked", lambda w: self.run_svc("stop"))
        btn_row.pack_start(btn_stop, True, True, 0)

        # Card 2: Memory & Host
        card2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card2.get_style_context().add_class("card")
        cards_hbox.pack_start(card2, True, True, 0)

        lbl2 = Gtk.Label(label="Memory Usage")
        lbl2.get_style_context().add_class("card-title")
        lbl2.set_halign(Gtk.Align.START)
        card2.pack_start(lbl2, False, False, 0)

        self.ram_val_lbl = Gtk.Label(label="-- MB")
        self.ram_val_lbl.get_style_context().add_class("metric-value")
        self.ram_val_lbl.set_halign(Gtk.Align.START)
        card2.pack_start(self.ram_val_lbl, False, False, 0)

        self.ram_sub_lbl = Gtk.Label(label="Alpine Linux 3.20 (musl)")
        self.ram_sub_lbl.get_style_context().add_class("metric-sub")
        self.ram_sub_lbl.set_halign(Gtk.Align.START)
        card2.pack_start(self.ram_sub_lbl, False, False, 0)

        # Card 3: Model Engine
        card3 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card3.get_style_context().add_class("card")
        cards_hbox.pack_start(card3, True, True, 0)

        lbl3 = Gtk.Label(label="Active Model")
        lbl3.get_style_context().add_class("card-title")
        lbl3.set_halign(Gtk.Align.START)
        card3.pack_start(lbl3, False, False, 0)

        self.model_val_lbl = Gtk.Label(label="SmolLM2 135M")
        self.model_val_lbl.get_style_context().add_class("metric-value")
        self.model_val_lbl.set_halign(Gtk.Align.START)
        card3.pack_start(self.model_val_lbl, False, False, 0)

        sub3 = Gtk.Label(label="Quantized Q4_K_M · Native Musl")
        sub3.get_style_context().add_class("metric-sub")
        sub3.set_halign(Gtk.Align.START)
        card3.pack_start(sub3, False, False, 0)

        # Bottom section: Live preview card
        preview_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        preview_card.get_style_context().add_class("card")
        vbox.pack_start(preview_card, True, True, 0)

        prev_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        preview_card.pack_start(prev_header, False, False, 0)

        lbl_log_head = Gtk.Label(label="Live AI Syscall & Task Activity")
        lbl_log_head.get_style_context().add_class("card-title")
        lbl_log_head.set_halign(Gtk.Align.START)
        prev_header.pack_start(lbl_log_head, True, True, 0)

        btn_view_full = Gtk.Button(label="Open Full Log")
        btn_view_full.connect("clicked", lambda w: self.set_active_page("live_log"))
        prev_header.pack_end(btn_view_full, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.overview_textbuffer = Gtk.TextBuffer()
        self.overview_textview = Gtk.TextView(buffer=self.overview_textbuffer)
        self.overview_textview.set_editable(False)
        self.overview_textview.set_monospace(True)
        self.overview_textview.get_style_context().add_class("terminal-view")
        scroll.add(self.overview_textview)
        preview_card.pack_start(scroll, True, True, 0)

        # Periodically refresh memory
        GLib.timeout_add_seconds(3, self.update_system_stats)
        self.update_system_stats()

    def update_system_stats(self):
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            mem_total = 0
            mem_avail = 0
            for l in lines:
                if l.startswith("MemTotal:"):
                    mem_total = int(l.split()[1]) // 1024
                elif l.startswith("MemAvailable:"):
                    mem_avail = int(l.split()[1]) // 1024
            used = mem_total - mem_avail
            self.ram_val_lbl.set_text(f"{used} MB / {mem_total} MB")
            pct = int((used / mem_total) * 100) if mem_total else 0
            self.ram_sub_lbl.set_text(f"Memory Utilization: {pct}%")
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
                self.header_status_badge.set_text("● DAEMON: ONLINE")
                ctx.add_class("pill-online")
                if hasattr(self, "overview_status_lbl"):
                    self.overview_status_lbl.set_markup("<span foreground='#10b981'>● ACTIVE</span>")
            else:
                self.header_status_badge.set_text("○ DAEMON: STOPPED")
                ctx.add_class("pill-offline")
                if hasattr(self, "overview_status_lbl"):
                    self.overview_status_lbl.set_markup("<span foreground='#ef4444'>○ STOPPED</span>")
        except Exception as e:
            self.header_status_badge.set_text("Error")
        return True

    # ── Page 2: LLM Engine Page ──────────────────────────────────────────────
    def setup_llm_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.stack.add_titled(box, "llm", "Local LLM Engine")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("card")
        box.pack_start(card, False, False, 0)

        title = Gtk.Label(label="Local LLM Engine Configuration")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        card.pack_start(title, False, False, 0)

        lbl = Gtk.Label(label="Select the local GGUF model executed via in-guest llama-server:")
        lbl.set_halign(Gtk.Align.START)
        card.pack_start(lbl, False, False, 0)

        self.model_combo = Gtk.ComboBoxText()
        card.pack_start(self.model_combo, False, False, 0)
        self.load_models()

        btn_apply = Gtk.Button(label="Apply & Restart Inference Engine")
        btn_apply.get_style_context().add_class("btn-primary")
        btn_apply.connect("clicked", self.on_apply_model)
        card.pack_start(btn_apply, False, False, 4)

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
        if not model:
            return
        full_path = os.path.join(MODELS_DIR, model)
        conf_data = f'LLAMA_MODEL="{full_path}"\n'
        try:
            if os.geteuid() == 0:
                with open(LLAMA_CONF_FILE, "w") as f:
                    f.write(conf_data)
            else:
                run_privileged(["tee", LLAMA_CONF_FILE], input=conf_data, text=True, capture_output=True, check=True)
            run_privileged(["rc-service", "llama-server", "restart"], capture_output=True)
            if hasattr(self, "model_val_lbl"):
                self.model_val_lbl.set_text(model.replace(".gguf", ""))
        except Exception as e:
            print(f"[ERROR] Failed to apply model: {e}")

    # ── Page 3: Capabilities Page ────────────────────────────────────────────
    def setup_capabilities_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.stack.add_titled(box, "caps", "Capabilities Registry")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("card")
        box.pack_start(card, True, True, 0)

        title = Gtk.Label(label="Registered Capabilities & IDE Associations")
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
        card.pack_start(hbox, False, False, 4)

        self.ide_entry = Gtk.Entry()
        self.ide_entry.set_placeholder_text("Enter primary IDE name (e.g. antigravity)")
        hbox.pack_start(self.ide_entry, True, True, 0)

        btn_set_primary = Gtk.Button(label="Register Primary IDE")
        btn_set_primary.get_style_context().add_class("btn-primary")
        btn_set_primary.connect("clicked", self.on_set_primary_clicked)
        hbox.pack_start(btn_set_primary, False, False, 0)

        GLib.timeout_add_seconds(5, self.load_capabilities_ui)
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

            lbl_key = Gtk.Label(label=f"<b>{k}</b>", use_markup=True)
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

    # ── Page 4: Remote Bridges ───────────────────────────────────────────────
    def setup_remote_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.stack.add_titled(box, "remote", "Remote Access")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("card")
        box.pack_start(card, False, False, 0)

        title = Gtk.Label(label="Mail-to-Intent Remote Trigger")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        card.pack_start(title, False, False, 0)

        lbl = Gtk.Label(label="Allow authorized external email addresses to dispatch syscall 548 tasks:")
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

        btn_save = Gtk.Button(label="Save Remote Bridge Config")
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

    # ── Page 5: Live Interaction Log ─────────────────────────────────────────
    def setup_live_log_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.stack.add_titled(box, "live_log", "Live Agent Log")

        head_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(head_box, False, False, 0)

        title = Gtk.Label(label="Real-Time Agent Syscall & Execution Log")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        head_box.pack_start(title, True, True, 0)

        btn_refresh = Gtk.Button(label="Refresh")
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
            lines = [f'{{"info": "Listening for kernel syscalls... Log: {log_path}"}}']

        self.log_textbuffer.set_text("")
        overview_text = ""
        for raw in reversed(lines):
            try:
                record = json.loads(raw)
                ts = record.get("timestamp", "")[:19].replace("T", " ")
                comm = record.get("comm", record.get("msg_type", "?"))
                query = record.get("query", "")[:60]
                response = record.get("response", "")[:80]
                latency = record.get("response_latency_ms", 0)
                line_txt = f"{ts}  {comm:<14}  {latency:>5.0f}ms  Q: {query!r}\n"
                if response:
                    line_txt += f"{'':>37}  R: {response!r}\n\n"
            except Exception:
                line_txt = raw[:120] + "\n"

            end_iter = self.log_textbuffer.get_end_iter()
            self.log_textbuffer.insert(end_iter, line_txt)
            if len(overview_text) < 1500:
                overview_text += line_txt

        if hasattr(self, "overview_textbuffer"):
            self.overview_textbuffer.set_text(overview_text)

        return True

    # ── Page 6: Process Stats / Analytics ────────────────────────────────────
    def setup_stats_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(20)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        self.stack.add_titled(box, "stats", "Process Analytics")

        title = Gtk.Label(label="Agent Syscall Activity by Process")
        title.get_style_context().add_class("card-title")
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("card")
        box.pack_start(card, True, True, 0)

        self.stats_store = Gtk.ListStore(str, int, float)
        treeview = Gtk.TreeView(model=self.stats_store)

        for col_idx, col_title in enumerate(["Process Name (comm)", "Queries Served", "Avg Latency (ms)"]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(col_title, renderer, text=col_idx)
            column.set_sort_column_id(col_idx)
            column.set_resizable(True)
            treeview.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(treeview)
        card.pack_start(scroll, True, True, 0)

        GLib.timeout_add_seconds(10, self._refresh_stats)
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
            self.stats_store.append([comm, counts[comm], round(avg_lat, 1)])
        return True


if __name__ == "__main__":
    win = SentinelDashboard()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
