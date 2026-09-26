import json
import urllib.request

LLAMA_URL = "http://127.0.0.1:11434/completion"

def query_ollama(prompt):
    payload = {
        "prompt": prompt,
        "n_predict": 128,
        "temperature": 0.1,
        "stop": [".", "\n", "\n\n", "```\n", "}\n\n", "<|im_end|>"],
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        LLAMA_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("content", "").strip()

query = "install curl"
prompt = f"<|im_start|>system\nYou are an OS orchestrator. You dispatch tasks to shell commands. Output ONLY JSON: {{\"target_software\": \"sh\", \"action\": \"execute\", \"args\": [\"-c\", \"<command>\"]}}<|im_end|>\n<|im_start|>user\n{query}<|im_end|>\n<|im_start|>assistant\n"

print(query_ollama(prompt))
