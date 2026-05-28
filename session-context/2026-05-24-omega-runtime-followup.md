# Omega Runtime Follow-Up

Date: 2026-05-24
Context: Omega persona was introduced as a new WhatsApp group posture for a high-provocation private group. The first step is persona-only so other chats and personas remain unaffected.

## Findings

- The first Omega rollout should use `personas/omega.md` in a chat-specific WhatsApp policy entry once the target group id is known.
- Keep the first live change isolated: do not edit alpha-2, global defaults, or shared responder behavior unless the persona-only rollout fails in live use.
- The responder social-holdback and talkative cooldown behavior can be made chat-parametric later instead of hard-coded globally.
- The later runtime knobs should stay chat-scoped: provocation posture, one-line social exit, stricter cooldown, mention-only reply mode, limited tools, and owner-DM preview for proactive consciousness.
- Omega intentionally allows rare ICD-10-style roast language in obvious friend-group banter while keeping sincere clinical diagnosis and psychiatric humiliation of vulnerable people off-limits.
- Desired spam posture is not just rate limiting: Omega should treat simultaneous bot-addressed messages as one short pipeline/thread and answer the whole batch once, instead of responding independently to every mention.

## Follow-up

- Add the target WhatsApp group to `/home/dm/.yeoman/policy.json` with `personaFile: personas/omega.md`.
- Start with `whenToReply.mode = mention_only`, `whoCanTalk.mode = everyone`, conservative tool allowlist, and voice output limits inherited or capped at 3 sentences / 500 chars.
- Consider `talkativeCooldown` around `streakThreshold = 3` (current schema minimum), `cooldownSeconds = 900-1200`, and `useLlmMessage = false` for cleaner exits.
- Keep `spontaneity` disabled or `preview = owner_dm` until the group style is observed.
- If Omega still gets baited into loops, implement chat-scoped responder parameters in `packages/gateway/yeoman_gateway/adapters/responder_llm.py` rather than changing global social-holdback behavior.
- If runtime prompt shaping is added, preserve the distinction between "ICD-code banter as a joke" and "no sincere diagnosis from chat fragments."
- Runtime direction: keep a Finanzgruppe-like policy surface, but add a chat-scoped reactive coalescing guard. When several messages in the same chat address Yeoman within a short window, delay briefly, build one batch context from the addressed messages plus ambient window, generate one reply to the combined situation, and mark/drop the rest as coalesced.
