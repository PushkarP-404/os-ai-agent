import paramiko
import os

host = '127.0.0.1'
port = 2222
user = 'root'
pwd = os.environ.get('VM_PASSWORD', 'password')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
print("Connecting...")
ssh.connect(host, port=port, username=user, password=pwd)

sftp = ssh.open_sftp()

print("Uploading agent_daemon files...")
_, out, _ = ssh.exec_command("mkdir -p /home/aiuser && mkdir -p /var/ai-agent/tasks && touch /var/log/task_runner.log && chmod 666 /var/log/task_runner.log")
out.read()
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\agent_daemon.py", "/usr/local/lib/ai-agent/agent_daemon.py")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\logger.py",       "/usr/local/lib/ai-agent/logger.py")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\task_runner.py",  "/usr/local/lib/ai-agent/task_runner.py")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\cdp_controller.py", "/home/aiuser/cdp_controller.py")
print("  -> agent_daemon.py, logger.py, task_runner.py, cdp_controller.py uploaded")

print("Uploading agent-cli files...")
_, out, _ = ssh.exec_command("mkdir -p /root/os-ai-agent/agent-cli")
out.read()
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-cli\agent-cli.c", "/root/os-ai-agent/agent-cli/agent-cli.c")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-cli\Makefile", "/root/os-ai-agent/agent-cli/Makefile")

print("Uploading dashboard files...")
ssh.exec_command("mkdir -p /root/os-ai-agent/dashboard")
sftp.put(r"C:\qemu-alpine\os-ai-agent\dashboard\dashboard.py", "/root/os-ai-agent/dashboard/dashboard.py")
sftp.put(r"C:\qemu-alpine\os-ai-agent\dashboard\ai-agent-dashboard.desktop", "/root/os-ai-agent/dashboard/ai-agent-dashboard.desktop")

sftp.close()

print("Compiling agent-cli...")
stdin, stdout, stderr = ssh.exec_command("cd /root/os-ai-agent/agent-cli && make")
print(stdout.read().decode())
print(stderr.read().decode())

print("Restarting daemon...")
stdin, stdout, stderr = ssh.exec_command("/etc/init.d/ai-agent restart")
print(stdout.read().decode())
print(stderr.read().decode())

ssh.close()
print("Done!")
