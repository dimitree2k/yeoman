# Yeoman Rework Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the owner-approved Yeoman architecture as a legacy-free reactive core followed immediately after stabilization by the mandatory Proactivity Phase 2.

**Architecture:** This file is the program index and state machine, not a duplicate implementation plan. Work advances through seven bounded milestone plans in one isolated source worktree and one fresh non-live state root; each milestone consumes only the normative specification sections and handoff receipt named here.

**Tech Stack:** Python 3.14, uv workspace, SQLite/WAL, encrypted local blob storage, TypeScript/Node 20 WhatsApp bridge, systemd user services, Bubblewrap, Unix-socket IPC, pytest, Ruff, mypy, and Node test runner.

## Global Constraints

- The normative design is `docs/superpowers/specs/2026-08-16-yeoman-target-architecture-design.md`, owner-approved at Git commit `1eb36c2a87fb0fd8cce86c137221668675aae14d`, SHA-256 `8201c5986e66292147e8682af506bf1978abe5338d56572b0395bf3d42affbfa`.
- The rework uses a separate source worktree, for example `~/Documents/yeoman-rework`, and a fresh non-live state root.
- The new worktree uses final package names and final contracts from its first commit.
- Do not create permanent `yeoman_v2`, `legacy`, compatibility, mirror, or dual-write packages.
- No milestone may leave a second live authorization, storage, delivery, provider, capsule, or configuration path temporarily enabled.
- Raw interactions and canonical memory are retained indefinitely in the first release; no state, credentials, identities, manifests, or private activation values enter Git.
- WhatsApp and Telegram are the only first-release channel implementations.
- The Core Release build contains no proactivity package, entrypoint, config, unit, or dormant definition.
- Persona evolution is absent from executable product behavior.
- Every milestone ends with software-architecture and security cross-review GO.
- Use Conventional Commits and preserve unrelated user changes.

---

## 1. Program authority and current state

Only the normative specification decides architecture. These plans decide execution order and file/task boundaries; a plan may narrow work but may not relax the specification. If source, a handoff receipt, and this state table disagree, stop before mutation and reconcile the mismatch with the owner.

| Field | Value |
|---|---|
| Program state | `READY_FOR_MILESTONE_01` |
| Active milestone | `01-release-baseline` |
| Last accepted milestone | none |
| Live cutover state | `PRE_TARGET_COMMIT` |
| Rollback authority | old release remains authoritative until the atomic `target_committed` transaction |
| Architecture completion | incomplete until Milestone 07 observation gate passes |

Advance this table only in the same commit that records both reviewer GOs and the accepted milestone handoff digest. Never mark a milestone complete from test claims without fresh command output and artifacts.

## 2. Context-loading protocol

Every fresh implementation task starts with this exact context packet:

1. Read this orchestration file completely.
2. Read only the normative-specification sections listed by the active milestone.
3. Read the active milestone plan completely.
4. For Milestones 02-07, read only the immediately preceding safe handoff reference at `artifacts/program/handoffs/mNN-handoff.json`, its digest-pinned cumulative registry at `artifacts/program/contracts/accepted-contracts.json`, and generated schemas/interfaces it explicitly names.
5. Inspect the current implementation, tests, generated schemas, and built interface artifacts for the task's declared file set before editing; plan documents never replace current code truth.
6. Read `AGENTS.md` and the required Superpowers execution skill.

Do not preload completed plan bodies, superseded specs, session notes, old chat context, or unrelated packages. A worker that needs an earlier fact obtains it from the cumulative accepted-contract registry, the protected receipt it digest-references, current source/tests, or the normative spec; it does not search conversational history for architectural authority.

Each task must fit one reviewable commit and end at a green task-specific gate. Do not carry a half-written task across tasks or sessions. If a task is interrupted, the next worker resumes from its declared files, failing test, and Git diff rather than from a prose summary.

## 3. Workspace topology

Implementation begins by applying `superpowers:using-git-worktrees` and proving the source baseline:

