#!/usr/bin/env python3
"""
Phase 10: Omniscient State Awareness (eBPF Senses)
Uses BCC (BPF Compiler Collection) to stream kernel filesystem and network events
into the Agent Daemon, giving the LLM a real-time state graph of the OS.
"""

from bcc import BPF
import json
import time
import socket

# eBPF program written in C
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>
#include <linux/fs.h>

BPF_PERF_OUTPUT(events);

struct data_t {
    u32 pid;
    u32 type; // 1 = File Open, 2 = Network Connect
    char comm[TASK_COMM_LEN];
    char filename[256];
};

// Hook for sys_openat
int trace_sys_openat(struct pt_regs *ctx, int dfd, const char __user *filename, int flags, int mode) {
    struct data_t data = {};
    data.pid = bpf_get_current_pid_tgid() >> 32;
    data.type = 1; // File Access
    bpf_get_current_comm(&data.comm, sizeof(data.comm));
    bpf_probe_read_user_str(&data.filename, sizeof(data.filename), filename);
    
    events.perf_submit(ctx, &data, sizeof(data));
    return 0;
}
"""

def process_event(cpu, data, size):
    event = b["events"].event(data)
    
    # Send event to agent daemon's state graph (Mocking via JSON print)
    msg = {
        "pid": event.pid,
        "process": event.comm.decode('utf-8', 'replace'),
        "event_type": "FILE_OPEN" if event.type == 1 else "NETWORK_CONNECT",
        "target": event.filename.decode('utf-8', 'replace')
    }
    
    # In full implementation, this sends via UDP or Unix Socket to agent_daemon.py
    # to update SYSTEM_CAPABILITIES or OS_STATE_GRAPH
    print(f"[eBPF SENSOR] {json.dumps(msg)}")

if __name__ == "__main__":
    print("Loading eBPF Senses...")
    b = BPF(text=bpf_text)
    
    # Attach kprobes
    b.attach_kprobe(event=b.get_syscall_fnname("openat"), fn_name="trace_sys_openat")
    
    print("eBPF Sensors Active. Streaming OS state to Agent Daemon...")
    
    # Open perf buffer
    b["events"].open_perf_buffer(process_event)
    
    while True:
        try:
            b.perf_buffer_poll()
        except KeyboardInterrupt:
            exit()
