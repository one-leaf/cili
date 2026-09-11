---
name: Context-Bounded Processing (Runtime)
description: General chunked-processing protocol for tasks that exceed the context window — the worker processes each chunk in-context, Python manages state. For translating large Office/PDF documents, use the translate-large-document skill instead.
roles: [worker]
---

## Core Principle

> **The agent (Worker) owns orchestration. Python manages state. The agent processes each bounded chunk in its own context.**

Context-bounded processing MUST be implemented as a **resumable stateful workflow**. This applies when:
- Processing files too large for the LLM context window
- Analyzing multiple files whose combined content exceeds context
- Batch database results that need semantic processing
- Any task where the total data exceeds what can fit in one LLM call

> **Not for large-document translation** (docx/xlsx/pdf/doc/xls) — use the `translate-large-document` skill instead (per-block Lite sub-agents + merge). This in-context loop is for summarization, extraction, analysis, and other bounded processing.

The agent calls tools step by step — Python for state management and file splitting, `read` + in-context processing for each chunk — rather than expecting a single Python call to complete the entire task.

```
Worker (orchestrator)
    │
    │ 1. python(code="init + split chunks + save state")
    ▼
    State saved to $CILI_TMP/{task_id}/state.json
    │
    │ 2. read chunk_000.txt → process in-context → write result_000.txt
    │ 3. read chunk_001.txt → process in-context → write result_001.txt
    │    ... repeat until all chunks done ...
    │    (result_NNN.txt IS the progress record — no per-chunk python call;
    │     python checkpoint every ~10 chunks, or once when resuming)
    │
    │ 4. python(code="merge results + cleanup")
    ▼
    Final output ready
```

## Execution Model

```
Worker (orchestrator loop)
  │
  ├─ python: init phase → split input, save chunks, init state.json
  │
  ├─ for each chunk:
  │    ├─ read + in-context: process one chunk → write result file
  │    └─ (no per-chunk python call — result_NNN.txt IS the progress record)
  │
  ├─ python: checkpoint every ~10 chunks (optional) — update state.json
  │
  └─ python: merge phase → combine all results, cleanup
```

The agent reads `state.json` / lists `results/` at start to know progress, then decides the next step. Python is called for discrete operations — never expected to loop over slow external calls internally.

## Python Execution Model

**How Python actually works in this system:**

- Python is a **tool call**, not an autonomous worker. The agent calls `python(code="...")` and gets the result back.
- Each Python invocation runs code and returns — it does NOT orchestrate loops or make repeated external calls.
- Python is ideal for: state management, file splitting, data transformation, chunking, merging results.
- Python MUST NOT loop over slow external operations (HTTP requests, etc.) — the agent handles that orchestration.

```python
# ✅ CORRECT: Python does one bounded operation
import json, os

state_path = os.path.join(os.environ["CILI_TMP"], "task_123/state.json")
with open(state_path) as f:
    state = json.load(f)

# Process next chunk
idx = state["cursor"]["next_chunk_idx"]
chunk_file = os.path.join(os.environ["CILI_TMP"], f"task_123/chunks/chunk_{idx:03d}.txt")
with open(chunk_file, encoding="utf-8") as f:
    chunk = f.read()

print(json.dumps({"chunk_idx": idx, "chunk_path": chunk_file, "chunk_size": len(chunk)}))
```

```python
# ❌ WRONG: Python looping over slow external calls
import requests

for url in urls:          # May timeout!
    response = requests.get(url)
    results.append(response.json())
```

## Chunk Processing

There is no standalone `llm` tool — the agent **is** the LLM. To process a chunk, `read` the chunk file, process the text directly in the agent's own context, then `write` the result file. Each chunk stays within the context window by construction (see Chunk Size Guidance), which is exactly why this bounded loop is safe.

### Processing a Chunk

```
// 1. read the chunk (stays small enough to fit in context)
read("$CILI_TMP/{task_id}/chunks/chunk_000.txt")

// 2. process the text in-context (translate / summarize / extract ...)

// 3. write the result immediately
write("$CILI_TMP/{task_id}/results/result_000.txt", <processed text>)
```

### Structured Output

There is no schema enforcement from a tool. Define the expected output structure in your own instruction to yourself, and write it in a consistent, machine-parseable form (e.g. one JSON object per result file):

```
// To extract structured data, process the chunk and write a JSON result:
// {"companies": ["Acme Corp", "GlobalTech"]}
```

### Common Patterns

- **Translation**: `read` chunk → translate in-context → `write` result
- **Summarization**: `read` chunk → summarize in-context → `write` result
- **Extraction**: `read` chunk → extract fields in-context → `write` result as JSON

## Persistent State

Task state MUST be persisted in `$CILI_TMP/{task_id}/state.json` outside the Python process.

```json
{
  "task_id": "task_abc123",
  "status": "running",
  "phase": "process",

  "file": {
    "path": "input.txt",
    "size_chars": 2_400_000,
    "total_chunks": 100
  },

  "cursor": {
    "next_chunk_idx": 35
  },

  "chunks": {
    "total": 100,
    "done": 35,
    "failed": 0
  },

  "processed": {
    "chunks": 35
  },

  "result": {
    "output_path": "output.txt"
  },

  "updated_at": "2026-08-23 14:30:00"
}
```

**The results directory is the fine-grained progress source** — `result_035.txt` existing means chunk 35 is done, so progress survives even if `state.json` is stale. `state.json` holds task metadata (input/output, total chunks) plus a checkpoint. Update `state.json` **once at init, then as a periodic checkpoint every ~10 chunks, and again when resuming** — never after every chunk (that is a wasted python call; the result file already records progress). On resume, compute the next chunk from `state.json` AND the results dir: process the **lowest-numbered missing result**, so re-running never redoes or skips chunks.

