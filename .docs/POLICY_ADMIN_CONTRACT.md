## Policy Admin Command Contract

This document is the canonical contract for deterministic policy administration via DM and CLI.

### Scope

- Namespace: `/policy`
- Surfaces:
  - Owner WhatsApp DM deterministic command handling.
  - CLI shared command entrypoint: `nanobot policy cmd "/policy ..."`.
- Non-goal: LLM interpretation for policy mutations.

### Command Grammar

- Canonical form: `/<namespace> <subcommand> [args...]`
- Parsing: `shlex` shell-style tokenization.
- Namespace is case-insensitive.
- Subcommand aliases:
  - `resume-group` -> `allow-group`
  - `pause-group` -> `block-group`
  - `groups` -> `list-groups`

### Authorization + Visibility Rules

1. Deterministic DM routing only for slash-prefixed commands.
2. Unknown slash namespace returns deterministic error only in owner-DM applicable context.
3. Non-owner `/policy ...` is silent ignore (no deterministic response).
4. Non-slash `policy ...` is treated as regular LLM text.
5. CLI `policy cmd` is local trusted execution (`source=cli`).

### Supported Commands

- `/policy help`
- `/policy list-groups [query]`
- `/policy resolve-group <name_or_id>`
- `/policy status-group <chat_id@g.us>`
- `/policy explain-group <chat_id@g.us>`
- `/policy allow-group <chat_id@g.us> [--dry-run]`
- `/policy block-group <chat_id@g.us> [--dry-run]`
- `/policy set-when <chat_id@g.us> <all|mention_only|allowed_senders|owner_only|off> [--dry-run]`
- `/policy set-persona <chat_id@g.us> <persona_path> [--dry-run]`
- `/policy clear-persona <chat_id@g.us> [--dry-run]`
- `/policy block-sender <chat_id@g.us> <sender_id> [--dry-run]`
- `/policy unblock-sender <chat_id@g.us> <sender_id> [--dry-run]`
- `/policy list-blocked <chat_id@g.us>`
- `/policy history [limit]`
- `/policy rollback <change_id> [--confirm] [--dry-run]`

### Invariants

- Mutations are validated before persistence.
- Persistence is atomic (`save_policy` temp-file replace semantics).
- Policy engine is reloaded via one central callback after successful writes.
- Dry-run never writes policy file, backup, or audit row.
- Rollback restores from stored snapshot backup reference.

### Audit + Rollback

- Audit log file: `~/.nanobot/policy/audit/policy_changes.jsonl`
- Backup snapshots: `~/.nanobot/policy/audit/backups/<change_id>.json`
- Audit fields:
  - `id`, `timestamp`, `actor_source`, `actor_id`, `channel`, `chat_id`, `command_raw`,
  - `dry_run`, `result`, `before_hash`, `after_hash`, `backup_ref`, `error`

### Runtime Guardrails

- DM admin rate limit: `runtime.adminCommandRateLimitPerMinute`.
- Risky command confirmation gate: `runtime.adminRequireConfirmForRisky`.
- Future optional behavior gating: `runtime.featureFlags`.

### Acceptance Checklist

1. Parser + routing
- `/policy help` routes deterministically.
- `policy help` does not route deterministically.
- Quoted args parse correctly.
- Unknown slash namespace behavior matches owner/non-owner rules.

2. Authorization
- Owner DM `/policy ...` executes.
- Non-owner DM `/policy ...` ignored silently.
- Group chat `/policy ...` not applicable.

3. Mutation semantics
- Every mutating command changes only intended fields.
- Dry-run returns predicted hash delta and no file mutation.
- Validation failure blocks write.

4. Audit + rollback
- Applied mutation writes one backup + one audit row.
- Rollback restores the exact target snapshot hash.
- Invalid rollback ID fails deterministically without partial write.

5. CLI/DM parity
- Same canonical command string produces equivalent behavior and hashes (surface-specific actor metadata may differ).

6. Observability
- Metrics emitted for execute/mutation/rollback/audit-write-failure paths.
