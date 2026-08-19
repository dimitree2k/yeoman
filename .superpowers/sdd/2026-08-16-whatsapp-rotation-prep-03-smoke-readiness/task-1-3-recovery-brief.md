# Prep-03 Tasks 1-3 recovery implementation brief

Read the exact task briefs first:

- `/home/dm/Documents/yeoman/.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-brief.md`
- `/home/dm/Documents/yeoman/.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-2-brief.md`
- `/home/dm/Documents/yeoman/.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-3-brief.md`

Worktree: `/home/dm/Documents/yeoman-migration-toolkit`, branch `c/yeoman-migration-toolkit`.
Accepted base: `f17ebe05598bf28c5f8a0f32dbc3b1d9dbb957a1`.
Preserved WIP start: `fc86076352e8aaa417b7315688c02872bddf4826`.

This is one recovery batch because the owner-approved pause checkpoint already intertwined Tasks 1-3 in the shared smoke/state files. Preserve that commit. Complete every requirement from all three briefs, then commit normal atomic follow-up commits; do not rewrite or squash the WIP checkpoint.

## Binding boundaries

- Synthetic-only. Do not run or modify systemd, services, QR, Bridge/Gateway processes, sockets/ports, live auth, program keys/evidence, messages, archives, memory, or network state. Use disposable fakes and temp directories only.
- Production additions remain limited to the four incident scripts named by the orchestration plan; tests remain under `tests/shared/`. Do not add dependencies.
- Reuse the existing canonical held-auth traversal and protected evidence helpers; do not duplicate a security-sensitive walker or crypto protocol.
- The primary orchestrator, not this module, obtains and rereads the exact host turn via `codex_app__read_thread`. Code accepts the specified `HostUserTurn` object and must fail closed on malformed content/timestamp, wrong fixed statement, predecessor/signature mismatch, combined/reused source, or source-journal conflict. Do not embed a Codex-app client or invent a caller boolean.
- Public Task 2/3 APIs have exactly the plan signatures and fixed production configuration. Dependency injection stays behind underscored test-only seams. No root/path/key/JID/nonce/kind/attempt override exists in production.
- Do not use Gateway IPC or the word/value `delivered`; only the direct authenticated protocol-v3 response and protected observer graph can support an outcome.

## Required completion order (TDD for each behavior)

1. Add tests for exact public signatures and fixed production wiring. Implement public `build_smoke_expectation(current_auth, inventory, observer_ready)` and `run_smoke(expectation, observer_ready)` without an injectable public runtime.
2. Add held-FD tests and implement fixed production adapters that reuse `canonical_auth_tree_v1`, open the auth root and `creds.json` no-follow, require current owner/private modes/regular type/nonempty `me.id`, normalize only supported WhatsApp JID domains, and recheck both whole-auth HMAC and self identity immediately before network.
3. Add raw protocol tests and replace caller-normalized `feed(raw, event)` as the production contract with authenticated protocol-v3 raw-envelope parsing. Persist the exact raw frame first, derive every normalized provenance field, keep a contiguous predecessor chain, use actual observed timestamps, add `capture_window()`, and require observer readiness before QR is allowed.
4. Add observer failure tests. Disconnect/drop/overflow/malformed/publication/close failure must durably record protected `capture_failed` when possible, block successful close, and never erase already captured raw/normalized evidence.
5. Add success-DAG tests. After one accepted send, bounded-wait on the same ready observer. Retain all unsolicited events. Create `inbound-reply-v2` only for an inbound event whose reply-to equals the accepted message ID; otherwise seal `accepted_no_reply` only after a complete bounded window. Successful close is published last with exact readiness/timestamps/count/heads/acceptance/optional inbound receipt and no terminal record.
6. Prove controller-produced zero-event, unsolicited-event, and causal-reply closes pass `compare_v2`; prove all malformed/mismatched/incomplete controller paths are mismatch or terminal NO-GO.
7. Redesign the keyed `.smoke-one-shot` journal as the smallest durable state machine satisfying the brief: reserve before intent/attempt, validate every stored commitment/reference/state transition, fsync, release lock before network/wait, burn every existing nonce, persist success or terminal outcome, and forbid a second send through every crash/publication/corruption/substitution/concurrency path.
8. Add the full negative matrix named in Task 3, plus no stdout/stderr JID/content/auth leakage and no test-only production bypass. Remove unused scaffolding.
9. Execute and record the mandated mutations: generic owner approval accepted, one unsolicited event discarded, retry after ambiguous send, and accepted timeout mapped to unknown. Each covering test must fail under mutation and pass after restoration.
10. Run the focused Tasks 1-3 pytest/Ruff commands, then the combined four-module checkpoint suite and `git diff --check`. Write the full RED/GREEN/mutation/verification report to `/home/dm/Documents/yeoman/.superpowers/sdd/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness/task-1-3-recovery-report.md`.

## Report contract

The report records: implementation by requirement; exact RED command/failure and GREEN command/result for each new behavior group; mutation command/failure/restored result; files/commits; full focused verification; self-review; concerns; and the explicit statement that no real Bridge, Gateway, service, QR, JID, message, owner turn, auth, key/evidence, archive, memory, or network path was touched.
