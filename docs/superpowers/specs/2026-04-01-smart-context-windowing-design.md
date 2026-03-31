# Smart Context Windowing

**Date**: 2026-04-01
**Status**: Draft
**Problem**: DM conversations send 50 session history turns + 8 redundant ambient messages to the LLM on every message. Groups send 20 + 8. This wastes tokens and dilutes signal for short interactions.

---

## Changes

### A. Skip ambient window in DMs

In `ReplyContextMiddleware._build_ambient_window()`, return `[]` when the chat is a 1:1 DM (no `@g.us` suffix). The ambient window exists for group chats where other participants' messages aren't in the session history — in DMs it duplicates session history.

**Files**: `yeoman_gateway/pipeline/reply_context.py`

### B. Lower default session history limits

Current hardcoded values in `responder_llm.py:1113`:
- DMs: 50 → **15**
- Groups: 20 → **20** (unchanged)

These become configurable (see D).

### C. `/new` session boundary command

A new admin command `/new` that inserts a boundary marker into the session. `Session.get_history()` scans backwards and stops at the most recent boundary marker (or at `max_messages`, whichever comes first).

**Marker format** in session JSONL:
```json
{"role": "session_boundary", "timestamp": "2026-04-01T12:00:00"}
```

**Behavior**:
- `/new` inserts the marker, responds with a short confirmation
- `get_history()` excludes messages before the most recent `session_boundary`
- Works in both DMs and groups (owner access required)
- `/reset` remains as the nuclear option (deletes entire session file)
- Memory, ambient window, and archive are unaffected — they operate independently

**Files**:
- `yeoman_gateway/session/manager.py` — modify `get_history()` to respect boundary
- `yeoman_gateway/adapters/policy_engine.py` — add `NewSessionCommandHandler`

### D. Configurable limits (global + per-chat override)

**Global defaults** in `config.json` under `whatsapp`:
```json
{
  "whatsapp": {
    "session_history_limit": 15,
    "session_history_limit_group": 20,
    "ambient_window_limit": 8
  }
}
```

`ambient_window_limit` already exists in the schema. Add `session_history_limit` and `session_history_limit_group`.

**Per-chat override** in `policy.json` chat entries:
```json
{
  "chat_id": "123@g.us",
  "context": {
    "session_history_limit": 30
  }
}
```

**Resolution order**: per-chat policy → global config → hardcoded default.

**Files**:
- `yeoman_shared/config/schema.py` — add fields to `WhatsAppConfig`
- `yeoman_shared/config/defaults.py` — add defaults
- `yeoman_gateway/adapters/responder_llm.py` — read config/policy instead of hardcoded values
- `yeoman_gateway/policy/schema.py` — add optional `context` block to chat policy

### E. Adaptive context: preflight heuristic + recall tool

Two-layer system so the bot can look deeper when needed without always paying the token cost.

#### E1. Preflight heuristic

Before building the LLM prompt, scan the incoming message for backward-reference signals:
- Explicit references: "earlier", "before", "we discussed", "as I said", "you said", "you mentioned", "remember when", "go back to", "what about the"
- Pronominal references without visible antecedent in the current window

If triggered, expand the session history window to `min(session_history_limit * 3, 50)` for that single request (not persisted).

**File**: `yeoman_gateway/pipeline/reply_context.py` or a new small helper called from the responder.

#### E2. `recall_conversation` tool

A new agent tool available to the LLM:

```
recall_conversation(query: str, max_messages: int = 30) -> str
```

Searches the session archive (full session JSONL, ignoring `/new` boundaries) using substring matching on message content. Returns matching messages formatted as `[timestamp] [role]: content`.

This is the fallback for when the heuristic misses — the bot can explicitly ask for more history. The system prompt includes a brief instruction: "If the user references something outside your visible conversation history, use `recall_conversation` to look it up."

**Files**:
- `yeoman_gateway/agent/tools/` — new `recall_conversation.py` tool
- `yeoman_gateway/agent/tools/registry.py` — register it
- System prompt addition in `yeoman_gateway/agent/context.py`

---

## What does NOT change

- **Ambient window in groups**: Stays as-is (valuable for capturing non-bot messages)
- **Memory system**: Unaffected — semantic/FTS recall still runs on every message
- **Reply context window**: Unaffected — still resolves quoted messages normally
- **`/reset` command**: Stays as the nuclear option
- **Archive**: Unaffected — all messages still archived for ambient/reply lookup

## Token impact estimate

For a typical DM one-shot:
- **Before**: ~50 history turns + 8 ambient lines + memory
- **After `/new`**: 0 history turns + 0 ambient + memory
- **After cold start (no `/new`)**: ~15 history turns + 0 ambient + memory

For groups:
- **Before**: ~20 history turns + 8 ambient lines + memory
- **After**: ~20 history turns + 8 ambient lines + memory (unchanged default, but configurable down)
