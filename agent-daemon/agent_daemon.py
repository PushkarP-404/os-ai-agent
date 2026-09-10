#!/usr/bin/env python3
import socket
import struct
import os
import sys
import time

NETLINK_AI_AGENT = 31

# struct nlmsghdr {
#   __u32 nlmsg_len;
#   __u16 nlmsg_type;
#   __u16 nlmsg_flags;
#   __u32 nlmsg_seq;
#   __u32 nlmsg_pid;
# };
NLMSG_HDR_FORMAT = "=IHHII"
NLMSG_HDR_SIZE = struct.calcsize(NLMSG_HDR_FORMAT)

# struct process_event {
#   pid_t parent_pid;  (int32)
#   pid_t child_pid;   (int32)
#   char comm[16];     (16 bytes)
# };
EVENT_FORMAT = "=ii16s"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

def main():
    print(f"[AGENT DAEMON] Starting userspace listener (PID: {os.getpid()})...")
    
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_AI_AGENT)
    except OSError as e:
        print(f"[ERROR] Failed to open Netlink socket (protocol {NETLINK_AI_AGENT}): {e}")
        print("Make sure ai_process_hook kernel module is loaded (`insmod ai_process_hook.ko`).")
        sys.exit(1)

    try:
        sock.bind((os.getpid(), 0))
    except OSError as e:
        print(f"[ERROR] Failed to bind Netlink socket: {e}")
        sock.close()
        sys.exit(1)

    # Send registration packet to kernel (destination pid 0)
    reg_hdr = struct.pack(NLMSG_HDR_FORMAT, NLMSG_HDR_SIZE, 0, 0, 1, os.getpid())
    try:
        sock.sendto(reg_hdr, (0, 0))
        print("[AGENT DAEMON] Registered with ai_process_hook kernel module.")
    except OSError as e:
        print(f"[ERROR] Failed to send registration message to kernel: {e}")
        sock.close()
        sys.exit(1)

    print("[AGENT DAEMON] Actively monitoring system-wide process creation events...")
    sys.stdout.flush()

    # If an optional max_events argument is passed, stop after N events
    max_events = int(sys.argv[1]) if len(sys.argv) > 1 else None
    events_count = 0

    try:
        while True:
            data, addr = sock.recvfrom(1024)
            if len(data) < NLMSG_HDR_SIZE + EVENT_SIZE:
                continue

            # Extract Netlink header
            nlmsg_len, nlmsg_type, nlmsg_flags, nlmsg_seq, nlmsg_pid = struct.unpack_from(
                NLMSG_HDR_FORMAT, data, 0
            )

            # Extract event payload
            payload = data[NLMSG_HDR_SIZE:NLMSG_HDR_SIZE + EVENT_SIZE]
            parent_pid, child_pid, comm_bytes = struct.unpack(EVENT_FORMAT, payload)
            comm = comm_bytes.split(b'\x00')[0].decode('utf-8', errors='replace')

            events_count += 1
            print(f"[EVENT #{events_count}] Parent: {parent_pid} ({comm}) ---> New Process PID: {child_pid}")
            sys.stdout.flush()

            if max_events and events_count >= max_events:
                print(f"[AGENT DAEMON] Reached target count of {max_events} events. Exiting.")
                break

    except KeyboardInterrupt:
        print("\n[AGENT DAEMON] Stopped by user.")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
