# Agent Guidelines

This file is read by AI coding agents (Claude Code, Codex, Cursor, etc.) working on this repo.
Human contributors should follow these rules too.

## Commit Messages — Conventional Commits (mandatory)

All commits **must** follow the [Conventional Commits](https://www.conventionalcommits.org/) spec:

```
<type>(<scope>): <short summary>

[optional body]

[optional footer(s)]
```

### Types

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `perf` | Performance improvement |
| `docs` | Documentation only |
| `test` | Adding or fixing tests |
| `chore` | Build, deps, tooling, CI |
| `style` | Formatting / lint (no logic change) |

### Scope (optional but recommended)

Use the module name or subsystem: `orchestrator`, `policy`, `memory`, `tts`, `context`, `config`, `channels`, `bridge`, etc.

### Examples

```
feat(orchestrator): add ambient context window for group chats
fix(tts): convert pcm16 to ogg/opus before sending voice note
docs(memory): add ambient-context-window design doc
chore(deps): bump litellm to 1.52
```

### Breaking changes

Append `!` after the type/scope and add `BREAKING CHANGE:` in the footer:

```
feat(config)!: rename reply_context_window to reply_context_window_limit

BREAKING CHANGE: config key renamed; update ~/.nanobot/config.json manually.
```

## General Rules

- Never commit secrets, API keys, or personal data (`~/.nanobot/` runtime data is gitignored).
- Keep PRs focused — one logical change per commit where practical.
- Run `pytest tests/` and `ruff check nanobot/` before pushing.
