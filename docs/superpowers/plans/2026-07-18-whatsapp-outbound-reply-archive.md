# WhatsApp Outbound Reply Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist successful WhatsApp outbound messages as direction-tagged reply anchors so delayed explicit replies resolve to their original thread without changing inbound-only history, activity, persona, or long-memory consumers.

**Architecture:** Extend the existing SQLite reply archive with a backward-compatible `direction` column and an explicit outbound write method. Exact reply lookups and context windows may see both directions, while range readers remain inbound-only by default. The bridge returns the real sent message ID and timestamp; the WhatsApp channel archives successful sends and removes the current synthetic quote seeding fallback.

**Tech Stack:** Python 3.14, SQLite, pytest/pytest-asyncio, TypeScript, Node test runner, Baileys WhatsApp bridge.

## Global Constraints

- Use `data/inbound/reply_context.db`; do not create another database or table.
- Inbound and outbound rows share the existing 30-day retention.
- Existing rows migrate to `direction=inbound`.
- Long-memory capture, session JSONL, media retention, contacts, and non-WhatsApp channels remain unchanged.
- Range-based archive consumers remain inbound-only unless they explicitly request outbound rows.
- Follow TDD: run each new regression test red before production changes and green afterward.

---

### Task 1: Direction-aware reply archive

**Files:**
- Modify: `packages/gateway/yeoman_gateway/storage/inbound_archive.py`
- Test: `tests/test_whatsapp_channel_v2.py`
- Test: `tests/gateway/test_summarize_history_tool.py`

**Interfaces:**
- Produces: `InboundArchive.record_outbound(..., sender_name: str = "Yeoman") -> None`
- Produces: `InboundArchive.lookup_messages_in_range(..., include_outbound: bool = False) -> list[dict[str, Any]]`
- Preserves: exact `lookup_message*` and `lookup_messages_before` return inbound and outbound anchors.

- [ ] **Step 1: Write failing migration and isolation tests**

Add tests that create an old schema without `direction`, reopen it through
`InboundArchive`, and assert the row reads as `direction == "inbound"`. Add
inbound and outbound rows to one chat and assert:

```python
assert [row["text"] for row in archive.lookup_messages_in_range(
    "whatsapp", "group@g.us", since, until
)] == ["human"]
assert [row["text"] for row in archive.lookup_messages_in_range(
    "whatsapp", "group@g.us", since, until, include_outbound=True
)] == ["human", "yeoman"]
assert archive.lookup_message("whatsapp", "group@g.us", "out-1")["direction"] == "outbound"
```

Extend `test_returns_formatted_messages` or add a focused
`test_summarize_history_ignores_outbound_archive_rows` proving the tool returns
the human line but not the Yeoman line.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run python -m pytest \
  tests/test_whatsapp_channel_v2.py \
  tests/gateway/test_summarize_history_tool.py -k 'direction or outbound' -q
```

Expected: failures because the schema has no `direction`, `record_outbound`
does not exist, and `include_outbound` is unsupported.

- [ ] **Step 3: Implement the schema migration and filtered range API**

Add `direction TEXT NOT NULL DEFAULT 'inbound'` to schema creation and a guarded
`ALTER TABLE`. Refactor the write path through one internal method so
`record_inbound` writes `inbound` and `record_outbound` writes `outbound`.
Select `direction` in every archive query. Add an inbound-only predicate to
`lookup_messages_in_range` unless `include_outbound=True`; do not filter exact
lookup or `lookup_messages_before`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

---

### Task 2: Real outbound receipt metadata from the bridge

**Files:**
- Modify: `packages/bridge/src/whatsapp.ts`
- Test: `packages/bridge/src/whatsapp.test.ts`

**Interfaces:**
- Produces: exported `outboundReceipt(sent: unknown, fallbackTimestampMs: number) -> { messageId: string; timestamp: number }`
- Changes successful `sendText` and `sendMedia` results to include `messageId` and epoch-second `timestamp`.

- [ ] **Step 1: Write a failing receipt-normalization test**

Add a Node test using a Baileys-like result:

```ts
const receipt = outboundReceipt(
  { key: { id: '3EB0REAL' }, messageTimestamp: 1784357445 },
  1784357999000,
);
assert.deepEqual(receipt, { messageId: '3EB0REAL', timestamp: 1784357445 });
```

Add cases for a Long-like timestamp and for timestamp fallback. A missing
message ID must return an empty ID so the gateway can warn without fabrication.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
cd packages/bridge && npm test -- --test-name-pattern='outboundReceipt'
```

Expected: TypeScript build/test failure because `outboundReceipt` is absent.

- [ ] **Step 3: Implement receipt normalization and return it from sends**

Normalize `sent.key.id`, coerce `sent.messageTimestamp` to finite epoch seconds,
and fall back to `Math.floor(fallbackTimestampMs / 1000)`. Merge the receipt
into the existing `sendText` and every `sendMedia` success result after
`rememberOutboundSelfMessage`.

- [ ] **Step 4: Build and test the bridge**

Run:

```bash
cd packages/bridge && npm run build && npm test
```

Expected: build succeeds and all bridge tests pass.

---

### Task 3: Archive successful outbound sends

