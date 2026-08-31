---
name: research
description: "Authoritative fact research (must delegate to SubAgent). Use when the user wants to learn a fact, verify a claim, or look up real information about a product/technology/policy. After reading this skill, the agent MUST use the subagent tool to delegate the research task, enforce primary sources (official docs, source code, official announcements), and output a Markdown report with verifiable citations. Also triggered when the user explicitly uses 'research'."
---

# Research — Authoritative Fact Finding (Standalone)

A lightweight but extremely rigorous **background research agent**. "Every claim must be backed by a precise primary source" is the unbreakable iron rule. Research results are written directly to a Markdown file in the repository for persistent reference.

---

## ⚠️ Execution Model (Mandatory)

**This skill MUST be delegated to a SubAgent — the main agent must NOT execute it directly!**

### Execution flow:

1. **After reading this skill, immediately delegate the research task via the `subagent` tool.**
2. **Build a detailed task description** that includes:
   - Research objective (what the user wants to know)
   - Iron rules (must follow the 4 iron rules below)
   - Source hierarchy table (Tier 0 / 1 / 2 definitions)
   - 5-step closed-loop process (clarify → target → verify → conflict → report)
   - Output requirements (Markdown report format)
   - Prohibited sources (Wikipedia, social media, etc.)

3. **Task example:**
```
Research objective: [user's research question]

Iron rules:
1. No source, no write: Every factual statement must be immediately followed by a verifiable primary citation.
2. Grey area = unverified: No hedging words allowed. Must explicitly state "no primary evidence found."
3. Conflict disclosure: When official sources contradict each other, present both sides honestly — no smoothing over.
4. Tier 0 primary sources only: Tier 0 is the sole ground truth; reject social media as factual basis.

Source hierarchy:
- Tier 0 (Gold standard): Official docs, government regulations, source code
- Tier 1 (Highly credible): Official blogs, official social media
- Tier 2 (Reference only): Industry standards, white papers
- 🚫 Rejected: Wikipedia, tech media, personal blogs

Execution flow:
1. Clarify research objective (ask back if vague)
2. Targeted source discovery (lock onto Tier 0 entry points)
3. Item-by-item verification and extraction (quote original text, record source URL)
4. Conflict check (are there conflicting official pages?)
5. Generate Markdown report (write to .research/ directory)

Output format:
- Filename: YYYY-MM-DD-<topic>.md
- Structure: Core findings (with primary source links) + Source conflict log + Unverified list + Search trail

Prohibited:
- ❌ Using Wikipedia as a factual basis
- ❌ Using tech media (TechCrunch, The Verge, etc.) as a factual basis
- ❌ Using personal blogs, Zhihu, Xiaohongshu as a factual basis
- ❌ Using hedging words like "possibly", "perhaps", "reportedly"
```

### Why delegate to a SubAgent?

- **Isolation**: Research may involve heavy searching and verification — avoid polluting the main conversation context.
- **Focus**: The SubAgent can execute the 5-step closed loop without distraction from other tasks.
- **Traceability**: Results are saved as a standalone Markdown file for easy future reference.

---

## Core Principles (Iron Rules)

> Adapted from the academic rigor of `deep-research`, tailored for "fact-checking" scenarios.

| # | Iron Rule | Description |
|---|-----------|-------------|
| ⚠️ **I** | **No source, no write** | Every factual statement, data point, and timeline must be immediately followed by a clickable or locatable primary citation. |
| ⚠️ **II** | **Grey area = unverified** | If a claim has no primary-source evidence, **never** use hedging words like "possibly", "perhaps", or "reportedly". Must explicitly state: `❌ No direct evidence found in primary sources`. |
| ⚠️ **III** | **Conflict disclosure** | If different official sources contradict each other (e.g. official site vs. official API docs), present both sides honestly — do NOT smooth over or pick a side. |
| ⚠️ **IV** | **Primary sources first** | Follow the source hierarchy below. Tier 2 sources are for **clues only**, never for **evidence**. |

---

## Source Hierarchy (Evidence Hierarchy for Facts)

Unlike academic papers, this skill's source hierarchy is defined as follows:

