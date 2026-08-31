---
name: task-delegation
description: Delegate large or complex multi-step tasks to SubAgent via subagent tool. For file translation, batch processing, data extraction, code refactoring, etc.
---

## Core Principle

> **Large files and complex tasks → delegate to SubAgent via `subagent` tool. Don't handle them yourself.**

The RootAgent's job is to understand user intent, orchestrate tasks, and present results. Heavy file I/O and batch processing should be delegated to SubAgent.

## When to Use subagent Tool

Use `subagent` tool instead of handling directly when:

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
✅ subagent(
    task="Read input.txt, translate each paragraph to Chinese, write the result to output.txt. Preserve original formatting.",
    plan=[
        "Read input.txt content",
        "Translate each paragraph to Chinese using llm tool",
        "Write translated content to output.txt"
    ]
)
```

### 2. Specify Input and Output

SubAgent doesn't know your context. Task description must include:
- Input file path and format
- Expected processing method
- Output file path and format

### 3. Break Down Complex Tasks

Use `plan` to list concrete steps. SubAgent follows them in order:

```
✅ subagent(
    task="Extract all failed records from data.csv and write to failed_records.json",
    plan=[
        "Read data.csv and inspect structure",
        "Filter rows where status column equals 'failed'",
        "Write filtered rows with timestamps to failed_records.json"
    ]
)
```

### 4. Mention llm for LLM Processing

If the task involves LLM processing, mention `llm` in the task:

```
✅ subagent(
    task="Read article.txt, use llm to translate the content to Chinese, write to output.txt.",
    plan=["Read article.txt", "Use llm tool to translate", "Write output.txt"]
)
```

## Examples

### File Translation
```
subagent(
    task="Read article.txt, translate to Chinese paragraph by paragraph, write to article_zh.txt. Keep markdown formatting intact.",
    plan=["Read article.txt", "Translate each paragraph using llm", "Write article_zh.txt with markdown formatting"]
)
```

### Batch Summarization
```
subagent(
    task="Read all .md files in docs/, generate a one-paragraph summary for each, write summaries to docs/index.md as a table of contents.",
    plan=["List all .md files in docs/", "For each file, use llm to generate summary", "Write all summaries to docs/index.md"]
)
```

### Data Extraction
```
subagent(
    task="Read data.csv, find all rows where status is 'failed', write those rows with timestamps to failed_records.json.",
    plan=["Read data.csv", "Filter rows where status='failed'", "Write results to failed_records.json"]
)
```

### Code Refactoring
```
subagent(
    task="Read src/utils.py, extract all functions longer than 50 lines into src/utils/split.py, update imports in src/utils.py.",
    plan=["Read src/utils.py and identify long functions", "Move long functions to src/utils/split.py", "Update imports in src/utils.py"]
)
```

### Structured Output with llm
```
subagent(
    task="Read logs.txt, use llm with output_schema to extract error messages and stack traces. Write results to errors.json.",
    plan=["Read logs.txt", "Use llm tool with output_schema to extract errors", "Write errors.json"]
)
```

## Important Notes

- **Task info is in system prompt**: Task objective and plan are appended to system prompt END, immune to compression
- SubAgent has access to `llm` for single-turn LLM calls (translation, summarization, extraction, structured output)
- SubAgent timeout is 1 hour
- SubAgent cannot call subagent tool (no nesting allowed)
- SubAgent execution is visible in UI (real-time progress)
- Return format: `{"status": "completed"/"error"/"timeout", "summary": "...", "iterations": N}`
- Use context-bounded-processing skill in SubAgent for large files that exceed context window
- **Temp files go under `.cili_tasks/`**: Chunks, intermediates, and other temp files created by SubAgent must be placed under `.cili_tasks/{task_id}/`, never scattered in the working directory. On success the SubAgent should clean up the directory; on failure the user can delete the entire `.cili_tasks/` directory.