```text
~/Documents/yeoman                 old/live source checkout until cutover
~/Documents/yeoman-rework          isolated target source worktree
~/Documents/yeoman-migration-toolkit
                                  temporary signed toolkit worktree/artifact
~/Documents/yeoman-proactivity     optional provisional Phase 2 worktree during Milestone 06 only
~/.yeoman-rework                   fresh target state root; never live by default
~/.yeoman                          old live state; read only except approved preservation fences/backups
```

The target worktree uses a `c/` branch and starts from the exact owner-accepted source preservation commit. The toolkit is built from a separate temporary branch/worktree, has no target runtime entrypoint, and is deleted with its branch and sealed evidence only after its recovery obligation ends. Git history is the only historical code archive.

The optional Phase 2 lane uses `~/Documents/yeoman-proactivity` on `c/yeoman-proactivity-phase2`, forked from accepted `m05`. It may complete green provisional commits through Milestone 07 Task 4 Step 3 and writes provisional protected receipts only: it cannot update the accepted-contract registry, merge into target, install, activate, access live credentials/channels, or alter the Core Release under observation. Every M06 Core source/build/config/policy/schema/provider/key/control change invalidates the lane; after accepted `m06`, Task 4 Step 4 rebases it onto the exact Core commit, reruns the complete affected gate, obtains new reviews, and only then permits merge/build/activation.

Before creating either worktree, disposition every dirty path in the live source checkout: commit it into the signed source baseline, or record an explicit owner-approved exclusion. Uncommitted content may not silently disappear from the rewrite baseline.

## 4. Milestone index and dependency graph

```text
01 Release baseline
  -> 02 Trust and storage
    -> 03 Reactive core
      -> 04 Workloads and control
        -> 05 Channels, migration, cutover
          -> 06 Stabilization and retirement -> 07 activation/observation/completion
           \-> 07 build-only Tasks 1-4 Step 3 (separate non-activated commits)
```

The build-only Phase 2 lane is the sole allowed overlap. It may begin from accepted `m05` while Milestone 06 observes the deployed Core Release, but it cannot merge into, install on, or activate against that release. Any Core correction that resets Milestone 06 also invalidates/rebases the Phase 2 artifact. `m06` is mandatory before Milestone 07 Task 4 Step 4 or any later merge/build-for-install/activation step, allowing inert-shadow activation to begin immediately when stabilization passes.

| Milestone | Plan | Read normative sections | Entry receipt | Exit result |
|---|---|---|---|---|
| 01 | `2026-08-16-yeoman-rework-01-release-baseline.md` | §§1-5, 14-16, 18.1-18.4, 19-21 | none | signed preservation baseline, clean target skeleton, build and residual gates, signed toolkit artifact |
| 02 | `2026-08-16-yeoman-rework-02-trust-storage.md` | §§3-8, 12-15, 18.2-18.3, 19 | `m01-handoff.json` | canonical storage/blob/key/Trust/config/migration foundation |
| 03 | `2026-08-16-yeoman-rework-03-reactive-core.md` | §§3-11, 12.3, 13, 14, 16, 19 | `m02-handoff.json` | channel-neutral trace-complete reactive turn with memory, egress, effects, and receipts |
| 04 | `2026-08-16-yeoman-rework-04-workloads-control-recovery.md` | §§4.4, 4.6-4.8, 6, 8.1-8.5, 10, 12-16, 19 | `m03-handoff.json` | workload/capsule/control/status/local-backup stack and fenced recovery |
| 05 | `2026-08-16-yeoman-rework-05-channels-migration-cutover.md` | §§1-4, 6, 8-16, 18-20 | `m04-handoff.json` | WhatsApp/Telegram parity, two rehearsals, blank-root proof, release proof, atomic cutover |
| 06 | `2026-08-16-yeoman-rework-06-stabilization-retirement.md` | §§12-16, 18.4, 18.7, 19 | `m05-handoff.json` | Core Stabilization Gate, bundle/toolkit retirement, Core Release completion |
| 07 | `2026-08-16-yeoman-rework-07-proactivity-phase2.md` | §§3, 4.4, 4.7-4.8, 6-13, 16.2, 17, 19.4, 20-21 | `m05-handoff.json` for provisional build-only Tasks 1-4 Step 3; `m06-handoff.json` for Task 4 Step 4 onward | staged proactivity activation, observation proof, final planning/doc cleanup, program completion |

