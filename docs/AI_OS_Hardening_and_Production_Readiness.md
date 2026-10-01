# AI-Agent OS: Technical Hardening & Production Readiness Guide

## Overview

You've completed (custom syscall) + fine-tuning + dashboard. Now you need to make everything **rock solid and production-grade**. This document covers:

1. **Syscall hardening** — edge cases, error paths, concurrency
2. **Data collection architecture** — privacy-preserving, scalable, reliable
3. **Training pipeline** — data labeling, LoRA fine-tuning, model evaluation
4. **Fine-tuning methodology** — what works, what doesn't, how to measure improvement
5. **Dashboard improvements** — UX, performance, reliability
6. **Testing & validation** — how to verify everything works at scale

---

# PART 1: SYSCALL HARDENING

## Current State Assessment

Before hardening, audit your `sys_agent_query` implementation. Look for:

### 1a. Input Validation

**Checklist:**

```c
// ❌ MISSING: Size validation
SYSCALL_DEFINE5(agent_query, pid_t, pid, const char __user *, context, 
                 size_t, context_size, char __user *, response, size_t, response_size)
{
    // ❌ What if context_size = 1GB? Kernel would allocate 1GB + crash
    // ❌ What if pid = -1? Or a PID that doesn't exist?
    // ❌ What if response = NULL?
}

// ✅ CORRECT: Add bounds checking
#define MAX_CONTEXT_SIZE  (8 * 1024)      // 8KB max context
#define MAX_RESPONSE_SIZE (16 * 1024)     // 16KB max response

SYSCALL_DEFINE5(agent_query, pid_t, pid, const char __user *, context, 
                 size_t, context_size, char __user *, response, size_t, response_size)
{
    // Validate inputs
    if (context_size > MAX_CONTEXT_SIZE || context_size == 0)
        return -EINVAL;
    
    if (response_size > MAX_RESPONSE_SIZE || response_size == 0)
        return -EINVAL;
    
    if (!access_ok(VERIFY_READ, context, context_size))
        return -EFAULT;
    
    if (!access_ok(VERIFY_WRITE, response, response_size))
        return -EFAULT;
    
    // Only allow querying own process or processes you have permission for
    struct task_struct *target = find_task_by_vpid(pid);
    if (!target)
        return -ESRCH;  // No such process
    
    // Privilege check: can this UID see/query that process?
    if (!current_can_view_pid(target)) {
        put_task_struct(target);
        return -EPERM;  // Permission denied
    }
}
```

**Specific validations to add:**

| Issue | Check | Fix |
| ------- | ------- | ----- |
| Context too large | `context_size > MAX_CONTEXT_SIZE` | Return `-EINVAL` |
| Response too large | `response_size > MAX_RESPONSE_SIZE` | Return `-EINVAL` |
| NULL pointers | `!context OR !response` | Return `-EFAULT` |
| Invalid PID | `pid < 0 OR pid > pid_max` | Return `-ESRCH` |
| Cross-user query | `current_uid() != target_uid() AND !CAP_SYS_ADMIN` | Return `-EPERM` |
| Uninitialized memory | Copy uninitialized fields | Initialize all struct fields |

### 1b. Copy-to/from-User Safety

**Pattern to follow:**

```c
// ❌ DANGEROUS: Direct dereference of __user pointer
char *kernel_context = context;  // NO! Pointer is in userspace
memcpy(kernel_buf, kernel_context, len);  // Could crash

// ✅ CORRECT: Use copy_from_user
char kernel_context[MAX_CONTEXT_SIZE];
unsigned long copy_err = copy_from_user(kernel_context, context, context_size);
if (copy_err) {
    pr_warn("sys_agent_query: copy_from_user failed, %lu bytes uncopied\n", copy_err);
    return -EFAULT;
}

// Later, fill response and copy back
unsigned long copy_err = copy_to_user(response, kernel_response, response_len);
if (copy_err) {
    pr_warn("sys_agent_query: copy_to_user failed\n");
    return -EFAULT;
}
```

**Critical checklist:**

- [ ] Every `__user` pointer is checked with `access_ok()` BEFORE use
- [ ] Every `copy_from_user()` return value is checked
- [ ] Every `copy_to_user()` return value is checked
- [ ] No direct dereference of `__user` pointers
- [ ] All kernel buffers are stack or heap-allocated (not `__user`)

### 1c. Race Conditions & Concurrency

**The Problem:** Multiple processes can call `sys_agent_query` simultaneously. The `pending_requests[pid % 1024]` table has no locking.

**Current code (hypothetical):**

```c
#define PENDING_MAX 1024
struct pending_req pending_requests[PENDING_MAX];

sys_agent_query(...) {
    int idx = pid % PENDING_MAX;
    
    // ❌ RACE: Two processes with same pid%1024 both enter here
    // ❌ One might overwrite the other's request
    pending_requests[idx].pid = pid;
    pending_requests[idx].context = context;
    
    wait_event_interruptible_timeout(...);
}
```

**Fixes needed:**

**Option A: Per-bucket spinlock (simple, good for most cases)**

```c
#define PENDING_MAX 1024

struct pending_req {
    pid_t pid;
    char context[MAX_CONTEXT_SIZE];
    char response[MAX_RESPONSE_SIZE];
    wait_queue_head_t wq;
    int ready;  // 0 = waiting, 1 = response ready
} pending_requests[PENDING_MAX];

spinlock_t pending_locks[PENDING_MAX];

sys_agent_query(...) {
    int idx = pid % PENDING_MAX;
    spinlock_t *lock = &pending_locks[idx];
    
    spin_lock(lock);
    
    // Critical section: no other process can enter here for this idx
    if (pending_requests[idx].pid != 0) {
        spin_unlock(lock);
        return -EBUSY;  // Slot occupied, try again later
    }
    
    pending_requests[idx].pid = pid;
    // ... copy context
    pending_requests[idx].ready = 0;
    
    spin_unlock(lock);
    
    // Now outside spinlock, wait for daemon to respond
    int ret = wait_event_interruptible_timeout(
        pending_requests[idx].wq,
        pending_requests[idx].ready,
        msecs_to_jiffies(5000)  // 5 second timeout
    );
    
    spin_lock(lock);
    
    if (ret <= 0) {  // Timeout or signal
        pr_warn("sys_agent_query: timeout or signal for pid %d\n", pid);
        pending_requests[idx].pid = 0;  // Clear slot
        spin_unlock(lock);
        return (ret == 0) ? -ETIMEDOUT : -EINTR;
    }
    
    // Copy response back to userspace
    if (copy_to_user(response, pending_requests[idx].response, response_size)) {
        pending_requests[idx].pid = 0;
        spin_unlock(lock);
        return -EFAULT;
    }
    
    pending_requests[idx].pid = 0;  // Clear slot
    spin_unlock(lock);
    
    return 0;
}
```

