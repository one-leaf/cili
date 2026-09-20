---
name: generate-agents-md
description: Scan project and generate AGENTS.md project instructions. Use when user mentions 生成 AGENTS.md、生成项目提示词、scan project、analyze codebase。
roles: [master, worker]
---

# Generate AGENTS.md

Scan the project in the current working directory and generate a comprehensive `AGENTS.md` file at the project root.

AGENTS.md is a project-level instruction file read by AI coding assistants (Cili, Claude Code, Codex, etc.) to understand project context, conventions, and workflows.

## Generation Process

### Step 1: Scan Project Structure

Use `bash` and `find` to scan the workspace:

```bash
# Top-level directory structure
ls -la

# File type distribution
find . -maxdepth 3 -type f | grep -oE '\.[a-z]+$' | sort | uniq -c | sort -rn | head -20

# Key configuration files
find . -maxdepth 3 -name "package.json" -o -name "pyproject.toml" -o -name "Makefile" \
  -o -name "Cargo.toml" -o -name "go.mod" -o -name "*.sln" -o -name "requirements.txt" \
  -o -name "setup.py" -o -name "tsconfig.json" -o -name ".eslintrc*" \
  -o -name "jest.config*" -o -name "pytest.ini" -o -name "tox.ini" \
  -o -name ".prettierrc*" -o -name "ruff.toml" -o -name ".flake8" 2>/dev/null

# Check for existing CLAUDE.md or AGENTS.md
ls -la CLAUDE.md AGENTS.md 2>/dev/null || echo "No existing project instructions"
```

### Step 2: Identify Project Type and Commands

Based on configuration files, determine project type:

| File | Project Type | Build Commands Source |
|------|--------------|---------------------|
| `package.json` | Node.js/TS | `scripts` field |
| `pyproject.toml` | Python | `tool.*` sections |
| `Cargo.toml` | Rust | `cargo build/test` |
| `go.mod` | Go | `go build/test` |
| `Makefile` | Generic | `make` targets |
| `*.sln` / `*.csproj` | C#/.NET | `dotnet build` |

**Read configuration files** and extract:
- Install commands (`npm install`, `pip install`, `cargo build`)
- Build commands (`npm run build`, `make`, `cargo build`)
- Test commands (`npm test`, `pytest`, `cargo test`)
- Lint/format commands (`npm run lint`, `ruff format`, `cargo clippy`)
- Dev server commands (`npm run dev`, `python manage.py runserver`)

### Step 3: Analyze Coding Conventions

Scan 3-5 representative source files to extract:

- **Naming**: variables (camelCase/snake_case), classes (PascalCase), files (kebab-case/snake_case)
- **Import style**: relative vs aliased, import ordering (stdlib → third-party → local)
- **String quotes**: single vs double quotes (JS/TS)
- **Semicolons**: used or omitted (JS/TS)
- **Indentation**: spaces vs tabs, 2/4 spaces
- **Comments**: Chinese vs English, inline vs block
- **Type annotations**: used or not (Python/TS)
- **Error handling**: exceptions vs Result types vs try-catch

### Step 4: Analyze Testing Conventions

- Test framework (jest/pytest/vitest/go test/cargo test)
- Test file naming (`*.test.ts` / `test_*.py` / `*_test.go` / `*_test.rs`)
- Test directory structure (`__tests__/` / `tests/` / colocated with source)
- Fixture and mock conventions
- Snapshot testing usage
- Integration vs unit test separation

### Step 5: Check for CI/CD and Git Conventions

```bash
# Check for CI configs
ls -la .github/workflows/ .gitlab-ci.yml Jenkinsfile .circleci/ 2>/dev/null

# Check for commit hooks
ls -la .husky/ .git/hooks/ 2>/dev/null

# Recent commit messages (for convention analysis)
git log --oneline -20 2>/dev/null | head -10
```

### Step 6: Generate AGENTS.md

Output in **English** following this structure (omit irrelevant sections):

```markdown
# {Project Name}

{One-line project description}

## Project Structure & Module Organization

{List 5-15 key directories with descriptions. Example:}
- `src/` - Main source code
  - `src/api/` - API handlers and routes
  - `src/models/` - Data models and schemas
  - `src/utils/` - Shared utilities
- `tests/` - Test files
- `docs/` - Documentation
- `scripts/` - Build and utility scripts

## Build, Test, and Development Commands

- `{install_cmd}`: Install dependencies
- `{build_cmd}`: Build the project
- `{test_cmd}`: Run all tests
- `{test_cmd} path/to/file`: Run specific test file
- `{lint_cmd}`: Run linter and fix issues
- `{format_cmd}`: Format code
- `{dev_cmd}`: Start development server

## Coding Style & Naming Conventions

{Describe conventions observed in the codebase. Examples:}
- Use `{naming_style}` for {variables/functions/classes}
- Import order: stdlib → third-party → local
- {Other project-specific conventions}

## Testing Guidelines

- Place tests in `{test_location}`
- Name test files as `{test_naming_pattern}`
- Use `{assertion_style}` for assertions
- {Other testing conventions}

## Commit & Pull Request Guidelines

{If commit conventions are observed:}
- Commit format: `{commit_format}` (e.g., `feat:`, `fix:`, `docs:`)
- PR title should match the primary change
- {Other conventions}

## Architecture Decisions

{Key architectural patterns and constraints. Examples:}
- API uses {REST/GraphQL/gRPC} with {auth pattern}
- State managed via {Redux/Zustand/Context}
- Database queries use {ORM/raw SQL/query builder}
- Error handling follows {pattern}
```

### Step 7: Write the File

Use the `write` tool to save the generated content to `AGENTS.md` at the project root.

## Guidelines

1. **Evidence-based**: All statements must come from actual files scanned. Never guess or fabricate.
2. **Comprehensive but focused**: Each section should be 5-20 lines. Prioritize information that helps AI make correct decisions.
3. **Language**: AGENTS.md content must be in **English** for maximum AI tool compatibility.
4. **Omit irrelevant sections**: If the project has no tests, skip Testing Guidelines. If no CI, skip Commit Guidelines.
5. **No secrets**: Never include API keys, passwords, or internal URLs.
6. **Be specific**: Instead of "follow best practices", state the actual pattern used.
7. **Include negative rules**: If you notice anti-patterns the project explicitly avoids, document them (e.g., "Do not use `any` type in TypeScript", "Never import from `../internal/`").
