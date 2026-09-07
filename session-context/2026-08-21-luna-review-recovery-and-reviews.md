# Luna review-channel recovery and independent re-review

## Trusted-core scope decision (2026-08-22)

Owner explicitly accepts a narrow trusted-core exception: hostile same-process
imports are out of scope for the storage/trusted-core getter boundary. This
does not relax provenance, authorization, memory isolation, evidence,
channel/provider/workload boundaries, dynamic-module checks, restart
durability, or production/release gates. The exception must remain visible in
future review reports and must not be generalized to caller-controlled DTOs or
runtime composition.

Date: 2026-08-21

## Channel recovery evidence

- Codex CLI updated to `0.149.0`.
- Standing review sessions are reachable on `remote-ssh-discovered:moltypython`.
- Agent A thread: `01a009b3-e740-7972-992b-5d63d6066b8c`; connectivity probe returned `READY A`.
- Agent B thread: `01a009b3-f495-7a90-8e50-8a22d4d306d2`; connectivity probe returned `READY B`.
- No replacement sessions were created.

## Review binding

- Checkout: `/home/dm/Documents/yeoman-rework`
- Branch: `c/yeoman-architecture-rework`
- Commit: `ef849aaaf78196109c8029cf7459d921b0bab4db`
- Agent B tree digest: `d4ddff1acdc8eccc67bdfe74c0c8fe7a61a35c8f`
- Review mode: read-only; no runtime, messaging, service, or file-mutating action by either reviewer.

## Agent A — senior software engineer

Verdict: **CONDITIONAL GO**. Offline architecture work may continue; activation, migration cutover, and release acceptance remain unauthorized.

Critical findings:

1. Runtime lifecycle is not wired. Overseer exits with `FENCED_NOT_ACTIVATED` (`packages/overseer/yeoman_overseer/app/main.py:4-6`) and Gateway only performs preflight (`packages/gateway/yeoman_gateway/app/main.py:11-32`). No executable reconciliation/startup path exists yet.
2. Traceability is not end-to-end. Evidence is only a DTO/inspection helper (`packages/gateway/yeoman_gateway/evidence/inspection.py:20-109`); `EvidencePort` has no concrete repository or graph-integrity verification (`turn/ports.py:147-149`). `TurnKernel` does not materialize a durable outbound `InteractionRecord` (`turn/kernel.py:94-117`), so sender, recipients, reply/mention relationship, platform ID, and final receipt are not one canonical trace.

Important findings:

- Modality/provider binding is declarative but unenforced: route selection checks handling and byte size, not modality/provider capability intersection (`egress/models.py:28-49`, `egress/service.py:90-107`).
- Release/migration evidence is incomplete: orchestration remains `READY_FOR_MILESTONE_01`; M05/M06 observational evidence is absent and stabilization tests use synthetic signing (`docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md:31-40`, `docs/operations/core-stabilization.md:48-53`). `ReleaseProof` validates caller-supplied facts but does not execute checks (`scripts/release/prove_release.py:158-191`).
- Freshness boundaries differ between generic status and stabilization (`status/snapshot.py:37-44`, `status/stabilization.py:886-888`).

Verification: `315 passed`; Ruff clean; mypy clean across all four packages; `uv lock --check` clean; worktree clean at the requested commit.

Smallest next order: implement the evidence owner/repository with hash and parent-closure validation; create durable outbound interaction records linked to delivery attempts/receipts; enforce typed modality/provider intersection; keep runtime fenced until accepted handoffs and executable Overseer lifecycle evidence exist.

## Agent B — senior security architect

Verdict: **NO-GO** for activation or cutover.

Critical findings:

1. Unrestricted core reads violate module isolation (`packages/gateway/yeoman_gateway/storage/connection.py:99-115`); owner checks apply only to writes (`:178-190`). A module holding `CoreDatabase` can read other domains' memory, interactions, and evidence.
2. Owner approval is syntactic, not authenticated (`cutover/models.py:196-228`, `cutover/repository.py:88-307`). Destructive cutover can be authorized by a caller-constructed object.
3. Expired delivery effects can dispatch: expiry is optional when `now=None` and equality is accepted (`delivery/repository.py:210-251`); the service lacks a mandatory trusted final deadline check (`delivery/service.py:37-75`).
4. Expired action effects can dispatch (`actions/repository.py:142-193`, `egress/service.py:42-79`).
5. Secret replay protection is process-local and capability expiry is unenforced (`secrets/service.py:45,93-109`, `secrets/contracts.py:73-79`).
6. Evidence inspection trusts caller-supplied events and principal-only authorization (`evidence/inspection.py:95-109`; `evidence/models.py:30-44` does not bind `evidence_ref` to the canonical event hash).

