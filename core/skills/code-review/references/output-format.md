# Output format (code-review)

## Full report template

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

## Clean review template

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
