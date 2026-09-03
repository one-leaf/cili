---
name: Context-Bounded Processing (Runtime)
description: Stateful resumable worker protocol for tasks that exceed context window — Python manages state, the agent orchestrates. Each step is bounded and resumable.
---

## Core Principle

> **The agent (SubAgent) owns orchestration. Python manages state. The `llm` tool performs text processing.**

Context-bounded processing MUST be implemented as a **resumable stateful workflow**. This applies when:
- Processing files too large for the LLM context window
- Analyzing multiple files whose combined content exceeds context
- Batch database results that need semantic processing
- Any task where the total data exceeds what can fit in one LLM call

The agent calls tools step by step — Python for state management and file splitting, `llm` for text processing — rather than expecting a single Python call to complete the entire task.

```
SubAgent (orchestrator)
    │
    │ 1. python(code="init + split chunks + save state")
    ▼
    State saved to $CILI_TMP/{task_id}/state.json
    │
    │ 2. llm(input_file="chunk_001.txt", output_file="result_001.txt")
    │ 3. python(code="update state, mark chunk 0 done")
    │ 4. llm(input_file="chunk_002.txt", output_file="result_002.txt")
    │ 5. python(code="update state, mark chunk 1 done")
    │    ... repeat until all chunks done ...
    │
    │ 6. python(code="merge results + cleanup")
    ▼
    Final output ready
```

## Execution Model

```
SubAgent (orchestrator loop)
  │
  ├─ python: init phase → split input, save chunks, init state.json
  │
  ├─ for each chunk:
  │    ├─ llm: process one chunk → write result file
  │    └─ python: update state.json (mark chunk done, advance cursor)
  │
  └─ python: merge phase → combine all results, cleanup
```

The agent reads `state.json` to know progress and decides the next step. Python is called for discrete operations — never expected to loop over slow external calls internally.

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

## llm Tool

`llm` is a **shared tool** available to both RootAgent and SubAgent for single-turn LLM calls. It reads an input file, processes it with the LLM, and writes the result.

### Tool Parameters

```json
{
  "input_file": "Path to input file (required)",
  "prompt": "System prompt - instructions for processing (optional)",
  "output_file": "Path to write result (optional, returns text if omitted)",
  "output_schema": {"type": "object", "properties": {...}}  // Optional, for structured output
}
```

### Basic Usage

```
// Process file with prompt
llm(prompt="Translate to Chinese", input_file="article.txt", output_file="article_zh.txt")
// Writes translated content to file

// Process file without explicit prompt (uses default)
llm(input_file="data.txt", output_file="summary.txt")
// Returns processed content
```

### Structured Output

```
// Define expected output structure
llm(
  prompt="Extract all company names from this text",
  input_file="document.txt",
  output_schema={
    "type": "object",
    "properties": {
      "companies": {"type": "array", "items": {"type": "string"}}
    }
  }
)
// Returns: {"companies": ["Acme Corp", "GlobalTech"]}
```

### Common Patterns

