# Agent Doctor Skill - V2 Plan

## Goal

Create a workspace skill that can diagnose a yeoman installation without relying on stale guesses
about yeoman internals. The skill should prefer supported CLI/status surfaces, fall back to direct
file inspection only where needed, and never apply fixes without explicit user confirmation.

## Why V2

The original plan had the right product idea but encoded several incorrect assumptions:

- It queried a non-existent `memories` table instead of the current `memory2_*` schema.
- It treated `.env` as mandatory even though yeoman supports config-file and process env
  credentials too.
- It marked `gateway.host=0.0.0.0` as a hard failure even though that is the current default.
- It used cron and runtime field names that do not match the current implementation.
- It put too much brittle implementation detail into `SKILL.md`.

V2 fixes that by using a lean skill plus references and a script.

## Scope

Deliver a workspace skill at `~/.yeoman/workspace/skills/agent-doctor/` with:

- `SKILL.md`
- `references/problems.md`
- `scripts/doctor.sh`

Also add this plan doc so the implementation has a grounded reference.

## Non-Goals

- No core `yeoman doctor` CLI command in this pass.
- No automatic remediation.
- No attempt to rewrite yeoman config from the script.
- No broad inventory of every possible failure mode in the runtime.

## Design

### 1. Lean `SKILL.md`

`SKILL.md` should stay short and procedural:

- Trigger phrases
- Primary workflow
- Command to run
- Rules for presenting findings
- Rule to consult `references/problems.md` only for relevant findings

It should not duplicate all checks inline.

### 2. Script First

`scripts/doctor.sh` is the source of truth for diagnostics.

It should:

- Use `yeoman status`, `yeoman memory status`, `yeoman gateway status`,
  `yeoman channels status`, and `yeoman channels bridge status` where available
- Use Python helper snippets to read raw `config.json` plus `.env` and process env where the CLI
  does not expose the needed signal
- Use raw JSON/file inspection only where the CLI does not expose the needed signal
- Emit a stable human-readable report with category summaries and issue IDs
- Exit `0` when no issues are found, `1` when warnings or critical issues are found

### 3. References File

`references/problems.md` should contain only targeted issue entries that the skill can consult after
the script reports a specific issue ID.

Each entry should contain:

- ID
- Severity
- Symptoms
- Likely cause
- Fix guidance

## Diagnostic Categories

V2 keeps the original user-facing shape but corrects the checks:

1. Memory
2. Cron
3. Config
4. Workspace
5. Gateway
6. Security
7. System

## Severity Policy

- `CRITICAL`: yeoman is currently broken for a common primary path, or a high-risk security problem
  is present.
- `WARNING`: degraded behavior, unsupported posture, or a likely future problem.
- `OK`: no issue found for that check.
- `SKIP`: signal intentionally skipped because the prerequisite is missing or the feature is
  disabled.

Important policy corrections:

- Missing `.env` is not automatically a problem.
- Secrets in `config.json` are supported but should be a warning, not a hard failure.
- `gateway.host=0.0.0.0` is a security warning, not a runtime failure.
- WhatsApp-specific checks only run when WhatsApp is enabled.

## Checks

### Memory

- `yeoman memory status` succeeds
- `memory.enabled`
- `memory.embedding.enabled`
- `memory.wal.enabled`
- DB file exists at resolved path
- DB size warning threshold at 500 MB
- Optional `sqlite3` journal mode check against `WAL`
- `total_active > 0`
- `wal_files > 0` when session WAL is enabled

### Cron

- `yeoman cron list` succeeds
- Parse `~/.yeoman/data/cron/jobs.json`
- Warn on disabled jobs
- Warn on jobs with `state.lastStatus` in a failure state or `state.lastError`

### Config

- `config.json` valid JSON
- `policy.json` valid JSON
- `yeoman status` succeeds
- At least one provider credential resolves through config/env
- Active default model resolves to a credential
- Warn when enabled Telegram lacks a token
- Warn when enabled WhatsApp lacks a bridge token
- Warn when raw `config.json` stores secrets directly

### Workspace

- `workspace/SOUL.md`
- `workspace/USER.md`
- `workspace/AGENTS.md`
- `workspace/skills/` exists and contains skill files
- Personas are optional but worth warning on when absent

### Gateway

- `yeoman gateway status`
- If any channel is enabled, gateway not running is `CRITICAL`
- `yeoman channels status`
- `yeoman channels bridge status` when WhatsApp is enabled
- WhatsApp auth files exist when WhatsApp is enabled
- Warn on high error counts in gateway/bridge logs

### Security

- Warn when `gateway.host` is not local-only
- Warn when WhatsApp bridge host is not local-only
- Warn when exec isolation is disabled
- Warn when neither workspace restriction nor strict profile is enabled
- Warn when `.env` permissions are too open
- Warn when secrets/auth directory permissions are too open
- Warn when likely API keys are found in workspace files

### System

- `python3` present and version meets current yeoman packaging floor (`>=3.14`)
- `node` present and version meets WhatsApp floor when WhatsApp is enabled (`>=18`)
- Warn when free disk space is below 1 GB
- Warn when `sqlite3` is missing while memory is enabled

## Deliverables

### `SKILL.md`

Short instructions:

- Run the doctor script first
- Present category summary
- If issues exist, use matching entries from `references/problems.md`
- Ask before applying fixes

### `references/problems.md`

Initial issue set:

- `MEM-*`
- `CRON-*`
- `CFG-*`
- `WORK-*`
- `GATE-*`
- `SEC-*`
- `SYS-*`

### `scripts/doctor.sh`

Standalone diagnostic runner with:

- category summaries
- issue list
- exit code

## Validation

1. Run `bash ~/.yeoman/workspace/skills/agent-doctor/scripts/doctor.sh`
2. Confirm it finishes and prints all categories
3. Confirm it reports the real state of the current environment
4. Confirm `SKILL.md` references only the script and the problems reference

## Future Follow-Up

If the skill proves useful, the next move should be a core `yeoman doctor` command that reuses the
same checks from inside the main codebase rather than leaving them in a workspace script.
