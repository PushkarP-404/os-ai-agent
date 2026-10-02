import paramiko
import os

host = '127.0.0.1'
port = 2222
user = 'root'
pwd = 'aPushkar@12784'

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
print("Connecting...")
ssh.connect(host, port=port, username=user, password=pwd)

sftp = ssh.open_sftp()
print("Uploading os_state_sensor.py...")
_, out, _ = ssh.exec_command("mkdir -p /root/os-ai-agent/ebpf_sensors")
out.read()
sftp.put(r"C:\qemu-alpine\os-ai-agent\ebpf_sensors\os_state_sensor.py", "/root/os-ai-agent/ebpf_sensors/os_state_sensor.py")
sftp.close()

print("Starting os_state_sensor.py in the background...")
# Run it using nohup
stdin, stdout, stderr = ssh.exec_command("nohup python3 /root/os-ai-agent/ebpf_sensors/os_state_sensor.py > /var/log/os_state_sensor.log 2>&1 &")
print("Done!")
ssh.close()
