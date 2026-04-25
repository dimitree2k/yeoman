# Consciousness Finanzgruppe Window Fix

Date: 2026-04-25

Short description: Fixed the manual Phase 2 trial reading stale group history and producing misleading silence reasons.

## Findings

- `Finanzgruppe` is `491786127564-1611913127@g.us` and is opted in with `spontaneity.enabled = true`, `profile = balanced`, and `preview = owner_dm` in `~/.yeoman/policy.json`.
- The archive had current messages for the group, but `ConsciousnessTools.read_chat_window()` received the oldest limited rows in the 7-day range.
- The planner therefore saw messages from 2026-04-19 even though the archive contained 2026-04-25 messages.
- After fixing the window, the planner saw current messages but falsely claimed daily cap was reached because the prompt did not expose `sent_today`.
- A stale approval created before owner JID normalization had `owner_chat_id = "+491757070305@s.whatsapp.net"`, so approvals could fail to match real WhatsApp owner-DM events.

## Implemented

- Added `latest=True` support to `InboundArchive.lookup_messages_in_range()` so callers can select the newest limited slice while receiving it oldest-first.
- Updated consciousness chat windows to request `latest=True`.
- Added daily cap state to the consciousness prompt: `sent_today` and `daily_remaining`.
- Clarified prompt semantics: `denied` is owner feedback; `rejected` and `expired` are system outcomes.
- Normalized WhatsApp owner phone numbers by stripping a leading `+` before constructing `@s.whatsapp.net` DMs.

## Verification

- `uv run python -m pytest tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py tests/gateway/test_inbound_archive_range.py tests/gateway/test_event_bus.py tests/gateway/test_workflow_state.py`
- `uv run ruff check packages/gateway/yeoman_gateway/consciousness/agent.py packages/gateway/yeoman_gateway/consciousness/tools.py packages/gateway/yeoman_gateway/storage/inbound_archive.py tests/gateway/test_consciousness_phase1.py tests/gateway/test_consciousness_phase2.py`

## Live Trial

- A manual group-focused tick after the fixes saw current `Finanzgruppe` messages and no longer claimed the daily cap was reached.
- The model still chose `silent_pass` under the `balanced` profile: `No new messages; prior summary proposal rejected (system outcome), hair joke too fleeting for balanced profile intervention.`
- Pending approval queue is empty after clearing the stale approval created before JID normalization.
- A forced owner-DM preview for `Finanzgruppe` was queued with proposal id `71104570c2494ce082df2c76f3da5b42`.
- The owner approval reply arrived in LID chat `34596062240904@lid` with participant `491757070305@s.whatsapp.net`, while the pending approval had owner chat `491757070305@s.whatsapp.net`.
- Approval matching now tries owner chat identity candidates from the inbound event, so LID chats can approve phone-JID previews.
- The existing approval was replayed through the fixed middleware and delivered to `Finanzgruppe`; the speakup row is now `sent`.

## Taste Note

- The `ist das Pelonis haar?` message referred to the top of a head visible in the Intel-trade screenshot.
- The joke was an indirect Nancy Pelosi trading joke: Pelosi is culturally associated with getting rich from market trades, so the hairline in the screenshot was interpreted as possibly hers.
- The bot did not appear to recognize the visual cue. That is acceptable, but future taste logic should allow light humor when the current chat context makes the joke legible and the profile permits it.
