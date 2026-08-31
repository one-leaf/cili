---
name: code-review
description: "Senior-level code review for git changes. Reviews correctness, security, architecture, performance, concurrency, error handling, testing, and maintainability. Supports Python, TypeScript/JavaScript, React, Vue, Go, SQL, and shell. Use when reviewing PRs, git diffs, code changes, or when the user asks for code review. Outputs structured findings with severity levels (P0-P3) and actionable recommendations."
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

### Phase 1 — Context Gathering

```bash
git status -sb
git diff --stat
git diff
```

If needed:
```bash
git log --oneline -n 10
git blame <file>
```

Determine:
- Changed files and lines
- New dependencies or config
- Schema/API/test changes
- Critical-path modifications

**No changes**: State clearly. Ask if user wants staged changes, a commit, or branch diff reviewed.

**Large changes (>400-500 lines)**: Summarize by file/module, group by feature, review critical paths first.

**Mixed concerns**: Review separately (feature changes vs refactoring, formatting vs behavior).

### Phase 2 — High-Level Analysis

**Architecture**:
- Does it fit existing architecture?
- Correct dependency direction?
- Appropriate layer placement?
- Unnecessary coupling or circular dependencies?

**SOLID** (check when relevant):
- **SRP**: Unrelated responsibilities in one class/function
- **OCP**: Repeated conditional blocks that should be polymorphic
- **LSP**: Subclass violates base type expectations
- **ISP**: Interface with many unimplemented methods
- **DIP**: High-level logic directly depends on infrastructure

### Phase 3 — Correctness

Trace execution paths. Check:
- Normal and failure paths
- Empty/null/invalid input
- Boundary values
- Missing records, stale state
- Retries, partial failures
- Transaction boundaries, rollback
- Idempotency, ordering
- Time/timezone handling
- Numeric overflow, off-by-one errors
- Incorrect boolean conditions

### Phase 4 — Security

**Injection**: SQL, command, template, LDAP — check if user data reaches interpreters. Use parameterized queries.

**XSS**: HTML rendering, unsafe DOM APIs, template escaping. Output encoding > input validation.

**SSRF**: Server-side requests with user-controlled URLs. Check internal IPs, metadata endpoints.

**Path Traversal**: File uploads/downloads, user-controlled filenames. Look for `../`, absolute paths, symlinks.

**Auth/Authz**: Missing auth, permission checks, IDOR/BOLA, tenant isolation, role escalation.

**Secrets**: Hardcoded keys, passwords, tokens, private keys. Never reproduce secrets in review output.

**Unsafe Deserialization**: Untrusted objects, unsafe pickle, Java serialization, unsafe YAML.

**Cryptography**: Weak algorithms, hardcoded keys, predictable randomness, insecure password storage.

### Phase 5 — Reliability

Check failure handling:
- Network/database failures, timeouts
- Retries, retry storms
- Partial success, transaction rollback
- Resource/connection/file cleanup
- Cancellation, shutdown behavior

Avoid:
- Swallowed exceptions
- Overly broad exception handling
- Empty catch blocks
- Returning fake success
- Silent failure ignoring

### Phase 6 — Concurrency

Explicitly inspect concurrent code. Look for:
- Shared mutable state without synchronization
- Check-then-act, TOCTOU
- Lost updates, inconsistent reads
- Deadlocks, lock ordering
- Unsafe caches, singleton mutable state
- Async task lifecycle, cancellation races
- Goroutine/thread leaks

Dangerous pattern:
```python
if not exists:
    create()  # Race condition if multiple workers
```

Consider: atomic operations, unique constraints, transactions, locks, optimistic concurrency, idempotency keys.

### Phase 7 — Performance

**Algorithmic**: O(n²) loops, repeated scans, expensive operations in hot loops.

**Database**: N+1 queries, missing indexes, loading unnecessary columns, unbounded queries, missing pagination.

**Memory**: Unbounded collections, loading huge files into memory, memory leaks, unnecessary copies.

**Network**: Repeated requests, missing batching, unnecessary round trips, missing timeouts.

**Caching**: Missing cache on hot paths, stale cache, cache invalidation, cache stampede.

### Phase 8 — Testing

Check if changed behavior is protected:
- Unit/integration/API tests
- Regression tests
- Error-path and boundary tests
- Concurrency tests (where applicable)

Ask:
- What could break?
- Is there a test that would catch the regression?
- Are failure paths and auth boundaries tested?

Run project's test suite, lint, type checking, and build when practical.

### Phase 9 — Code Quality

Check:
- Unclear naming
- Excessive nesting, deep conditionals
- Duplicated logic
- Parameter sprawl, overly large functions/classes
- Leaky abstractions, stringly-typed state
- Dead branches, no-op updates, hidden side effects
- Comments contradicting code

Prefer simple, readable code. Avoid over-engineering.

### Phase 10 — Removal Candidates

Look for:
- Unused functions/variables/branches
- Obsolete compatibility code, dead feature flags
- Duplicated implementations
- Deprecated paths, unused dependencies