**Option B: Per-PID request allocation (more robust, but more complex)**

```c
// Allocate a unique request struct per caller, use hash table to look up
// This avoids collisions entirely but requires more bookkeeping

struct pending_req {
    struct hlist_node node;  // For hash table
    pid_t pid;
    wait_queue_head_t wq;
    int ready;
    struct kref refcount;  // Ensure cleanup
    // ... context, response
};

static DEFINE_HASHTABLE(pending_ht, 10);  // 2^10 = 1024 buckets
static DEFINE_SPINLOCK(pending_ht_lock);

sys_agent_query(...) {
    struct pending_req *req = kmalloc(sizeof(*req), GFP_KERNEL);
    if (!req)
        return -ENOMEM;
    
    // Initialize
    kref_init(&req->refcount);
    req->pid = pid;
    init_waitqueue_head(&req->wq);
    req->ready = 0;
    
    // Copy context from userspace
    if (copy_from_user(req->context, context, context_size)) {
        kfree(req);
        return -EFAULT;
    }
    
    // Add to hash table
    spin_lock(&pending_ht_lock);
    hash_add(pending_ht, &req->node, pid);
    spin_unlock(&pending_ht_lock);
    
    // Notify daemon (netlink)
    netlink_notify_daemon(req);
    
    // Wait for response (timeout: 5s)
    int ret = wait_event_interruptible_timeout(req->wq, req->ready, msecs_to_jiffies(5000));
    
    if (ret <= 0) {
        pr_warn("sys_agent_query timeout/signal for pid %d\n", pid);
        spin_lock(&pending_ht_lock);
        hash_del(&req->node);
        spin_unlock(&pending_ht_lock);
        kfree(req);
        return (ret == 0) ? -ETIMEDOUT : -EINTR;
    }
    
    // Copy response back
    if (copy_to_user(response, req->response, response_size)) {
        spin_lock(&pending_ht_lock);
        hash_del(&req->node);
        spin_unlock(&pending_ht_lock);
        kfree(req);
        return -EFAULT;
    }
    
    // Cleanup
    spin_lock(&pending_ht_lock);
    hash_del(&req->node);
    spin_unlock(&pending_ht_lock);
    kfree(req);
    
    return 0;
}
```

**Recommendation:** Start with **Option A (per-bucket spinlock)** — it's simpler and sufficient unless you're hitting collisions in real testing. If performance issues arise (high syscall volume on same pid%1024), migrate to Option B.

### 1d. Error Handling & Resource Cleanup

**Checklist:**

```c
// For every allocation, verify cleanup on all error paths:

sys_agent_query(...) {
    // Allocate
    struct pending_req *req = kmalloc(...);
    if (!req) return -ENOMEM;  // ✓ Cleaned up implicitly (stack alloc)
    
    // Register with netlink
    int ret = netlink_notify_daemon(req);
    if (ret < 0) {
        kfree(req);  // ✓ Cleanup on error
        return ret;
    }
    
    // Wait (can be interrupted)
    ret = wait_event_interruptible_timeout(...);
    if (ret < 0) {
        // ✓ Cleanup on signal/timeout
        hash_del(&req->node);
        kfree(req);
        return -EINTR;
    }
    
    // Copy back (can fail)
    if (copy_to_user(...)) {
        // ✓ Cleanup on copy failure
        hash_del(&req->node);
        kfree(req);
        return -EFAULT;
    }
    
    // ✓ Cleanup on success
    hash_del(&req->node);
    kfree(req);
    return 0;
}
```

### 1e. Timeout Handling

**Current approach (likely):**

```c
// ❌ What if timeout fires but daemon responds anyway?
// Process wakes, returns to user. Daemon tries to write response to stale memory.
```

**Correct approach:**

```c
// ✓ Use reference counting to track validity
struct pending_req {
    struct kref refcount;  // Ensures cleanup only when all refs gone
    // ...
};

// Daemon increments ref before accessing request
// Syscall increments ref before waiting
// Timeout decrements ref
// Last ref holder frees the memory

// OR simpler: Verify request still exists in hash table before using

sys_agent_query(...) {
    // After timeout:
    spin_lock(&pending_ht_lock);
    if (hash_hashed(&req->node)) {
        // Still in table, safe to remove and cleanup
        hash_del(&req->node);
        ret = -ETIMEDOUT;
    } else {
        // Already removed by daemon (response arrived)
        ret = 0;  // Treat as success if data was copied
    }
    spin_unlock(&pending_ht_lock);
}
```

### 1f. Daemon Crash Resilience

**If daemon dies:**

- Any pending requests will timeout (5s)
- Process calling syscall wakes, returns error
- Kernel state is cleaned (requests removed from table, memory freed)
- Next call to syscall works fine

**Test this:**

```bash
# Terminal 1
./agent_daemon.py

# Terminal 2
./test_syscall 1234 "test context"  # This should hang for up to 5s

# Terminal 1: Ctrl+C to kill daemon (while test_syscall is waiting)

# Terminal 2: Should return -ETIMEDOUT after 5s
```

---

## Hardening Checklist: Before Moving Forward

- [ ] All input sizes validated against MAX_CONTEXT_SIZE, MAX_RESPONSE_SIZE
- [ ] All `__user` pointers checked with `access_ok()`
- [ ] All `copy_from_user()` / `copy_to_user()` return values checked
- [ ] Privilege checks (can only query own process or with CAP_SYS_ADMIN)
- [ ] Concurrency locking (spinlock or hash table refcounting) in place
- [ ] Every error path cleans up all allocated resources
- [ ] Timeout prevents permanent hangs (5 second timeout)
- [ ] Daemon crash doesn't crash kernel or hang processes indefinitely
- [ ] Test: daemon killed mid-request → syscall returns timeout after 5s ✓
- [ ] Test: two processes simultaneously querying → both get responses ✓
- [ ] Test: oversized context (>8KB) → returns -EINVAL ✓
- [ ] Test: NULL pointer → returns -EFAULT ✓
- [ ] Test: invalid PID → returns -ESRCH ✓
- [ ] dmesg clean (no warnings, oopses, lockdep reports) ✓

---

# PART 2: DATA COLLECTION ARCHITECTURE

## Design Principles

Good data collection is:

1. **Privacy-preserving** — no sensitive data (passwords, IPs, PII)
2. **Efficient** — minimal CPU/memory overhead on agent
3. **Reliable** — data is not lost (local buffer + flush to cloud)
4. **Queryable** — can reconstruct what happened on a server
5. **Scalable** — handles 10K syscalls/second without dropping samples

## 2a. Data Schema & Sampling Strategy

### What to Collect

