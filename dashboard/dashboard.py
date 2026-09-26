#!/usr/bin/env python3
import gi
import json
import os
import subprocess

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

CAPABILITIES_FILE = "/var/ai-agent/capabilities.json"

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
        self.setup_capabilities_page()
        self.setup_remote_page()
        
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
                self.status_lbl.set_markup("<span foreground='green'>● RUNNING</span>")
            else:
                self.status_lbl.set_markup("<span foreground='red'>● STOPPED</span>")
        except Exception as e:
            self.status_lbl.set_text(f"Error checking status: {e}")
        return True # keep timeout running

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
            except: pass
            
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
            except: pass
            
        caps["primary_ide"] = ide_name
        
        try:
            with open(CAPABILITIES_FILE, "w") as f:
                json.dump(caps, f)
        except: pass
        
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
        print(f"Saved authorized sender: {self.entry_sender.get_text()}")

if __name__ == "__main__":
    win = SentinelDashboard()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
