# Runtime/Source Structure Normalization — Execution Baseline

Captured 2026-09-07 before any migration or service change. This note records metadata only; no database or message contents are copied here.

## Source repository

- Checkout: `/home/dm/Documents/yeoman`
- Current branch: `main`
- HEAD: `428a5d9086dbc13f5137202113c86bb444c90e32` (`fix(bridge): default media storage under var`)
- `c/turn-engine-v2`: `2c7cdd1ff7678dffac84c93382ded970782580b7`
- Relationship: `c/turn-engine-v2` is an ancestor of `main`; `main` is seven commits ahead and the branch has no commits absent from `main`.
- `c/turn-engine-v2` itself does not contain the current `a2a/`, owner-turn IPC/observability, or associated test paths. Those paths are present in `main` through `c/turn-engine-v2-preserve-20260907` commit `2afd4ca31b5920a0334d4ff545ffc23a2fe09772` (with `tests/gateway/test_a2a.py` retained by `3ca4d62a547a6bd38ee78000ab07f894d6dfb4a2`). The branch remains retained until the final path/commit comparison is rechecked and the classification is attached to the migration evidence.
- Pre-existing working-tree changes are not part of this migration: deletions below `.superpowers/sdd/2026-08-16-*`, modifications to `packages/gateway/yeoman_gateway/adapters/responder_llm.py`, `packages/gateway/yeoman_gateway/agent/tools/a2a.py`, `tests/gateway/test_a2a.py`, and the earlier review note `session-context/2026-09-07-runtime-source-normalization-review.md`.
- Branch/path classification: the six commits in `main` after `c/turn-engine-v2` are the preserved working-state series (`2afd4ca3`, `129047fc`, `91d3cf52`, `fc6ed17c`, `3ca4d62a`, `c3bc9ea0`) plus the current Bridge fix (`428a5d90`). The A2A implementation is in `packages/gateway/yeoman_gateway/agent/tools/a2a.py` from `2afd4ca3`, with `tests/gateway/test_a2a.py` retained by `3ca4d62a`; owner-turn IPC is `packages/gateway/yeoman_gateway/ipc/owner_turn.py`, with `tests/gateway/test_owner_turn.py`, and observability coverage is `tests/gateway/test_a2a_observability.py`. Classification: required-now/present on `main`; no commit unique to `c/turn-engine-v2` remains absent from `main`; the `c/turn-engine-v2` ref remains retained until the final integration decision.

## Runtime and service baseline

- Runtime root: `/home/dm/.yeoman`; source root: `/home/dm/Documents/yeoman`.
- `yeoman-gateway.service`: loaded, active/running, PID `2060818`; launcher `/home/dm/.local/bin/yeoman gateway --port 18790`; effective Python executable `/home/dm/.local/share/uv/tools/yeoman-gateway/bin/python3`.
- `yeoman-bridge.service`: loaded, active/running, PID `1971241`; launcher `/usr/bin/node /home/dm/.yeoman/var/cache/bridge/dist/index.js`; Bridge `127.0.0.1:3001`.
- `yeoman-overseer.service`: loaded, inactive/dead, no PID.
- `yeoman-pinchtab.service`: loaded, inactive/dead, no PID. Pinchtab remains outside this normalization and is not removed here.
- Active Gateway socket: `/home/dm/.yeoman/run/gateway.sock`, owned by PID `2060818`.
- `/home/dm/.yeoman/run/overseer.sock` is absent because Overseer is inactive.
- Existing non-canonical runtime entries: `/home/dm/.yeoman/var/run/overseer.lock` and `/home/dm/.yeoman/var/run/whatsapp-bridge.pid`. They must not be copied as durable state; owning processes must recreate handles after the cutover.
- Bridge media environment points to `/home/dm/.yeoman/var/media`, with incoming/outgoing WhatsApp subdirectories.
- Effective runtime config has `memory.wal.stateDir = "memory/session-state"` and IPC paths `~/.yeoman/run/gateway.sock` / `~/.yeoman/run/overseer.sock`.

## State inventory

The current session-state files are Markdown PRE/POST logs under `/home/dm/.yeoman/workspace/memory/session-state/`; this is not a SQLite WAL store. The active SQLite data set includes contacts, inbound indexes, document cache, memory, and consciousness databases. Several active databases have `-wal` and `-shm` companions; database migration must be quiesced and WAL-aware.