```json
{
  "version": 1,
  "batch_id": "sha256(server_id + timestamp)",
  "server_id": "sha256(hostname + mac_address)",  // Anonymous
  "timestamp": 1727866496,  // Unix timestamp
  "batch_start": 1727866496,
  "batch_end": 1727866497,
  "sample_rate": 100,  // Every Nth syscall (100 = 1%)
  "sample_count": 1023,  // How many syscalls in this batch
  "syscalls": [
    {
      "timestamp": 1727866496.123,  // Microsecond precision
      "pid": 1234,
      "uid": 1000,
      "gid": 1000,
      "process_name": "python3",  // Basename only, no path
      "exe_hash": "sha256(/usr/bin/python3)",  // Hash of full executable
      "syscall_num": 2,  // open()
      "syscall_name": "open",
      "args": [
        "/var/www/index.html",  // Paths sanitized (no home dirs, no passwords)
        0  // Flags (no PII)
      ],
      "return_value": 12,
      "return_error": 0,  // 0 = success, ENOENT, EPERM, etc.
      "duration_us": 45  // Microseconds to execute
    }
  ]
}
```

### Sampling Strategy

**Why sample?** A busy server generates 10K syscalls/second. Sending all of them is bandwidth-prohibitive.

**Recommended approach:**

```c
// Adaptive sampling: higher for interesting syscalls, lower for boring ones

enum syscall_importance {
    BORING = 100,      // open(), read(), write() → sample 1%
    INTERESTING = 10,  // connect(), execve() → sample 10%
    CRITICAL = 1,      // execve(), fork() → sample 100%
};

// In your agent (Python or C):
if (should_sample(syscall_num)) {
    record_syscall(...);
}

// Example:
bool should_sample(int syscall_num) {
    switch (syscall_num) {
        case SYS_fork:
        case SYS_clone:
        case SYS_execve:
            return true;  // Always sample process lifecycle
        
        case SYS_connect:
        case SYS_bind:
            return (rand() % 10 == 0);  // 10% sample rate
        
        case SYS_open:
        case SYS_read:
        case SYS_write:
            return (rand() % 100 == 0);  // 1% sample rate
        
        default:
            return (rand() % 50 == 0);  // 2% sample rate
    }
}
```

**Rationale:**

- Process lifecycle (fork, execve) is CRITICAL → always sample
- Network (connect, bind) is interesting for security → 10% sample
- File I/O is high-volume but less interesting → 1% sample

**Result:** Your data is 10-100x smaller but still captures all important events.

### Sensitive Data Sanitization

**NEVER collect:**

- Full file paths that expose user directories (`/home/user/.ssh/...` → `/home/...` or hash)
- Command-line arguments (may contain passwords)
- Network payloads
- Environment variables
- Process credentials beyond uid/gid

**DO collect (safely):**

- Syscall numbers and return values (no PII here)
- Process name (basename, not full path)
- Hash of executable (allows matching across runs without storing path)
- Sanitized paths (if needed)

**Sanitization function:**

```python
def sanitize_path(path):
    """Remove sensitive parts from file paths."""
    # Never store /home, /root, or /etc paths in full
    if path.startswith("/home/"):
        return "/home/***"
    if path.startswith("/root/"):
        return "/root/***"
    if path.startswith("/etc/"):
        return "/etc/***"  # Ok to store, not sensitive in this context
    if path.startswith("/proc/"):
        # /proc/[pid]/... → /proc/*/...
        parts = path.split("/")
        if len(parts) >= 3 and parts[2].isdigit():
            parts[2] = "*"
        return "/".join(parts)
    return path

def sanitize_syscall_arg(arg_num, syscall_name, value):
    """Sanitize specific syscall arguments."""
    if syscall_name == "open" and arg_num == 0:
        # First arg to open() is a path
        return sanitize_path(value)
    if syscall_name in ["execve", "execveat"] and arg_num == 0:
        # First arg to execve() is executable path
        return sanitize_path(value)
    # For most other args, return as-is (they're flags, fds, numbers)
    return value
```

---

## 2b. Local Buffering & Reliability

**Problem:** Network is unreliable. Data can be lost mid-upload. Agent can crash.

**Solution:** Write to local disk first, then upload.

```python
import json
import os
import gzip
from pathlib import Path

class LocalBuffer:
    def __init__(self, buffer_dir="/var/ai-agent/syscall-buffer"):
        self.buffer_dir = buffer_dir
        os.makedirs(buffer_dir, exist_ok=True)
        
        # File format: syscall-buffer-{timestamp}.jsonl.gz
        # Each line is a complete syscall record (newline-delimited JSON)
        self.current_file = None
        self.current_file_path = None
        self.record_count = 0
        self.rotate_after_records = 1000  # Rotate every 1000 syscalls
    
    def append(self, syscall_record: dict):
        """Append a syscall record to local buffer."""
        # Auto-rotate file if needed
        if self.current_file is None or self.record_count >= self.rotate_after_records:
            self._rotate_file()
        
        # Write as newline-delimited JSON (can decompress and parse line-by-line)
        json_line = json.dumps(syscall_record) + "\n"
        
        try:
            self.current_file.write(json_line.encode("utf-8"))
            self.current_file.flush()  # Ensure written to disk
            self.record_count += 1
        except IOError as e:
            print(f"Error writing to buffer: {e}")
            # Don't crash; just drop this record
            pass
    
    def _rotate_file(self):
        """Close current file and open a new one."""
        if self.current_file:
            self.current_file.close()
        
        # New filename: syscall-buffer-{timestamp}.jsonl.gz
        import time
        timestamp = int(time.time())
        filename = f"syscall-buffer-{timestamp}.jsonl.gz"
        self.current_file_path = os.path.join(self.buffer_dir, filename)
        
        # Open with gzip for compression
        self.current_file = gzip.open(self.current_file_path, "ab")
        self.record_count = 0
    
    def list_pending_files(self):
        """List all buffered files ready for upload."""
        files = []
        for f in Path(self.buffer_dir).glob("syscall-buffer-*.jsonl.gz"):
            # Only return files that are not currently being written to
            if f.path != self.current_file_path:
                files.append(f)
        return sorted(files)
    
    def upload_and_delete(self, filepath, upload_fn):
        """Upload a file and delete if successful."""
        try:
            upload_fn(filepath)
            os.remove(filepath)
            print(f"Uploaded and deleted {filepath}")
        except Exception as e:
            print(f"Upload failed for {filepath}: {e}")
            # Retry later; don't delete
```

**Usage:**

```python
buffer = LocalBuffer()

# Append each syscall
for syscall in syscalls:
    buffer.append(syscall)

# Periodically (every 60 seconds), upload pending files
def upload_worker():
    while True:
        time.sleep(60)
        for f in buffer.list_pending_files():
            buffer.upload_and_delete(f, upload_to_backend)

# Run in background thread
upload_thread = threading.Thread(target=upload_worker, daemon=True)
upload_thread.start()
```

