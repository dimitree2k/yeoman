# Session Context

Durable notes for agent sessions that contain useful findings, orientation, or
handoff context that should survive chat compaction.

Use this directory for concise notes that are too specific for `AGENTS.md` but
worth finding later. Keep stable rules and navigation in `AGENTS.md`; move
large designs or implementation plans to `docs/superpowers/`.

## Naming

Use:

```text
YYYY-MM-DD-short-description.md
```

Examples:

```text
2026-04-25-project-navigation.md
2026-04-25-runtime-debugging-findings.md
2026-04-25-consciousness-layer-review.md
```

## Template

```markdown
# Short Title

Date: YYYY-MM-DD
Context: one sentence about why this note exists.

## Findings

- High-signal facts discovered during the session.

## Follow-up

- Concrete next actions, if any.
```
