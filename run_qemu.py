import subprocess
import sys

qemu_exe = r"C:\Program Files\qemu\qemu-system-x86_64.exe"
image = r"..\ai-agent-os-v0.1.qcow2"
memory = "4096"
cores = "4"
ssh_port = "2222"

cmd = [
    qemu_exe,
    "-m", f"{memory}M",
    "-smp", cores,
    "-hda", image,
    "-boot", "c",
    "-netdev", f"user,id=n1,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
    "-device", "e1000,netdev=n1",
    "-monitor", "tcp:127.0.0.1:5555,server,nowait"
]

print("Starting QEMU...")
try:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        print(line, end="")
except Exception as e:
    print(f"Error starting QEMU: {e}")
