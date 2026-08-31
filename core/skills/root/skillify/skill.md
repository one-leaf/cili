---
name: skillify
description: Solidify the current session's repeatable process into a reusable skill and save to user memory. Use when the user says "skillify", "固化会话", "save as skill", "把这个做成技能", or after completing a reusable workflow.
---

# Skillify — Solidify a Session into a User Skill

Turn the repeatable process completed in this session into a reusable skill stored in the user's memory. You are doing knowledge extraction: pulling a generic workflow out of one concrete execution.

## Step 1: Analyze the current session

Read the current session messages (via `session_manager.get_messages()`). Focus on the user message flow and identify:

- **What process was performed** (not "wrote code" but "created a PR via gh CLI and configured review")
- **Input parameters** (file paths, URLs, model names, commands — concrete values)
- **Step order** (in execution sequence)
- **Success artifact per step** (files, IDs, outputs, state)
- **User corrections** (focus especially — these become hard constraints)
- **Tools used** (read/write/bash/browser/subagent etc.)
- **Independent steps that could run in parallel**

If the session history has been compacted, rely on `session_memory` (if available) or ask the user to fill in the key steps.

## Step 2: Confirm with the user (conversational interaction)

Do not dump all questions at once. Confirm in rounds.

### Round 1: High-level confirmation

Present the process summary you identified and propose:

- **Skill name** (`kebab-case`, e.g. `cherry-pick-pr`, `deploy-staging`)
- **Display name** (human-readable, ≤64 chars, e.g. "GitHub Release")
- **Description** (≤200 chars, trigger word first)
- High-level goal and success criteria
- User corrections (flag these for confirmation as hard constraints)

Ask the user to confirm or adjust.

### Round 2: Refine steps

Present the numbered step list and, for each step, ask:

- What does this step produce that later steps depend on? (data, IDs, file paths)
- How do we prove this step is done? (a specific checkable criterion)
- Does the user need to confirm before proceeding? (especially for irreversible operations: push, merge, delete)
- Are there independent steps that can run in parallel?

### Round 3: Parameters and triggers

Ask:

- Does the skill need parameters? (e.g. `$pr_number`, `$target_env`, `$file_path`)
- When should this skill auto-trigger?
- What might the user say to trigger it? (provide 2-3 example trigger phrases)
- Any tags for categorization? (e.g. `github`, `deployment`, `testing`)

**Do not over-ask for simple processes** — if steps are clear and unambiguous, 2 rounds are enough.

## Step 3: Build skill content

Build the skill body (without frontmatter — the `memory` tool will add it). Key requirements:

```markdown
## Inputs
- `$param_name`: Description of this input (omit this section if no parameters)

## Step 1: <Verb> <object>
- <Action 1>
- <Action 2>

**Done when:** <Checkable completion criterion>

## Step 2: <Verb> <object>
- <Action 1>

**Done when:** <Checkable completion criterion>

## Hard constraints (from user corrections)
- <Rule 1>
- <Rule 2>
```

**Every step must annotate**:
- **Done when** — a binary completion criterion (not "understood", but "listed all files that import X")
- Parameterizable spots use `$param_name` placeholders

**Hard constraints in a separate section**:
User corrections are the most valuable knowledge — list them explicitly as rules.

## Step 4: Save using memory tool

Use the `memory` tool to save the skill to the user's memory:

```python
memory(
    action="store",
    memory_type="skill",
    skill_name="github-release",        # kebab-case, cannot be UUID
    name="GitHub Release",              # display name, ≤64 chars
    description="Create a GitHub release and upload build artifacts. Use when the user says '发版', 'create release'.",
    content="<the skill body from Step 3>",
    tags=["github", "release"]          # optional tags
)
```

The memory tool will:
- Auto-generate frontmatter with `name`, `description`, `tags`, `created`, `updated`
- Save to `workspace/{workspace_id}/memory/skills/{skill_name}/skill.md`

## Step 5: Confirm with user

After saving, tell the user:

- Skill name and description
- Where it was saved (`memory/skills/{name}/skill.md`)
- How to invoke: `/<skill-name>` or via trigger phrases
- How to update: `memory(action="update", memory_type="skill", skill_name="...", ...)`
- How to delete: `memory(action="delete", memory_type="skill", skill_name="...")`

## Key principles

- **Do not infer what the user never said** — if the session doesn't carry explicit information, ask the user instead of guessing
- **Preserve user corrections** — "don't use this library", "must use JSON format" are hard constraints
- **Steps must be executable** — not "consider error handling" but "catch HTTPError and return ToolResult(error=True)"
- **No frontmatter in content** — the `memory` tool generates frontmatter automatically

## Example

Suppose the user completed a "create GitHub release" flow. After confirmation rounds, save:

```python
memory(
    action="store",
    memory_type="skill",
    skill_name="github-release",
    name="GitHub Release",
    description="Create a GitHub release and upload build artifacts. Use when the user says '发版', 'create release', '发布 v1.2.3'.",
    content="""## Inputs
- `$version`: Version number (e.g., "1.2.3")
- `$notes`: Release notes (provided by user)

## Step 1: Validate tag
- Check local tag: `git tag -l v$version`
- If missing, create: `git tag -a v$version -m "Release $version"`
- Push: `git push origin v$version`

**Done when:** `git ls-remote --tags origin` includes `v$version`

## Step 2: Build artifacts
- Run: `python setup.py bdist_wheel`
- Artifact path: `dist/$package-$version-py3-none-any.whl`

**Done when:** artifact file exists and size > 0

## Step 3: Create release
- Run: `gh release create v$version dist/* --title "v$version" --notes "$notes"`

**Done when:** `gh release view v$version` returns 200

## Hard constraints
- Must validate tag first; never skip Step 1
- Artifacts must live under dist/; never upload source directly
- Release notes come from the user; never generate them""",
    tags=["github", "release", "deployment"]
)
```