No milestone is optional. Corrective integrity/security work may interrupt the sequence; its receipt must point back to the blocked milestone and it cannot add product scope.

## 5. Handoff and cumulative-contract registry

Each milestone writes two artifacts:

1. one canonical protected operational receipt in target Evidence, containing private counts, paths, generations, identities, activation values, and complete command outputs; and
2. one release-safe Git handoff reference plus an updated cumulative accepted-contract registry containing only safe digests, version identifiers, ownership, and review decisions.

Milestone 01 is the bootstrap exception because target Evidence and Secret/Key Authority do not exist yet. Its full receipt is canonicalized, signed by the offline program-signing key, encrypted into the preservation evidence set, and committed only by digest in the safe reference. Milestone 02 verifies and imports that immutable receipt into target Evidence before closing; no Milestone 01 private record is retroactively rewritten.

The cumulative registry is transitive: each update carries forward every accepted dependency rather than pointing only to the immediately preceding milestone. It names exact source/build commits and digests, exported interfaces, schema and migration versions, build/activation schema versions, systemd definition digests, fence/capability types, toolkit/rehearsal digests, verification evidence digests, unresolved risks, and the protected receipt commitment for all accepted milestones.

The safe handoff reference has this shape:

```json
{
  "schema": "yeoman.program-handoff-reference.v1",
  "milestone": "m01",
  "status": "accepted",
  "source_commit": "40-hex Git commit",
  "spec_sha256": "8201c5986e66292147e8682af506bf1978abe5338d56572b0395bf3d42affbfa",
  "build_manifest_sha256": "64-hex digest",
  "protected_receipt_sha256": "64-hex digest",
  "accepted_contracts_sha256": "64-hex digest",
  "exported_interfaces": ["yeoman.shared.generations.v1"],
  "schema_versions": {"core": 0, "control": 0},
  "migration_ids": [],
  "systemd_definition_sha256": {},
  "capability_and_fence_types": ["EDGE_CAPTURE", "CORE_INGEST", "PROCESS", "EGRESS", "DELIVER"],
  "verification": [
    {"command": "uv run pytest tests/architecture -q", "exit_code": 0, "output_sha256": "64-hex digest"}
  ],
  "architecture_review": {"decision": "GO", "task": "01a00731-aa0d-7591-9382-2c52becddbae"},
  "security_review": {"decision": "GO", "task": "01a00731-aa5c-7232-93c3-d165eabc5d58"},
  "open_risks": [],
  "next_milestone": "m02"
}
```

Private counts, chat IDs, principals, source paths containing user identity, credential aliases, real activation values, signed migration inventories, backups, runtime evidence, and state hashes that leak equality stay in protected canonical evidence. Git contains only safe commitments and references. A non-empty `open_risks` array blocks advancement unless every entry is an owner-accepted non-blocking operational observation and neither reviewer classifies it as an architecture/security blocker.

## 6. Cross-milestone ownership and invalidation

| Contract | Owner | Change protocol |
|---|---|---|
| Shared wire/identifier/generation schemas | `packages/shared` contract owner established in Milestones 01-02 | Change version explicitly, update every producer/consumer contract test, rebuild artifacts, and invalidate every downstream handoff that references the old digest. |
| `core.db` registry/global migration order | Gateway storage owner from Milestone 02 | Logical modules own their DDL/repositories/migration definitions; storage orders them. Any reordering or historical migration edit invalidates all state/rehearsal receipts from the affected generation. |
| Evidence protection and inspection | Gateway Evidence owner from Milestone 02 | Each logical module writes causal evidence in its fact-owning transaction. Protection/inspection changes require Trust isolation and every emitting module's contract tests. |
| Capabilities and fence records/evaluation | Gateway Capabilities owner from Milestone 02 | Milestone 03 enforces them at ingress/process/egress/delivery; Milestone 04 adds fixed Overseer issuer/reconciliation only. Changes invalidate affected safety and startup receipts. |
| Public build/private activation schemas | Release owner from Milestone 01 | Modules contribute allowlisted fragments; only deterministic release tooling assembles them. A manifest/schema digest change invalidates the built release proof. |
| Systemd unit definitions | Process-owning package; fixed registry owned by Overseer from Milestone 04 | Change unit digest, rerun deployed hardening/postcondition tests, and invalidate affected workload/capsule/recovery/cutover proof. |
| Migration toolkit and source-native Edge segment | Temporary toolkit owner from Milestones 01 and 05 | Never imported by runtime. Any reader/normalizer/segment change invalidates prior rehearsals and requires a new signed artifact plus two fresh full reconciliations. |
| Channel/provider/workload definitions | Owning adapter/module | Change exact public type and private activation generations; rerun common contract, isolation, release, and observation coverage gates. |

