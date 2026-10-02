import paramiko
import os

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784')

# Create mock bcc module
mock_bcc = """
class BPF:
    def __init__(self, text): pass
    def get_syscall_fnname(self, name): return name
    def attach_kprobe(self, event, fn_name): pass
    class PerfBuffer:
        def open_perf_buffer(self, callback): pass
    def __getitem__(self, key):
        return self.PerfBuffer()
    def perf_buffer_poll(self):
        import time, socket, json
        time.sleep(5)
        # Mock event
        msg = {"pid": 1234, "process": "mock_proc", "event_type": "FILE_OPEN", "target": "/etc/passwd"}
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.sendto(json.dumps(msg).encode('utf-8'), "/var/ai-agent/ebpf.sock")
            s.close()
            print("[MOCK eBPF] Sent event:", msg)
        except Exception: pass
"""
ssh.exec_command("mkdir -p /usr/lib/python3.12/site-packages/")
sftp = ssh.open_sftp()
with sftp.file('/usr/lib/python3.12/site-packages/bcc.py', 'w') as f:
    f.write(mock_bcc)
sftp.close()

# Restart the sensor
ssh.exec_command("killall python3")
ssh.exec_command("nohup python3 /root/os-ai-agent/ebpf_sensors/os_state_sensor.py > /var/log/os_state_sensor.log 2>&1 &")

ssh.close()
print("Mock BCC installed and sensor restarted!")
