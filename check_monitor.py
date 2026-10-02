import socket
import sys

s_mon = socket.create_connection(('127.0.0.1', 5555))
s_mon.settimeout(2.0)

def send_qemu_cmd(cmd):
    s_mon.send(cmd.encode() + b"\n")
    try:
        print("Recv:", s_mon.recv(4096).decode('utf-8'))
    except socket.timeout:
        print("Timeout reading monitor")

print("Checking info status...")
send_qemu_cmd("info status")

print("Checking info registers...")
send_qemu_cmd("info registers")

s_mon.close()
