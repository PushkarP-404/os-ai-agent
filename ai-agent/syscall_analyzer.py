#!/usr/bin/env python3
import sys
import json
import urllib.request
import urllib.error
import os

def analyze_syscalls(syscall_data, ollama_url="http://10.0.2.2:11434"):
    """Send syscall data to an LLM backend (Ollama) for analysis."""

    prompt = f"""You are an OS system analyst. Analyze these system calls and explain:
1. What the process is trying to do
2. Any suspicious behavior
3. What this process should be allowed to do
4. Security concerns

Syscall data:
{syscall_data}

Keep analysis concise."""

    data = {
        "model": "llama3.2", # Fallback to llama3.2 if available, user can change this
        "prompt": prompt,
        "stream": False
    }

    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=json.dumps(data).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )

    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get("response", "No response from model.")
    except urllib.error.URLError as e:
        return f"Error connecting to Ollama at {ollama_url}: {e}"

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 syscall_analyzer.py <syscall_data_string> [ollama_url]")
        sys.exit(1)

    syscall_data = sys.argv[1]
    ollama_url = sys.argv[2] if len(sys.argv) > 2 else "http://10.0.2.2:11434"
    
    print("Analyzing syscalls...\n")
    analysis = analyze_syscalls(syscall_data, ollama_url)
    print("AI Analysis:")
    print("=" * 50)
    print(analysis)
    print("=" * 50)

if __name__ == "__main__":
    main()
