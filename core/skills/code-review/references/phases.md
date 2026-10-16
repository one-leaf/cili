# Review phases in detail (code-review)

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
