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
| `processing.threads.followupWindowSeconds` | how long a thread keeps taking continuations | `600` |
| `processing.ambientChats` | chats where an unaddressed message may still be answered | `[]` |
| `processing.replyActions` | per chat `answer` (default), `react` or `silence` | `{}` |
| `processing.reactionEmojis` | the emojis a model-chosen reaction may use | 14 approved emojis |
| `processing.reactionRoute` | model route that picks the emoji for `react` (empty = the memory capture route) | `""` |
| `processing.ambient.minSecondsBetweenAnswers` | minimum distance between two ambient answers in one chat | `300` |
| `processing.ambient.minMessagesSinceAnswer` | how much new chatter is required first | `6` |
| `processing.ambient.judgeMinConfidence` | how sure the judge must be before answering at all | `0.75` |
| `processing.extraction.timezone` | IANA zone for relative dates ("morgen") | `UTC` |

The gateway log states the effective mode on every start:

```bash
yeoman logs | grep -E "processing mode|protocol v"
yeoman logs | grep reaction_emojis      # the vocabulary that is actually live
```

Routing decisions and reactions are observable per message without any content:

```bash
yeoman logs | grep routing_decision     # classification, candidates, signal, action, lineage
yeoman logs | grep routing_effect       # one line per queued effect
yeoman logs | grep reaction_dropped     # a model-chosen emoji that is not approved
```

### Ambient answers (unaddressed messages)

Answering a message Arvid was not addressed in is the most expensive thing this mode can
do, so it takes three gates, in this order:

1. **Permission** - the chat's policy must allow an answer at all (`all`, `allowed_senders`
   or `owner_only`; `mention_only` observes). Direct addresses - mention, reply, or a plain
   `Arvid, …` request - never take this path and are answered immediately.
2. **Brake** (`processing.ambient.*`) - both thresholds must be met: enough time since the
   last ambient answer *and* enough new messages since then. Until they are, the message is
   only observed: no turn, no typing indicator, no model call. A message that *names* him
   skips the brake (owner decision): a sentence with "Arvid" in it is worth the judge's
   look even when it is not a request - but it is still the judge that decides.
3. **Judge** - one small model call (`processing.ambient.judgeRoute`) with a strict question
   and three possible outcomes:
   * `answer` - a real reply; only then is a turn opened (and typing shown),
   * `react` - one emoji from `processing.reactionEmojis`, sent as a reaction effect with no
     turn, no typing and no text,
   * `none` - silence.

   Every outcome needs `processing.ambient.judgeMinConfidence`; an error, a timeout, an
   unparsed verdict or an unapproved emoji mean silence. A `none` restarts the brake window,
   so the judge is asked at most once per window instead of once per message.

Observable per message: `ambient_brake` (why not yet), `ambient_judge` (answer, confidence,
threshold), `ambient_answer_granted`. A declined message stays declined: the classic
pipeline cannot answer it later.

### Reaction vocabulary

The model picks the emoji, the owner owns the list. `processing.reactionEmojis` is the
complete set a *model-chosen* reaction may use; an emoji outside it is dropped and logged
as `reaction_dropped` - never replaced by a guessed face, and never sent as text, so a
reaction-only reply simply stays silent. Confirmations the gateway decides itself (blocked
input, name mention, admin acknowledgement) are not model choices and are unaffected.

Two paths produce a model-chosen reaction, and both use this vocabulary:

* the persona's `::reaction::<emoji>` marker in a generated answer;
* `processing.replyActions: "react"` for a chat - a message that would have been answered
  gets one reaction instead. That choice is a single small model call
  (`processing.reactionRoute`), so it costs neither a persona prompt nor a typing
  indicator, and it never acknowledges messages the chat only observes.

Editing the list takes effect on the next gateway start:

```bash
# config.json (runtime tree), then:
systemctl --user restart yeoman-gateway
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

- Copies outside the two databases are not purged by a fact revocation. `InvalidationReport.remaining_copies`
  names them: journal payloads until retention, SQLite backups and the inbound reply archive.
  `yeoman memory facts revoke` does not claim more than it does.
- **The inbound reply archive is kept complete on purpose** (`retention_days=None`): every
  message that reaches the orchestrator is recorded and nothing is purged, not at startup
  and not by the hourly maintenance pass. `purge_older_than()` is a no-op in this mode, so
  an operator command cannot shorten the record by accident. Messages refused before the
  orchestrator (for example a blocked sender) never reach it and are therefore absent.
- **Journal payload retention is deliberately not scheduled yet.** The windows
  (`processing.retention.*`) and `ProcessingStore.purge()` exist and are unit-tested, but
  nothing calls them in the running gateway, so event and effect payloads persist. Treat
  the journal as retaining raw payloads until that is implemented properly.
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
