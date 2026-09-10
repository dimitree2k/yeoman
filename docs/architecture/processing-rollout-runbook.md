# Processing Rollout Runbook (Plan 06)

Operational guide for the state-aware message processing mode. Every command below is a
real interface of this checkout; nothing here is aspirational. Read the whole page before
the first switch.

Scope: `processing.enabled` (threads, turns, effect outbox, reconciliation) and
`memory.shared.enabled` (shared chat facts). Both are **off by default**.

## 1. Current state

```bash
yeoman status                      # config, policy, workspace, providers
yeoman channels status              # which channels are enabled and configured
yeoman channels whatsapp bridge status
systemctl --user is-active yeoman-gateway
systemctl --user show yeoman-gateway -p MainPID -p NRestarts -p ActiveState
```

Effective switches live in `config.json` (runtime tree):

| Key | Meaning | Default |
|---|---|---|
| `processing.enabled` | master switch for the new mode | `false` |
| `processing.chats` | activated chats, `whatsapp:<chat-id>` | `[]` |
| `processing.shadowChats` | journal + decide without sending | `[]` |
| `memory.shared.enabled` | shared facts (read path + extraction) | `false` |
| `memory.shared.extractionEnabled` | extraction worker | `false` |
| `processing.budgets.chat_hard_units` / `_window_seconds` | hard per-chat send budget | `6` / `60` |
| `processing.budgets.thread_soft_units` / `_window_seconds` | soft thread deferral | `2` / `10` |
| `processing.budgets.outbox_waiting_per_chat` | waiting outbox cap | `20` |

The gateway log states the effective mode on every start:

```bash
yeoman logs | grep -E "processing mode|protocol v"
```

`new processing mode disables non-migrated capabilities: [...]` is expected and means the
listed legacy capabilities (A2A, exec, browse, calendar) are refused for activated chats.

## 2. Disabled introduction (the safe order)

Nothing below changes behaviour for chats that are not listed in `processing.chats`.

1. Deploy the code with every switch off: `cd ~/Documents/yeoman && yeoman deploy`.
2. Confirm inertness: the processing database may exist (schema only), but there must be
   no thread, turn or effect rows, and no shared-fact rows:

   ```bash
   python - <<'PY'
   import sqlite3
   c = sqlite3.connect("file:" + __import__("os").path.expanduser("~/.yeoman/data/processing/processing.db") + "?mode=ro", uri=True)
   for t in ("threads", "turns", "effects"):
       print(t, c.execute(f"select count(*) from {t}").fetchone()[0])
   PY
   ```

3. **Shadow first.** Add the chat to `processing.shadowChats` and restart the gateway.
   Shadow mode journals events and records decisions without sending anything. Watch for
   one real conversation cycle, then check the decision distribution.
4. Activate one chat: move it from `shadowChats` to `processing.chats`, restart, and send
   one real message. Verify a `thread_assigned` line and a `sent` effect with a real
   `turn_id`.
5. Only then consider `memory.shared.enabled` (with `extractionEnabled` still off), and
   only after the Plan 05 acceptance was signed off.

## 3. Status and evidence

```bash
yeoman logs | grep -E "thread_assigned|assignment_unavailable|degraded|effect blocked"
yeoman memory facts list --limit 20     # shared facts: metadata only
yeoman memory facts jobs --state queued # extraction backlog
```

Durable evidence per effect (states: `planned/queued/executing/sent/blocked/expired/failed/cancelled/unknown/unknown_nonrepeatable`):

```bash
python - <<'PY'
import os, sqlite3
path = os.path.expanduser("~/.yeoman/data/processing/processing.db")
c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for row in c.execute("select effect_id, state, capability, turn_id, turn_revision from effects order by rowid desc limit 10"):
    print(dict(row))
print("receipts:", c.execute("select count(*) from transport_receipts").fetchone()[0])
print("probes:", c.execute("select count(*) from reconciliation_probes").fetchone()[0])
PY
```

`sent` is only ever claimed from a transport or probe receipt. An effect that stays
`unknown` is **not** a failure verdict; the reconciler probes it with backoff and escalates
to `unknown_nonrepeatable` after the deadline. A late receipt still corrects it to `sent`.

## 4. Admission stop (freeze without rolling back)

Set `processing.enabled=false` (or remove the chat from `processing.chats`) and restart.
New admissions stop; already claimed work keeps its state, and `unknown` effects stay
unknown instead of being retried blindly. Nothing is deleted — the database and archives
are untouched, so returning to the new mode later reconstructs the same state.

## 5. Backup — do not use `cp`

The processing and memory databases run in **WAL mode**. A plain `cp` of the `.db` file
misses everything still in the `-wal` file and silently produces an older state (observed
in practice: a copy showed schema 2 with 4 events while the live database was at schema 4
with 19 events). Use SQLite's own backup API:

```bash
python - <<'PY'
import sqlite3, time, os
home = os.path.expanduser("~/.yeoman/data")
stamp = time.strftime("%Y%m%d-%H%M%S")
for name in ("processing/processing.db", "memory/memory.db"):
    src = sqlite3.connect(f"file:{home}/{name}?mode=ro", uri=True)
    dst = sqlite3.connect(f"{home}/{name}.walsafe-{stamp}.bak")
    src.backup(dst)
    dst.close(); src.close()
    print("backed up", name)
PY
```

Verify a backup before trusting it: open it read-only and compare the schema version and a
row count against the live database.

## 6. Restart

```bash
systemctl --user restart yeoman-gateway
sleep 20
systemctl --user show yeoman-gateway -p NRestarts -p ActiveState
yeoman logs | tail -40
```

Expected on a healthy start: `Connected to WhatsApp bridge (protocol v4)`, the effective
processing mode line, and no `ERROR`/`CRITICAL`. A start that exits immediately with
status 0 is the single-instance guard, not a crash.

## 7. Bridge and IPC check

```bash
yeoman channels whatsapp bridge status     # protocol version and process state
ss -ltnp | grep 3001                        # bridge socket
ls -l ~/.yeoman/run/                        # gateway.sock, overseer.sock, pid files
```

If the bridge reports a protocol below the one the gateway expects, the gateway refuses to
start (`Bridge manifest protocol mismatch`) — check `packages/bridge/bridge.manifest.json`
against `PROTOCOL_VERSION` before blaming the process manager.

## 8. Rollback

Rollback is a configuration change, not a data operation:

1. `processing.enabled=false` (keep `memory.enabled` as it is), restart the gateway.
2. Wait for in-flight work: `executing` claims recover to `unknown` on the next start and
   are probed; nothing is re-sent on the strength of an unproven outcome.
3. Legacy chats continue on the old path unchanged. New-mode tables stay in place;
   rollback deletes no database and no archive.
4. Re-activating later reconstructs lease, unknown and thread state from the same tables.

Known limits to state honestly when reporting a rollback or a deletion:

- Copies outside the two databases are not purged by a fact revocation: SQLite backups,
  the inbound archive, session-state Markdown and journal payloads until retention expires.
  `InvalidationReport.remaining_copies` names them; `yeoman memory facts revoke` does not
  claim more than it does.
- Deletion completeness inside external providers/caches is not asserted.
- Extracted statements are candidates, not verified truth.

## 9. Verification before declaring a rollout done

```bash
cd ~/Documents/yeoman
uv run pytest -q            # full suite
uv run ruff check .
git diff --check
```

For bridge changes additionally `cd packages/bridge && npm run build && npm test`. Report
the checks that actually ran, and name any that did not.