Before migration, the following active database checksum manifest was captured with `sha256sum` (the listed sizes are the main database files):

```text
6bdaee859cfdb050beb37afbf58f1f43a654fea6844bc437ff58c1bf  897024  data/consciousness/speakups.db
926433b42966592fd08460af09b485d1cf57d3108f590849e285a63da2883c8a  110592  data/contacts/contacts.db
837e9e93948df4a523851c37ef1e8f744521162546ba4d543b0035bf4af25f27  372736  data/inbound/chat_registry.db
f5e4e77602d4619b1a3b5e14ac36620d6ae5933e407cc70ca68dcc9feade26c5  4485120  data/inbound/reply_context.db
034adad4e752d9e6895db73b6e040e903d91250c43dd0b9ab34f90d26518a1ef  983040  data/media/document_cache.db
2832846fe28b00d5f69633c6cbff0443c7d84d490c9f8a59ba1d3c5e7d7f9b03  61435904  data/memory/memory.db
```

Active `-wal`/`-shm` companions were present for contacts, inbound indexes and memory; their sizes and hashes were captured in the command evidence for this session. No database bytes were changed.

## Execution boundary

No service was stopped, no runtime database/media/policy file was moved, and no installed/generated artifact was edited while capturing this baseline. The next source changes must preserve the pre-existing working-tree changes above and remain limited to this normalization.

## Completed low-risk changes

- Root-level runtime backups were moved into owner-only `backups/config/`, `backups/policy/` and `backups/migrations/` directories. The old `backups/turn-engine-v2-20260726-205115/` directory is empty and was removed.
- Source-to-target checksum evidence for the moved files is preserved by the pre-move and post-move `sha256sum` outputs in this session. The target files retain the original bytes and sizes.
- Recovery window for this migration: 14 days after the final successful post-cutover verification. Planned expiry: 2026-09-21 (Europe/Berlin), unless a later evidence note extends it.
- No backup, database, media file or policy file was deleted as part of this reorganization.
- The generated `var/cache/bridge.bak-*` directories were inventoried after quiescing/restarting the Bridge. Thirty-five old cache trees (115,343,336 bytes total) had no open handles; `ensure_runtime()` only reads the source checkout and the active `var/cache/bridge/` target, and no deployment/service reference points at a `bridge.bak-*` path. The old generated caches were therefore removed; the active Bridge cache remains at `var/cache/bridge/` and is recreatable from source.
- Six inactive memory snapshot databases and their 12 generated WAL/SHM companions were not active stores and had no open handles. They were moved byte-for-byte from `data/memory/` into the owner-only archive `/home/dm/.yeoman/backups/migrations/2026-09-07T232352+0200-memory-snapshots/`; the archive contains a SHA-256 manifest and is retained through 2026-09-21. The productive memory owner remains only `data/memory/memory.db` with its live WAL/SHM companions.

## Session-state and runtime-handle cutover

- Pre-cutover config and Bridge unit backup: `/home/dm/.yeoman/backups/migrations/2026-09-07T230817+0200-session-state-cutover/`. The config backup hash is `6f468e18e8d461beca247397d29271396c3c63de55a5ef02ab7474a82e7e5326`; the Bridge unit backup hash is `a156dc8e6ec14bea78dd553fd3989ab65f60f8cd06b35bac928b88f86fd1ac4a`.
- Gateway and Bridge were stopped through their user-level systemd units after source tests and backup creation. Both were inactive with no matching TCP/Unix listeners before the state move; Overseer and Pinchtab were already inactive and were not started.
- Controlled restart sequence/runbook: record unit state and handles; stop Gateway and Bridge via `systemctl --user`; verify no affected process, socket, port or state writer remains; reload user units; deploy from the source checkout while quiesced; start Bridge, then Gateway; leave inactive Overseer/Pinchtab inactive; verify systemd state, process command lines, port/socket ownership, IPC response and fresh logs. The sequence was executed for the cutover and repeated for the Bridge bind-mount correction.
- The 45 Markdown session-state files (954875 bytes) were copied to a read-only legacy archive and staged under the target. Filename/size and per-file SHA-256 manifests matched before the atomic rename. The active source is now `/home/dm/.yeoman/data/memory/session-state/`; `/home/dm/.yeoman/workspace/memory/session-state/` no longer exists.
- Runtime config now contains `memory.wal.stateDir = "data/memory/session-state"`. Post-cutover config hash: `9e062780eb26d7a7d5c02060860bfc8a3e0dd4d116903404f9ec5e68bcde561c`.
- Stale `/home/dm/.yeoman/run/gateway.sock` was removed only after the Gateway process and listener were gone. Legacy `/home/dm/.yeoman/var/run/whatsapp-bridge.pid` and `overseer.lock` were archived as non-production evidence; no PID, lock or socket file was copied into the new run root.
- `yeoman deploy` completed successfully with the source checkout, Bridge build hash `44846e2bc21d`, and no service restart during deployment because the units were quiesced. `yeoman-bridge.service` and `yeoman-gateway.service` were then started in that order and are active/running.
- Post-cutover evidence: Gateway PID `2156778`, Bridge PID `2156775`, Gateway IPC `/home/dm/.yeoman/run/gateway.sock`, Bridge `127.0.0.1:3001`, Gateway IPC `{"cmd":"ping"}` returned `{"status":"ok","response":"pong"}`, and `yeoman memory status` reported `state_dir: /home/dm/.yeoman/data/memory/session-state`. The 45-file pre-cutover manifest remains intact; post-cutover activity added `cli_test.md` and `whatsapp_group@g.us.md`, so the canonical target now has 47 files. Both files remain in the canonical target and were not overwritten or discarded.
- A controlled Bridge restart was performed after the cutover because the first post-deploy process retained an old `bridge.bak-*` working-directory mount. The replacement process has PID `2160176`, `cwd=/home/dm/.yeoman/var/cache/bridge`, and the exclusive listener `127.0.0.1:3001`; no old cache directory remains open.