Important findings:

- Raw-message metadata is not cryptographically bound to durable encrypted blob content (`interaction/models.py:153-246`, `interaction/repository.py:138-294`).
- Relationship targets lack enforced same-domain/conversation binding (`interaction/models.py:117-151`).
- Memory content-key domain is not constrained to source domains (`memory/models.py:78-155`, `memory/capture.py:147-201`).
- Suppression relies on a synthetic step-up fixture, not a production owner verifier (`memory/suppression.py:47-85`, `trust/contracts.py:11-14`).
- Capsule execution does not verify installed executable bytes against `build_sha256` (`workloads/capsule/definition.py:110-133`).
- Backup/restore remains fenced and unauthenticated (`backup/contracts.py:23-116`, `backup/restore.py:18-33`).
- Unknown-effect persistence can fail open when terminal-record writes fail (`egress/service.py:72-83`, `delivery/service.py:74-84`).

Verification: clean checkout and exact commit verified; architecture specification and durable notes read; trust, interaction, memory, evidence, delivery, actions/egress, secrets, capsule, backup, and cutover code/tests inspected; no runtime, service, network, messaging, or file-mutating action performed.

Smallest next order: implement one trusted owner/step-up and receipt-verification boundary; enforce module-scoped reads; require trusted `now >= deadline` checks immediately before external effects; add adversarial restart/replay/expiry/cross-domain tests; then bind raw blobs and executable bytes cryptographically and complete authenticated secret and backup/recovery paths.

## Decision

The review channel is restored, but the implementation gate remains **NO-GO for activation/cutover**. Continue only with offline, read-only or fenced implementation work addressing Agent B's critical authorization, expiry, isolation, and provenance findings first. Preserve the raw-memory and traceability requirements; do not delete or migrate existing memory until reconciliation and authenticated backup/restore evidence are accepted.

## Task 1 standing review — exact HEAD 192dd64824efc5cbe22801fc1c4dded06d41cc2b

Date: 2026-08-21

Scope: Task 1 storage-isolation turn, exact diff `aa66e28..192dd64`, read-only review on `/home/dm/Documents/yeoman-rework`; no runtime, service, messaging, or file-mutating action.

Agent A (senior software engineer): **NO-GO**.

- Critical: SQLite bracket-quoted identifiers are not recognized by `StorageRegistry` and bypass ownership. A TRUST reader accepted `SELECT t.x, m.x FROM trust_items AS t JOIN [memory_items] AS m ON 1=1` and returned the MEMORY row (`storage/registry.py:45-49,65-125`; disposable synthetic state).
- The same bypass affects `UPDATE ... FROM [foreign]`, `INSERT ... SELECT FROM [foreign]`, and `CREATE VIEW ... FROM [foreign]` through `validate_sql()`.
- Fixed-reader API and nested/direct source checks otherwise appear correctly bound; Agent A observed 322 full-suite tests, Ruff/mypy/diff clean.

Agent B (senior security architect): **NO-GO**.

- Critical: comments between `JOIN` and a table bypass source validation, e.g. `JOIN /*x*/ memory_objects`; `--` and newline comments also reproduced (`storage/registry.py:45-48,116-122`; `storage/connection.py:208-214`).
- Critical: bracket-quoted and schema-qualified bracket identifiers bypass ownership, including `[main].[memory_objects]`.
- Critical: owner-controlled DDL validates only the first comma-separated source, allowing a TRUST owner to create a leaking `trust_` view/table-as-select from `memory_objects`, then expose/copy foreign data through the bound reader.
- Important: tests lack comment, bracket, schema-qualified, and DDL leakage cases. Agent B confirmed storage tests 26 passed and the worktree was clean.

Decision: Task 1 remains **NO-GO** and Task 2 must not start. The next fix must replace the regex-only read/write source validation with an authoritative SQLite-aware parser/allow-list, or fail closed for all unsupported lexical forms (comments, bracket/schema-qualified identifiers, and indirect DDL sources), with adversarial regression tests.

