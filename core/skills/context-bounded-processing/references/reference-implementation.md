# Reference implementation (context-bounded-processing)

Read this when actually building the init/process/merge loop. **The agent orchestrates; Python manages state:**

## Step 1: Init (Python)

```python
# Agent calls: python(code="...")
import os, json, time

TASK_DIR = os.environ["CILI_TMP"]
INPUT_FILE = "input.txt"
OUTPUT_FILE = "output.txt"
task_id = f"task_{int(time.time())}"

# Read and split input
with open(INPUT_FILE, encoding="utf-8") as f:
    text = f.read()

def split_into_chunks(text, chunk_size=10000):
    paragraphs = text.split('\n\n')
    chunks, current, size = [], [], 0
    for para in paragraphs:
        if size + len(para) > chunk_size and current:
            chunks.append('\n\n'.join(current))
            current, size = [para], len(para)
        else:
            current.append(para)
            size += len(para)
    if current:
        chunks.append('\n\n'.join(current))
    return chunks

chunks = split_into_chunks(text)

# Create task directory and save chunks
task_dir = f"{TASK_DIR}/{task_id}"
os.makedirs(f"{task_dir}/chunks", exist_ok=True)
os.makedirs(f"{task_dir}/results", exist_ok=True)

for i, chunk in enumerate(chunks):
    with open(f"{task_dir}/chunks/chunk_{i:03d}.txt", "w", encoding="utf-8") as f:
        f.write(chunk)

# Init state
state = {
    "task_id": task_id,
    "status": "running",
    "phase": "process",
    "file": {"path": INPUT_FILE, "size_chars": len(text), "total_chunks": len(chunks)},
    "cursor": {"next_chunk_idx": 0},
    "chunks": {"total": len(chunks), "done": 0, "failed": 0},
    "processed": {"chunks": 0},
    "result": {"output_path": OUTPUT_FILE},
    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
}
with open(f"{task_dir}/state.json", "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2, ensure_ascii=False)

print(json.dumps({"status": "initialized", "task_id": task_id, "total_chunks": len(chunks)}))
```

## Step 2: Process each chunk (agent loop)

Read `state.json` / list `results/` once, then for each chunk:

1. `read` the chunk and process it in-context, then `write` the result:
   ```
   read("$CILI_TMP/{task_id}/chunks/chunk_000.txt")     # → chunk text
   # ... translate / summarize / extract in-context ...
   write("$CILI_TMP/{task_id}/results/result_000.txt", <processed text>)
   ```

2. The existence of `result_000.txt` **is** the progress record — do NOT call python after every chunk (that wastes 1 of your ~200 tool calls per chunk and halves your throughput).

3. Checkpoint (~every 10 chunks, or when resuming) — one python call that re-reads the results dir, updates `state.json`, and prints missing chunks:
   ```python
   # Agent calls: python(code="...") — every ~10 chunks, or once when resuming
   import json, os, glob
   base = os.path.join(os.environ["CILI_TMP"], "{task_id}")
   done = set()
   for p in glob.glob(os.path.join(base, "results", "result_*.txt")):
       name = os.path.basename(p)                     # result_000.txt
       done.add(int(name[len("result_"):-len(".txt")]))
   with open(os.path.join(base, "state.json")) as f:
       state = json.load(f)
   total = state["file"]["total_chunks"]
   next_idx = next((i for i in range(total) if i not in done), total)
   state["cursor"]["next_chunk_idx"] = next_idx
   state["chunks"]["done"] = len(done)
   state["processed"]["chunks"] = len(done)
   import time; state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
   with open(os.path.join(base, "state.json"), "w") as f:
       json.dump(state, f, indent=2, ensure_ascii=False)
   missing = [i for i in range(total) if i not in done]
   print(json.dumps({"progress": f"{len(done)}/{total}",
                     "next": next_idx, "missing": missing}))
   ```

4. Repeat until all chunks are done.

**Resume:** the first step of ANY processing task is to list `results/` and read `state.json`, then process the **lowest-numbered missing chunk**. This makes re-delegation with the same `task_id` safe after a `max_iterations` stop or context compaction — missing chunks get processed, done chunks are never redone.

## Step 3: Merge (Python)

```python
# Agent calls: python(code="...")
import os, json, shutil

TASK_DIR = os.environ["CILI_TMP"]
task_id = "..."  # from state
OUTPUT_FILE = "output.txt"
task_dir = f"{TASK_DIR}/{task_id}"

# Load state to get total chunks
with open(f"{task_dir}/state.json") as f:
    state = json.load(f)

total = state["file"]["total_chunks"]

# Merge all results
results = []
for i in range(total):
    result_path = f"{task_dir}/results/result_{i:03d}.txt"
    if os.path.exists(result_path):
        with open(result_path, encoding="utf-8") as f:
            results.append(f.read())

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    f.write('\n\n'.join(results))

# Update state and cleanup
state["status"] = "completed"
state["phase"] = "done"
with open(f"{task_dir}/state.json", "w") as f:
    json.dump(state, f, indent=2, ensure_ascii=False)

shutil.rmtree(task_dir, ignore_errors=True)
print(json.dumps({"status": "completed", "output": OUTPUT_FILE, "chunks_merged": len(results)}))
```

## HTTP Request Batch Processing Pattern

When processing multiple HTTP requests, apply the same single-call-per-invocation rule. The agent orchestrates; Python does one call at a time:

```python
# Agent calls: python(code="...") — processes exactly 1 URL
import requests, json, os

state_path = os.path.join(os.environ["CILI_TMP"], "task_abc/state.json")
with open(state_path) as f:
    state = json.load(f)

idx = state["cursor"]["next_url_idx"]
urls = state["urls"]

if idx >= len(urls):
    print(json.dumps({"status": "completed"}))
else:
    url = urls[idx]
    try:
        response = requests.get(url, timeout=30)
        state.setdefault("results", []).append(response.json())
    except Exception as e:
        state.setdefault("errors", []).append({"url": url, "error": str(e)})

    state["cursor"]["next_url_idx"] = idx + 1
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    if idx + 1 >= len(urls):
        print(json.dumps({"status": "completed", "processed": idx + 1}))
    else:
        print(json.dumps({"status": "running", "processed": idx + 1, "remaining": len(urls) - idx - 1}))
```

**Do NOT loop in Python:**
```python
# ❌ WRONG - may timeout
for url in urls:
    requests.get(url)  # 100 HTTP requests = slow

# ✅ CORRECT - agent calls Python once per URL, agent handles the loop
```
