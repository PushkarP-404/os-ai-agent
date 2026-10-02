import paramiko
import time
import sys

key = paramiko.RSAKey.from_private_key_file("id_rsa_wsl")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

for _ in range(30):
    try:
        print("Connecting...")
        client.connect('127.0.0.1', port=2222, username='root', pkey=key, timeout=30)
        print("Connected!")
        break
    except Exception as e:
        print(f"Failed: {e}")
        time.sleep(2)
else:
    print("Could not connect.")
    sys.exit(1)

commands = [
    "cd /root/os-ai-agent/agent-cli && make",
    "/etc/init.d/ai-agent restart",
    "cat /var/ai-agent/ebpf.log"
]

for cmd in commands:
    print(f"Executing: {cmd}")
    stdin, stdout, stderr = client.exec_command(cmd)
    print("STDOUT:", stdout.read().decode())
    print("STDERR:", stderr.read().decode())

client.close()