---

## 2c. Cloud Backend Architecture

**Stack recommendation:**

- **Time-series DB:** InfluxDB (write-heavy, queryable)
- **Message queue:** Kafka or Redis Streams (handle bursts)
- **Data lake:** S3 or GCS (immutable archive for training data)
- **Streaming:** Flink or Spark Streaming (optional, for real-time anomaly detection)

**Data flow:**

```
Agent's local buffer
        ↓
Upload (gzip'd JSONL) to backend API
        ↓
Receive request in backend
        ↓
Parse & validate
        ↓
Write to InfluxDB (for dashboards/queries)
        ↓
Write to Kafka topic (for training pipeline)
        ↓
Archive to S3 (immutable, for compliance)
        ↓
Respond 200 OK to agent
```

**Backend API (pseudocode):**

```python
from fastapi import FastAPI, File, UploadFile
import gzip
import json
from influxdb import InfluxDBClient

app = FastAPI()
influx = InfluxDBClient(host="localhost", port=8086, database="ai_os")
kafka = KafkaProducer(bootstrap_servers=["localhost:9092"])
s3 = boto3.client("s3")

@app.post("/api/v1/telemetry")
async def upload_telemetry(file: UploadFile):
    """
    Receive compressed syscall data from agent.
    
    Expected: gzip'd JSONL file with syscall records.
    """
    server_id = request.headers.get("X-Server-ID")
    
    if not server_id:
        return {"error": "X-Server-ID header required"}, 400
    
    # Read compressed data
    compressed = await file.read()
    
    try:
        decompressed = gzip.decompress(compressed)
        lines = decompressed.decode("utf-8").split("\n")
    except Exception as e:
        return {"error": f"Decompression failed: {e}"}, 400
    
    # Process each syscall record
    for line in lines:
        if not line.strip():
            continue
        
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"Skipping malformed JSON: {line[:100]}")
            continue
        
        # Validate record
        if not validate_record(record):
            print(f"Skipping invalid record: {record}")
            continue
        
        # Add server_id for tracking
        record["server_id"] = server_id
        
        # Write to InfluxDB (for dashboards)
        influx_point = {
            "measurement": "syscall",
            "tags": {
                "server_id": server_id,
                "process_name": record["process_name"],
                "syscall_name": record["syscall_name"],
            },
            "fields": {
                "duration_us": record["duration_us"],
                "return_value": record["return_value"],
            },
            "time": int(record["timestamp"] * 1e9),  # Convert to nanoseconds
        }
        influx.write_points([influx_point])
        
        # Publish to Kafka (for training pipeline)
        kafka.send("syscall-stream", json.dumps(record).encode("utf-8"))
        
        # Archive to S3 (immutable)
        s3.put_object(
            Bucket="ai-os-telemetry",
            Key=f"raw/{server_id}/{record['timestamp']}.jsonl",
            Body=line,
        )
    
    return {"status": "ok", "records_processed": len([l for l in lines if l.strip()])}

def validate_record(record):
    """Ensure required fields are present."""
    required = ["timestamp", "pid", "syscall_num", "syscall_name", "return_value"]
    return all(k in record for k in required)
```

---

# PART 3: TRAINING PIPELINE

## 3a. Data Preparation & Labeling

**Challenge:** Raw syscall sequences have no ground truth labels. You need to annotate them with "what was this process trying to do?"

### Strategy 1: Automated Heuristics (Fast, 70% Accuracy)

```python
def auto_label_syscall(syscall_record):
    """
    Assign a high-level intent label based on syscall pattern.
    Fast, no human input, but not perfect.
    """
    syscall_num = syscall_record["syscall_num"]
    syscall_name = syscall_record["syscall_name"]
    process_name = syscall_record["process_name"]
    
    # Network syscalls
    if syscall_name in ["connect", "bind", "listen", "accept"]:
        return "network_io"
    
    # File I/O
    if syscall_name in ["open", "read", "write", "close"]:
        return "file_io"
    
    # Process management
    if syscall_name in ["fork", "clone", "execve", "exit"]:
        return "process_mgmt"
    
    # Privilege escalation attempts
    if syscall_name in ["setuid", "setgid", "setcap", "prctl"]:
        return "privilege_change"
    
    # Memory management
    if syscall_name in ["mmap", "mprotect", "munmap"]:
        return "memory_mgmt"
    
    # Default
    return "other"

# Apply to all collected syscalls
labeled_syscalls = [auto_label_syscall(sc) for sc in raw_syscalls]
```

### Strategy 2: LLM-Assisted Labeling (Better Accuracy)

```python
import anthropic

client = anthropic.Anthropic()

def llm_label_syscall_sequence(syscall_sequence, batch_size=50):
    """
    Use Claude to label a sequence of syscalls.
    More accurate than heuristics, but slower and costs $ per call.
    
    Batch multiple syscalls per request to amortize cost.
    """
    
    # Group syscalls into sequences of up to 50
    batches = [syscall_sequence[i:i+batch_size] 
               for i in range(0, len(syscall_sequence), batch_size)]
    
    labeled = []
    for batch in batches:
        # Format syscalls for LLM
        syscall_text = "\n".join([
            f"{sc['timestamp']} {sc['process_name']}({sc['pid']}) "
            f"→ {sc['syscall_name']}({', '.join(map(str, sc['args']))}) "
            f"= {sc['return_value']}"
            for sc in batch
        ])
        
        prompt = f"""
Given this sequence of syscalls from a process, what is the high-level intent?
Choose ONE from: [file_io, network_io, process_mgmt, privilege_change, memory_mgmt, crypto, logging, other]

Syscalls:
{syscall_text}

Respond with ONLY the label, no explanation.
"""
        
        message = client.messages.create(
            model="claude-opus-4-5",  # Use your preferred model
            max_tokens=10,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        label = message.content[0].text.strip().lower()
        labeled.extend([{"syscall": sc, "intent": label} for sc in batch])
    
    return labeled

# Example
raw_syscalls = [...]  # Your collected syscall data
labeled = llm_label_syscall_sequence(raw_syscalls)

# Save for training
with open("labeled_syscalls.jsonl", "w") as f:
    for item in labeled:
        f.write(json.dumps(item) + "\n")
```

**Cost analysis:**

- Claude Opus: ~$3 per million input tokens
- 50 syscalls ≈ 500 tokens
- Labeling 100K syscalls = 200 API calls = ~$0.60
- Label accuracy: ~95% (much better than heuristics)

**Recommendation:** Use LLM labeling for your first 50K syscalls to build ground truth. After that, use heuristics + spot-check with LLM.

---

## 3b. Training Dataset Construction

**Goal:** Create a JSONL file where each line is a (syscall_sequence, intent_label) pair.

