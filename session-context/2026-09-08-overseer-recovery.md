# Overseer recovery — 2026-09-08

User authorized repair and reactivation after read-only diagnosis.

## Cause and changes
- Overseer was inactive/disabled; last heartbeat 2026-08-16 10:06:56 UTC. Exact reason for the earlier stop is unproven.
- Gateway health v1 supplied a process name to a PID-only check, tested true instead of failure, and had no parseable deterministic actions.
- Added gateway_healthy check: user systemd state plus bounded socket ping; health-gateway v2 restarts on failure, with cooldown and failure quarantine. Startup synchronized the runtime copy automatically.
- Moved OnFailure and start-limit settings into [Unit]; added RestartSec=5. Updated source templates and installed user units.
- Existing alert environment lacked TELEGRAM_OWNER_CHAT_ID. Set it from the existing policy owner, without changing policy. Curl now fails on HTTP errors and has a 15-second timeout.
- Enabled and started yeoman-overseer.service. User lingering was already enabled.

## Validation
- Regression checks failed before implementation; all 228 Overseer tests pass after repair.
- Ruff on changed Python files, git diff --check, and systemd-analyze verify on corrected units pass.
- Live gateway check returned pong; bridge reported connected=True, running=True, reconnectAttempts=0.
- Overseer loaded 13 runbooks; socket ping responds and persisted heartbeat advances. systemd watchdog is refreshed; no automatic restarts or new startup warnings/errors observed.
- Gateway and Bridge PIDs were preserved; no production outage was induced.
- config.json and policy.json are byte-identical to pre-repair backups.
- Telegram alert delivery was not tested by sending a message. Corrected configuration is verified; delivery is not claimed.

## Handoff
Source changes are not committed. Existing unrelated worktree edits were preserved.
A regression test is marked intent-to-add because tests/overseer/* is ignored.
Backup: /home/dm/.yeoman/backups/overseer-recovery-20260908T002158
This repair covers gateway monitoring and Overseer service recovery, not a full audit of all maintenance runbooks.
