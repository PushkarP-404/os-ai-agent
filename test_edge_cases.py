import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784')

queries = [
    "missing tool test",
    "timeout test",
    "error test",
    "large output test"
]

for q in queries:
    print(f"\n--- Testing query: '{q}' ---")
    cmd = f'/root/os-ai-agent/agent-cli/agent-cli "{q}"'
    _, stdout, stderr = ssh.exec_command(cmd)
    
    out = stdout.read().decode()
    err = stderr.read().decode()
    
    if out:
        print(f"STDOUT:\n{out.strip()}")
    if err:
        print(f"STDERR:\n{err.strip()}")

ssh.close()
