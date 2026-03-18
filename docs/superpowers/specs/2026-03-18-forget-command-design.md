# /forget Command Design

**Date:** 2026-03-18
**Status:** Approved

## Problem

The bot's semantic memory captures one-time requests (e.g. "antworte als Voice Message") as
standing preferences. When recalled into the LLM prompt, these stale memories steer behavior
indefinitely. There is no way for the owner to remove specific memories — `/reset` only clears
conversation history (session messages), not the memory store.

## Goals

1. Let the owner search memories by free-text query and preview matches.
2. Let the owner soft-delete all or selected matches after previewing.
3. Keep the flow lightweight — the adapter holds a single transient preview slot
   (list of matched IDs) validated by a hash token. No persistent state, no timeout
   logic. If the slot is stale or empty, the user simply re-runs `/forget`.

## Non-Goals

- CLI equivalent (`yeoman memory forget`). No other WhatsApp admin command has a CLI twin.
- Contradiction detection (auto-invalidating old memories when newer ones conflict).
- Hard-delete or data export.

## UX Flow

```
User:  /forget voice bei Vinz
Bot:   Found 3 memories:
       1. (Finanzgruppe, 2026-02-14) "eigentlich soll er mit voice antworten"
       2. (Finanzgruppe, 2026-03-11) "Als Sprachnachricht bitte"
       3. (DM, 2026-03-17) "antworte kurz als Voice Message"

       /forget confirm a7f3 — delete all
       /forget confirm a7f3 1,3 — delete selected

User:  /forget confirm a7f3
Bot:   Forgot 3 memories.
```

## Architecture

### Hash-based confirmation with transient preview slot

The preview computes a 4-character confirmation token: first 4 hex chars of
`SHA256(sorted entry IDs joined by newline)`. The adapter stores the matched IDs in a
single transient slot (`_forget_preview_ids`). On confirm, the hash is recomputed from
the stored IDs and validated against the user-provided token. If the slot is empty or
the hash mismatches, the user is told to re-run `/forget`.

This is not fully stateless — the adapter holds one preview result in memory. This is
acceptable because only one owner uses the bot, preview → confirm happens within
seconds, and there is no persistence or timeout logic to manage.

### Components

**1. `MemoryStore.soft_delete(ids: list[str]) -> int`**

Sets `is_deleted = 1, updated_at = now()` on rows matching the given IDs.
Returns count of rows affected. Single `UPDATE ... WHERE id IN (...)` statement.

**2. `MemoryService.forget(query: str) -> list[MemoryHit]`**

Runs the existing search pipeline (lexical + vector, merged and ranked) across all
scope keys in the owner's workspace. Returns up to 10 hits for preview. Does not delete.

**3. `MemoryService.forget_confirm(ids: list[str]) -> int`**

Delegates to `store.soft_delete(ids)`. Returns deletion count.

**4. `ForgetCommandHandler` (WhatsApp admin command)**

- `namespace()` → `"forget"`
- `is_applicable()` → owner-only, DM-only (preview may contain sensitive cross-chat data)
- `handle(ctx, argv)` routes based on argv:

| argv pattern | Action |
|---|---|
| `[]` (empty) | Return usage hint |
| `["confirm", hash, ...]` | Validate hash, resolve IDs, soft-delete |
| `[...query tokens...]` | Search, format preview, compute hash |

**5. Adapter methods on `EnginePolicyAdapter`**

- `forget_is_applicable(ctx) -> bool` — owner check
- `forget_handle(ctx, argv) -> AdminCommandResult` — preview or confirm logic

The adapter holds a reference to `MemoryService` (already injected via bootstrap).

### Search scope

Search is global across the owner's workspace — no chat_id or sender_id filter.
The query terms naturally narrow results via lexical + semantic matching.

### Chat name resolution in preview

The preview shows human-readable chat names instead of raw chat IDs. The adapter
already has `_group_display_name()` for this. DM entries display as "DM".

### Registration

Add `ForgetCommandHandler(self)` to the handler list in
`EnginePolicyAdapter.__init__()`, alongside the existing 13 handlers.

## Edge Cases

| Condition | Response |
|---|---|
| No query (`/forget`) | "Usage: /forget \<query\>" |
| No results | "No memories found matching '\<query\>'." |
| Invalid/expired hash | "Preview expired or invalid. Run /forget again." |
| Index out of range | "Index N out of range (1-M). Run /forget again." |
| More than 10 matches | Top 10 shown (search pipeline caps results); user refines query if needed |

## Files Changed

| File | Change |
|---|---|
| `yeoman/memory/store.py` | Add `soft_delete(ids) -> int` |
| `yeoman/memory/store.py` | Add `distinct_scope_keys(workspace_id) -> list[str]` |
| `yeoman/memory/service.py` | Add `forget(query) -> list[MemoryHit]`, `forget_confirm(ids) -> int` |
| `yeoman/adapters/policy_engine.py` | Add `ForgetCommandHandler`, `forget_is_applicable()`, `forget_handle()`, register handler |
