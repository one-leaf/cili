---
name: code-review
description: "对 git 变更做资深级代码审查（正确性/安全/架构/性能/并发/错误处理/测试/可维护性），输出 P0-P3 分级发现与可执行建议。Use when user mentions 代码审查、代码评审、审查代码、审 PR、code review。Supports Python, TypeScript/JavaScript, React, Vue, Go, SQL, and shell."
roles: [master]
---

# Code Review — Senior-Level Analysis

Perform rigorous but constructive code review on current git changes. Focus on real bugs, security vulnerabilities, architecture issues, and reliability risks.

---

## Core Principles

### 1. Review the change, not the entire universe

Start from the diff. Understand:
- What changed and why
- What behavior is affected
- Which APIs, schemas, or contracts are impacted
- What critical paths are touched

Only expand beyond the diff when needed to understand callers, interfaces, data flow, auth, tests, or concurrency.

### 2. Prioritize correctness over style

Review order:
1. **Correctness / bugs**
2. **Security**
3. **Data integrity**
4. **Reliability / error handling**
5. **Concurrency / races**
6. **Performance**
7. **Architecture / maintainability**
8. **Testing**
9. **Style** (leave to linters)

### 3. Evidence before criticism

For every finding, determine:
- What exactly happens and under what conditions
- Why it's incorrect or risky
- What the impact is
- Whether it can actually occur in this architecture

Avoid speculative findings. When uncertain, label it explicitly.

### 4. Search before proposing abstractions

Before recommending new helpers or abstractions:
- Search for existing implementations
- Check shared utilities and adjacent modules
- Prefer reuse over new abstractions

---

## Severity Levels

| Level | Meaning | Examples | Action |
|-------|---------|----------|--------|
| **P0 Critical** | Severe security, data, or correctness failure | RCE, auth bypass, data corruption | Must fix before merge |
| **P1 High** | Significant defect blocking release | Logic bug, auth flaw, race condition | Should fix before merge |
| **P2 Medium** | Maintainability, reliability, or design issue | Weak error handling, missing tests | Fix now or create follow-up |
| **P3 Low** | Non-blocking improvement | Naming, minor duplication | Optional |

Do not inflate severity. P0/P1 only for concrete, meaningful impact.

---

## Review Workflow

Ten phases: **Context Gathering → High-Level Analysis → Correctness → Security → Reliability → Concurrency → Performance → Testing → Code Quality → Removal Candidates**. Read the detailed checklist for each phase from `references/phases.md` before starting the review; consult `references/language-guidance.md` for the changed files' languages when they are non-trivial.

---

## Feedback Style

**Be specific, actionable, evidence-based, respectful.**

Good:
> This can race when two workers process the same record concurrently. Both can pass the existence check before either inserts. Consider enforcing uniqueness at the database level and handling the conflict.

Bad:
> This is wrong.

Good:
> Could this receive an empty list? The current indexing would fail. Consider returning an empty result or validating before indexing.

Bad:
> You must rewrite this.

Do not turn subjective preferences into blocking findings.

---

## Output

Render findings with the report templates in `references/output-format.md` (full report; or the clean-review template when nothing significant was found).

---

## Review-Only Rule

**Do not modify code unless explicitly asked.**

Default workflow:
1. Inspect and analyze
2. Verify where practical
3. Report findings
4. Wait for instructions

If asked to implement fixes, switch to implementation mode and verify changes.

---

## Avoid False Positives

Before reporting:
1. Is it actually reachable?
2. Does existing validation prevent it?
3. Is the behavior intentional?
4. Does surrounding code compensate?
5. Is it relevant to this architecture?
6. Is severity proportional to impact?
7. Can I explain the failure scenario concretely?

If uncertain:
> Potential risk: depends on whether X is user-controlled. If yes, this is P1; otherwise not a concern.

---

## Review Philosophy

A good review answers:
1. **Does it work?**
2. **Is it secure?**
3. **Will it remain reliable under failure and concurrency?**
4. **Will it remain understandable and maintainable?**
5. **Is there a simpler or safer way?**

The best review catches the issues that matter, explains why, verifies assumptions, and gives a practical path forward.