Classify:
- **Safe Delete Now**: Evidence shows code is unused
- **Defer With Plan**: Appears obsolete but needs verification or coordination

---

## Language-Specific Guidance

### Python

- Mutable defaults (`def f(lst=[])`)
- Exception handling (avoid bare `except:`)
- Resource cleanup (`with` statements)
- Async correctness (avoid blocking in async)
- Global mutable state
- Type assumptions (use type hints)

### TypeScript / JavaScript

- Unsafe `any`, incorrect type narrowing
- Type assertions hiding bugs
- Nullability (`undefined` vs `null`)
- Async behavior, promise handling
- Discriminated unions for state

### React

- Hooks rules (no conditional hooks)
- Stale closures in effects
- Missing/wrong effect dependencies
- Unnecessary effects, state synchronization
- Rendering performance (memo, useMemo, useCallback)

### Vue

- Composition API correctness
- Reactivity pitfalls (reactive vs ref)
- Watcher cleanup, computed values
- Lifecycle hooks
- Props/emits validation

### Go

- Error handling (don't ignore errors)
- Goroutine leaks (missing context cancellation)
- Channel ownership (who closes?)
- Context propagation
- Data races (use `-race` flag)
- Resource cleanup (defer)

### SQL

- SQL injection (use parameterized queries)
- Missing indexes on filtered/joined columns
- Unbounded queries (missing LIMIT)
- Transaction boundaries
- Index usage (avoid functions on indexed columns)
- NULL handling (NULL != NULL)

### Rust

- Ownership and borrowing issues
- Lifetime correctness (avoid `'static` when unnecessary)
- Unsafe blocks (minimize, document invariants)
- Send/Sync trait bounds for concurrency
- Async cancellation (drop behavior, resource cleanup)
- Error propagation (`?` operator, custom error types)
- Resource management (RAII, Drop trait)
- Panic vs Result (avoid panics in library code)
- Integer overflow (use checked arithmetic in release builds)

### C / C++

- Memory safety (use-after-free, double-free, memory leaks)
- Buffer overflow (bounds checking, use `std::vector`/`std::array` over raw arrays)
- Undefined behavior (uninitialized variables, signed overflow, strict aliasing)
- RAII and smart pointers (`std::unique_ptr`, `std::shared_ptr`)
- Thread safety (data races, mutex usage, atomic operations)
- Resource cleanup (file handles, sockets, use RAII)
- Null pointer dereference (check before use, use `std::optional` in C++17+)
- Exception safety (strong exception guarantee, noexcept)
- Macro pitfalls (prefer `inline`/`constexpr` in C++)

### CSS / Less / Sass

- Specificity wars (avoid `!important`, overly nested selectors)
- Dead/unused selectors and variables
- Inconsistent units (mix `px`, `rem`, `em` without reason)
- Missing fallbacks for modern features (`gap`, `aspect-ratio`, custom properties)
- Magic numbers (hardcoded z-index, positioning values without context)
- Vendor prefix inconsistencies (use Autoprefixer or consistent `-webkit-`/`-moz-`)
- Selector performance (avoid deep descendant chains, prefer class selectors)
- Preprocessor anti-patterns: excessive nesting (>3-4 levels), variable name collisions, mixin overuse
- Print/debug code left in (e.g., `@debug`, `content: "test"`)

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

## Output Format

```markdown
## Code Review Summary

**Files reviewed**: X
**Lines changed**: +X / -Y
**Assessment**: APPROVE / COMMENT / REQUEST_CHANGES

### Review Scope
- Correctness, security, reliability
- Concurrency, performance
- Architecture, maintainability
- Testing, removal candidates

---

## Findings

### P0 - Critical
1. **[file:line] Title**
   - **Problem:** ...
   - **Impact:** ...
   - **Fix:** ...

### P1 - High
2. **[file:line] Title**
   - **Problem:** ...
   - **Impact:** ...
   - **Fix:** ...

### P2 - Medium
3. **[file:line] Title**
   - **Problem:** ...
   - **Fix:** ...

### P3 - Low
4. **[file:line] Title**
   - **Suggestion:** ...

---

## What Looks Good
- ...

## Removal Candidates
### Safe to remove
- ...
### Defer with plan
- ...

## Testing / Verification
- Tests run: ...
- Lint/type-check: ...
- Not verified: ...

## Residual Risks
- ...

## Next Steps
Found **X issues**: P0: X, P1: X, P2: X, P3: X

1. **Fix all**
2. **Fix P0/P1 only**
3. **Fix specific items** (specify numbers)
4. **No changes**
```

If a section has nothing to report, say "None found." Do not omit sections.

---

## Clean Review

When no significant issues:

```markdown
## Code Review Summary

**Assessment**: APPROVE

No P0/P1/P2 issues identified.

### What was checked
- Correctness, edge cases, security
- Error handling, concurrency
- Database interactions, performance
- Architecture, test coverage

### Verification
- Tests: passed
- Build: passed

### Not verified
- Production config, external service behavior

### Residual risks
- ...
```

Never claim tests passed unless actually run.

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
