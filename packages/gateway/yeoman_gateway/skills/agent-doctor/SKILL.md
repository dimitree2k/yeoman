---
name: agent-doctor
description: Diagnose the current yeoman installation and workspace. Use for self-checks, health checks, runtime troubleshooting, memory issues, gateway/bridge problems, cron problems, or security posture review. Runs a local doctor command, summarizes findings by severity, and proposes fixes without applying them automatically.
---

# Agent Doctor

Use this skill when the user asks to:

- diagnose yeoman
- run a health check
- check what is broken
- self-check the agent
- inspect memory, gateway, bridge, cron, config, or security problems

## Workflow

1. Run:

```bash
yeoman doctor
```

2. Present the category summary first.
3. If the command reports issues, consult `references/problems.md` only for the reported issue IDs.
4. Recommend concrete fixes, but do not apply anything until the user confirms.
5. Before any fix, show the exact command or file change you plan to make.

## Rules

- Prefer the doctor output over ad-hoc diagnosis.
- Prefer supported yeoman commands over guessing internals.
- Treat warnings as degraded posture, not necessarily breakage.
- Do not require `.env` when credentials are already resolved from config or process env.
- Do not treat `gateway.host=0.0.0.0` as a runtime failure by itself; present it as a security warning.
- WhatsApp-specific findings only matter when WhatsApp is enabled.
