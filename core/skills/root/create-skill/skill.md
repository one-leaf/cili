---
name: create-skill
description: How to write skills. Use when creating or editing a skill.
---

# Create Skill

A skill has two parts: **description** (decides when to load) and **body** (what to do once loaded). Both must be ruthlessly minimal.

## Description: The Trigger

The description is a context pointer. Every word competes for attention. Make it count.

**Formula:** `[Action verb] [specific trigger]. Use when [concrete condition].`

**Examples:**
- ✅ "Translate files to Chinese. Use when user says '翻译' or needs file translation."
- ❌ "This skill helps with various translation tasks and language processing needs."

**Rules:**
1. **Front-load the trigger word** — the word that fires the skill should be first
2. **One branch per skill** — if description needs "or" twice, split into two skills
3. **Max 200 chars** — longer descriptions waste context on every turn

## Body: The Execution

The body is a recipe, not documentation. Agent reads it once, acts, discards.

**Core Principle:** If the agent can figure it out from context, delete it.

### What to Include
- **Steps in order** — numbered, imperative, no explanation
- **Completion criteria** — how agent knows each step is done
- **Edge cases** — only ones agent won't handle by default

### What to Delete
- Explanations of why (agent doesn't care)
- Background context (agent already has it)
- Flexible guidance ("consider", "might want to")
- Restatements of agent's default behavior

**Test:** Read each sentence. Ask: "Does this change what the agent does?" If no, delete it.

## Concrete Techniques

### 1. Show, Don't Tell

**Bad:** "Write concise comments that explain the why, not the what."
**Good:**
```python
# Bad: Loop through users and print names
for user in users:
    print(user.name)

# Good: Debug: skip banned users (see issue #42)
for user in users:
    if not user.banned:
        print(user.name)
```

### 2. Use Leading Words

Pick one word from agent's pretraining that anchors a behavior cluster.

**Examples:**
- "tight loop" → fast, deterministic, no overhead
- "red test" → failing test that proves the bug exists
- "tracer bullet" → minimal end-to-end path to validate architecture

Use the word repeatedly. Don't define it — let pretraining do the work.

### 3. Completion Criteria Must Be Checkable

**Bad:** "Understand the codebase." (When is this done?)
**Good:** "List all files that import `User` model." (Binary: done or not)

**Bad:** "Write tests."
**Good:** "Every public function has one test covering its happy path."

### 4. Split by Sequence, Not by Topic

**Bad:** One skill "Code Review" with steps for style, bugs, performance.
**Good:** Three skills: "Review Style", "Review Bugs", "Review Performance".

Why? Agent rushes step 1 when it sees steps 2-3 ahead. Split sequence = force focus.

### 5. Prune Ruthlessly

**Delete if:**
- Agent already does this by default → no-op
- Another skill covers this → duplication
- Environment already says this (config, code) → cache
- Never used in real runs → sediment

**Test:** Run the skill. If agent ignores a rule, the rule is noise. Delete it.

## Information Hierarchy

Decide where each piece sits:

1. **In-file step** — what agent does, in order (primary)
2. **In-file reference** — rules/facts consulted on demand (secondary)
3. **Disclosed reference** — pushed to separate file, loaded only when needed (tertiary)

**Rule:** Inline what every branch needs. Disclose what only some branches need.

**Example:**
```markdown
## Step 1: Parse input
- Use `json.loads()` for JSON
- Use `yaml.safe_load()` for YAML

## Step 2: Validate schema
See [validation-rules.md](validation-rules.md)
```

Step 1 is always needed (inline). Step 2 varies by case (disclose).

## Anti-Patterns

**❌ Documentation Style**
```markdown
## Overview
This skill helps you understand how to...

## Background
In software engineering, it's important to...

## Conclusion
In summary, this skill enables...
```
Agent discards this immediately. Delete everything except steps.

**❌ Flexible Guidance**
```markdown
You might want to consider checking for errors.
It could be helpful to add tests.
```
Agent ignores "might" and "could". Use imperative: "Check for errors. Add tests."

**❌ Negation**
```markdown
Don't write verbose comments.
Avoid skipping error handling.
```
Agent thinks about the forbidden thing. State the positive: "Write one-line comments. Handle every error."

## Minimal Template

```markdown
---
name: [Skill Name]
description: [Action verb] [trigger]. Use when [condition].
---

# [Skill Name]

## Step 1: [Verb] [object]
- [Action 1]
- [Action 2]

**Done when:** [Checkable criterion]

## Step 2: [Verb] [object]
- [Action 1]
- [Action 2]

**Done when:** [Checkable criterion]
```

That's it. Everything else is optional.