## Task 1 standing-review fix loop — exact HEAD d0fab93

- `46417cb` applied the fail-closed lexical/indirect-DDL policy and added regressions.
- Scoped Luna re-review found a remaining Critical transaction bypass: nested `DELETE` and `REPLACE ... SELECT` could still use foreign sources.
- `d0fab93` closes those DELETE/REPLACE/UPSERT subquery paths via fail-closed write-select handling.
- Scoped Luna re-review: **CLEAN**. Storage tests 34 passed; full suite 330 passed; Ruff, repo-wide mypy, and diff check clean.
- A fresh standing Agent A/B review is still required at `d0fab93` before Task 2; no runtime or cutover action is authorized.

Fresh standing Agent A/B review at `d0fab93`: both **NO-GO**.

- Agent A Critical: quoted/backtick/schema-qualified index targets are not bound (`CREATE INDEX trust_idx ON "memory_items"(x)` accepted); `ALTER TABLE trust_items RENAME TO memory_items` validates only the old table and creates a cross-module alias. Required transaction-level tests and fail-closed target validation.
- Agent B Critical: SQLite single-quoted table sources bypass reads, including EXPLAIN (`JOIN 'memory_objects'` returned a MEMORY secret); quoted index targets, `ALTER TABLE ... RENAME TO`, and quoted `REFERENCES` targets remain unbound. Agent B also marked caller-selected owner strings in `CoreDatabase.reader/transaction` and `execute_owned` as Important capability-by-convention risk.
- Both confirmed the prior comment/bracket/schema-qualified/nested/DDL/DML source fixes are effective and the fixed-reader API has no unrestricted `read_one/read_all` callers.

Decision: Task 1 remains **NO-GO**. The next fix must fail closed or fully bind single-quoted sources and every schema target position (index `ON`, ALTER rename, REFERENCES), add exact regressions including EXPLAIN, and keep Task 2 held. The owner-capability concern is recorded for architecture disposition after the critical parser boundary is closed.

Follow-up closure:

- `0be9740` bound quoted/index/REFERENCES target policy but scoped review found quoted/backtick/single/schema-qualified ALTER rename and `CREATE TABLE ... AS WITH` bypasses.
- `585f367` rejects those forms; scoped Luna re-review: **CLEAN** (storage tests 40 passed; diff clean; implementer reports full suite 336 and Ruff/repo-wide mypy clean).
- Fresh standing Agent A/B review is required at `585f367`; the owner-capability concern remains recorded for architectural disposition, and no Task 2/live action is authorized yet.

Review-channel availability note (2026-08-22): the Codex app refreshed the host identifier to `slingshot:env_e_6a06d04da98083299cf79b35096693ac`, but `list_threads` reports both standing-review host entries unavailable and read/send calls fail. No files, services, runtime, or live state were changed. Until the standing remote is reachable again, any substitute review must be a fresh Luna-only session and remain read-only.

Fallback Luna standing reviews at `585f367` (read-only substitute sessions):

- Substitute Agent A: **CONDITIONAL GO** for scoped reader isolation. Conditions: migration metadata fails open for unknown/ahead versions; storage transaction is a broad migration writer; production registry composition/freeze is absent.
- Substitute Agent B: **NO-GO**. Critical overlapping table prefixes (`trust_` and `trust_items_`) permit cross-module read/write acceptance. Important: `CoreDatabase.reader/transaction` and `execute_owned` remain caller-selected owner capabilities.
- Durable reports: `.superpowers/sdd/2026-08-21-security-blocker-closure/standing-agent-a-585f367.md` and `standing-agent-b-585f367.md`.

Decision: keep Task 2 held. Task 1B must close prefix overlap and disposition migration metadata, storage-writer authority, and frozen composition before another exact-head standing review.

Fresh fallback standing reviews at `a023dbe`:

- Substitute Agent A: **NO-GO**. `CoreDatabase.registry` can be replaced/unfrozen and `_migration_capability` replaced to obtain the storage-wide writer; raw registry and owner-selection residuals remain.
- Substitute Agent B: **NO-GO**. Mutable `_active_token` permits forged owner substitution; importable raw connection registry permits unvalidated writes; `_migration_capability`, `_migration_transaction`, and `_perform_transaction` remain callable; canonical use is not enforced; reader owner remains mutable.
- Both verified prefix overlap, migration metadata rollback, and normal guarded SQL paths.