```
// Translation with prompt
llm(prompt="Translate to Chinese", input_file="en.txt", output_file="zh.txt")

// Summarization with prompt
llm(prompt="Summarize in 3 sentences", input_file="article.txt", output_file="summary.txt")

// Information extraction with structured output
llm(
  prompt="Extract dates from this document",
  input_file="document.txt",
  output_schema={"type": "object", "properties": {"dates": {"type": "array", "items": {"type": "string"}}}}
)

// File processing with default prompt
llm(input_file="input.txt", output_file="output.txt")
```

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

  "llm": {
    "calls": 35,
    "input_tokens_approx": 1_050_000,
    "output_tokens_approx": 70_000
  },

  "result": {
    "output_path": "output.txt"
  },

  "updated_at": "2026-08-23 14:30:00"
}
```

Next invocation loads this state and resumes from `cursor.next_chunk_idx = 35`.

## Processing Phases

A context-bounded task SHOULD use explicit phases:

```
INIT     → analyze input, split into chunks, init state
PROCESS  → process chunks with llm (one per step)
MERGE    → merge result files into final output
DONE     → cleanup, return final result
```

The agent tracks which phase it is in via `state.json` and resumes accordingly.

## Execution Budget

**Important: Different limits for different scenarios**

### SubAgent Python + slow external calls

Each Python invocation MUST execute exactly **1 slow external call** and return immediately. Applies to:
- `llm` — LLM processing calls (called directly by the agent, not from Python)
- HTTP requests — `requests.get()`, `requests.post()`, API calls, web scraping with multiple pages
- Any network I/O that may be slow or timeout

Do NOT loop over multiple calls in a single Python invocation — this risks timeout (300s hard limit). The agent should orchestrate the loop: call Python → read progress → call `llm` → call Python to update state → repeat.

```python
CALLS_PER_INVOCATION = 1   # MUST be 1 — never loop slow external calls in Python
```

**Why single call per invocation?**
- Each external call is slow (network latency, LLM inference)
- Looping multiple calls risks hitting the hard timeout (300s)
- The agent should orchestrate the loop: call Python → read progress → call `llm` → repeat

### Python + normal operations (file I/O, data processing, etc.)

If Python is NOT making slow external calls, it can loop normally without this restriction:
- File reading/writing: OK to process all files in one invocation
- Data transformation: OK to loop over all records
- System commands: OK to execute multiple bash commands

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
    "llm": {"calls": 0},
    "result": {"output_path": OUTPUT_FILE},
    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
}
with open(f"{task_dir}/state.json", "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2, ensure_ascii=False)

print(json.dumps({"status": "initialized", "task_id": task_id, "total_chunks": len(chunks)}))
```

### Step 2: Process each chunk (agent loop)

The agent reads `state.json`, then for each chunk:

1. Call `llm` to process the chunk:
   ```
   llm(input_file="$CILI_TMP/{task_id}/chunks/chunk_000.txt",
       prompt="Translate to Chinese",
       output_file="$CILI_TMP/{task_id}/results/result_000.txt")
   ```

2. Call `python` to update state:
   ```python
   # Agent calls: python(code="...")
   import json, os
   state_path = os.path.join(os.environ["CILI_TMP"], "{task_id}/state.json")
   with open(state_path) as f:
       state = json.load(f)
   idx = state["cursor"]["next_chunk_idx"]
   state["cursor"]["next_chunk_idx"] = idx + 1
   state["chunks"]["done"] = idx + 1
   state["llm"]["calls"] += 1
   import time; state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
   with open(state_path, "w") as f:
       json.dump(state, f, indent=2, ensure_ascii=False)
   print(json.dumps({"chunk_done": idx, "progress": f"{idx+1}/{state['chunks']['total']}"}))
   ```

3. Repeat until all chunks are done.

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
2. **Agent orchestrates, Python manages state** — The agent (SubAgent) calls tools step by step. Python handles state management, file splitting, and result merging. Never let Python loop over slow external calls.
3. **Use `llm` directly for text processing** — Call `llm` as a tool for each chunk. Do not invoke the LLM from within Python code.
4. **Single external call per Python invocation** — Each Python call processes at most 1 slow external operation (HTTP request, etc.) then returns; the agent handles loop orchestration.
5. **Python owns execution state** — Always rely on `state.json` to track progress, never on the agent's conversation context.
6. **Normal operations can loop** — Local operations such as file I/O, data transformation, and bash commands may process all items in a single Python invocation.
7. **Split by paragraph boundaries** — Preserve completeness; never cut mid-sentence.
8. **Persist immediately** — Save results and update `state.json` after every successful step.
9. **Do not hardcode file content** — Never write large file contents as string literals in Python code.
10. **Do not process large content in the agent context** — Always delegate to Python for splitting and `llm` for processing.
11. **Resume via the same `task_id`** — Python loads state from `state.json` and continues from the last checkpoint.
12. **Use `llm`'s `output_schema`** — Always define the expected output structure to get typed results.
