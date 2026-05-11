# WhatsApp Memory Backfill - Design

Status: Draft for review
Date: 2026-05-12
Owner: Tim

## 1. Purpose

Add a safe way to backfill a bounded missing slice of WhatsApp group messages
into Yeoman's inbound archive and memory capture pipeline.

The immediate failure case is a one-day gap where Yeoman was off by a day and
important group messages may not have been captured into long-term memory. The
messages are visible in WhatsApp Web, which means the account can likely access
them, but Yeoman currently only captures messages that the bridge receives as
live Baileys events.

The feature should recover selected historical messages without making Yeoman
reply, trigger consciousness, rewrite broad history, or duplicate existing
memory entries.

## 2. Current Runtime Anchors

Use these files as source of truth while implementing:

| Concern | Current file |
|---------|--------------|
| WhatsApp bridge connection and inbound event handling | `packages/bridge/src/whatsapp.ts` |
| Bridge command server | `packages/bridge/src/server.ts` |
| Gateway WhatsApp channel command client | `packages/gateway/yeoman_gateway/channels/whatsapp.py` |
| Inbound archive storage | `packages/gateway/yeoman_gateway/storage/inbound_archive.py` |
| Inbound archive adapter | `packages/gateway/yeoman_gateway/adapters/reply_archive_sqlite.py` |
| Orchestrator typed intents | `packages/gateway/yeoman_gateway/core/intents.py` |
| Memory notes queue and flush logic | `packages/gateway/yeoman_gateway/memory/service.py` |
| Memory capture route config | `packages/shared/yeoman_shared/config/defaults.py`, `~/.yeoman/config.json` |
| Policy resolution for background notes | `packages/gateway/yeoman_gateway/policy/engine.py`, `packages/gateway/yeoman_gateway/policy/schema.py` |
| Existing history read tool | `packages/gateway/yeoman_gateway/agent/tools/summarize_history.py` |

Current bridge behavior:

- `syncFullHistory` is disabled in the Baileys socket options.
- The bridge processes `messages.upsert` events with type `notify` or `append`.
- The gateway archive dedupes by `(channel, chat_id, message_id)`.
- Background memory notes already support no-reply capture for silent messages,
  but there is no replay/import API for historical rows.

If docs disagree with source and tests, trust source and tests first.

## 3. Design Principles

### Bounded By Default

Every backfill must specify a channel, chat id, start time, end time, and limit.
There is no global "sync all history" mode.

### Source-Agnostic Import

The importer should accept normalized messages from any trusted source:

1. Baileys on-demand history fetch from the bridge.
2. Manual JSONL export produced from WhatsApp Web or another operator tool.

Baileys is the preferred source because it can preserve message ids and sender
metadata when available. Manual import is the fallback when WhatsApp Web can
show a gap but Baileys cannot retrieve it reliably.

### Non-Reactive

Backfilled messages must not enter the live inbound queue as normal chat
traffic. They must not generate replies, typing indicators, reactions,
consciousness burst/lull triggers, scheduled proactive output, or chat-visible
side effects.

### Idempotent

Rerunning the same backfill must not duplicate archive rows or memory entries.
Archive idempotency uses the existing `(channel, chat_id, message_id)` key.
Memory idempotency uses existing memory dedupe plus a backfill metadata marker.

### Inspectable

The operator needs a dry run and a final report: rows fetched, rows imported,
duplicates skipped, rows excluded, memory batches queued, memories saved, and
errors by source.

## 4. Non-Goals

- Do not enable global `syncFullHistory` for the live bridge.
- Do not replay historical messages through the normal inbound bus.
- Do not send any chat-visible output during backfill.
- Do not create new memories from broad, unbounded WhatsApp history.
- Do not store raw exported personal data in the repo.
- Do not bypass memory disclosure policy or security input checks.
- Do not build a general WhatsApp data warehouse.
- Do not retag existing memories as part of this feature.

## 5. User-Facing Workflow

The operator should be able to run a dry run first:

```bash
yeoman memory backfill-whatsapp \
  --chat-id 491786127564-1611913127@g.us \
  --since 2026-05-10T00:00:00+02:00 \
  --until 2026-05-11T00:00:00+02:00 \
  --source baileys \
  --limit 500 \
  --dry-run
```

Then import:

```bash
yeoman memory backfill-whatsapp \
  --chat-id 491786127564-1611913127@g.us \
  --since 2026-05-10T00:00:00+02:00 \
  --until 2026-05-11T00:00:00+02:00 \
  --source baileys \
  --limit 500
```

If Baileys cannot retrieve the target range:

```bash
yeoman memory backfill-whatsapp \
  --chat-id 491786127564-1611913127@g.us \
  --since 2026-05-10T00:00:00+02:00 \
  --until 2026-05-11T00:00:00+02:00 \
  --source jsonl \
  --input ~/.yeoman/workspace/backfill/finanzgruppe-2026-05-10.jsonl \
  --dry-run
```

The command must print a compact summary and exit non-zero if no source rows
were available, timestamps were invalid, the chat id was missing, or the
normalized rows could not be validated.

## 6. Normalized Message Model

Both sources should feed one gateway-side normalized model:

```python
@dataclass(frozen=True, slots=True)
class BackfillMessage:
    channel: str
    chat_id: str
    message_id: str
    sender_id: str | None
    participant: str | None
    sender_name: str | None
    text: str
    timestamp: int
    source: Literal["baileys", "jsonl"]
    source_metadata: Mapping[str, object]
```

Validation rules:

- `channel` is `whatsapp` for this feature.
- `chat_id`, `message_id`, and non-empty normalized `text` are required.
- `timestamp` must be inside the requested inclusive range.
- `message_id` from Baileys is preserved when available.
- JSONL input must provide stable message ids. If the source cannot provide
  ids, the importer may derive deterministic ids from
  `chat_id + timestamp + sender_id + text`, but those ids must be prefixed with
  `manual-backfill:` so they cannot be mistaken for WhatsApp ids.
- Media-only rows are skipped unless the row has an existing text caption or
  precomputed media description.

## 7. Baileys Source

Add a bridge command that attempts bounded history retrieval for one chat and
time range.

The bridge can use Baileys history capabilities, including on-demand history
fetch where supported by the current Baileys version. The command should return
normalized raw message payloads to the gateway, not import into memory itself.

Required properties:

- Owner-initiated only through the local gateway/bridge control path.
- Requires `chatId`, `since`, `until`, and `limit`.
- Does not change the socket's global `syncFullHistory` setting.
- Does not emit fetched messages as normal `messages.upsert` live traffic.
- Returns a structured response with `messages`, `source`, and `warnings`.
- If Baileys returns only partial history, the response must say so.

The gateway should treat Baileys as best-effort. Failure to retrieve the day is
not a failure of the importer; it means the operator should use JSONL fallback.

## 8. JSONL Source

Manual JSONL import exists because WhatsApp Web may display messages that
Baileys cannot fetch on demand from this linked-device session.

Accepted line format:

```json
{"chat_id":"491786127564-1611913127@g.us","message_id":"manual-1","sender_id":"491234567890","sender_name":"Alice","text":"Message text","timestamp":1778352000}
```

Rules:

- The file lives outside the repo, preferably under `~/.yeoman/workspace/backfill/`.
- The importer validates every line before writing anything.
- Invalid rows are reported with line numbers.
- The importer supports `--dry-run` to show exactly how many rows would be
  accepted, skipped, or rejected.
- The importer never commits or copies the JSONL file into source control.

## 9. Archive Import

Add an archive import path that writes backfilled rows directly into
`InboundArchive` without using the live inbound bus.

`InboundArchive.record_inbound()` currently sets `created_at` to import time.
For backfill, add a narrow method that preserves original message time for
range queries and diagnostics:

```python
def record_backfilled_inbound(
    self,
    *,
    channel: str,
    chat_id: str,
    message_id: str,
    participant: str | None,
    sender_id: str | None,
    text: str,
    timestamp: int,
    sender_name: str | None,
    backfill_source: str,
) -> Literal["inserted", "duplicate", "updated"]:
```

Implementation details:

- Keep the existing primary key unchanged.
- Preserve `timestamp` from the source.
- Set `created_at` to import time unless a schema-safe `observed_at` column is
  added. `lookup_messages_in_range()` already prefers `timestamp`, so original
  chronology remains intact.
- Do not overwrite existing text unless the existing row is empty or the new
  row adds a media description that the old row lacks.
- Record `backfill_source` only if a small metadata column or side-table is
  added. Do not expand the archive schema broadly for this one feature.

## 10. Memory Capture

Backfilled messages should use the existing background memory notes pipeline,
not the reactive responder capture path.

Flow:

```text
BackfillMessage[]
  -> archive import
  -> MemoryService.enqueue_background_note(...)
  -> MemoryService.flush_background_notes(...)
  -> existing extractor route memory.capture.extract
  -> existing memory store and dedupe
```

Backfill should group messages by chat and chronological order. It should call
`enqueue_background_note()` with:

- `channel="whatsapp"`
- `chat_id=<target group>`
- `sender_id=<row sender>`
- `message_id=<row message id>`
- `content=<row text>`
- `is_group=True`
- `mode` from resolved memory notes policy unless the CLI explicitly requests
  `--mode heuristic` or `--mode hybrid`
- short batch interval or explicit final flush so the command completes