Decision: Task 1B remains blocked. Next closure must seal post-composition registry and transaction authority state, then obtain fresh exact-head reviews again.

Task 1B exact-head review and deblock outcome (2026-08-22):

- Implementation commit: `5697ef9dfad557c2b181c48b2ee9929b1f8607e7` (`fix(storage): gate private storage boundary`). The patch requires an identity-checked storage-boundary capability for registry, raw-connection, authority-state, and migration-capability helpers. Focused storage/architecture tests: 51 passed; full suite: 353 passed; Ruff, mypy, and diff checks clean.
- Scoped Luna Task 1B re-review: **CLEAN** under the stated trusted-core assumption (`task-1b-re-review-6.md`).
- The two fresh Luna fallback standing sessions were deblocked and reviewed this exact head. Agent A: **NO-GO** (`standing-agent-a-5697ef9.md`). Agent B: **NO-GO** (`standing-agent-b-5697ef9.md`). Both independently identified the same Critical issue: `_storage_boundary_capability()` is an importable callable that returns the valid singleton, so same-process code can obtain raw SQLite, mutable authority state, and migration-writer authority. Missing/forged-token rejection does not close this runtime path.
- The original Codex app standing remote remains unavailable after the host refresh; these substitute reviewers were new Luna-only sessions, read-only, and no runtime/service/live-database action was taken.

Owner decision (2026-08-22): explicitly accept the trusted-core exception; hostile same-process imports are out of scope. Task 2 may resume offline. The `_storage_boundary_capability()` getter is documented as a trusted-core mechanism, not hard runtime isolation. Ordinary checked-in domain code remains architecture-fenced, and the exception must not extend to untrusted plugins, provider adapters, capsules, workloads, channel adapters, or dynamically loaded code. A separate storage worker/process remains optional future hardening. No runtime, service, live database, migration, or external-effect action is authorized.

## Task 2 final closure — exact HEAD `b362421d16cc081fa89b9537e610fa0723da0d4f`

Task 2 is complete for offline/fenced development. The implementation now has:

- Trust-owned durable enrollment of the sole owner Ed25519 key; the verifier has no caller-selected key path and rejects normal post-construction authority replacement.
- Canonical signed owner approvals with durable nonce replay protection, generation/trace/action/target binding, and authenticated idempotent cutover-marker replay.
- Memory suppression guarded at both service and repository boundaries; the repository pins lifecycle generation at construction, verifies the signed manifest itself, and uses its trusted clock for receipt/projection timestamps.
- Mandatory equality-expiring effect leases, fresh trusted clocks immediately before adapter/provider invocation, durable rejection of `DISPATCHED` retries, and fail-closed delivery/egress persistence fencing.
- TurnKernel equality checks and mandatory effect-time propagation.

Evidence: full suite `366 passed`; Ruff clean; mypy clean across 126 source files; diff clean. Scoped Task 2 Luna review `task-2-re-review-final-5.md` is CLEAN. Fresh standing Agent A and Agent B Luna reviews at this exact commit are both CONDITIONAL GO for offline/fenced Task 2 and NO-GO for production activation. Their later-stage blockers remain assigned to Tasks 3–5: canonical composition/current-generation authority, repository-backed evidence and parent integrity, complete interaction/outbound receipt traceability, stranded-effect reconciliation, authenticated backup/capsule assurance, exact-HEAD artifact proof, and operational rehearsal.

Runtime/services/live database/migrations/channels/providers/cutover remain offline. Next implementation task is Task 3 (canonical evidence and interaction provenance); no external effect is authorized by this closure.

## Trusted-core exception reaffirmed — 2026-08-22

Owner explicitly accepts the trusted-core exception: hostile same-process imports are out of scope. This is a narrow architectural decision for the storage trusted-core capability/getter and does not weaken the controls around evidence, interaction provenance, memory authority, channels, adapters, providers, workloads, capsules, or dynamically loaded/untrusted modules. Runtime, service, live-database, migration, and external-effect actions remain unauthorized while the offline rework proceeds.

## Task 3 final closure — exact code HEAD `1b888b4f3082e9e2d46d608e5917bf8865654c1`