```python
import json
from collections import deque

def build_training_dataset(labeled_syscalls, window_size=10):
    """
    Convert individual labeled syscalls into training examples.
    
    Each training example is: "given this sequence of N syscalls,
    predict the high-level intent."
    """
    
    training_examples = []
    syscall_window = deque(maxlen=window_size)
    
    for labeled_sc in labeled_syscalls:
        syscall = labeled_sc["syscall"]
        intent = labeled_sc["intent"]
        
        syscall_window.append(syscall)
        
        # Only create example once we have enough context
        if len(syscall_window) == window_size:
            example = {
                "process_name": syscall["process_name"],
                "pid": syscall["pid"],
                "uid": syscall["uid"],
                "syscall_sequence": [
                    {
                        "name": sc["syscall_name"],
                        "args_hash": hash(str(sc["args"])),  # Anonymize args
                        "return": sc["return_value"],
                        "duration_us": sc["duration_us"],
                    }
                    for sc in syscall_window
                ],
                "intent": intent,
                "timestamp": syscall["timestamp"],
            }
            training_examples.append(example)
    
    return training_examples

# Build dataset
training_data = build_training_dataset(labeled_syscalls, window_size=10)

# Save
with open("training_data.jsonl", "w") as f:
    for example in training_data:
        f.write(json.dumps(example) + "\n")

print(f"Created {len(training_data)} training examples")
```

**Dataset statistics to track:**

```python
from collections import Counter

intents = [ex["intent"] for ex in training_data]
intent_dist = Counter(intents)

print("Intent distribution:")
for intent, count in intent_dist.most_common():
    print(f"  {intent}: {count} ({count/len(training_data)*100:.1f}%)")
```

Expected output:

```
Intent distribution:
  file_io: 45000 (45.0%)
  network_io: 20000 (20.0%)
  process_mgmt: 15000 (15.0%)
  other: 15000 (15.0%)
  privilege_change: 5000 (5.0%)
```

---

## 3c. LoRA Fine-Tuning Pipeline

**Goal:** Take a base LLM (e.g., Mistral 7B) and fine-tune it on your specific syscall-to-intent mapping.

```python
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model, TaskType
from datasets import Dataset
import json

# Load base model (mistral in this example)
MODEL_NAME = "mistralai/Mistral-7B-Instruct-v0.1"

# 4-bit quantization to fit on consumer GPU
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16
)

model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    quantization_config=bnb_config,
    device_map="auto"
)

tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
tokenizer.pad_token = tokenizer.eos_token

# LoRA config
lora_config = LoraConfig(
    r=8,                              # LoRA rank
    lora_alpha=16,                    # LoRA scaling
    lora_dropout=0.05,
    bias="none",
    task_type=TaskType.CAUSAL_LM,
    target_modules=["q_proj", "v_proj"],  # Target attention layers
)

model = get_peft_model(model, lora_config)
model.print_trainable_parameters()  # Shows parameter count

# Load training data
def load_training_data(jsonl_path, max_examples=10000):
    examples = []
    with open(jsonl_path) as f:
        for i, line in enumerate(f):
            if i >= max_examples:
                break
            ex = json.loads(line)
            examples.append(ex)
    return examples

training_examples = load_training_data("training_data.jsonl", max_examples=10000)

# Format for LLM training
def format_example(example):
    """Convert syscall sequence to LLM prompt + completion."""
    syscall_str = ", ".join([
        f"{sc['name']}→{sc['return']}"
        for sc in example["syscall_sequence"]
    ])
    
    prompt = f"""Process: {example['process_name']}
Syscalls: {syscall_str}

What is this process doing?"""
    
    completion = f" {example['intent']}"
    
    return {"text": prompt + completion}

formatted_data = [format_example(ex) for ex in training_examples]

# Create HuggingFace dataset
dataset = Dataset.from_dict({
    "text": [ex["text"] for ex in formatted_data]
})

# Split train/test
dataset = dataset.train_test_split(test_size=0.1)

# Training config
from transformers import TrainingArguments, Trainer

training_args = TrainingArguments(
    output_dir="./checkpoints",
    per_device_train_batch_size=4,
    per_device_eval_batch_size=4,
    gradient_accumulation_steps=4,
    learning_rate=2e-4,
    num_train_epochs=3,
    evaluation_strategy="steps",
    eval_steps=100,
    save_steps=500,
    logging_steps=10,
    warmup_steps=100,
    weight_decay=0.01,
    optim="paged_adamw_8bit",
    fp16=True,
    seed=42,
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=dataset["train"],
    eval_dataset=dataset["test"],
)

# Train
trainer.train()

# Save fine-tuned LoRA weights
model.save_pretrained("./agent_lora_v2")
tokenizer.save_pretrained("./agent_lora_v2")
```

**Training time estimates:**

- 10K examples: 30 minutes on RTX 3090
- 50K examples: 2-3 hours
- 100K examples: 4-6 hours

---

## 3d. Evaluation & Comparison

**Before deploying a new agent, verify it's actually better.**

```python
def evaluate_agent(model, test_dataset, tokenizer, max_eval_examples=500):
    """
    Evaluate agent accuracy on held-out test set.
    """
    correct = 0
    total = 0
    
    for i, example in enumerate(test_dataset):
        if i >= max_eval_examples:
            break
        
        # Format prompt (without answer)
        syscall_str = ", ".join([
            f"{sc['name']}→{sc['return']}"
            for sc in example["syscall_sequence"]
        ])
        
        prompt = f"""Process: {example['process_name']}
Syscalls: {syscall_str}

What is this process doing?"""
        
        # Generate prediction
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        outputs = model.generate(**inputs, max_new_tokens=10)
        prediction = tokenizer.decode(outputs[0], skip_special_tokens=True)
        
        # Extract predicted intent from generation
        prediction_intent = prediction.split("doing?\n")[-1].strip().lower()
        
        # Compare to ground truth
        if prediction_intent == example["intent"].lower():
            correct += 1
        else:
            # Debug: print mismatches
            if correct == 0:  # Print first few
                print(f"Mismatch: predicted '{prediction_intent}' but ground truth is '{example['intent']}'")
        
        total += 1
    
    accuracy = correct / total if total > 0 else 0
    print(f"Evaluation accuracy: {accuracy * 100:.1f}% ({correct}/{total})")
    return accuracy

# Evaluate old vs. new model
old_model = load_model("./agent_lora_v1")
new_model = load_model("./agent_lora_v2")

old_acc = evaluate_agent(old_model, test_dataset, tokenizer)
new_acc = evaluate_agent(new_model, test_dataset, tokenizer)

print(f"Old model: {old_acc*100:.1f}%")
print(f"New model: {new_acc*100:.1f}%")
print(f"Improvement: {(new_acc - old_acc)*100:+.1f}%")

if new_acc > old_acc:
    print("✓ New model is better, deploying...")
    deploy_model("./agent_lora_v2")
else:
    print("✗ New model is worse, keeping old version")
```

