import paramiko
import os

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784')
_, out, err = ssh.exec_command('rm -f /lib/apk/db/lock && apk add py3-bcc bcc-tools bcc-dev bcc')
print(out.read().decode())
print(err.read().decode())