- Task 3 remains offline/fenced only. The final code removes the caller-controlled interaction `owner_private` bypass and requires outbound evidence references to resolve to protected, same-trace canonical EvidenceRepository events. Unknown, foreign, and public/unprotected references fail closed.
- Fresh scoped Luna review is **CLEAN** (`task-3-review-final.md`). Refreshed standing Luna Agent A is **GO offline/fenced / NO-GO production-release** (`standing-agent-a-1b888b4.md`); refreshed standing Luna Agent B is **GO offline/fenced / NO-GO production-release** (`standing-agent-b-1b888b4.md`).
- Independent verification at this code head: focused suite `54 passed`, full suite `380 passed`, fresh adversarial subset `6 passed`, Ruff clean, mypy clean, and diff clean. Runtime `/home/dm/.yeoman`, services, live databases, channels, providers, migration, cutover, and external effects were untouched.
- Explicit Task 5 residuals: concrete composition/dependency binding, trusted timestamp ownership for outbound/memory attempt chronology, TurnKernel canonical outbound receipt composition, and release evidence/rehearsal. These residuals keep production activation NO-GO; they do not reopen the narrow trusted-core exception.

## Task 4 final closure — exact implementation HEAD `094d52759eda049f218b73be03a11ea97a785f69`

- Milestone 04 remained offline/fenced; `/home/dm/.yeoman`, services, live databases, channels, providers, credentials/keys, network, cutover, and external effects were untouched.
- Implementation sequence: `a1dd51e` backup/Capsule/trusted-time hardening; `e494219` restore ordering and workload deadline/runner lease binding; `6aa261c` immutable definition effect catalog and terminal unknown replay; `fbce5c0` unknown-effect fence; `fd7b409` late canonical replay ordering; `127f61e` rejection of option-like Capsule values including `--no-sandbox`.
- Fresh scoped Luna review is CLEAN offline/fenced only (`task-4-review-final.md`, final report commit `0a347e0`). Fresh standing Agent A is CONDITIONAL GO offline/fenced / NO-GO production-release (`standing-agent-a-094d527.md`, `c24b5a9`). Fresh standing Agent B is CLEAN offline/fenced / NO-GO production-release (`standing-agent-b-094d527.md`, `bf86184`).
- Verification: full suite `399 passed`; focused Task 4 suite `77 passed` (standing review expanded affected suite to `134 passed`); Ruff clean; mypy clean across 128 files; lock, diff, and all package builds clean. The checked-in artifact manifest remains stale and failed the artifact verifier; it was not rewritten.
- Remaining release gates: fixed Trust-owned backup authority/key identity/read-back and blank-root recovery; executable digest-to-bind TOCTOU and deployed isolation proof; concrete authenticated status/startup/trusted-time composition; durable cross-restart runner replay and live lease revocation/IPC; canonical end-to-end workload/control/outbound evidence trace composition; release artifact manifest/signing. Production activation remains NO-GO.

## Task 5 bounded composition/trace slice — exact HEAD `1dca187ec2aaa3ff595cc5f7d00445ba77566b9f`

- Luna implementation added fresh second-Trust materialization for Delivery, typed success/failed/unknown outbound handoff contracts with exact trace/effect/result/evidence-parent checks, and a frozen `TurnComposition` wrapper.
- Verification at this head: full `405 passed`; focused affected suite `140 passed` in the security review; Ruff, mypy across 129 source files, lock, diff, and all-package build clean. The checked-in artifact manifest still fails the verifier with stale-manifest codes; it was not rewritten. Runtime `/home/dm/.yeoman`, services, live databases, channels, providers, credentials/keys, network, cutover, and external effects were untouched.
- Fresh standing Agent A Luna: **CONDITIONAL GO offline/fenced / NO-GO production-release** (`standing-agent-a-1dca187.md`, report commit `d9c4841`). Fresh standing Agent B Luna: **CONDITIONAL CLEAN offline/fenced / NO-GO production-release** (`standing-agent-b-1dca187.md`, report commit `08a19fe`). Both independently confirm the approved hostile same-process-import exception is narrow to the trusted-core storage capability/getter and does not extend to Trust, evidence, interaction provenance, memory, channels/adapters/providers, workloads, capsules, or plugins.
- Required next closure: genuinely seal the TurnKernel dependency graph (not only the wrapper); reject action/delivery trace and source-interaction mismatches before commit/send; converge adapter/trace failures to a typed durable `external_effect_unknown` path; expose canonical attempt/provider identity and trusted acceptance time; add the composition-owned adapter invoking `InteractionRepository.record_outbound_receipt()` with protected same-trace EvidenceRepository refs. Backup authority/recovery, status/startup/trusted time, runner replay/revocation/IPC, Capsule TOCTOU/deployed isolation, and artifact/release gates remain open. Production/release/activation stays NO-GO.