| Tier | Category | Examples | Usable as evidence? |
|------|----------|----------|---------------------|
| **Tier 0 (Gold standard)** | Official primary docs / legal texts / source code repos | Official site announcements, GitHub main branch source code, government regulation text, first-party API response JSON | ✅ **Yes (sole ground truth)** |
| **Tier 1 (Highly credible)** | Official engineering blogs / official social media (cross-verified against official site) | Company blog posts, Twitter/X official account (must be linked from official site) | ✅ Yes (must label as "official statement") |
| **Tier 2 (Reference only)** | Authoritative industry standards / white papers | W3C specs, RFC documents, IEEE standards | ⚠️ Background context only — never as primary evidence |
| **🚫 Rejected** | Secondary interpretation / social media / tech news aggregators | Any tech media (e.g. TechCrunch, The Verge), Wikipedia, personal blogs, Zhihu/Xiaohongshu/Weibo | ❌ **Never used as factual basis** (only for finding Tier 0 leads) |

---

## Trigger Conditions

Activate when the user's intent matches any of these patterns (**cross-language recognition**):

- **English**: research, investigate, fact-check, verify, look up, find out, tell me about, check if
- **Chinese**: 研究、调查、查一下、查查、事实核查、验证、了解、搜索、帮我查查、我想知道、确认一下、有没有证据

**Auto-routing**: Whenever the user wants to know a "fact" rather than "write an article", prefer this skill.

**Important**: After triggering, the agent must read this skill, then immediately delegate via the `subagent` tool.

---

## Execution Protocol (5-step closed loop, executed by SubAgent)

Upon receiving the research task, the SubAgent must execute the following flow:

### Step 1: Clarify Research Objective (if vague)
- If the user's question is too broad (e.g. "look up OpenAI"), the agent must **ask one round of clarification** to narrow the scope:
  - *"Which aspect of OpenAI are you interested in — model pricing, context length, latest version number, or organizational changes?"*
- If the question is clear, proceed directly to the next step.

### Step 2: Targeted Source Discovery
- Based on the question type, lock onto the corresponding **Tier 0 source entry point**:
  - **Product / API** → Official developer docs, official site Pricing page, GitHub Release Notes
  - **Policy / Regulation** → Government `.gov` domain, official gazette
  - **Company news** → Official Newsroom or Press Release (follow footer links on official site)
- When searching, prefer `site:` qualifiers (e.g. `site:openai.com pricing`).

### Step 3: Item-by-item Verification and Extraction
- For each key information point:
  1. Extract the original text (quote directly if possible).
  2. Record the full source URL and access date.
  3. If not found in Tier 0, **downgrade to Tier 1 search**.
  4. If Tier 1 also yields nothing, **stop searching** and mark as "No primary evidence found."

### Step 4: Conflict Check (Built-in "Lightweight Devil's Advocate")
- Ask yourself: *"Is there another official page that states a different fact?"*
- If yes, find that page and record it as well. Example:
  > ⚠️ **Source Conflict Warning**: OpenAI's official pricing page shows $20/month, but the API Changelog on the same date shows $25/month. Both are official domains and cannot be reconciled — both sides are presented here in parallel.

### Step 5: Generate Markdown Report
- Write the research results to an existing `docs/`, `notes/`, or `.research/` directory in the repository. If no convention exists, create `.research/` by default and inform the user of the file path.
- Filename format: `YYYY-MM-DD-<research-topic>.md` (e.g. `2026-08-29-openai-o1-context-window.md`).

---

## Output Template (Must Follow)

The generated Markdown must include the following structure:

```markdown
# Research Report: [Topic]

**Research date**: YYYY-MM-DD HH:MM
**Research scope**: [Brief boundary description, e.g. "Official public docs only"]
**Conclusion statement**: [Most concise factual summary, no more than 3 sentences]

---

## Core Findings

### 1. [Finding title]
- **Factual statement**: [Specific content]
- **Primary source**: [Quote original text] — [Source link with access date]
- **Confidence rating**: ✅ Tier 0 (Official documentation)

### 2. [Finding title]
...(same format)

---

## Source Conflict Log (if any)
> ⚠️ [Describe both sides of the conflict with original text and links]

---

## Unverified List
> ❌ [Items the user cares about but no primary evidence was found] — Recommendation: Further investigate [specific suggestion]

---

## Search Trail
- Search engine / strategy: [e.g. Bing search `site:...`]
- Sources checked but not adopted: [List rejected secondary sources and reasons, e.g. "TechCrunch report is secondary interpretation — ignored"]
```