No task edits a cross-owned contract without naming the owner, consumers, invalidated receipts, and rerun gates in its commit body and updated cumulative registry.

## 7. Task and milestone commit protocol

For every task:

- write the failing test or executable validation first;
- run it and preserve the expected failure;
- implement the smallest production change;
- run the targeted test, then the milestone-owned suite;
- inspect the exact diff and forbidden-residual scan;
- commit only declared task files with a Conventional Commit message.

At each milestone end:

1. Run every command in that milestone's release gate from a clean target worktree.
2. Build non-editable artifacts from a clean clone where the milestone requires it.
3. Generate the protected operational receipt, safe handoff reference, and transitively complete accepted-contract registry with real digests and command output hashes.
4. Ask the two standing reviewer tasks for independent review of the commit range and receipt.
5. Resolve every blocker and obtain explicit GO from both.
6. Commit only the safe handoff reference, accepted-contract registry, and this file's state-table advance together; protected operational evidence remains outside Git.
7. Start the next milestone in a fresh task using §2, not the previous chat transcript.

Milestone 07 is the terminal exception to steps 3 and 6 because its reviewed cleanup deletes the completed handoff JSON and accepted-contract registry from the released checkout. Before deletion it imports their exact digests into protected Evidence. The cleanup may change only pre-reviewed documentation/handoff paths and the exhaustive manifest's documentation inventory; bit-identical runtime-source closure, runtime-manifest section, executable artifact, activation, and every observation-cohort component are mandatory, otherwise the full observation resets. After clean-clone proof, it closes one immutable protected `m07` receipt body with no mutable reviewer fields; both post-cleanup reviewer receipts bind that body and the exact cleanup commit. The body is never rewritten after review. The cryptographically signed final architecture-program tag then becomes the sole release-safe handoff reference. Its signed message binds the exact released commit, normative-spec digest, observed runtime-source closure/executable/runtime-manifest/activation/cohort digests, final documentation-updated exhaustive-manifest digest/signature, immutable protected `m07` receipt-body digest, immutable final accepted-contract registry digest, both architecture/security GO receipt digests and standing task IDs, and `next_milestone=null`. A pinned program public key must verify the tag from a clean checkout.

## 8. Cutover state machine

Cutover is the only point where old and target runtime authority move:

```text
OLD_AUTHORITATIVE
  -> QUIESCED_TARGET_CAPTURE_ONLY
  -> TARGET_INGEST_RECONCILED
  -> atomic(first non-migration live event + producer cutoff + target_committed)
  -> TARGET_FORWARD_ONLY
```

Before the atomic marker, the signed old release and untouched old state may be restored only through the §18.5 spool-replay protocol. After it, the old release may never execute; all repair is forward recovery. No plan, operator note, service unit, or Overseer desired-state record can override this boundary.

## 9. Program completion and cleanup

The Core Release completes only after Milestone 06. The Architecture Program completes only after Milestone 07 reaches the owner-approved autonomous scope and passes its separate 14-day/100-trace gate.

Final cleanup removes from the released branch and installed/runtime trees:

- every old executable, compatibility path, schema, test, dependency, unit, timer, cache, and generated artifact;
- all superseded specs, plans, session notes, diagrams, and operator procedures;
- the migration toolkit branch/worktree/artifact and sealed legacy-bundle key after their gate;
- all persona-evolution execution artifacts;
- these orchestration/milestone plans after their final accepted receipt is preserved in protected program evidence.

The owner-approved normative architecture and current operator/product documentation remain. Git history retains historical source without exposing it to the released checkout or runtime.
