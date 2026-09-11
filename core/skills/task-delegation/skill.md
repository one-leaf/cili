---
name: task-delegation
description: Delegate large or complex multi-step tasks to a Worker/Lite sub-agent via the agent tool. For file translation, batch processing, data extraction, code refactoring, etc.
roles: [master, worker]
---

## Core Principle

> **Large files and complex tasks → delegate to a Worker/Lite sub-agent via `agent` tool. Don't handle them yourself.**

The Master agent's job is to understand user intent, orchestrate tasks, and present results. Heavy file I/O and batch processing should be delegated to a Worker/Lite sub-agent (`agent` tool, `agent_type` defaults to `worker`).

## When to Use agent Tool

Use `agent` tool instead of handling directly when:

- File content exceeds ~10000 characters and needs chunked processing (translation, summary, extraction)
- Multiple read → process → write loops are required
- Batch operations across multiple files (e.g., translating an entire directory)
- Task requires multi-step decisions that can't be done in a single tool call
- Task involves semantic analysis that needs LLM (summarization, classification, etc.)

## When NOT to Use

Handle directly without delegation when:

- File can be read and processed in one go (< 10000 characters)
- A single bash command can complete the task
- Simple file creation or editing
- Code execution without LLM involvement

## How to Delegate

### 1. Provide Structured Task Info

Always provide both `task` (objective) and `plan` (execution steps):

```
✅ agent(
    task="Read input.txt, translate each paragraph to Chinese, write the result to output.txt. Preserve original formatting.",
    plan=[
        "Read input.txt content",
        "Translate each paragraph to Chinese in-context",
        "Write translated content to output.txt"
    ]
)
```

### 2. Specify Input and Output

The sub-agent doesn't know your context. Task description must include:
- Input file path and format
- Expected processing method
- Output file path and format

### 3. Break Down Complex Tasks

Use `plan` to list concrete steps. The sub-agent follows them in order:

```
✅ agent(
    task="Extract all failed records from data.csv and write to failed_records.json",
    plan=[
        "Read data.csv and inspect structure",
        "Filter rows where status column equals 'failed'",
        "Write filtered rows with timestamps to failed_records.json"
    ]
)
```

### 4. Mention LLM Processing Explicitly

If the task involves LLM processing (the sub-agent itself is the LLM — there is no separate `llm` tool), state the processing step clearly:

```
✅ agent(
    task="Read article.txt, translate the content to Chinese in-context, write to output.txt.",
    plan=["Read article.txt", "Translate in-context", "Write output.txt"]
)
```

### 5. Synchronous by Default

Call sub-agents **synchronously** (omit `run_in_background`) unless you are launching **multiple independent sub-agents at once**. A single sub-agent: wait for its result, then continue — the result stays in your context and ordering is preserved. Use `run_in_background: true` only to parallelize several sub-agents (e.g. translating N block files at once), then `read_task` each until it completes.

### 6. Concurrent Cap

Background sub-agents are capped at `system.max_concurrent_agents` (default 5, range 1-10). When you launch more than the cap, later spawn calls wait (queue) until an earlier sub-agent finishes — so you can safely launch all N sub-agents in a row and they will self-throttle. A spawn call that takes a moment is normal at the cap.

## Examples

### File Translation
```
agent(
    task="Read article.txt, translate to Chinese paragraph by paragraph, write to article_zh.txt. Keep markdown formatting intact.",
    plan=["Read article.txt", "Translate each paragraph in-context", "Write article_zh.txt with markdown formatting"]
)
```

### Batch Summarization
```
agent(
    task="Read all .md files in docs/, generate a one-paragraph summary for each, write summaries to docs/index.md as a table of contents.",
    plan=["List all .md files in docs/", "For each file, summarize in-context", "Write all summaries to docs/index.md"]
)
```

### Data Extraction
```
agent(
    task="Read data.csv, find all rows where status is 'failed', write those rows with timestamps to failed_records.json.",
    plan=["Read data.csv", "Filter rows where status='failed'", "Write results to failed_records.json"]
)
```

### Code Refactoring
```
agent(
    task="Read src/utils.py, extract all functions longer than 50 lines into src/utils/split.py, update imports in src/utils.py.",
    plan=["Read src/utils.py and identify long functions", "Move long functions to src/utils/split.py", "Update imports in src/utils.py"]
)
```

### Structured Output
```
agent(
    task="Read logs.txt, extract error messages and stack traces, write structured results to errors.json.",
    plan=["Read logs.txt", "Extract errors in-context into a JSON object", "Write errors.json"]
)
```

## Important Notes

- **Task info is pinned**: Task objective and plan are the pinned first user message, immune to compression
- `agent_type`: `agent` accepts `agent_type="worker"` (default, full tool set + check phase) or `agent_type="lite"` (read/write/edit/bash only) — use lite for simple, self-contained file tasks
- Worker/Lite sub-agent performs LLM processing in its own context (there is no separate `llm` tool)
- Sub-agent timeout is 1 hour
- **Delegation depth limit (1 level)**: only master can delegate — to a worker or lite (depth 1). A sub-agent at depth 1 cannot delegate further; calling `agent` there returns an error, so complete the task directly
- **Partial runs resume**: if a sub-agent returns before finishing (hit `max_iterations` or reported partial progress), its chunks, results, and `state.json` persist under `$CILI_TMP/{task_id}/`. Re-delegate the same task with the **same `task_id`** — it resumes from the lowest missing chunk; never restart from scratch. For jobs that clearly exceed one run (~90 chunks, see context-bounded-processing), split the input into segments and delegate one sub-agent per segment up front, or plan for re-delegation rounds.
- Sub-agent execution is visible in UI (real-time progress)
- Return format: `{"status": "completed"/"error"/"timeout", "summary": "...", "iterations": N}`
- Use the context-bounded-processing skill inside the sub-agent for files that exceed the context window
- **Temp files go under `$CILI_TMP/`** (i.e. `data/tmp/`): Chunks, intermediates, and other temp files created by the sub-agent must be placed under `$CILI_TMP/{task_id}/`, never scattered in the working directory. On success the sub-agent should clean up the directory; on failure the user can delete the entire `data/tmp/` directory.
