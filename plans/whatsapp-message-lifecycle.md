# WhatsApp Message Lifecycle and Memory (Current Runtime)

This document describes what happens, step by step, for WhatsApp group and DM messages in the current nanobot runtime.

## 1. Runtime Components Involved

- Bridge (Node.js): `bridge/src/server.ts`, `bridge/src/whatsapp.ts`
- Python WhatsApp channel: `nanobot/channels/whatsapp.py`
- Message bus: `nanobot/bus/queue.py`
- Orchestrator/policy/security: `nanobot/core/orchestrator.py`, `nanobot/adapters/policy_engine.py`, `nanobot/policy/engine.py`, `nanobot/security/engine.py`
- Responder + prompt assembly: `nanobot/adapters/responder_llm.py`, `nanobot/agent/context.py`
- Session history: `nanobot/session/manager.py`
- Long-term memory: `nanobot/memory/service.py`, `nanobot/memory/store.py`, `nanobot/memory/extractor.py`
- Reply-context archive: `nanobot/storage/inbound_archive.py`

## 2. Inbound Flow (Message You Send in WhatsApp)

1. WhatsApp message arrives in the bridge (`messages.upsert` in `bridge/src/whatsapp.ts`).
2. Bridge ignores your own outgoing messages (`msg.key.fromMe`) and status broadcast messages.
3. Bridge computes dedupe key `(chatJid + messageId)` and drops duplicates (20 min TTL cache).
4. Bridge extracts:
   - `chatJid`
   - `participantJid` / `senderId`
   - `isGroup`
   - text (or placeholders like `[Image]`, `[Voice Message]`, etc.)
   - mention metadata (`mentionedBot`)
   - reply metadata (`replyToMessageId`, `replyToText`, `replyToParticipantJid`)
5. Bridge emits protocol v2 `type=message` over websocket to Python.
6. Python `WhatsAppChannel` parses the frame, validates protocol version, and converts payload to internal `InboundEvent`.
7. Python channel dedupes again (`chat_jid:message_id`, TTL cache) as a second safety layer.
8. Optional media enrichment runs:
   - image description can be added to text
   - voice is currently converted to a placeholder text for assistant input
9. Message is archived to reply-context DB (`~/.nanobot/inbound/reply_context.db`).
10. Debounce logic may merge rapid messages from same sender/chat into one event.
11. Final event is published to message bus as `InboundMessage`.

## 3. Group vs DM Behavior

`is_group` is inferred from JID suffix (`@g.us`).

Policy defaults for WhatsApp (`nanobot/policy/schema.py`):
- `whenToReply=mention_only`

Effective behavior:
- DM: bot responds by default in mention-only mode (`mention_only_dm` path in policy engine).
- Group: bot responds only if one of these is true:
  - bot was mentioned (`mentioned_bot=true`), or
  - user replied to a bot message (`reply_to_bot=true`).

So group silence is usually policy, not memory.

## 4. Orchestrator Pipeline (After Bus Inbound)

1. Orchestrator normalizes content and drops empty text.
2. Orchestrator dedupes by `(channel, chat_id, message_id)` with TTL.
3. Orchestrator records inbound into reply archive (again via adapter) and tries reply-context lookup:
   - if replying to older message, it can fetch quoted text from archive
   - can build a short “topic window” of prior lines
4. Admin command interception can short-circuit normal flow:
   - `/policy ...` is owner-only and DM-only
   - `/reset` is owner-only on WhatsApp
5. Policy decision:
   - `accept_message` (who can talk / blocked senders)
   - `should_respond` (when to reply)
   - allowed tools
   - persona file text (if configured)
6. Optional security input check can block before model call.
7. If responding, typing indicator is started for WhatsApp.
8. Responder generates reply.
9. Optional security output check can sanitize/block text.
10. Outbound event is emitted to bus.

## 5. Prompt Construction (Why Style/Persona Behaves as It Does)

For each turn, responder builds prompt with:
1. System prompt from `ContextBuilder`:
   - core identity
   - style persistence guardrail (prevents persistent user-injected catchphrases)
   - channel persona override text (if policy provides `personaFile`)
   - bootstrap files from workspace (`AGENTS.md`, `SOUL.md`, `USER.md`, `TOOLS.md`, `IDENTITY.md`)
2. Last session history messages (up to 50 messages) from `~/.nanobot/sessions/*.jsonl`.
3. Retrieved long-term memory block (if hits found).
4. Current user message + reply context block.

