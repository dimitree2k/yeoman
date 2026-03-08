# Agent Doctor Problems

Consult this file only for issue IDs reported by `yeoman doctor`.

## Memory

### MEM-001
Severity: CRITICAL
Problem: `yeoman memory status` failed.
Likely cause: invalid config, import/runtime error, or broken memory setup.
Fix: run `yeoman memory status` directly, inspect the traceback, then validate `config.json` and memory settings.

### MEM-002
Severity: CRITICAL
Problem: `memory.enabled` is false.
Likely cause: memory disabled in config.
Fix: set `memory.enabled` to `true` in `~/.yeoman/config.json`, then restart the gateway.

### MEM-003
Severity: WARNING
Problem: `memory.embedding.enabled` is false.
Likely cause: vector recall disabled.
Fix: set `memory.embedding.enabled` to `true` if semantic recall is desired.

### MEM-004
Severity: WARNING
Problem: `memory.wal.enabled` is false.
Likely cause: session-state WAL disabled.
Fix: set `memory.wal.enabled` to `true` for session-state persistence.

### MEM-005
Severity: WARNING
Problem: memory has zero active entries.
Likely cause: fresh install or capture not working.
Fix: verify capture settings and test with `yeoman memory add --text "test" --kind fact`.

### MEM-006
Severity: WARNING
Problem: SQLite journal mode is not WAL.
Likely cause: DB recreated outside the normal yeoman path or altered manually.
Fix: `sqlite3 ~/.yeoman/data/memory/memory.db "PRAGMA journal_mode=WAL;"`

### MEM-007
Severity: WARNING
Problem: memory DB is large.
Likely cause: long retention with no compaction.
Fix: back up the DB, then run `sqlite3 ~/.yeoman/data/memory/memory.db "VACUUM;"`

## Cron

### CRON-001
Severity: WARNING
Problem: `yeoman cron list` failed.
Likely cause: invalid cron store or runtime issue.
Fix: inspect `~/.yeoman/data/cron/jobs.json` and rerun `yeoman cron list`.

### CRON-002
Severity: WARNING
Problem: one or more cron jobs are disabled.
Likely cause: manual disable, one-shot completion, or previous failure handling.
Fix: inspect with `yeoman cron list`, then re-enable intentionally disabled recurring jobs.

### CRON-003
Severity: WARNING
Problem: one or more cron jobs show failure state.
Likely cause: bad payload, missing credentials, or downstream model/runtime errors.
Fix: inspect `state.lastError` in `~/.yeoman/data/cron/jobs.json` and rerun the affected job.

## Config

### CFG-001
Severity: CRITICAL
Problem: `config.json` is invalid JSON.
Likely cause: syntax error or manual edit mistake.
Fix: repair the JSON, then rerun `yeoman status`.

### CFG-002
Severity: CRITICAL
Problem: `policy.json` is invalid JSON.
Likely cause: syntax error or partial write.
Fix: repair the JSON, then rerun `yeoman policy explain ...` or `yeoman status`.

### CFG-003
Severity: CRITICAL
Problem: no provider credentials resolve for the active model.
Likely cause: missing API keys in config, `.env`, or process env.
Fix: add at least one provider credential usable by the active default model.

### CFG-004
Severity: CRITICAL
Problem: Telegram is enabled but no token resolves.
Likely cause: missing `TELEGRAM_BOT_TOKEN` or empty `channels.telegram.token`.
Fix: set the token in `.env`, process env, or config.

### CFG-005
Severity: WARNING
Problem: raw `config.json` stores secrets directly.
Likely cause: credentials kept in config instead of env.
Fix: run `yeoman config migrate-to-env` or move the values manually to `.env`.

## Workspace

### WORK-001
Severity: WARNING
Problem: required workspace file is missing.
Likely cause: incomplete onboarding or accidental deletion.
Fix: recreate the missing file under `~/.yeoman/workspace/`.

### WORK-002
Severity: WARNING
Problem: no persona files found.
Likely cause: minimal workspace setup.
Fix: add persona files under `~/.yeoman/workspace/personas/` if you rely on persona switching.

## Gateway

### GATE-001
Severity: CRITICAL
Problem: gateway is not running while channels are enabled.
Likely cause: gateway stopped, crashed, or failed to start.
Fix: run `yeoman gateway start --daemon` or `yeoman gateway`.

### GATE-002
Severity: CRITICAL
Problem: WhatsApp bridge is not running while WhatsApp is enabled.
Likely cause: bridge crash, port conflict, or missing runtime dependencies.
Fix: run `yeoman channels bridge restart`.

### GATE-003
Severity: CRITICAL
Problem: WhatsApp auth state is missing.
Likely cause: QR login never completed or auth directory was removed.
Fix: run `yeoman channels login`.

### GATE-004
Severity: WARNING
Problem: gateway log has a high recent error count.
Likely cause: provider failures, policy/config errors, or channel transport issues.
Fix: inspect `~/.yeoman/var/logs/gateway.log`.

### GATE-005
Severity: WARNING
Problem: bridge log has a high recent error count.
Likely cause: WhatsApp runtime mismatch, auth issues, or network problems.
Fix: inspect `~/.yeoman/var/logs/whatsapp-bridge.log`.

## Security

### SEC-001
Severity: WARNING
Problem: gateway binds beyond localhost.
Likely cause: default or intentionally exposed network binding.
Fix: if external access is not needed, set `gateway.host` to `127.0.0.1`.

### SEC-002
Severity: WARNING
Problem: WhatsApp bridge binds beyond localhost.
Likely cause: custom `channels.whatsapp.bridge_host`.
Fix: set the bridge host to `127.0.0.1` or `localhost` unless remote access is intentional.

### SEC-003
Severity: WARNING
Problem: exec isolation is disabled.
Likely cause: relaxed tool security posture.
Fix: set `tools.exec.isolation.enabled` to `true`.

### SEC-004
Severity: WARNING
Problem: neither workspace restriction nor strict profile is enabled.
Likely cause: permissive file/tool posture.
Fix: enable `tools.restrictToWorkspace` and/or `security.strictProfile`.

### SEC-005
Severity: WARNING
Problem: `.env` permissions are too open.
Likely cause: default file mode or manual chmod.
Fix: `chmod 600 ~/.yeoman/.env`

### SEC-006
Severity: WARNING
Problem: secrets/auth directory permissions are too open.
Likely cause: default directory mode or manual chmod.
Fix: `chmod 700 ~/.yeoman/secrets`

### SEC-007
Severity: CRITICAL
Problem: likely API keys were found in workspace files.
Likely cause: secrets pasted into prompt files, personas, or skills.
Fix: remove the secret, rotate the credential, and check git history if applicable.

## System

### SYS-001
Severity: WARNING
Problem: Python version is below the current yeoman packaging floor.
Likely cause: older interpreter on the host.
Fix: install Python 3.14+ and reinstall yeoman into that interpreter.

### SYS-002
Severity: WARNING
Problem: Node.js version is below the WhatsApp floor.
Likely cause: older Node runtime.
Fix: install Node.js 18+.

### SYS-003
Severity: WARNING
Problem: low free disk space.
Likely cause: large logs, media, or databases.
Fix: clear logs/media and vacuum large SQLite databases if needed.

### SYS-004
Severity: WARNING
Problem: `sqlite3` is missing while memory is enabled.
Likely cause: SQLite CLI not installed.
Fix: install `sqlite3` so low-level DB checks and manual maintenance are available.