---

# PART 4: IMPROVED FINE-TUNING METHODOLOGY

## 4a. Continuous Learning Flywheel

**Conceptually:**

```
Month 1: Fine-tune on 10K syscall sequences → Agent v1.0
         ↓
Month 2: Collect 50K more syscalls from production + data
         Fine-tune on 60K total → Agent v1.1 (2% accuracy improvement)
         ↓
Month 3: Collect 100K more syscalls
         Fine-tune on 150K total → Agent v1.2 (4% improvement)
         ↓
Month 6: 500K syscall sequences → Agent v1.5 (15% improvement)
         ↓
Month 12: 2M syscall sequences → Agent v2.0 (30% improvement)
```

**The math:** Every doubling of training data gives ~3-5% accuracy improvement (follows scaling law).

### Implementation

```python
# Monthly fine-tuning job (runs automatically)

import datetime
from pathlib import Path

def monthly_finetuning_job():
    """
    Run every month to fine-tune agent on latest data.
    """
    
    # 1. Collect syscall data from past month
    cutoff_date = datetime.date.today() - datetime.timedelta(days=30)
    syscall_files = []
    for f in Path("/var/ai-agent/training_data/").glob("*.jsonl"):
        if datetime.datetime.fromtimestamp(f.stat().st_mtime).date() >= cutoff_date:
            syscall_files.append(f)
    
    print(f"Found {len(syscall_files)} syscall files from past month")
    
    # 2. Load all syscalls
    all_syscalls = []
    for f in syscall_files:
        with open(f) as file:
            for line in file:
                all_syscalls.append(json.loads(line))
    
    print(f"Loaded {len(all_syscalls)} syscall records")
    
    # 3. Label new syscalls (auto-label + spot-check with LLM)
    labeled = []
    for i, sc in enumerate(all_syscalls):
        if i % 100 == 0:  # Spot-check every 100th with LLM
            label = llm_label_syscall(sc)
        else:
            label = auto_label_syscall(sc)  # Fast heuristic
        
        labeled.append({"syscall": sc, "intent": label})
    
    print(f"Labeled {len(labeled)} syscalls")
    
    # 4. Combine with old data (don't throw away old examples)
    old_data = load_json("training_data_all.jsonl")  # Running dataset
    all_training_data = old_data + labeled
    
    # Remove duplicates, keep latest version
    # (In practice, use a proper deduplication strategy)
    all_training_data = list({
        (sc["syscall"]["timestamp"], sc["intent"]): sc
        for sc in all_training_data
    }.values())
    
    print(f"Total training data: {len(all_training_data)} examples")
    
    # 5. Split and train
    train_data = all_training_data[:-int(0.1*len(all_training_data))]  # 90% train
    test_data = all_training_data[-int(0.1*len(all_training_data)):]  # 10% test
    
    # Fine-tune (pseudocode)
    model = load_base_model()
    model = apply_lora_config(model)
    model = train(model, train_data)
    
    # 6. Evaluate
    new_acc = evaluate(model, test_data)
    old_acc = evaluate(load_model("agent_latest"), test_data)
    
    if new_acc > old_acc:
        # Save and deploy
        model.save_pretrained("agent_v{version}")
        deploy_to_fleet("agent_v{version}")
        print(f"✓ Deployed new agent (accuracy: {new_acc*100:.1f}%)")
    else:
        print(f"✗ New model worse ({new_acc*100:.1f}% vs {old_acc*100:.1f}%), rolling back")

# Schedule this to run monthly
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.add_job(monthly_finetuning_job, "cron", day=1, hour=0)  # 1st of month, midnight
scheduler.start()
```

---

## 4b. Multi-Adapter Strategy

**Insight:** Different workloads need different agents.

- **Web servers (nginx):** Focus on network + file I/O patterns
- **Databases (postgres):** Focus on memory + I/O patterns
- **Batch processing (python):** Focus on process + memory patterns

**Solution:** Train separate LoRA adapters per workload.

```python
# Train per-process-type adapters

processes_to_fine_tune = ["nginx", "postgres", "python", "redis", "node"]

for process_name in processes_to_fine_tune:
    # Filter training data to only this process
    process_specific_data = [
        ex for ex in all_training_data
        if ex["syscall"]["process_name"] == process_name
    ]
    
    if len(process_specific_data) < 1000:
        print(f"Skipping {process_name} (only {len(process_specific_data)} examples)")
        continue
    
    print(f"Training adapter for {process_name} ({len(process_specific_data)} examples)...")
    
    # Train
    model = load_base_model()
    lora_config = LoraConfig(...)
    model = get_peft_model(model, lora_config)
    model = train(model, process_specific_data)
    
    # Save
    model.save_pretrained(f"./adapters/{process_name}_lora")
    print(f"✓ Saved adapter for {process_name}")
```

**Deployment:** Agent selects adapter based on process name.

```python
def query_agent(process_name, syscall_sequence):
    # Load process-specific adapter if available
    if os.path.exists(f"./adapters/{process_name}_lora"):
        model = load_base_model()
        model = load_lora_adapter(f"./adapters/{process_name}_lora")
    else:
        # Fall back to generic adapter
        model = load_model("./adapters/generic_lora")
    
    # Generate response
    prediction = model.generate(format_syscalls(syscall_sequence))
    return prediction
```

---

# PART 5: DASHBOARD IMPROVEMENTS

## 5a. Architecture Overview

Current dashboard (probably): single-server view. Improve to:

```
Dashboard Backend (FastAPI)
    ├─ InfluxDB connector (query metrics)
    ├─ Kafka consumer (real-time syscall stream)
    ├─ Cache layer (Redis for hot queries)
    └─ WebSocket server (live updates)

Dashboard Frontend (React)
    ├─ Process tree view (parent-child hierarchy)
    ├─ Syscall timeline (chart of syscall rate over time)
    ├─ Agent interpretation (what is each process doing?)
    ├─ Anomaly highlights (unusual syscall patterns)
    └─ Real-time log view (live syscalls as they happen)
```

---

## 5b. Key Features to Add

### Feature 1: Process Tree with Live Updates

