#!/usr/bin/env python3
import gi
import json
import os
import subprocess

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

CAPABILITIES_FILE = "/var/ai-agent/capabilities.json"
MODELS_DIR = "/var/lib/ai-agent/models"
LLAMA_CONF_FILE = "/etc/conf.d/llama-server"

class SentinelDashboard(Gtk.Window):
    def __init__(self):
        super().__init__(title="Sentinel Control Center")
        self.set_default_size(800, 500)
        self.set_border_width(10)
        
        # Main layout
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add(hbox)
        
        # Sidebar
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_UP_DOWN)
        self.stack.set_transition_duration(400)
        
        stack_switcher = Gtk.StackSidebar()
        stack_switcher.set_stack(self.stack)
        hbox.pack_start(stack_switcher, False, False, 0)
        
        # Separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        hbox.pack_start(separator, False, False, 0)
        
        hbox.pack_start(self.stack, True, True, 0)
        
        # Pages
        self.setup_status_page()
        self.setup_llm_page()
        self.setup_capabilities_page()
        self.setup_remote_page()
        self.setup_live_log_page()
        self.setup_stats_page()
        
    def setup_status_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_start(20)
        box.set_margin_top(20)
        self.stack.add_titled(box, "status", "Daemon Status")
        
        title = Gtk.Label(label="<big><b>OS AI Agent Status</b></big>", use_markup=True)
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)
        
        self.status_lbl = Gtk.Label(label="Checking...")
        self.status_lbl.set_halign(Gtk.Align.START)
        box.pack_start(self.status_lbl, False, False, 0)
        
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(btn_box, False, False, 0)
        
        btn_start = Gtk.Button(label="Start Daemon")
        btn_start.connect("clicked", lambda w: self.run_svc("start"))
        btn_box.pack_start(btn_start, False, False, 0)
        
        btn_stop = Gtk.Button(label="Stop Daemon")
        btn_stop.connect("clicked", lambda w: self.run_svc("stop"))
        btn_box.pack_start(btn_stop, False, False, 0)
        
        btn_restart = Gtk.Button(label="Restart Daemon")
        btn_restart.connect("clicked", lambda w: self.run_svc("restart"))
        btn_box.pack_start(btn_restart, False, False, 0)
        
        # Periodically update status
        GLib.timeout_add_seconds(2, self.update_status)
        self.update_status()
        
    def run_svc(self, action):
        subprocess.run(["sudo", "rc-service", "ai-agent", action], capture_output=True)
        self.update_status()

    def update_status(self):
        try:
            res = subprocess.run(["sudo", "rc-service", "ai-agent", "status"], capture_output=True, text=True)
            if "started" in res.stdout:
                self.status_lbl.set_markup("<span foreground='green'>â— RUNNING</span>")
            else:
                self.status_lbl.set_markup("<span foreground='red'>â— STOPPED</span>")
        except Exception as e:
            self.status_lbl.set_text(f"Error checking status: {e}")
        return True # keep timeout running

    def setup_llm_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_start(20)
        box.set_margin_top(20)
        self.stack.add_titled(box, "llm", "Local LLM Engine")
        
        title = Gtk.Label(label="<big><b>LLM Engine Settings</b></big>", use_markup=True)
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)
        
        lbl = Gtk.Label(label="Select the local .gguf model to run in the kernel:")
        lbl.set_halign(Gtk.Align.START)
        box.pack_start(lbl, False, False, 0)
        
        self.model_combo = Gtk.ComboBoxText()
        box.pack_start(self.model_combo, False, False, 0)
        
        self.load_models()
        
        btn_apply = Gtk.Button(label="Apply & Restart Engine")
        btn_apply.connect("clicked", self.on_apply_model)
        box.pack_start(btn_apply, False, False, 0)
        
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
        if not model: return
        
        full_path = os.path.join(MODELS_DIR, model)
        conf_data = f'LLAMA_MODEL="{full_path}"\n'
        
        # Security fix (2026-09-27): Removed shell=True which allowed shell injection
        # via malformed model filenames. Now uses list-form subprocess with piped input.
        try:
            subprocess.run(
                ["sudo", "tee", LLAMA_CONF_FILE],
                input=conf_data,
                text=True,
                capture_output=True,
                check=True
            )
            subprocess.run(["sudo", "rc-service", "llama-server", "restart"], capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to apply model: {e}")

    def setup_capabilities_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_start(20)
        box.set_margin_top(20)
        self.stack.add_titled(box, "caps", "Capabilities Registry")
        
        title = Gtk.Label(label="<big><b>Known Native AI IDEs</b></big>", use_markup=True)
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)
        
        self.caps_listbox = Gtk.ListBox()
        self.caps_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(self.caps_listbox)
        box.pack_start(scroll, True, True, 0)
        
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.pack_start(hbox, False, False, 0)
        
        self.ide_entry = Gtk.Entry()
        self.ide_entry.set_placeholder_text("e.g. antigravity")
        hbox.pack_start(self.ide_entry, True, True, 0)
        
        btn_set_primary = Gtk.Button(label="Set as Primary IDE")
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
            except (json.JSONDecodeError, IOError, OSError) as e:
                print(f"[WARN] Failed to load capabilities: {e}")
            
        for k, v in caps.items():
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.add(hbox)
            lbl_key = Gtk.Label(label=f"<b>{k}</b>", use_markup=True)
            lbl_val = Gtk.Label(label=v)
            hbox.pack_start(lbl_key, False, False, 10)
            hbox.pack_start(lbl_val, False, False, 10)
            self.caps_listbox.add(row)
            
        self.caps_listbox.show_all()
        return True

    def on_set_primary_clicked(self, widget):
        ide_name = self.ide_entry.get_text().strip()
        if not ide_name: return
        
        caps = {}
        if os.path.exists(CAPABILITIES_FILE):
            try:
                with open(CAPABILITIES_FILE, "r") as f:
                    caps = json.load(f)
            except (json.JSONDecodeError, IOError, OSError) as e:
                print(f"[WARN] Failed to load capabilities: {e}")
            
        caps["primary_ide"] = ide_name
        
        try:
            with open(CAPABILITIES_FILE, "w") as f:
                json.dump(caps, f)
        except (IOError, OSError) as e:
            print(f"[WARN] Failed to save capabilities: {e}")
        
        self.ide_entry.set_text("")
        self.load_capabilities_ui()

    def setup_remote_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_start(20)
        box.set_margin_top(20)
        self.stack.add_titled(box, "remote", "Remote Access")
        
        title = Gtk.Label(label="<big><b>Mail-to-Intent Config</b></big>", use_markup=True)
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)
        
        grid = Gtk.Grid()
        grid.set_row_spacing(10)
        grid.set_column_spacing(10)
        box.pack_start(grid, False, False, 0)
        
        lbl_sender = Gtk.Label(label="Authorized Sender (Owner Email):")
        lbl_sender.set_halign(Gtk.Align.START)
        self.entry_sender = Gtk.Entry()
        grid.attach(lbl_sender, 0, 0, 1, 1)
        grid.attach(self.entry_sender, 1, 0, 1, 1)
        
        btn_save = Gtk.Button(label="Save Config")
        btn_save.connect("clicked", self.on_save_remote)
        box.pack_start(btn_save, False, False, 0)
        
    def on_save_remote(self, widget):
        sender = self.entry_sender.get_text().strip()
        if not sender: return
        
        conf_data = f'AUTHORIZED_SENDER="{sender}"\n'
        try:
            with open("/etc/conf.d/mail-trigger", "w") as f:
                f.write(conf_data)
            print(f"Saved authorized sender to config: {sender}")
        except Exception as e:
            print(f"[ERROR] Failed to save config: {e}")

    # ── Part 5b: Live Interaction Log (real-time log view) ─────────────────

    def setup_live_log_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(20)
        box.set_margin_top(20)
        self.stack.add_titled(box, "live_log", "Live Agent Log")

        title = Gtk.Label(label="<big><b>Live Agent Interactions</b></big>", use_markup=True)
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(label="Auto-refreshes every 3 seconds. Shows the last 50 interactions.")
        subtitle.set_halign(Gtk.Align.START)
        box.pack_start(subtitle, False, False, 0)

        # Scrollable monospace text view
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.log_textbuffer = Gtk.TextBuffer()
        self.log_textview = Gtk.TextView(buffer=self.log_textbuffer)
        self.log_textview.set_editable(False)
        self.log_textview.set_monospace(True)
        self.log_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.add(self.log_textview)
        box.pack_start(scroll, True, True, 0)

        # Tag for colouring anomalies
        tag_table = self.log_textbuffer.get_tag_table()
        tag_warn = Gtk.TextTag(name="warn")
        tag_warn.set_property("foreground", "#e74c3c")
        tag_table.add(tag_warn)
        tag_ok = Gtk.TextTag(name="ok")
        tag_ok.set_property("foreground", "#27ae60")
        tag_table.add(tag_ok)

        # Refresh button
        btn_refresh = Gtk.Button(label="Refresh Now")
        btn_refresh.connect("clicked", lambda w: self._refresh_live_log())
        box.pack_start(btn_refresh, False, False, 0)

        # Auto-refresh every 3 seconds
        GLib.timeout_add_seconds(3, self._refresh_live_log)
        self._refresh_live_log()

    def _refresh_live_log(self):
        """Read today's JSONL response log and update the text view."""
        import datetime as dt
        date_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
        log_path = f"/var/ai-agent/logs/agent_responses/responses_{date_str}.jsonl"

        lines = []
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    all_lines = [l.strip() for l in f if l.strip()]
                lines = all_lines[-50:]  # Last 50
            except Exception as e:
                lines = [f"{{\"error\": \"{e}\"}}"]
        else:
            lines = [f'{{"info": "No log file yet: {log_path}"}}']  

        self.log_textbuffer.set_text("")  # Clear
        for raw in reversed(lines):  # Most recent first
            try:
                record = json.loads(raw)
                ts       = record.get("timestamp", "")[:19].replace("T", " ")
                comm     = record.get("comm", record.get("msg_type", "?"))
                query    = record.get("query", "")[:60]
                response = record.get("response", "")[:80]
                latency  = record.get("response_latency_ms", 0)
                line_txt = f"{ts}  {comm:<16}  {latency:>6.0f}ms  Q: {query!r}\n"
                line_txt += f"{'':>39}  R: {response!r}\n\n"
            except Exception:
                line_txt = raw[:120] + "\n"

            end_iter = self.log_textbuffer.get_end_iter()
            self.log_textbuffer.insert(end_iter, line_txt)

        return True  # Keep timeout alive

    # ── Part 5b: Process Stats panel ───────────────────────────────────────

    def setup_stats_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_start(20)
        box.set_margin_top(20)
        self.stack.add_titled(box, "stats", "Process Stats")

        title = Gtk.Label(label="<big><b>Agent Query Stats by Process</b></big>", use_markup=True)
        title.set_halign(Gtk.Align.START)
        box.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(label="Query counts from today's log. Refreshes every 10 seconds.")
        subtitle.set_halign(Gtk.Align.START)
        box.pack_start(subtitle, False, False, 0)

        # List store: process name, query count, avg latency
        self.stats_store = Gtk.ListStore(str, int, float)
        treeview = Gtk.TreeView(model=self.stats_store)

        for col_idx, (col_title, col_type) in enumerate([
            ("Process (comm)", str), ("Query Count", int), ("Avg Latency (ms)", float)
        ]):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(col_title, renderer, text=col_idx)
            column.set_sort_column_id(col_idx)
            column.set_resizable(True)
            treeview.append_column(column)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.add(treeview)
        box.pack_start(scroll, True, True, 0)

        btn_refresh = Gtk.Button(label="Refresh Now")
        btn_refresh.connect("clicked", lambda w: self._refresh_stats())
        box.pack_start(btn_refresh, False, False, 0)

        GLib.timeout_add_seconds(10, self._refresh_stats)
        self._refresh_stats()

    def _refresh_stats(self):
        """Parse today's log and aggregate per-comm query stats."""
        import datetime as dt
        from collections import defaultdict

        date_str = dt.datetime.utcnow().strftime("%Y-%m-%d")
        log_path = f"/var/ai-agent/logs/agent_responses/responses_{date_str}.jsonl"

        counts: dict = defaultdict(int)
        latencies: dict = defaultdict(list)

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
                            lat  = float(rec.get("response_latency_ms", 0))
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

        return True  # Keep timeout alive


if __name__ == "__main__":
    win = SentinelDashboard()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
