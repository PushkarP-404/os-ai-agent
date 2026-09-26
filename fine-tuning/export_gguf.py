import os
import subprocess
import sys

def main():
    lora_dir = "adapters/os_agent_lora"
    output_file = "os_agent_lora.gguf"
    
    print("==================================================")
    print("  AI-Agent OS: LoRA to GGUF Converter Tool  ")
    print("==================================================\n")
    
    if not os.path.exists(lora_dir):
        print(f"[ERROR] LoRA directory '{lora_dir}' not found.")
        print("Please wait for train_lora.py to finish training first.")
        sys.exit(1)
        
    # We need llama.cpp on the Windows host to use its conversion scripts.
    if not os.path.exists("llama.cpp"):
        print("[INFO] llama.cpp not found on host. Cloning repository to get conversion tools...")
        try:
            subprocess.run(["git", "clone", "https://github.com/ggml-org/llama.cpp.git", "--depth", "1"], check=True)
        except subprocess.CalledProcessError:
            print("[ERROR] Failed to clone llama.cpp. Is Git installed on Windows?")
            sys.exit(1)
        
    print("\n[INFO] Installing llama.cpp python dependencies...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", "llama.cpp/requirements.txt"], check=False)
    
    # The conversion script for LoRA is typically convert_lora_to_gguf.py
    # We'll check a few possible names just in case the repo updated it.
    possible_scripts = [
        os.path.join("llama.cpp", "convert_lora_to_gguf.py"),
        os.path.join("llama.cpp", "convert-lora-to-gguf.py")
    ]
    
    script_path = None
    for sp in possible_scripts:
        if os.path.exists(sp):
            script_path = sp
            break
            
    if not script_path:
        print(f"[ERROR] Could not find the LoRA conversion script in the llama.cpp repository.")
        sys.exit(1)
        
    print(f"\n[INFO] Converting LoRA weights from {lora_dir} to {output_file}...")
    cmd = [
        sys.executable,
        script_path,
        "--outfile", output_file,
        lora_dir
    ]
    
    try:
        subprocess.run(cmd, check=True)
        print(f"\n[SUCCESS] Converted adapter is ready at: {os.path.abspath(output_file)}")
        print("\n==================================================")
        print("                 NEXT STEPS                       ")
        print("==================================================")
        print("1. Copy the adapter into the running OS VM:")
        print(f"   scp -P 2222 {output_file} root@127.0.0.1:/var/lib/ai-agent/models/")
        print("\n2. Modify /etc/init.d/llama-server in the VM to load the adapter:")
        print(f"   Add: --lora /var/lib/ai-agent/models/{output_file}")
        print("3. Restart the service:")
        print("   rc-service llama-server restart")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Conversion failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
