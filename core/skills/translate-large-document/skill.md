---
name: translate-large-document
description: Translate large or multi-format documents (docx/xlsx/pdf/doc/xls) into another language. Use when the user asks to translate a big file or a document — split into ~5K blocks, group into batches, delegate each batch to a Worker sub-agent that loops over its blocks, track with todo_write, merge back into the original format.
roles: [master]
---

# Translate Large Document

Translate a large or multi-format document by splitting it into ~5K-character blocks, grouping the blocks into batches, delegating each batch to a **Lite** sub-agent that loops over its blocks internally, and merging the results back into the original format.

Follow the `file-processing` skill for all format conversion mechanics (pdf2markdown, python-docx, openpyxl, LibreOffice / Office COM).

> **Batch, don't spawn per block.** One `agent` call per block burns the master's tool-call budget (264 blocks → 264 calls) and hits `max_iterations` on large files. A Lite sub-agent has a ~200-call budget (and `python` for progress state), so it can process ~40 blocks in one delegation. 264 blocks → ~7 Lite delegations.

## Step 1: Normalize the input

- **pdf** → `pdf2markdown(file_path="...")` produces `.md`; translate the `.md`, then convert the final `.md` → `.docx`.
- **doc** → convert to `.docx` first (LibreOffice / Office COM).
- **xls** → convert to `.xlsx` first.
- **docx / xlsx** → use directly.

**Done when:** the input is a `.md` (from pdf), `.docx`, or `.xlsx` file.

## Step 2: Split into ~5K blocks

Use the `python` tool to extract text in document order and split into blocks of ~5000 characters (4000–6000 ok), never cutting mid-paragraph:

- **docx**: iterate body paragraphs + table cells in order; emit each element as a marker line `[[i]]` followed by its text; group into blocks.
- **xlsx**: iterate non-empty text cells in row order; emit each as `[[r,c]]` followed by its text; group into blocks.
- **md**: split by lines/paragraphs; keep Markdown syntax (headings, tables, code fences) inside a block.

Save `chunk_000.txt`, `chunk_001.txt`, … and `manifest.json` (block → element mapping + total count) under `$CILI_TMP/{task_id}/chunks/`.

**Done when:** every block file exists and `manifest.json` records the total count.

## Step 3: Create the todo list (per batch, not per block)

Group the blocks into consecutive batches of **~40 blocks each** (last batch may be smaller), e.g. `chunk_000-039`, `chunk_040-079`, … Call `todo_write` with one `pending` item **per batch** — `Translate chunks 000-039` — plus a final `pending` item `Merge translated blocks into {output}`. Keep the list small (~7 items for a 264-block document), not one item per block.

## Step 4: Translate each batch via a background Lite sub-agent

For each batch, in order:

1. Mark that batch's todo item `in_progress`.
2. Call `agent(..., agent_type="lite", run_in_background=true)` with a task that makes the Lite agent loop over its whole block range in one run:

   ```
   agent(
     task="Process chunk_000.txt through chunk_039.txt under $CILI_TMP/{task_id}/chunks/ in ascending order. For each chunk: read it, translate ALL its text into {target_lang}, keep every [[...]] marker and Markdown syntax unchanged, then write the translation to $CILI_TMP/{task_id}/results/result_000.txt. Never skip or leave out a chunk — every chunk in the range must produce a result file. Track progress in $CILI_TMP/{task_id}/results/ (the results directory is the source of truth): use python to list which results already exist, then process the lowest-numbered missing chunk. Do not stop until the whole range has result files.",
     plan=[
       "Use python to list which result files already exist under $CILI_TMP/{task_id}/results/",
       "Loop over the range in ascending order: read chunk_NNN.txt, translate in-context, write result_NNN.txt",
       "Use python to verify every chunk in the range has a result file; retranslate any missing one"
     ],
     agent_type="lite",
     run_in_background=true
   )
   ```

   Spawn **all** batches at once — they self-throttle to `system.max_concurrent_agents` (default 5), so the later spawns simply queue.
3. When a batch's results all exist, mark it `completed`.

**Done when:** every batch's result files exist (count == total blocks).

### Wait for the batches (avoid per-task polling)

Instead of calling `read_task` once per batch, wait with a single bounded bash loop and re-check:

- `bash` `while [ $(ls $CILI_TMP/{task_id}/results/*.txt 2>/dev/null | wc -l) -lt {total} ]; do sleep 30; done` — but keep each bash call under ~4 minutes (hard 300s limit). If it is not done in one call, repeat it.

### Recover missing blocks

After all results are in (or the wait loop has stalled), compute missing results with one `python` call; re-delegate the missing ones in one small batch (sync worker, same task wording with the missing indices). A few missing blocks are normal after compression/long runs — do not re-run whole batches.

**Done when:** every block has a `result_NNN.txt` file.

## Step 5: Merge back into the ORIGINAL file (preserve structure)

Use the `python` tool. **Always modify the ORIGINAL file in place** — open it, write each translated text back to its element, save to the output path. Never rebuild the document from scratch; that drops headers, footers, images, page setup, and layout.

- **docx**: load the original with python-docx; for each `[[i]]` marker, replace that paragraph's / table cell's text with the translated block, keeping the first run's formatting:

  ```python
  p = doc.paragraphs[idx]           # or doc.tables[t].rows[r].cells[c].paragraphs[0]
  if p.runs:
      p.runs[0].text = translated
      for r in p.runs[1:]:
          r.text = ""
  else:
      p.text = translated
  ```

  Save as `{output}.docx`.
- **xlsx**: load the original with openpyxl; write each translated text back to cell `[[r,c]]`, preserving layout and formulas; save as `{output}.xlsx`.
- **md**: concatenate `result_*.txt` in order → `{output}.md`; then convert `.md` → `.docx` (python-docx, per `file-processing` skill).

Mark the final todo item `completed` and report the output path.

**Done when:** the output opens and every source block has translated text, with the original structure (styles, tables, headers/footers, images, layout) intact.

## Key Rules

1. Temp files live under `$CILI_TMP/{task_id}/`; only the final output goes to the user's path.
2. **Batch, don't spawn per block** — delegate each ~40-block batch to one Lite sub-agent that loops internally. Never launch one sub-agent per block; it burns the master's tool-call budget.
3. Blocks are translated by sub-agents — never translate them inline in your own context.
4. Never hardcode file content into Python string literals — read and write files.
5. Preserve `[[...]]` markers during translation; strip them only at merge time.
6. Use the user's requested target language; ask only if it is genuinely unspecified.
