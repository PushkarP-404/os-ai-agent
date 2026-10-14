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
_, out, _ = ssh.exec_command("mkdir -p /home/aiuser /var/ai-agent/tasks /var/ai-agent/jobs && chmod 777 /var/ai-agent/tasks && touch /var/log/task_runner.log && chmod 666 /var/log/task_runner.log")
out.read()
for base_dir in ["/usr/local/lib/ai-agent", "/usr/lib/ai-agent"]:
    sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\agent_daemon.py", f"{base_dir}/agent_daemon.py")
    sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\logger.py",       f"{base_dir}/logger.py")
    sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\task_runner.py",  f"{base_dir}/task_runner.py")
    sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\harness_v2.py",    f"{base_dir}/harness_v2.py")
    sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\harness_privileged.py", f"{base_dir}/harness_privileged.py")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\cdp_controller.py", "/home/aiuser/cdp_controller.py")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\openrc\ai-agent", "/etc/init.d/ai-agent")
sftp.put(r"C:\qemu-alpine\os-ai-agent\agent-daemon\openrc\ai-agent-privileged", "/etc/init.d/ai-agent-privileged")
ssh.exec_command("chmod 755 /etc/init.d/ai-agent /etc/init.d/ai-agent-privileged && rc-update add ai-agent-privileged default 2>/dev/null || true")
print("  -> daemon, verified harness, privileged broker, and OpenRC services uploaded")

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
stdin, stdout, stderr = ssh.exec_command("cd /root/os-ai-agent/agent-cli && make && cp agent-cli /usr/bin/agent-cli && cp agent-cli /usr/local/bin/agent-cli && chmod +x /usr/bin/agent-cli /usr/local/bin/agent-cli")
print(stdout.read().decode())
print(stderr.read().decode())

print("Restarting daemon...")
stdin, stdout, stderr = ssh.exec_command("/etc/init.d/ai-agent-privileged restart && /etc/init.d/ai-agent restart")
print(stdout.read().decode())
print(stderr.read().decode())

ssh.close()
print("Done!")
