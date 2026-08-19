# SDD ledger — plan: docs/superpowers/plans/2026-08-16-whatsapp-rotation-prep-03-smoke-readiness.md

Resume date: 2026-08-19
Accepted plan SHA-256: `6074344d5f1c6e6d6f1a3d40fab82983ee34825002a74fb224bcf415184abe30`
Implementation worktree: `/home/dm/Documents/yeoman-migration-toolkit`, branch `c/yeoman-migration-toolkit`
Accepted pre-WIP base: `f17ebe05598bf28c5f8a0f32dbc3b1d9dbb957a1`
Paused WIP checkpoint: `fc86076352e8aaa417b7315688c02872bddf4826`

No separate product spec is referenced by this prep plan. The accepted prep-01, prep-02, prep-03, orchestration plan, and gate-bound amendment decision note are the binding authorities; rulings without them would be provisional.

## Resume rulings

- Ruling: preserve `fc86076` as a WIP checkpoint rather than rewrite or split it — it was intentionally created to prevent context/code loss at the owner pause and contains intertwined Tasks 1-3 changes — cost if wrong: the task review diff is broader and must review the combined recovery batch instead of one clean commit per task.
- Ruling: finish Tasks 1-3 as one tightly coupled recovery batch, then perform one task-scoped spec/quality review over `f17ebe0..HEAD` — all three tasks share the smoke controller and state comparator, and the paused commit already crosses their boundaries — cost if wrong: a defect may be attributed to the batch rather than its original plan task, but no requirement is dropped.
- Ruling: all resumed work is synthetic-only with Overseer, Bridge, Gateway, and Pinchtab inactive and disabled — the owner resumed implementation, not the live incident — cost if wrong: live validation is delayed until the already-defined Luna/owner gates.
- Ruling: use explicit task-brief output paths because the installed Superpowers `task-brief` helper calls a non-executable `sdd-workspace` sibling (both mode `0644`) — cost if wrong: none to product code; the plugin cache remains untouched.

## Preflight task/interface scan

| Tasks | Producer/consumer contract | Finding / ruling |
|---|---|---|
| Task 1 | owner turns, quarantine receipt verifier, inventory, common artifact verifier | Internally consistent after accepted gate amendment; phone-ready consumes the verified quarantine receipt. |
| Task 2 | observer ready/raw/normalized/close records and state comparator | Internally consistent; successful comparator input is close-v2 only. |
| Task 3 | expectation/intent/attempt/acceptance/inbound/close and one-shot journal | Internally consistent; no retry after any nonce reservation or ambiguous external effect. |
| Task 4 | prep-03 report consumes Tasks 1-3 evidence | Internally consistent; report cannot be finalized before combined implementation/review evidence. |
| Tasks 1 → 2 | Task 2 reuses the common fixed-kind artifact verifier and smoke module | Compatible; verifier is required before observer raw-event validation. |
| Tasks 1 → 3 | Task 3 consumes current-auth, phone-ready, inventory, and observer-ready graph | Compatible only with the amended gate-bound receipt chain; review must reject any reduced/test-only expectation schema. |
| Tasks 2 → 3 | Task 3 consumes the exact observer and comparator DAG from Task 2 | Compatible; the shared smoke/state files make these tasks tightly coupled in the paused WIP. |
| Tasks 1-3 → 4 | Task 4 records RED/GREEN/mutation and no-live evidence | Compatible; report is pending and must not infer evidence from green tests alone. |

Task 1-3 recovery batch: complete from WIP `fc86076`; final code/task acceptance at `b788608` with Terra SPEC ACCEPT and QUALITY ACCEPT, no findings.
Task 4 report: complete; see `task-4-prep-03-report.md`.
Combined Terra task review: accepted.
Synthetic integration/task review set: accepted at `995c3ac` with SPEC ACCEPT and QUALITY ACCEPT, no findings.
Standing Luna Agent A/B major-turn code review: pending.  It is the next gate; do not perform a live action before its first evidence-limited GO.

## Recovery gap audit

Fresh Terra read-only audit `/root/smoke_gap_audit` against `f17ebe0..fc86076` found these blocking gaps:

1. Fixed production `build_smoke_expectation` and `run_smoke` APIs/adapters are absent; only injectable `_SmokeCore` methods exist.
2. Held no-follow/current-owner/private-mode `creds.json` and stable canonical-auth checks are absent at build and immediately pre-send.
3. The observer accepts caller-normalized dictionaries instead of authenticating/parsing raw protocol-v3 envelopes, lacks `capture_window()`, and does not prove pre-QR readiness/capture completeness.
4. Accepted send seals no-reply immediately; there is no bounded causal quote-reply wait, inbound-reply receipt, or controller-produced comparator-compatible reply DAG.
5. The one-shot journal stops at `attempt_durable`, does not persist/verify successful outcomes, and lacks complete crash/publication/concurrency/prior-receipt recovery.
6. Observer disconnect/drop/overflow/publication failures are only in memory rather than protected terminal `capture_failed` evidence.
7. Actual controller outputs are not proven through `compare_v2`; state tests synthesize their own records.
8. Required credential/protocol/DAG/crash/leak/mutation tests and the prep-03 report are missing.

- Ruling: the audit request to embed host acquisition/reread inside the incident library is not adopted — the accepted plan explicitly assigns trusted `codex_app__read_thread` acquisition and reread to the primary orchestrator, while the public code interface intentionally accepts `HostUserTurn`; implementation must strictly validate the exact five-field tuple, timestamp/content, fixed statement semantics, predecessor, signature, and one-time source journal, with no opaque fallback or combined/reused source — cost if wrong: host-API trust remains outside the Python module and must be proven in the later owner-turn evidence package rather than a unit test pretending to call Codex.