The command should flush queued notes before exiting and report the memory
service counters after the flush.

## 11. Safety And Policy

Backfill is an owner/operator action. It should not be exposed as a normal chat
tool.

Safety requirements:

- CLI command only for V1.
- No group-visible messages.
- No consciousness observer events.
- No outbound events.
- No tool call surface for non-owner chat users.
- Run the same input security checks used by memory notes unless an explicit
  `--trusted-jsonl` flag exists later. V1 should not add that flag.
- Respect `memory.capture.channels`; if WhatsApp capture is disabled, the
  command should import archive rows but skip memory capture unless
  `--archive-only` is selected.
- Log source, chat id, range, counts, and errors without printing full message
  contents by default.

## 12. Error Handling

The command should distinguish these outcomes:

| Outcome | Behavior |
|---------|----------|
| No source rows | Exit non-zero; print that nothing was retrieved/imported. |
| Partial Baileys fetch | Import retrieved rows only if not dry run; warn that source was partial. |
| Invalid JSONL row | Dry run fails; normal run fails before writes. |
| Duplicate archive row | Skip and count as duplicate. |
| Memory extractor failure | Keep archive import; report memory failure and exit non-zero. |
| Security block | Skip memory capture for that row; keep archive row only if archive import already succeeded. |
| Gateway/bridge unavailable | Baileys source exits non-zero and suggests JSONL fallback. |

## 13. Observability

Add structured logs for:

- backfill start: source, chat id, since, until, limit, dry-run
- source result: fetched rows, partial flag, warnings
- validation result: accepted, rejected, skipped
- archive result: inserted, duplicate, updated
- memory result: queued, flushed, saved, dropped_low_confidence,
  dropped_safety, deduped
- completion: duration and final status

The CLI output should be shorter than the logs:

```text
WhatsApp memory backfill
source: baileys
chat: 491786127564-1611913127@g.us
range: 2026-05-10T00:00:00+02:00 -> 2026-05-11T00:00:00+02:00
source rows: 121
archive: inserted=0 duplicate=121 updated=0
memory: queued=121 flushed=11 saved=8 deduped=3 dropped_safety=0
status: complete
```

## 14. Tests

### Python Gateway Tests

Add focused tests for:

- JSONL normalization accepts valid rows and rejects invalid lines before
  writing.
- Time range parsing handles timezone-aware timestamps.
- Archive backfill import is idempotent.
- Backfill import does not publish outbound events.
- Backfill memory capture calls `enqueue_background_note()` in chronological
  order.
- Dry run performs source and validation work but writes no archive or memory
  rows.
- `archive-only` imports rows without memory capture.
- Security-blocked text is not sent to memory capture.

### Bridge Tests

Add TypeScript tests for:

- Bridge command validates `chatId`, `since`, `until`, and `limit`.
- Bridge command returns structured partial/failure responses.
- Fetched messages are normalized without passing through live inbound emit
  handling.
- Global `syncFullHistory` remains disabled.

### Manual Runtime Validation

After implementation:

1. Run dry run for the target missing day and chat.
2. Compare archive row count before and after.
3. Run import.
4. Query `reply_context.db` for the exact day and chat.
5. Run `yeoman memory status`.
6. Search memory for two known facts from the missing day.
7. Confirm gateway/bridge logs show no outbound sends caused by the backfill.

## 15. Performance And Cost

Backfill can invoke the memory extractor many times. Keep the default limits
conservative:

- Default `--limit`: 300.
- Hard max V1 limit: 1000.
- Existing memory chunking should remain in control.
- Do not embed every raw message individually if the existing batch extractor
  can produce compact notes.
- Print an estimated batch count in dry run so the operator sees likely cost
  before importing.

## 16. Security And Privacy

The importer handles private group transcripts. Therefore:

- Keep raw JSONL files out of the repo.
- Redact or omit message text in normal logs.
- Store only the same archive and memory data Yeoman would have stored if it
  had been online.
- Do not create extra durable raw-transcript stores.
- Do not expose backfill through chat tools.

## 17. Rollout Plan

V1 should ship in three slices:

1. Gateway JSONL importer and archive/memory backfill pipeline.
2. Bridge Baileys source command.
3. Operator polish: count reports, dry-run estimates, and runtime validation
   notes.

This order matters. The importer is the stable core. Baileys history fetch is a
source adapter and may need iteration depending on what WhatsApp exposes for a
linked device.

## 18. Open Decisions

These are deliberately fixed for V1:

- Scope: WhatsApp only.
- Surface: CLI only.
- Import size: bounded by explicit range and limit.
- Side effects: archive and memory only.
- Primary source: Baileys where available.
- Fallback source: operator-provided JSONL.
- Live reply/consciousness replay: never.

