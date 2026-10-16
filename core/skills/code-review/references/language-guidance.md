# Language-specific guidance (code-review)

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