## Ownership and retention evidence

- Gateway bootstrap owns `contacts/contacts.db` through `ContactsService`/`ContactsStore`, `inbound/chat_registry.db` through `ChatRegistry`, `inbound/reply_context.db` through `InboundArchive`, `media/document_cache.db` through `DocumentCache`, and `memory/memory.db` through `MemoryService`/`MemoryStore`. Their resolvers are `get_operational_data_path()` or the effective `Config.memory.db_path`.
- `PolicyLoader` resolves the productive root `policy.json`; `PolicyAdminService` creates the append-only `policy/audit/` journal and policy snapshots beneath it. No `data/policy/audit/` writer was found.
- Bridge `media_paths.ts` and the effective Bridge environment own `var/media/incoming/whatsapp` and `var/media/outgoing/whatsapp`; Gateway media configuration supplies retention and per-type deletion flags. No media was moved or deleted.
- Gateway, Bridge and Overseer logs remain under `var/logs/` through shared helpers or systemd append targets. No log deletion or retention-policy change was made here; existing operator/log retention remains authoritative.

## Verification

- `uv run pytest tests/shared tests/gateway tests/overseer`: **907 passed** in 87.18 seconds.
- `uv run ruff check .`: passed.
- `npm test` in `packages/bridge/`: build and all **24 tests passed**.
- A repository-wide `uv run mypy packages/shared/yeoman_shared packages/gateway/yeoman_gateway packages/overseer/yeoman_overseer` run remains red with 245 existing errors across 64 files; this is a repository-wide typing baseline outside the path-normalization change and is not used as release evidence for this migration.
- Read-only SQLite verification after the snapshot move returned `quick_check=ok` for the six productive databases (`consciousness/speakups.db`, `contacts/contacts.db`, `inbound/chat_registry.db`, `inbound/reply_context.db`, `media/document_cache.db`, `memory/memory.db`). Expected live `-wal`/`-shm` companions remain beside the actively written stores; no retired location contains database or handle files.
- Retired-path scan found no active source/config/service reference to `var/run`, `workspace/memory/session-state`, `data/policy/audit`, `workspace/data` or `docs/plans/`. Remaining matches are historical evidence, this plan, intentional rejection/tests, or the canonical `var/media/...` paths.
- Final live check: `yeoman-gateway.service` is active with PID `2156778` from the uv-managed executable; `yeoman-bridge.service` is active with PID `2160176`, `cwd=/home/dm/.yeoman/var/cache/bridge`, and only listener `127.0.0.1:3001`; Overseer and Pinchtab remain inactive. `yeoman gateway status`, `yeoman channels bridge status`, and the Unix-socket ping are successful. Gateway logs show a clean post-restart Bridge reconnect at 23:17:25 with protocol v3, and `yeoman memory status` reports `state_dir=/home/dm/.yeoman/data/memory/session-state` and `wal_files=47`.