## Task 5B sealed-kernel/unknown fence — exact HEAD `fc8d5616056c99bd35eb814fadfb415bf973dbc6`

- Luna implementation commit `1ad1744` sealed ordinary post-bind mutation of the TurnKernel dependency slots, added pre-effect action/delivery trace/source/domain/destination/audience/generation checks, and returned explicit `TurnState.UNKNOWN` plus typed unknown trace/result on delivery, outbound-trace, or receipt-evidence exceptions. The fallback is intentionally local and does not claim durable restart fencing.
- Verification: focused Turn/Delivery/Interaction suite `42 passed`; full `417 passed`; Ruff, mypy across 129 files, lock, diff, and four-package build clean. Artifact verification retains expected stale-manifest codes; no manifest rewrite. Runtime/services/live databases/channels/providers/credentials/network/cutover/external effects untouched.
- Fresh standing Agent A Luna: **CONDITIONAL GO offline/fenced / NO-GO production-release** (`standing-agent-a-5b-fc8d561.md`, `5215198`). Fresh standing Agent B Luna: **CONDITIONAL CLEAN offline/fenced / NO-GO production-release/activation** (`standing-agent-b-5b-fc8d561.md`, `097a933`). Both confirm the storage-only trusted-core exception remains narrow.
- Next blockers found: Trust envelope source-domain/audience/context claims are not cross-bound before effects; repeated requests can dispatch twice because UNKNOWN fallback is not durable or retry-fenced; provider egress and post-egress evidence exceptions can still collapse possible effects into generic FAILED/DENIED; canonical attempt/provider/trusted-clock receipt linkage and full Gateway composition remain absent. Backup/recovery, status/startup, runner replay/revocation/IPC, Capsule TOCTOU/deployed isolation, and artifact/release gates remain open. Production/release/activation stays NO-GO.

## Task 5C Trust/provider uncertainty fence — exact HEAD `9649cdf50a99c80aa9e0fb0bff3b5e2de6f321ff`

- Luna implementation commit `eeef781` extended typed TrustEnvelope claims with DestinationRef/owner binding, checked source-domain/audience/action coverage before effects and again at fresh delivery authorization, and converted provider BaseException/UNKNOWN/post-egress evidence/fresh-Trust uncertainty to typed UNKNOWN without fabricated kernel metadata.
- Verification: focused Turn/Egress/Delivery/Interaction `60 passed`; full `430 passed`; Ruff, mypy (129 files), lock, diff, and package builds clean. Artifact verifier retains stale-manifest codes; no manifest rewrite. Runtime/services/live databases/channels/providers/credentials/network/cutover/external effects untouched.
- Fresh standing Agent A Luna: **CONDITIONAL GO offline/fenced / NO-GO production-release** (`standing-agent-a-5c-9649cdf.md`, `6d911ac`). Fresh standing Agent B Luna: **CONDITIONAL CLEAN offline/fenced / NO-GO production-release/activation** (`standing-agent-b-5c-9649cdf.md`, `b20583f`).
- Required next closure: independently bind semantic destination context/completeness to the interaction domain and Trust authority; bind action initiator to requester/owner; validate TrustPort runtime type before claims and avoid writing `turn_authorized` before validation; remove synthetic `unknown-{attempt_id}` provider identity from ControlledEgressService; retain explicit non-durable UNKNOWN/retry fence until canonical restart-safe persistence exists. Canonical receipt/attempt/provider/trusted-clock composition and prior backup/recovery/status/startup/runner/Capsule/artifact gates remain open. Production/release/activation stays NO-GO.

## Task 5D semantic destination/unknown identity fence — exact HEAD `c221b228804de85a197de030d308d0fda6b91498`