```javascript
// React component: shows process hierarchy with live syscall counts

function ProcessTree({ serverData, refreshInterval = 1000 }) {
    const [processes, setProcesses] = useState([]);
    
    useEffect(() => {
        // Fetch process tree
        const fetchProcessTree = async () => {
            const response = await fetch(`/api/v1/process-tree?server_id=${serverId}`);
            const data = await response.json();
            setProcesses(data.processes);
        };
        
        fetchProcessTree();
        const interval = setInterval(fetchProcessTree, refreshInterval);
        return () => clearInterval(interval);
    }, [serverData]);
    
    return (
        <div>
            <h2>Process Tree</h2>
            {processes.map(proc => (
                <ProcessNode key={proc.pid} process={proc} />
            ))}
        </div>
    );
}

function ProcessNode({ process }) {
    const [expanded, setExpanded] = useState(false);
    
    return (
        <div style={{ marginLeft: "20px" }}>
            <div onClick={() => setExpanded(!expanded)}>
                <span>{process.name}({process.pid})</span>
                <span style={{ color: "gray" }}> uid={process.uid}</span>
                <span style={{ color: "blue" }}> syscalls/sec: {process.syscall_rate}</span>
            </div>
            {expanded && process.children && (
                <div>
                    {process.children.map(child => (
                        <ProcessNode key={child.pid} process={child} />
                    ))}
                </div>
            )}
        </div>
    );
}
```

**Backend (FastAPI):**

```python
@app.get("/api/v1/process-tree")
def get_process_tree(server_id: str):
    """Return process tree with syscall rates."""
    # Query InfluxDB for syscall rates per process
    query = f"""
        SELECT MEAN(duration_us) as avg_duration, COUNT(*) as syscall_count
        FROM syscall
        WHERE server_id='{server_id}' AND time > now() - 1m
        GROUP BY pid
    """
    syscall_stats = influx.query(query)
    
    # Build tree structure
    ps_output = execute("ps auxf")  # Get current process tree
    tree = parse_ps_output(ps_output)
    
    # Annotate with syscall stats
    annotate_tree_with_stats(tree, syscall_stats)
    
    return {"processes": tree}
```

### Feature 2: Syscall Timeline Chart

```javascript
import { LineChart, Line, XAxis, YAxis, CartesianGrid } from "recharts";

function SyscallTimeline({ serverData, processId = null }) {
    const [data, setData] = useState([]);
    
    useEffect(() => {
        const fetchMetrics = async () => {
            const params = new URLSearchParams({
                server_id: serverId,
                time_range: "1h",
                ...(processId && { pid: processId }),
            });
            
            const response = await fetch(`/api/v1/syscall-metrics?${params}`);
            const metrics = await response.json();
            setData(metrics.timeline);
        };
        
        fetchMetrics();
        const interval = setInterval(fetchMetrics, 5000);  // Refresh every 5s
        return () => clearInterval(interval);
    }, [processId]);
    
    return (
        <LineChart width={800} height={300} data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="time" />
            <YAxis />
            <Line type="monotone" dataKey="syscall_count" stroke="#8884d8" />
            <Line type="monotone" dataKey="anomaly_score" stroke="#ff7300" />
        </LineChart>
    );
}
```

**Backend metric calculation:**

```python
@app.get("/api/v1/syscall-metrics")
def get_syscall_metrics(server_id: str, time_range: str = "1h", pid: int = None):
    """Return syscall metrics over time."""
    
    query = f"""
        SELECT MEAN(duration_us) as avg_duration, COUNT(*) as syscall_count
        FROM syscall
        WHERE server_id='{server_id}' AND time > now() - {time_range}
        {'AND pid=' + str(pid) if pid else ''}
        GROUP BY time(1m)
    """
    
    results = influx.query(query)
    
    # Calculate anomaly score (Z-score)
    timeline = []
    for point in results:
        count = point["syscall_count"]
        baseline = np.mean([p["syscall_count"] for p in results[-10:]])  # Last 10 points
        std_dev = np.std([p["syscall_count"] for p in results[-10:]])
        
        anomaly_score = abs((count - baseline) / std_dev) if std_dev > 0 else 0
        
        timeline.append({
            "time": point["time"],
            "syscall_count": count,
            "anomaly_score": anomaly_score,
        })
    
    return {"timeline": timeline}
```

### Feature 3: Agent Interpretation

```javascript
function AgentInterpretation({ processId }) {
    const [interpretation, setInterpretation] = useState(null);
    const [loading, setLoading] = useState(true);
    
    useEffect(() => {
        const fetchInterpretation = async () => {
            const response = await fetch(`/api/v1/process-interpretation?pid=${processId}`);
            const data = await response.json();
            setInterpretation(data);
            setLoading(false);
        };
        
        fetchInterpretation();
    }, [processId]);
    
    if (loading) return <div>Loading...</div>;
    
    return (
        <div style={{ border: "1px solid #ccc", padding: "10px" }}>
            <h3>What is this process doing?</h3>
            <p><strong>Intent:</strong> {interpretation.intent}</p>
            <p><strong>Confidence:</strong> {(interpretation.confidence * 100).toFixed(1)}%</p>
            <p><strong>Recent syscalls:</strong></p>
            <ul>
                {interpretation.recent_syscalls.map((sc, i) => (
                    <li key={i}>{sc.name}({sc.args}) → {sc.return}</li>
                ))}
            </ul>
            {interpretation.anomalies.length > 0 && (
                <div style={{ color: "red" }}>
                    <p><strong>Anomalies detected:</strong></p>
                    <ul>
                        {interpretation.anomalies.map((a, i) => (
                            <li key={i}>{a}</li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}
```

**Backend:**

```python
@app.get("/api/v1/process-interpretation")
def get_process_interpretation(pid: int):
    """Query agent for interpretation of a process."""
    
    # Get recent syscalls for this PID
    query = f"""
        SELECT syscall_name, args, return_value
        FROM syscall
        WHERE pid={pid}
        ORDER BY time DESC
        LIMIT 20
    """
    
    recent_syscalls = influx.query(query)
    
    # Format for agent
    syscall_sequence = [
        {"name": sc["syscall_name"], "return": sc["return_value"]}
        for sc in recent_syscalls
    ]
    
    # Query agent
    try:
        intent, confidence = query_agent(syscall_sequence)
    except Exception as e:
        intent = "unknown"
        confidence = 0
    
    # Detect anomalies
    anomalies = []
    for sc in recent_syscalls:
        if is_anomalous(sc):
            anomalies.append(f"{sc['syscall_name']} to {sc['args'][0]}")
    
    return {
        "pid": pid,
        "intent": intent,
        "confidence": confidence,
        "recent_syscalls": recent_syscalls,
        "anomalies": anomalies,
    }
```

### Feature 4: Real-Time Log View (WebSocket)