**Files:**
- Modify: `packages/gateway/yeoman_gateway/channels/whatsapp.py`
- Test: `tests/test_whatsapp_channel_v2.py`

**Interfaces:**
- Consumes bridge result shape: `{"sent": {"messageId": str, "timestamp": int, ...}}`
- Consumes: `InboundArchive.record_outbound(...)`
- Produces: `_archive_outbound_receipt(chat_id, content, result) -> None`

- [ ] **Step 1: Write failing text-send archive tests**

Create a connected `WhatsAppChannel` with a temporary archive, replace
`_send_command_with_retry` with an async stub returning:

```python
{"sent": {"messageId": "3EB0OUT", "timestamp": 1784357445}}
```

Send an `OutboundMessage`, then assert the exact row exists with text,
timestamp, sender name `Yeoman`, and `direction == "outbound"`. Add cases proving
a result without `messageId` creates no row and a raised send error creates no
row.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run python -m pytest tests/test_whatsapp_channel_v2.py -k 'outbound and archive' -q
```

Expected: failures because successful sends are not archived.

- [ ] **Step 3: Implement best-effort outbound archiving**

Capture each successful `send_text`/`send_media` command result. Unwrap
`result["sent"]`, validate `messageId`, use its timestamp or send-completion
time, and call `record_outbound`. Log and continue on missing IDs or archive
write failures; do not turn a delivered message into a visible send failure.
For media, archive the visible caption, or a stable media placeholder when no
caption exists.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

---

### Task 4: Remove synthetic anchors and enforce quote precedence

**Files:**
- Modify: `packages/gateway/yeoman_gateway/channels/whatsapp.py`
- Modify: `packages/gateway/yeoman_gateway/pipeline/archive.py`
- Modify: `packages/gateway/yeoman_gateway/agent/context.py`
- Test: `tests/test_whatsapp_channel_v2.py`
- Test: `tests/test_pipeline_middleware.py`
- Test: `tests/gateway/test_context_windowing.py`

**Interfaces:**
- Preserves payload quote text on archive miss.
- Removes writes that copy a quoted target with the new reply's timestamp.
- Makes `quoted_message` authoritative over `recent_messages`.

- [ ] **Step 1: Write failing delayed-reply regression tests**

Add tests proving:

1. ingesting a reply with an unseen `replyToMessageId` does not create a row for
   that target ID;
2. `ArchiveMiddleware` records only the current inbound event;
3. a stored outbound anchor from hours earlier builds its topic window around
   the old timestamp;
4. `_with_reply_context` emits explicit guidance that the quoted message is the
   authoritative referent and recent messages cannot redirect it.

- [ ] **Step 2: Run regression tests and verify RED**

Run:

```bash
uv run python -m pytest \
  tests/test_whatsapp_channel_v2.py \
  tests/test_pipeline_middleware.py \
  tests/gateway/test_context_windowing.py \
  -k 'quoted or delayed or authoritative or synthetic' -q
```

Expected: failures showing synthetic target insertion and missing precedence
guidance.

- [ ] **Step 3: Remove synthetic seeding and add prompt precedence**

Delete quoted-target archive writes from both WhatsApp ingestion and
`ArchiveMiddleware`. Keep `ReplyContextMiddleware`'s payload quote-only miss
path. Update `[Reply Context]` guidance to state that `quoted_message` is the
authoritative referent, topic-window lines describe the original thread, and
recent messages are secondary.

- [ ] **Step 4: Run focused regressions and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

---

### Task 5: Full verification and live deployment

**Files:**
- Verify all modified files above.

**Interfaces:**
- No new interfaces.

- [ ] **Step 1: Run gateway tests and lint**

```bash
uv run python -m pytest tests/gateway/ tests/test_whatsapp_channel_v2.py tests/test_pipeline_middleware.py -q
uv run ruff check packages/gateway/yeoman_gateway tests/gateway tests/test_whatsapp_channel_v2.py tests/test_pipeline_middleware.py
```

Expected: zero failures and zero lint errors.

- [ ] **Step 2: Run bridge build and tests**

```bash
cd packages/bridge && npm run build && npm test
```

Expected: build succeeds and all tests pass.

- [ ] **Step 3: Verify the migration against a temporary copy**

Copy `data/inbound/reply_context.db` to a temporary path, open it with the new
`InboundArchive`, and verify existing rows report `direction=inbound` while a
new outbound row is excluded from default range reads and available by exact
ID.

- [ ] **Step 4: Deploy bridge and gateway**

Run from the repository root:

```bash
yeoman deploy
```

Then restart the affected gateway and bridge services through the supported
deployment/service path.

- [ ] **Step 5: Verify live processes and schema**

Check gateway and bridge service PIDs/start times, inspect post-restart logs,
and query:

```sql
PRAGMA table_info(inbound_messages);
SELECT direction, COUNT(*) FROM inbound_messages GROUP BY direction;
```

Expected: `direction` exists, historical rows are inbound, both services are
healthy, and no long-memory/session files were modified by migration.

- [ ] **Step 6: Commit implementation**

Stage only implementation and test files, review `git diff --cached`, and commit
with:

```bash
git commit -m "fix: preserve delayed WhatsApp reply context"
```
