#!/usr/env python3
import sys
import os
import time

# We must set DISPLAY and import gi
if 'DISPLAY' not in os.environ:
    os.environ['DISPLAY'] = ':0'

import gi
gi.require_version('Atspi', '2.0')
gi.require_version('GLib', '2.0')
from gi.repository import Atspi, GLib

def dump_node(node, indent=0, counter=[0]):
    if not node:
        return
    
    role = node.get_role_name()
    name = node.get_name()
    states = node.get_state_set().get_states()
    
    counter[0] += 1
    cid = counter[0]
    
    # Output format: [ID] Role: "Name"
    print(f"{'  ' * indent}[{cid}] {role}: \"{name}\"")
    
    # Recursively dump children
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        dump_node(child, indent + 1, counter)

def get_active_window():
    desktop = Atspi.get_desktop(0)
    if not desktop:
        print("Error: Could not get Atspi desktop")
        return None

    # This is a naive way to find the active window
    # We iterate over applications and their windows looking for the 'active' state
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if not app: continue
        for j in range(app.get_child_count()):
            window = app.get_child_at_index(j)
            if not window: continue
            
            states = window.get_state_set().get_states()
            if Atspi.StateType.ACTIVE in states:
                return window
    
    return None

def main():
    Atspi.init()
    
    # Pump events for a short period to allow AT-SPI to populate
    ctx = GLib.MainContext.default()
    start = time.time()
    while time.time() - start < 1.0:
        ctx.iteration(False)
        time.sleep(0.01)
        
    desktop = Atspi.get_desktop(0)
    if not desktop:
        print("Error: Could not get Atspi desktop")
        Atspi.exit()
        return
        
    print(f"Desktop: \"{desktop.get_name()}\"")
    
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if not app: continue
        print(f"App {i}: role={app.get_role_name()}, name={app.get_name()}")
        for j in range(app.get_child_count()):
            window = app.get_child_at_index(j)
            if not window: continue
            print(f"  Window {j}: role={window.get_role_name()}, name={window.get_name()}")
            
    Atspi.exit()

if __name__ == '__main__':
    main()
