# Participation knowledge selection

Participation knowledge is optional, same-chat source material for the existing Judge and draft-only Writer. It provides context for a decision; it does not create a trigger, evidence ID, permission, or action. The `processing.participation.knowledgeEnabled` configuration field defaults to `false` and gates selection and knowledge-specific revalidation. Existing advisory taste remains separate and may still be rendered when knowledge is disabled.

## Selection and boundaries

When enabled, the selector uses the verified authors of the current WhatsApp group trigger and the group’s current recipient membership to read protected Knowledge statements and shared facts. Reader contexts must agree on the same chat and member set. Group-wide selection excludes author-only statements and facts not shared with every current member. Selection is lexical and bounded to 600 query characters, at most three whole entries, and 1,200 rendered characters. It does not call an embedding or external research provider.

Only promoted, active, source-backed records in those protected stores are candidates. The raw archive is not searched directly; unpromoted observations, episode search, and automatic consolidation are outside this feature. Lexical retrieval can miss a relevant fact. This is limited to WhatsApp group chats (`@g.us`); direct chats and other channels stay on recent conversation context. Multi-author triggers stay recent-context-only because the reader contract does not combine multiple authors’ authority.

The selected block is rendered as untrusted prior context to the actual Judge and Writer requests. The current trigger remains the target and sole current evidence. Whole optional entries may be removed at either input budget; if the required target and selected block cannot fit, the selection is dropped or the decision fails closed. Source authority is revalidated before decision use, before drafting, and at the effect boundary. If the selected source changes or is revoked during drafting, the affected draft is discarded and the existing bounded reconsideration path is used. Protected reads and effect submission use separate stores, so revocation is not atomic with the final effect commit.

Knowledge-backed initiation that requires a delayed owner-approval queue is refused with `approval_knowledge_revalidation_unavailable`: the queue does not retain the protected authority needed to revalidate later. Draft generation remains tool-free and draft-only. Selection adds protected-store reads and revalidation work, plus selected text in the existing Judge and Writer inputs; it adds no separate selection model call.

## Activation checks

The knowledge switch is global, while Participation opt-in is per chat. Enabling the switch does not itself opt in a chat. A single-group pilot must leave only the approved WhatsApp group effectively opted in for Participation. Do not remove other entries from `processing.chats`; that list also controls direct processing. There is no per-chat knowledge-only switch.

After code review and explicit owner authorization:

1. Confirm the reviewed source checkout and branch head, then deploy through the supported `yeoman deploy` path. On systemd hosts, verify the gateway, bridge, and overseer units and `yeoman status` after deployment.
2. Use the native read-only policy commands `/policy list-groups` and `/policy status-group <chat_id@g.us>` to reconcile the pilot and other group opt-ins. The JSON opt-in lives at `channels.whatsapp.chats[chat_id].participation.enabled`; inherited default opt-ins must also be considered. Change only reviewed opt-in fields and preserve unrelated policy.
3. Back up the private runtime JSON files and record their current hashes. Set only `processing.participation.knowledgeEnabled=true` after confirming other group opt-ins are off. Preserve `processing.chats`. Restart the gateway through its service manager, then check effective policy and service health again.
4. For the pilot, use an existing non-sensitive fact and a naturally occurring matching topic. Record source selection, Judge and Writer inclusion, target, revalidation, decision, effect attempt, and recipient receipt separately. A selected record, test, rendered prompt, stub effect call, or transport acceptance is not a recipient receipt.
5. To roll back, set `knowledgeEnabled=false` with a fresh hash precondition, restore only the pre-pilot group opt-in fields after checking for intervening edits, restart the gateway, and verify service/policy status. Disabling knowledge does not disable advisory taste or its optional maintenance path.

The deployment and checks above are operational steps, not proof of live behavior until each is performed and its resulting service and message evidence is recorded. Do not enable the switch solely because source tests pass.
