import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784')
sftp = ssh.open_sftp()
sftp.put(r'C:\qemu-alpine\os-ai-agent\test_llama.py', '/root/test_llama.py')
sftp.close()

_, stdout, _ = ssh.exec_command('python3 /root/test_llama.py')
print(stdout.read().decode())
ssh.close()
