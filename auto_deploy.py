import socket
import time
import sys
import threading
import http.server
import socketserver
import os

# Start HTTP Server in a background thread
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

def start_server():
    os.chdir(r"C:\qemu-alpine\os-ai-agent")
    with socketserver.TCPServer(("", 8000), QuietHandler) as httpd:
        httpd.serve_forever()

t = threading.Thread(target=start_server, daemon=True)
t.start()

print("Waiting for QEMU serial port (4444)...")
s = None
for i in range(30):
    try:
        s = socket.create_connection(('127.0.0.1', 4444), timeout=2)
        break
    except:
        time.sleep(1)

if not s:
    print("Could not connect to QEMU serial port.")
    sys.exit(1)

print("Connected to serial! Waiting for boot...")
s.settimeout(1.0)
buf = ""

def wait_for(text, timeout_sec=60):
    global buf
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            chunk = s.recv(1024).decode(errors='ignore')
            buf += chunk
            if text in buf:
                buf = buf.split(text)[-1] # clear buffer up to the text
                return True
        except socket.timeout:
            pass
    return False

# Sometimes pressing Enter helps trigger the prompt
s.send(b"\n")

if not wait_for("login: ", 90):
    print("Never saw login prompt. Buffer:")
    print(buf)
    sys.exit(1)

print("Saw login prompt. Sending root...")
s.send(b"root\n")

if not wait_for("Password: ", 10):
    print("Never saw password prompt.")
    sys.exit(1)

print("Saw password prompt. Sending password...")
s.send(b"password\n")

# Wait for shell prompt
if not wait_for("~# ", 10):
    # try alpine default prompt
    if not wait_for("~ # ", 5):
        print("Never got shell prompt!")
        sys.exit(1)

print("Logged in as root! Sending payload...")
# Send the wget payload
s.send(b"wget http://10.0.2.2:8000/u.sh && sh u.sh\n")

# Wait for completion message
if wait_for("SUCCESS! All updates applied.", 60):
    print("Payload executed successfully!")
else:
    print("Payload execution might have failed or timed out.")

# Clean up
s.send(b"exit\n")
s.close()