```javascript
function RealtimeLogView({ serverId }) {
    const [logs, setLogs] = useState([]);
    
    useEffect(() => {
        // Connect to WebSocket
        const ws = new WebSocket(`wss://backend.example.com/ws/syscalls?server_id=${serverId}`);
        
        ws.onmessage = (event) => {
            const syscall = JSON.parse(event.data);
            setLogs(prev => [syscall, ...prev.slice(0, 99)]);  // Keep last 100
        };
        
        return () => ws.close();
    }, [serverId]);
    
    return (
        <div style={{ fontFamily: "monospace", height: "400px", overflow: "auto" }}>
            <h3>Live Syscalls</h3>
            {logs.map((log, i) => (
                <div key={i} style={{ fontSize: "12px", lineHeight: "1.4" }}>
                    <span style={{ color: "blue" }}>{log.process_name}({log.pid})</span>
                    {" → "}
                    <span style={{ color: "green" }}>{log.syscall_name}</span>
                    {" = "}
                    <span>{log.return_value}</span>
                </div>
            ))}
        </div>
    );
}
```

**Backend (FastAPI with WebSockets):**

```python
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
    
    async def connect(self, server_id: str, websocket: WebSocket):
        await websocket.accept()
        if server_id not in self.active_connections:
            self.active_connections[server_id] = []
        self.active_connections[server_id].append(websocket)
    
    async def broadcast(self, server_id: str, message: dict):
        if server_id in self.active_connections:
            for connection in self.active_connections[server_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass  # Connection closed

manager = ConnectionManager()

@app.websocket("/ws/syscalls")
async def websocket_syscalls(websocket: WebSocket, server_id: str):
    await manager.connect(server_id, websocket)
    
    # Subscribe to Kafka topic and stream to WebSocket
    try:
        for message in kafka_consumer.subscribe(f"syscall-stream-{server_id}"):
            await manager.broadcast(server_id, json.loads(message.value))
    except Exception:
        pass
```

---

## 5c. Performance Optimizations

### Problem: Querying 1M syscalls is slow

**Solution: Pre-computed aggregates**

```python
# Every minute, compute aggregates and store in InfluxDB

def compute_minute_aggregates():
    """
    Instead of querying raw syscalls, query pre-computed minute-level stats.
    Much faster.
    """
    
    # For each (server_id, process_name, syscall_name), compute:
    # - Count (how many times this syscall happened)
    # - Mean duration
    # - P95 duration
    # - Error rate
    
    query = """
        SELECT
            COUNT(*) as count,
            MEAN(duration_us) as mean_duration,
            PERCENTILE(duration_us, 95) as p95_duration,
            SUM(CASE WHEN return_error > 0 THEN 1 ELSE 0 END) as error_count
        FROM syscall
        WHERE time > now() - 1m
        GROUP BY server_id, process_name, syscall_name
    """
    
    results = influx.query(query)
    
    # Write aggregates back to InfluxDB (different measurement)
    aggregates = [
        {
            "measurement": "syscall_aggregate_1m",
            "tags": {
                "server_id": result["server_id"],
                "process_name": result["process_name"],
                "syscall_name": result["syscall_name"],
            },
            "fields": {
                "count": result["count"],
                "mean_duration_us": result["mean_duration"],
                "p95_duration_us": result["p95_duration"],
                "error_count": result["error_count"],
            },
        }
        for result in results
    ]
    
    influx.write_points(aggregates)

# Schedule this to run every minute
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.add_job(compute_minute_aggregates, "interval", minutes=1)
scheduler.start()
```

### Problem: Dashboard loads slow with 100s of servers

**Solution: Lazy loading + pagination**

```javascript
function ServerList() {
    const [servers, setServers] = useState([]);
    const [page, setPage] = useState(0);
    const pageSize = 10;
    
    useEffect(() => {
        const fetchServers = async () => {
            const response = await fetch(
                `/api/v1/servers?page=${page}&page_size=${pageSize}&sort=-last_seen`
            );
            const data = await response.json();
            setServers(data.servers);
        };
        
        fetchServers();
    }, [page]);
    
    return (
        <div>
            {servers.map(server => (
                <ServerCard key={server.id} server={server} />
            ))}
            <div>
                <button onClick={() => setPage(page - 1)} disabled={page === 0}>
                    Previous
                </button>
                <span>Page {page + 1}</span>
                <button onClick={() => setPage(page + 1)}>
                    Next
                </button>
            </div>
        </div>
    );
}
```

---

# PART 6: TESTING & VALIDATION CHECKLIST

Before moving to Tier 2 SaaS launch, verify all of this works:

## Syscall Hardening Testing

- [ ] `test_syscall_bounds.c` — oversized context/response → returns -EINVAL
- [ ] `test_syscall_privilege.c` — unprivileged process querying another user's PID → returns -EPERM
- [ ] `test_syscall_concurrent.c` — 100 processes calling sys_agent_query simultaneously → all succeed with correct responses
- [ ] `test_daemon_crash.c` — kill daemon mid-request → syscall returns -ETIMEDOUT after 5s (not -EINTR)
- [ ] `dmesg clean` — no oopses, lockdep warnings, or RCU stalls during above

## Data Collection Testing

- [ ] Telemetry flows from agent → backend reliably (0% loss over 1 hour)
- [ ] Data is correctly sanitized (no full paths, no home dirs, no passwords)
- [ ] Sampling rate works correctly (1% rate = 1% of syscalls captured)
- [ ] Disk buffer survives daemon restart (buffered data is uploaded after recovery)
- [ ] Compression works (gzip'd data is 10-50x smaller than raw)

## Training Pipeline Testing

- [ ] Auto-label achieves ~70% accuracy on ground truth
- [ ] LLM-label achieves ~95% accuracy
- [ ] Training data loading handles malformed JSON gracefully (skip, log, continue)
- [ ] LoRA fine-tuning completes in <3 hours on 10K examples
- [ ] Evaluation shows measurable improvement (new model > old model on test set)

## Agent Testing

- [ ] Agent response time: <500ms on average (including LLM inference)
- [ ] Agent accuracy on held-out test set: ≥80%
- [ ] Process-specific adapters are loaded correctly
- [ ] Fallback to generic adapter if process-specific not available

## Dashboard Testing

- [ ] Process tree loads in <1 second (even with 1000 processes)
- [ ] Syscall timeline renders smoothly with 1 hour of data
- [ ] Real-time log view updates within 1 second of syscall
- [ ] Agent interpretation displays correctly with confidence score
- [ ] No frontend errors in browser console

## End-to-End Testing

- [ ] Boot fresh VM, run OS with all components
- [ ] Let run for 24 hours unattended
- [ ] Verify:
  - No kernel crashes/oopses
  - Telemetry flowing correctly
  - Dashboard updates in real-time
  - Agent providing sensible interpretations
  - No file descriptor/memory leaks

---

## Success Criteria for "Strong" State

You can move forward to Tier 2 SaaS launch when:

1. ✅ **Syscall is rock-solid** — hardening checklist complete, all tests pass
2. ✅ **Data collection is reliable** — 99.9% uptime, 0% data loss over 24 hours
3. ✅ **Agent accuracy is good** — ≥85% on real-world syscall sequences
4. ✅ **Dashboard is responsive** — all pages load in <2 seconds, real-time updates work
5. ✅ **24-hour soak test passes** — no crashes, memory leaks, or data loss