Important: personas are not “remembered” by chat history. They are reloaded each turn from policy/persona files.

## 6. Outbound Flow (Bot Reply Sent to WhatsApp)

1. Orchestrator publishes outbound message to bus.
2. `ChannelManager` consumes outbound and routes to WhatsApp channel.
3. WhatsApp channel stops typing indicator.
4. Reply markdown is converted to WhatsApp formatting (`_markdown_to_whatsapp`).
5. Bridge command `send_text` is sent over websocket with protocol token.
6. Node bridge validates token/schema and calls Baileys `sendMessage`.
7. Message appears in WhatsApp chat/group.

## 7. Memory Concept (What Gets Persisted, Where, and Why)

### 7.1 Short-Term Conversation Memory (Session History)

- Location: `~/.nanobot/sessions/<channel>_<chat>.jsonl`
- Written every successful turn (user + assistant)
- Used directly as recent chat history in prompt
- This is the biggest source of short-term style drift/catchphrase persistence

### 7.2 Reply Context Archive

- Location: `~/.nanobot/inbound/reply_context.db`
- Stores inbound message text keyed by `(channel, chat_id, message_id)`
- Used to resolve quoted/reply context
- Retention purge default: 30 days

### 7.3 Long-Term Semantic Memory (Active `memory`)

- Location default: `~/.nanobot/memory/memory.db`
- Backend tables are named `memory2_*` for schema continuity, but this is the active memory system
- Capture is asynchronous via queue/thread
- Capture sources:
  - user message (always eligible)
  - assistant reply only if `memory.capture.capture_assistant=true` (default false)

Capture filters/triggers:
- channel must be in `memory.capture.channels`
- extraction mode (`heuristic`, `llm`, `hybrid`)
- candidate must pass:
  - `confidence >= memory.capture.min_confidence`
  - `salience >= memory.capture.min_salience`
- anti-injection filter blocks phrases like `ignore previous instructions`, `system prompt`, etc.
- if `memory.acl.owner_only_preference=true` (default), non-owner semantic/procedural writes are dropped

Scope model:
- `episodic` / `emotional` -> chat scope
- `semantic` / `procedural` -> user scope
- `reflective` -> global scope

Retrieval:
- query = current message (+ quoted reply text if present)
- lexical FTS search + vector search (if embeddings enabled)
- merged and ranked by composite score:
  - lexical
  - vector
  - salience
  - recency decay
- top hits are rendered into `[Retrieved Memory]` system block

### 7.4 Session WAL (Turn Durability / Debug)

- Location default (workspace-relative): `memory/session-state/*.md`
- PRE entry written before generation
- POST entry written after generation
- Contains compact turn snapshots for diagnostics/recovery

## 8. Why a Bad Greeting Can Keep Reappearing

Usually one (or more) of these:
1. It is still in recent session history (`~/.nanobot/sessions/...jsonl`).
2. It was captured as long-term memory and recalled.
3. Persona text or policy still enforces/encourages it.
4. Group conversation keeps re-seeding it in fresh turns.

## 9. Practical Reset Points

- Clear one chat short-term history: owner command `/reset` (also clears session WAL file for that session).
- Inspect/search long-term memory:
  - `nanobot memory status`
  - `nanobot memory search --query "..." --scope all`
- Add corrective long-term fact manually:
  - `nanobot memory add --kind preference --scope chat --channel whatsapp --chat-id <chat> --text "Do not use phrase X"`
- Policy/persona-level control:
  - set/clear persona per group via `/policy ...` owner commands
  - edit policy defaults in `~/.nanobot/policy.json`

## 10. Files Under `~/.nanobot` Most Relevant to WhatsApp Message Handling

- `~/.nanobot/config.json` - runtime + model + memory config
- `~/.nanobot/policy.json` - access/reply/tool/persona policy
- `~/.nanobot/sessions/*.jsonl` - short-term chat history
- `~/.nanobot/inbound/reply_context.db` - quoted-message lookup archive
- `~/.nanobot/memory/memory.db` - long-term semantic memory DB
- `~/.nanobot/whatsapp-auth/` - WhatsApp bridge auth/session credentials
- `~/.nanobot/logs/whatsapp-bridge.log` - bridge log
- `~/.nanobot/run/whatsapp-bridge.pid` - bridge PID
