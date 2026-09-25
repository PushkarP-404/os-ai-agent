import paramiko

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784')

sftp = ssh.open_sftp()
sftp.get('/var/ai-agent/training_data/dataset.jsonl', 'dataset.jsonl')
sftp.close()
ssh.close()

print("Dataset fetched successfully!")