## Processing Phases

A context-bounded task SHOULD use explicit phases:

```
INIT     → analyze input, split into chunks, init state
PROCESS  → process chunks in-context (one per step)
MERGE    → merge result files into final output
DONE     → cleanup, return final result
```

The agent tracks which phase it is in via `state.json` and resumes accordingly.

## Execution Budget

**Important: Different limits for different scenarios**

### Worker Python + slow external calls

Each Python invocation MUST execute exactly **1 slow external call** and return immediately. Applies to:
- HTTP requests — `requests.get()`, `requests.post()`, API calls, web scraping with multiple pages
- Any network I/O that may be slow or timeout

(In-context chunk processing is done by the agent via `read`/`write`, not from within Python.)

Do NOT loop over multiple calls in a single Python invocation — this risks timeout (300s hard limit). The agent should orchestrate the loop: call Python → read progress → read chunk + process in-context → call Python to update state → repeat.

```python
CALLS_PER_INVOCATION = 1   # MUST be 1 — never loop slow external calls in Python
```

**Why single call per invocation?**
- Each external call is slow (network latency, LLM inference)
- Looping multiple calls risks hitting the hard timeout (300s)
- The agent should orchestrate the loop: call Python → read progress → read chunk + process in-context → repeat

### Python + normal operations (file I/O, data processing, etc.)

If Python is NOT making slow external calls, it can loop normally without this restriction:
- File reading/writing: OK to process all files in one invocation
- Data transformation: OK to loop over all records
- System commands: OK to execute multiple bash commands

### Worker iteration budget — how much fits in one run

A Worker sub-agent has a ~200-tool-call budget (`max_iterations`, default 200) plus a check phase. Each chunk costs **2 calls** (`read` + `write`), so **one run handles ~90 chunks** (~180 calls, leaving room for the check phase):

| chunk_size | chars per run |
|------------|---------------|
| 10000 | ~900K chars |
| 20000-30000 | ~2M-2.7M chars |

**Files larger than one run must be processed across multiple runs via resume:**

- When a run stops at `max_iterations`, the results so far and `state.json` are already on disk under `$CILI_TMP/{task_id}/` — nothing is lost.
- Re-delegate the same task with the **same `task_id`**: the worker's first step re-reads `state.json` and lists `results/`, then continues from the lowest missing chunk (see Resume below). Never restart the task from scratch.
- A partial run's summary must say explicitly how many chunks are done and that `task_id` should be re-delegated.

### Chunk Size Guidance

**Default chunk_size = 10000 characters** (conservative for translation tasks)

**Calculation formula:**
- Output limit: max_tokens = 16384
- Translation expansion factor: 2.5x (text may grow when translated)
- Available input tokens: 16384 / 2.5 = 6554 tokens
- Character conversion:
  - Chinese: ~1.5 chars/token → 6554 × 1.5 ≈ 9800 chars → **10000 chars**
  - English: ~3.5 chars/token → 6554 × 3.5 ≈ 23000 chars

**Why conservative?**
- Prevents output truncation during translation tasks
- Safer for mixed-language content
- Slightly more API calls but more reliable

**For non-translation tasks** (summarization, extraction) with smaller output:
- Can increase chunk_size to 20000-30000 chars
- Adjust based on expected output size

## Reference Implementation Pattern

The following pattern shows how to orchestrate context-bounded processing. **The agent orchestrates; Python manages state:**

### Step 1: Init (Python)

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

### Step 2: Process each chunk (agent loop)

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

### Step 3: Merge (Python)

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

## Key Rules

1. **Temp files must live under `$CILI_TMP/`** — All split chunks, intermediate results, state files, and temporary text MUST be written inside `$CILI_TMP/{task_id}/` (i.e., `data/tmp/{task_id}/`). Never scatter temp files in the working directory. Only the final output is written to the user-specified path. On failure the user can simply delete the entire `data/tmp/` directory to clean up.
2. **Agent orchestrates, Python manages state** — The agent (Worker) calls tools step by step. Python handles state management, file splitting, and result merging. Never let Python loop over slow external calls.
3. **Process each chunk in-context** — `read` the chunk, process the text directly (the agent is the LLM), then `write` the result. Never invoke the LLM from within Python code.
4. **Single external call per Python invocation** — Each Python call processes at most 1 slow external operation (HTTP request, etc.) then returns; the agent handles loop orchestration.
5. **Results dir owns progress** — `result_NNN.txt` existing is the progress record; `state.json` holds metadata + a checkpoint. Never rely on the agent's conversation context for progress.
6. **Normal operations can loop** — Local operations such as file I/O, data transformation, and bash commands may process all items in a single Python invocation.
7. **Split by paragraph boundaries** — Preserve completeness; never cut mid-sentence.
8. **Persist immediately** — `write` each result file right after processing it; update `state.json` as a periodic checkpoint (~every 10 chunks), not after every chunk.
9. **Do not hardcode file content** — Never write large file contents as string literals in Python code.
10. **Keep each step bounded in context** — Split large files via Python; process exactly one chunk per step in the agent context (chunk_size keeps each within the context window).
11. **Resume via the same `task_id`** — On any start or resume, list `results/` + read `state.json`, then process the lowest-numbered missing chunk. Never restart the task from scratch; a run that stops at `max_iterations` is continued by re-delegating the same `task_id`.
12. **Write results immediately** — After processing a chunk, `write` the result to `$CILI_TMP/{task_id}/results/` before moving to the next step; never accumulate processed text in context.
