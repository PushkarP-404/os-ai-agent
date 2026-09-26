import paramiko
import time
import sys

def main():
    print("Connecting to VM...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect('127.0.0.1', port=2222, username='root', password='aPushkar@12784', timeout=10)
    except Exception as e:
        print(f"Failed to connect to VM: {e}")
        sys.exit(1)

    print("1. Uploading os_agent_lora.gguf to VM...")
    sftp = ssh.open_sftp()
    try:
        sftp.put('os_agent_lora.gguf', '/var/lib/ai-agent/models/os_agent_lora.gguf')
    except Exception as e:
        print(f"Failed to upload: {e}")
        sys.exit(1)
    finally:
        sftp.close()

    print("2. Modifying /etc/init.d/llama-server to include LoRA weights...")
    # Find the line starting with command_args= and append the lora flag if it doesn't already have it
    cmd = """
    if ! grep -q "\-\-lora" /etc/init.d/llama-server; then
        sed -i 's|--ctx-size 512|--ctx-size 512 --lora /var/lib/ai-agent/models/os_agent_lora.gguf|g' /etc/init.d/llama-server
    fi
    cat /etc/init.d/llama-server | grep command_args
    """
    stdin, stdout, stderr = ssh.exec_command(cmd)
    print("Updated args:", stdout.read().decode().strip())

    print("3. Restarting llama-server service...")
    stdin, stdout, stderr = ssh.exec_command("rc-service llama-server restart")
    print(stdout.read().decode().strip())
    
    # Wait for the server to load the base model + LoRA
    print("Waiting for inference server to initialize (loading weights)...")
    time.sleep(10)

    print("4. Testing the custom syscall...")
    stdin, stdout, stderr = ssh.exec_command("/root/os-ai-agent/test-programs/test_syscall")
    out = stdout.read().decode().strip()
    
    print("\n--- SYSCALL TEST OUTPUT ---")
    print(out)
    print("---------------------------\n")
    
    ssh.close()

if __name__ == "__main__":
    main()