- Luna implementation commit `85f2d5f` now semantically rejects incomplete audiences and foreign DOMAIN contexts, enforces OWNER_PRIVATE owner/assistant-only audience invariants, requires canonical SEND grants for EXTERNAL/cross-domain effects, binds action initiator exactly to the requester, validates TrustPort runtime type before claims/evidence, and allows missing provider request identity only for UNKNOWN outcomes (removing `unknown-{attempt_id}` fabrication in ControlledEgressService).
- Verification: focused Turn/Egress/Delivery/Interaction `71 passed`; full `441 passed`; Ruff, mypy (129 files), lock, diff, and package builds clean. Artifact verifier remains stale-manifest failure; no manifest rewrite. Runtime/services/live databases/channels/providers/credentials/network/cutover/external effects untouched.
- Fresh standing Agent A/B Task5D reviews are pending at this exact head. Production/release/activation remains NO-GO. Residuals: UNKNOWN fallback is local/non-durable and retry-fenced only by contract, canonical InteractionRepository receipt/attempt/provider/trusted-clock/full Gateway composition, and backup/recovery/status/startup/runner/Capsule/artifact gates.

## Task 5D standing-review result — exact HEAD `c221b228804de85a197de030d308d0fda6b91498`

- Fresh Agent A Luna: **CONDITIONAL GO offline/fenced / NO-GO production-release** (`standing-agent-a-5d-c221b22.md`, `bfc4d44`). Fresh Agent B Luna: **NO-GO for Task 5D security closure and NO-GO production/release/activation** (`standing-agent-b-5d-c221b22.md`, `82b0db3`). Both reviewed the exact product head read-only; runtime/services/live state remained untouched.
- Agent B found a P0 effect-boundary gap: complete same-domain DOMAIN destinations return before `evaluate_access`, so absent/incomplete/stale membership or audience-outside-membership can reach action commit, provider, and delivery. Caller-supplied grants/membership are also not bound to a Trust decision or protected evidence. Agent A independently confirmed the same-domain membership bypass and caller-supplied non-canonical grant residual.
- Next closure: require canonical same-domain `evaluate_access(SEND)` with complete, verified, same-domain membership and audience containment before any effect; fail closed when membership/grants are not Trust-bound rather than treating caller DTOs as authority. Keep UNKNOWN replay non-durable/retry-fenced, action commit uncertainty, canonical receipt/attempt/provider/trusted-clock composition, and all backup/status/startup/runner/Capsule/recovery/artifact gates explicit. Production/release/activation remains NO-GO.

## Task 5E standing-review result — exact HEAD `8cc35da74d981111f7bc598cc4213646f6407691`

- Fresh Agent A Luna: **CONDITIONAL GO offline/fenced / NO-GO production-release** (`standing-agent-a-5e-8cc35da.md`, `813561c`). Fresh Agent B Luna: **CONDITIONAL GO offline/fenced / NO-GO production-release/activation** (`standing-agent-b-5e-8cc35da.md`, `4acf2f9`). Both reviewed the exact head read-only; runtime/services/live state remained untouched.
- Same-domain membership/audience checks and Trust membership/grant IDs are materially improved. Agent A found a remaining multi-source DOMAIN path that evaluates a SEND grant but skips the envelope grant-ID binding; an unbound caller grant still reached action/egress/delivery. Both reviewers also retain caller-DTO provenance, process-local UNKNOWN replay, generic FAILED after post-commit action uncertainty, canonical receipt/attempt/provider/trusted-clock composition, and prior operational/recovery/artifact blockers.
- Next closure: apply the same Trust-bound selected-grant ID check to multi-source DOMAIN effects and convert action commit/evidence post-commit uncertainty to typed UNKNOWN/fence rather than generic FAILED. Caller DTO authority, durable replay, canonical receipt composition, backup/status/startup/runner/Capsule/recovery/artifact gates remain explicit production blockers.
### 2026-08-22 trusted-core scope decision

Owner explicitly accepts a narrow trusted-core exception: hostile same-process
imports are out of scope for the storage/trusted-core getter boundary. This
does not relax provenance, authorization, memory isolation, evidence,
channel/provider/workload boundaries, dynamic-module checks, restart
durability, or production/release gates. The exception must remain visible in
future review reports and must not be generalized to caller-controlled DTOs or
runtime composition.
