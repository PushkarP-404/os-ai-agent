import paramiko
import sys

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784')
cmd = '/root/os-ai-agent/agent-cli/agent-cli "install curl"'
print(f"Running: {cmd}")
_, stdout, stderr = ssh.exec_command(cmd)

out = stdout.read().decode()
err = stderr.read().decode()

print(out)
print(err)

if "SUCCESS" in out or "exit" in out:
    # also try "cat /root/os-ai-agent/agent-cli/test.txt" if we wrote a file, but we just installed curl
    print("Curl installation check:")
    _, c_out, _ = ssh.exec_command("curl --version")
    print(c_out.read().decode())
