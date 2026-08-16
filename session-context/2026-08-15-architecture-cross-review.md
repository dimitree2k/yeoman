# Yeoman architecture migration cross-review

Date: 2026-08-15

Status: Independent software-architecture and security reviews completed. The consolidated recommendation awaits owner approval.

## Reviewers

- `Yeoman Architecture Migration Review` — task `01a00731-aa0d-7591-9382-2c52becddbae`
- `Yeoman Security Architecture Review` — task `01a00731-aa5c-7232-93c3-d165eabc5d58`

Both tasks were created as user-owned Codex sessions in isolated worktrees. They performed read-only repository reviews and did not edit source, runtime state, or services.

## Inputs reviewed

- `session-context/2026-08-15-architecture-audit.md`
- `session-context/2026-08-15-target-architecture-decisions.md`
- current implementation under `packages/gateway`, `packages/shared`, `packages/overseer`, and `packages/bridge`
- current tests, configuration schemas, migration scripts, retention behavior, and architecture/spec surfaces

## Consolidated verdict

Conditionally approve the target logical architecture:

- modular Gateway and small Turn Kernel
- retained Node bridge
- supervised persistent/temporary Workload Fabric
- Overseer/systemd control plane
- separate Interaction, Trust, Memory, Execution, Control, and Evidence responsibilities

These must remain logical module boundaries, not become seven services or a generic framework.

Migration and release are a no-go until the Trust Plane, preservation protocol, canonical interaction history, side-effect contract, and clean-release gates below are executable and proven.

## Required target-design corrections

1. The Interaction Ledger replaces reply-context/session history as historical truth; it must not become another duplicate archive.
2. Every consequential side-effect attempt has a durable intent and trace, then reaches `confirmed`, `failed`, or explicitly `unknown`. Not every platform can prove final delivery.
3. Add one controlled egress boundary after authorized context selection and before every external model, embedding, transcription, vision, TTS, telemetry, or tool call. Future anonymization belongs there and records transformation provenance.
4. Principal, domain, audience, grant, owner-private/operator context, and policy generation require exact schemas, resolver assurance, and fail-closed behavior.
5. Authority is an intersection of principal grants, domain policy, audience policy, workload class, data classification, destination capability, and model/tool handling policy. It is not a Boolean `is_owner`.
6. Only the Trust Plane may mint an immutable execution envelope. Consumers may narrow it but never widen it.
7. Tools receive the envelope per invocation and must not retain mutable authority between concurrent turns.
8. Unknown identity, origin, scope, disclosure, or security metadata becomes restrictive/quarantined, never global or speakable by default.

## Why the live strangler proposal was rejected

- Downtime is explicitly acceptable.
- Legacy and target memory policies have different security semantics. Behavioral parity could preserve a privacy defect.
- Two live memory writers or senders would require dual-write, routing, and cleanup machinery that must later be removed.
- Current inbound storage can lose/drop events before durable recording; outbound sends lack authoritative intents and receipts.
- Current raw interaction/media retention already deletes data, so a long transition increases unrecoverable gaps.
- The earlier parallel Turn Engine V2 experience demonstrated the cost of maintaining a second conversational path before foundational contracts and telemetry were stable.

Use parallelism only for isolated source development, offline replay, and disposable migration rehearsals—not concurrent live truth ownership.

## Recommended migration gates

### G0 — Preservation fence

- Disable message, media, session, and memory cleanup that can destroy required source state.
- Contain sensitive bridge logs and private runtime archives before copying backups.
- Inventory every enabled channel's path from platform event to durable storage.
- Add durable ingress capture/replay or prove equivalent upstream replay.
- Take encrypted WAL-aware backups and restore them on an isolated host/root.

Exit: no enabled ingress can disappear between platform receipt and durable ledger commit; every extant store is in the preservation manifest.

### G1 — Executable contract baseline

Define and test:

- canonical principal, platform identity, context domain, audience, thread/reply/mention relationships
- immutable execution envelope
- memory-domain grants and owner-private/operator authentication
- interaction and trace records
- side-effect intent/idempotency/receipt states
- provider/tool/telemetry data-handling policy
- migration mapping/disposition contract
- strict configuration schemas and ownership

Repair the audit's P0/P1 security, database, runbook, and clean-clone test findings needed for trustworthy evidence.

Exit: target invariants are executable fail-closed tests in authoritative clean-clone CI.

### G2 — Isolated replacement

- Build the replacement in a separate rewrite worktree/branch.
- Use the final package name; do not introduce shipped `v2`, `next`, `legacy`, or compatibility namespaces.
- New runtime code may depend only on canonical contracts and new stores.
- Legacy-format readers exist only inside a one-shot migration artifact outside the final runtime package.
- No new runtime import/call may reference the old Gateway modules.

Recommended final structure:

```text
packages/gateway/yeoman_gateway/
  app/
  edge/
  interactions/
  trust/
  turn/
  execution/
  delivery/
  memory/
  evidence/
  operations/
  providers/
  tools/
```

Bridge and Overseer remain separate packages. Shared contains only genuinely cross-process schemas, paths, and build metadata.

### G3 — Offline comparison

- Replay a representative interaction corpus with external side effects disabled.
- First inject recorded model/tool results to compare deterministic authorization, context, memory reach, routing, intents, and traces.
- Run current providers separately for qualitative and latency evaluation.
- Compare deterministic structures, not byte-identical LLM prose.

Exit:

- zero unauthorized memory IDs
- zero missing causal/message edges
- zero unaccounted side-effect intents
- every difference classified as accepted, defect, or intentional tightening

### G4 — Full migration rehearsals

Run at least two independent complete imports into disposable fresh state roots, including interruption/fault injection and blank-host restore.

Exit:

- every source object is migrated or quarantined
- all counts, hashes, integrity checks, and referential checks pass
- repeat imports produce identical canonical IDs and dispositions
- no ACL/readable-audience expansion occurs without explicit owner grant
- measured outage fits the accepted window
- rollback before commitment is successfully rehearsed

### G5 — Clean release retirement gate

Create the release from a clean clone after deleting:

- old runtime modules and imports
- legacy feature flags, selectors, compatibility adapters, and migration readers
- old config keys and permissive schemas
- old tests and test helpers
- old systemd/unit/deploy paths
- old architecture docs, plans, and superseded specs

Git history is the archive; no in-tree legacy documentation is required.

Exit:

- tracked-tree/path/token/AST dependency gates find no legacy reference
- strict config rejects removed/unknown keys
- wheel/deployment manifests contain no legacy module or artifact
- canonical documentation allowlist contains current architecture/operations/accepted decisions only

### G6 — Quiesced final cutover

1. Freeze source/config/policy changes.
2. Stop Overseer and timers first so no reconciler can revive state.
3. Put channel edges into durable capture-only mode and fence outbound delivery.
4. Drain old queues, debounce buffers, pending sends, tools, and workloads.
5. Stop Gateway and prove no remaining state writers.
6. Take final consistent backups and source manifests.
7. Migrate the final delta into a fresh target state root.
8. Boot the new stack with external I/O fenced.
9. Run storage, policy, identity, domain, model/tool, trace, and synthetic delivery probes.

Before commitment, rollback means restoring pointers to the signed old release plus untouched old state and replaying the durable edge inbox. No reverse migration is required.

### Commitment point

Persist a signed cutover record, atomically assign the new release/state generation as sole owner, start bridge then Gateway, release captured ingress, and start Overseer last.

The first live interaction consumed by the new Gateway is the irreversible commitment point. After that, the old snapshot is stale; ordinary rollback would lose new interactions. Recovery is forward-only unless a separately verified reconciliation migration preserves all post-cutover events, intents, receipts, memory changes, and traces.

### G7 — Acceptance and external rollback-bundle retirement

After an approved observation threshold:

- verify complete ledger/envelope/trace linkage
- verify all outbound attempts terminal or explicitly `unknown`
- verify zero unauthorized cross-domain access or disclosure
- verify zero unexplained duplicate/lost interactions
- verify daily storage integrity and backup restore
- remove old units, caches, config, runtime pointers, and external rollback artifacts when separately approved

The released source tree must already be legacy-free at G5. The old executable/state exists only outside the repository as an encrypted rollback bundle until acceptance.

## Lossless state and memory protocol

“Memory” means all extant assistant state, not only `memory2_nodes`:

- active and soft-deleted/tombstoned memory rows and embeddings
- session JSONL and boundaries
- reply/context archives
- contacts and identity mappings
- chat registry
- raw media and document cache
- tool traces and private handoffs
- workflow/cron state
- consciousness, speakup, persona, and preference state
- Overseer state/audit
- config, policy, personas, skills, and relevant bridge/ingress state
- backup-only artifacts that contain the only remaining copy of data

For each snapshot:

1. Stop writers or use SQLite's backup API with verified WAL handling.
2. Record path/table, schema version, primary key, status, permissions, size, timestamps, row counts, integrity result, and SHA-256.
3. Create one migration receipt per source object containing source coordinate, canonical target ID, stable digest, original origin/scope/provenance, and disposition.
4. Allowed dispositions are `preserved`, `split`, `aggregated-with-all-source-edges`, `quarantined`, or explicit owner-approved exclusion.
5. Equal content may share a derived node only if every original source/provenance/ACL edge remains independently reconstructible.
6. Derive origin domains from channel/chat/source evidence, never semantic type or contact identity.
7. Unknown or ambiguous origin/identity/ACL becomes owner-private, no-provider quarantine; never silently drop or widen.
8. Prove each target readable audience is no broader than the conservative source audience. Any expansion requires a signed owner grant.
9. Require zero unmapped source objects, zero unresolved message/reply/media/provenance edges, and zero unexplained collisions.
10. Restore and verify on a blank host/root before cutover.

The migration can preserve all extant state. It cannot reconstruct messages/media already deleted by historical retention unless an external archive exists. This must be recorded as a legacy provenance gap, not represented as successful reconstruction.

## Proving zero legacy remains

- Evaluate only files present in a clean clone (`git ls-files`), not ignored local tests/docs.
- Maintain a final path allowlist and forbidden legacy token/key list.
- Add AST dependency rules and negative-import tests for removed modules.
- Reject unknown config keys instead of ignoring them.
- Build wheels/deploy bundles from a clean clone and inspect manifests.
- Delete superseded plans/specs/docs; rely on Git history.
- Verify live systemd `ExecStart`, loaded module paths, build ID, config generation, socket owners, and bridge manifest all identify the new release.
- Store rollback/migration artifacts outside source Git and runtime Git.

## Security-specific blockers

- Current owner context can unlock owner-only memory even inside a group.
- Current recall includes sender/contact scopes across conversations.
- Memory semantic sector currently influences visibility.
- Unknown disclosure metadata and manual unknown scopes can fail open.
- Background/IPC direct turns can default to owner/all-tools authority.
- Shared mutable tool context can cross concurrent sessions.
- Current message/media retention contradicts indefinite raw retention.
- Provider, embedding, media-model, and external telemetry egress lacks classification-aware policy.
- Outbound delivery lacks durable intent, stable Python-side client ID, and persisted platform receipt.

These behaviors must not be used as migration truth. The target contract intentionally tightens them.

## Ranked joint risks

1. High — ongoing deletion or volatile ingress loses source evidence before migration.
2. High — naïve user/contact/global migration broadens cross-group access.
3. High — deduplication, re-scoping, collision deletion, or partial backfill loses provenance/memory.
4. High — owner-in-group, background, or shared tool context produces overprivileged disclosure/action.
5. High — old snapshot is falsely treated as rollback-safe after new live events exist.
6. High — raw data leaks through providers, embeddings, media models, telemetry, logs, or backups.
7. Medium — nondeterministic output parity hides deterministic authorization defects.
8. Medium — new plane coordinators recreate current god objects under different names.
9. Medium — stale Overseer/config writers revive removed state during cutover.
10. Medium — indefinite retention lacks final encryption, capacity, backup, and deletion policy.

## Decisions still required

- **Approved 2026-08-15:** rehearsed offline replacement and quiesced one-time cutover.
- **Approved 2026-08-15:** the first live interaction consumed by the new Gateway is the irreversible commitment point.
- Define the live acceptance threshold before deleting the external rollback bundle.
- Decide whether Interaction and Trace Ledgers are logical APIs over one transactional SQLite file or separate physical stores.
- Define owner/operator authentication assurance and principal-link/revocation rules.
- Define time-versioned audience/membership semantics.
- Define cross-domain grant lifetime, derivation, and revocation.
- Define model/provider/telemetry trust classifications and anonymization requirements.
- Define encryption, backup destination, capacity alarms, and future deletion controls for indefinite retention.
- Decide which secondary channels/workloads must ship in the first target release; unsupported ones must be removed rather than carried dormant.

## Owner decision recorded after review

The owner approved the reviewers' revised process without changes. This retires the proposed long-lived live strangler/canary migration. The implementation design must use isolated replacement, deterministic offline replay, repeated complete migration rehearsals, a clean legacy-free release artifact, and one quiesced cutover. The old snapshot is a valid rollback target only until the first live interaction is consumed by the new Gateway.

## Major-step cross-review: Trust Plane

Date: 2026-08-15

Verdict from both reviewers: **conditional go for design; no-go for G1 implementation baseline until the corrections are executable as fail-closed contracts and tests.**

### Joint corrections

1. Authorization is conjunctive: source-domain access, outbound-disclosure permission, and action/data/provider-route permission must all pass.
2. Immutable envelopes need expiry, authentication assurance, parent/child lineage, authority/revocation epochs, and exact identity/membership/grant/policy/config/provider generations. Historical evidence is immutable; current executability is not.
3. Revalidate at security boundaries and immediately before delivery. Long-lived work uses renewable narrow leases rather than ambient owner authority.
4. Distinguish intended recipients, conservative platform-reachable membership, unresolved recipients, delivery evidence, and read evidence. Receipts never define ACLs.
5. Platform identities are epoch-qualified and have at most one active evidence-bearing principal link. Conflicts quarantine privileged access. Links never rewrite historical ACLs.
6. Context domains are stable across ordinary membership changes but retire/relineage on material platform conversation reuse or re-key.
7. Membership snapshots are normalized, evidence-rated, time-versioned records referenced by interactions; partial rosters are not complete audiences.
8. Identity/membership/classification uncertainty never blocks durable capture, but blocks protected retrieval, shared disclosure, mutation, and unapproved egress.
9. Derived memory/model output inherits every source edge, most restrictive classification, audience intersection, and domain/grant constraint.
10. Grants require exact resource selectors and distinct actions; reading or summarizing never implies quoting, sending, persisting, or exporting.
11. Owner-private read-all and provider/tool egress are separate decisions. Shared destinations never inherit owner read-all.
12. High-risk mutation, bulk/sensitive retrieval, export, and cutover need action-bound step-up authentication; a WhatsApp identity or non-group heuristic is insufficient.
13. Migrated records without historical membership remain `legacy_unknown`/`membership_unknown`; current rosters must never be projected backward.
14. Authorization evidence is access-controlled and may use opaque references/digests plus periodic tamper-evident checkpoints.

### Required architecture simplifications

- Keep Trust as a logical in-Gateway boundary with an identity/membership registry, pure evaluator, and envelope/decision persistence.
- Do not introduce a Trust microservice, OPA/general ABAC language, policy plugin engine, graph database, bitemporal ORM, or general IAM platform.
- Authorize materialization/security boundaries, not every pure function or every returned row independently.
- Keep the owner as one human principal with step-up operator sessions rather than a duplicate operator person.
- Sign cross-process child authority, operator sessions, cutover records, and periodic ledger checkpoints; in-process envelopes need immutable IDs, not per-row signatures.

### Acceptance-test themes required in G1/G3/G4/G6

- identity linking across groups never widens memory access
- source-domain and outbound-audience checks remain independent
- link/revoke/relink leaves historical resolutions intact
- membership joins/leaves apply prospectively and audience changes cancel or reauthorize a pending send
- derived results never become more visible than any source permits
- child envelopes cannot add domains, resources, tools, audiences, providers, classifications, or lifetime
- revocation stops paused/long-running work at its next security boundary
- owner DM/operator retrieval succeeds where authorized while the identical group disclosure fails
- unknown legacy membership remains preserved but protected/quarantined
- provider route denial still applies after owner-private retrieval
- concurrent tool calls cannot exchange authority
- grants remain non-transitive across read/summarize/quote/send/persist/export
- every provider/tool/send/mutation has an envelope, decision, trace, and terminal or explicit-unknown receipt
- clean restart cannot resurrect revoked policy or authority generations

### Remaining owner decisions after this review

- **Approved 2026-08-15:** the Trust Plane, prospective group-history rule, and owner authentication split
- define which classifications an owner grant can never broaden
- define step-up thresholds for bulk or highly sensitive retrieval
- define per-channel membership freshness/assurance requirements
- decide whether quarantined legacy audience-unknown data is permanent or has an owner-reviewed remediation path
- choose default envelope/lease lifetimes and reauthorization checkpoints

## Major-step cross-review: physical data and evidence architecture

Date: 2026-08-15

Initial verdict: both reviewers conditionally approved a transactional core plus bounded specialized stores and rejected fully separated causal ledgers and a monolithic all-state database.

Reconciliation verdict after direct cross-review: **joint go for A-prime as the first-release physical design**, subject to release-blocking durability, encryption, IPC, integrity, backup, and recovery contracts. This is an architecture go, not a migration/cutover go.

### Resolved reviewer disagreement: canonical memory placement

The security review initially kept canonical memory in a separate `memory.db` with core-intent, memory-applied-pending, core-receipt staging. The software architect challenged this because Gateway would own both files with the same process, credentials, keys, and lifecycle.

The reconciled design co-locates canonical memory metadata/provenance/ACL state in `core.db`, while memory plaintext/large content stays in encrypted blobs and FTS/vector remains a rebuildable projection. Both reviewers agree this is simpler and safer because visibility activates atomically with source edges and security constraints. A separate file under the same authority would be isolation theater plus a real consistency gap.

Future physical memory separation is gated on a measured different writer, key authority, erasure/retention lifecycle, performance/backup SLO breach, or demonstrated corruption isolation need. The intent/receipt protocol remains a design option, not dormant shipped code.

### Joint physical boundaries

- `core.db`, Gateway only: causally coupled Interaction, Trust, consequential Evidence, Delivery, Workload, config/policy generation, blob metadata, and canonical Memory records.
- Edge durable spools/outboxes: native events not yet durably accepted by core and transport recovery state.
- encrypted blob store: raw and transformed content, media, memory content, large provider/tool artifacts, immutable non-secret snapshots.
- search/vector: sensitive but disposable candidate projection, never authority.
- `control.db`, Overseer only: desired/observed lifecycle state and durable evidence outbox.
- workload-local state only when truly internal and registered; central effects remain in core.

### Release-blocking corrections and tests

- Blob durability must precede every committed reference; kill tests cover each write/fsync/rename/commit boundary.
- Core/spools use WAL plus `synchronous=FULL`, foreign keys, bounded busy timeouts, explicit checkpoint/disk alarms, and strict database-wide schema versions/migrations.
- Canonical IDs are account-epoch/conversation/event-kind qualified; digest conflicts preserve/quarantine instead of overwrite.
- Processing never precedes durable interaction history; all relationships/raw refs/work item commit together or not at all.
- External calls and sends always have prior decisions/intents; uncertain outcomes become `unknown` and are never blindly replayed.
- Delivery authority/audience is fresh immediately before send; stable client IDs survive retries where the platform supports idempotency.
- Memory version/source/ACL/trace/projection activation is one core transaction after encrypted content is durable; incomplete/quarantined/erasure-pending state cannot materialize.
- Search returns IDs only and stale/malicious candidates are rejected by canonical authorization.
- Producer IPC is authenticated, role-limited, versioned, bounded, sequenced, replay-safe, and cannot accept raw SQL/paths or caller-created authority.
- Direct Overseer/agent database query/prune paths are removed; typed redacted health APIs replace read-only SQL access.
- Disk-full and corruption scenarios fence effects, preserve capture where possible, alert, and never purge or auto-repair.
- Raw and every transformation remain immutable separate objects with provenance.
- Global plaintext-hash blob addressing/cross-domain deduplication is forbidden; use scoped keyed IDs, randomized AEAD, and granular wrapped data keys.
- Backups/restore include Edge/outbox state and a signed generation manifest; projections rebuild; keys are separate and erasure-monotonic.
- Performance tests at twice expected peak measure core commits, WAL/checkpoint behavior, backup/restore, and integrity duration before accepting SQLite.

### Migration/backup consequences

Import order is raw/media/config evidence to encrypted blobs; principal/domain/conservative Trust state to core; interactions/causal edges; canonical memory versions/provenance/ACL; control/workload evidence without automatic reactivation; then projection rebuild. Every source object retains a migration receipt and ambiguity remains quarantined.

Normal backup generations may be coordinated with a brief write fence while Edge stays capture-only and outbound is fenced. Cutover uses stopped writers. A backup is complete only after remote verification and manifest closure; upload alone is not success. Blank-root restore runs with all external effects fenced.

### Historical disaster-recovery recommendation — superseded 2026-08-16

Both reviewers reject mandatory synchronous second-failure-domain durability in first release unless the owner explicitly requires zero loss after complete host destruction.

Joint distinction:

- acknowledged/captured state has RPO 0 for process/power/reboot failures on a recoverable fsync-honoring primary filesystem;
- complete loss of the primary host recovers only to the newest verified off-host generation;
- a visible recovery watermark defines the actual catastrophic-host RPO.

The security reviewer recommends a 15-minute catastrophic-host-loss objective with five-minute backup attempts and immediate immutable-blob uploads. The architect considered up to one hour acceptable with a simpler cadence. The primary recommendation adopts the stricter 15-minute objective. Literal RPO 0 across host destruction would require synchronous remote acknowledgement and a separate availability/latency decision.

## Historical backup/recovery follow-up — superseded 2026-08-16

Date: 2026-08-16

Owner proposal: save memory daily in local Git, push the latest state weekly, and create an encrypted off-host backup hourly.

Joint verdict: **conditional go for layered local/off-host backups; no-go for any live-state snapshot in Git.**

### Why Git is excluded

- The source/runtime audit already found a staged private runtime archive containing conversations, databases, contacts, sessions, and bridge state. Git-backed memory would deliberately recreate that failure mode.
- Git history keeps prior blobs reachable and makes retention, pruning, and provable/cryptographic erasure harder.
- Changing database/ciphertext snapshots create opaque repository growth and operational churn.
- Git has no concept of SQLite/WAL consistency, producer cutoffs, cross-store blob reachability, manifest closure, key availability, or restore verification.
- A successful push proves neither decryptability nor restorability.
- Restoring “memory” alone omits causally required interactions, Trust state, delivery/effect records, Edge spools, workload/control state, and encrypted content.
- Even encrypted Git content reveals sizes, timing, and churn. Git LFS changes mechanics, not recovery correctness.

Source Git remains essential, but for a different recovery input: exact signed releases, migrations, schemas, recovery tooling, docs, and sanitized templates. Every release that can write a new state schema must be durably off-host before activation; weekly source pushes are unsafe.

### Corrected recovery layers

1. **Ordinary crash:** systemd restarts processes; WAL and durable spools replay. Overseer reconciles desired lifecycle and can fence effects/capture-only mode, but neither component chooses/restores an older state generation.
2. **Fast local recovery:** one coordinated daily generation on a different physical disk. Same-disk copies are convenience rollback only.
3. **Host disaster:** encrypted/versioned backup repository in a separate failure domain. Attempts every 30 minutes provide headroom for a one-hour verified-watermark objective; actual RPO is always the current watermark age.
4. **Software recovery:** install the exact signed/tagged release matching the backup schema, then restore state. Source and state are separate recovery inputs.

### Release-blocking verification and authority

- Validity requires SQLite backup-API snapshots, Edge/workload cutoffs, every referenced blob, hashes, schema/config/policy/key/erasure generations, and reconciled pending intents.
- Upload the signed/MACed manifest last; only manifest closure advances the recovery watermark.
- Verify every generation, including real decryption and SQLite integrity. Run a daily disposable restore and storage-release/periodic blank-host drills with all effects fenced.
- Backup writers should be append-only where possible; pruning uses separate operator credentials.
- Store at least two recovery-key copies separately from ciphertext and the primary host.
- Overseer may detect, fence, restart, and recommend. It may not select a backup, roll state backward, reactivate old authority/jobs, or release outbound I/O.
- Restore requires step-up operator authentication, explicit generation selection, integrity/reconciliation, a signed recovery record, capture-only boot, and deliberate outbound release.
- Retain multiple generations because the newest may already contain propagated corruption.

### RPO clarification

Both reviewers accept a one-hour catastrophic-host-loss objective if the owner explicitly accepts non-zero disaster RPO. Hourly scheduling alone is not a one-hour guarantee; the meaningful measurement is the age of the latest verified closed generation. The proposed 30-minute attempt cadence with one-hour degradation threshold is the simplest corrected policy.

## Final owner decision: local-only first-release recovery

Date: 2026-08-16

The owner selected local backups for first release and deferred remote/off-host backup to a later reviewed module.

Both reviewers returned a go/conditional go with these binding qualifications:

- Local-only is an explicit product-risk acceptance, not disaster recovery.
- Preserve durably captured state across process crashes, restarts, reboots, and recoverable local-filesystem failures.
- Recovery is limited to surviving local media. No first-release RPO or recovery claim exists for theft, fire, total host loss, total primary-disk loss when all copies share it, malicious destruction of every local copy, or destruction/compromise of all local media.
- “No memory may be lost” remains absolute for migration/cutover and durably captured state while the local recovery media survive; it is not a total-site-loss guarantee.
- Do not ship dormant remote adapters, schedules, configuration, or RPO monitoring. Keep only a transport-neutral generation/manifest boundary for a future reviewed module.
- Never place live state or encrypted state snapshots in Git. Git restores the exact release/migrations/schema; the backup repository restores state.
- Every live-writing release must be recoverable from Git before activation.
- systemd owns automatic process restart. Overseer observes/reconciles lifecycle and fences effects/capture-only mode. Neither automatically selects or restores a backup.
- Back up the entire recovery unit, not semantic memory alone.
- Create a coordinated generation at least daily and before every migration/upgrade/cutover/high-risk state change.
- Prefer an encrypted/versioned separate physical local disk. Same-disk backup is accepted only with an explicit convenience/logical-corruption limitation.
- Verify every generation completely. Perform a daily disposable restore, a full restore before first release and after storage/key changes, and at least quarterly thereafter.
- Retain at least 14 daily generations and the last verified pre-migration/pre-upgrade generation until its observation gate closes.
- Recovery keys survive independently of the primary data disk and have at least two separately stored, restore-tested copies.
- Restore requires action-bound step-up authentication, explicit known-good generation selection, integrity/reconciliation, inert restored jobs/leases/authority, a signed recovery record, capture-only boot, and deliberate outbound release.

The current normative policy is in `session-context/2026-08-15-target-architecture-decisions.md`. Earlier off-host RPO proposals above are review history only and must not appear in the final released architecture/operations documentation.

## Major-step cross-review: Controlled Egress and Model Fabric

Date: 2026-08-16

Both reviewers returned a **conditional go for the hybrid approach**:

- trusted, bounded, latency-sensitive model/media calls pass through one logical in-Gateway Controlled Egress boundary;
- risky, long-running, persistent, or independent network tools run as supervised workers with default network denial and enforceable scoped grants;
- first release does not build a separate broker or generic plugin/anonymization framework;
- Gateway is explicitly trusted first-party code and is not claimed to isolate credentials from its own compromise.

### Joint corrections

1. Record a generic action intent before materialization. Record the concrete provider-attempt intent only after exact source objects, effective handling, transform, route, policy generation, and one-use child envelope are known.
2. Keep ContextDomain, owner-private status, audiences, and grants in Trust. They are orthogonal to the four handling classes `public`, `protected`, `restricted`, and `host_only`.
3. Unknown data is `restricted + unclassified` with no external route. Credentials, platform authentication, cryptographic/recovery material, and session-ratchet state are non-model-materializable hard denies.
4. Classification covers the entire request, not only the newest message. A remote model cannot classify material in order to authorize sending that same material to itself.
5. Strict profiles bind the actual processor/tenant/account, endpoint/network policy, allowed classes/tags, retention/training/jurisdiction assertions, credential alias, transforms, budgets, trace policy, and generations. Provider gateways do not hide the actual processor chain.
6. Callers request capabilities/constraints and may prefer a registered profile; they never choose raw endpoints, headers, credentials, tenants, or unregistered model strings.
7. Every retry/fallback is a separate current-authorized attempt within a pre-authorized route set. Availability cooldown never changes authorization, and hedged multi-provider disclosure is omitted.
8. Provider/tool outputs inherit restrictions and provenance and cannot mint authority. Tool invocation, memory persistence, and delivery remain separate decisions.
9. External telemetry receives no exception. Delete Langfuse and keep content-bearing evidence local.
10. Overseer never calls models directly. Channel transport remains governed by Delivery rather than being folded into Model Fabric.

### Boundary limitation and broker trigger

An in-process boundary prevents accidental architectural bypass but is not a sandbox against Gateway compromise. Move policy re-evaluation, credentials, and network authority into a separate Egress Broker only when a concrete requirement appears: untrusted executable extensions, multiple OS trust domains, third-party provider-capable workloads, credentials hidden from Gateway, independently auditable egress, or enforceable per-provider isolation. Until then, a dormant broker would add critical-path failure, IPC/payload transfer, backpressure, and crash-reconciliation complexity without delivering the full claimed isolation.

### Joint release blockers

- no direct network/provider/credential/dynamic-plugin path outside enumerated channel, Controlled Egress, and constrained-worker boundaries;
- strict configuration with no implicit provider substitution or stale profile resurrection;
- complete class/tag/profile and owner-read-all-versus-route-denial matrix;
- sentinel non-leakage, crash/unknown-outcome, transaction-stall, fallback, redirect/DNS/SSRF, revocation, credential-isolation, multimodal provenance, output-taint, worker-sandbox, and no-external-telemetry tests;
- removal, not disabling, of external Langfuse, Overseer model calls, direct memory/media/tool provider construction, provider inference/fallback shortcuts, global SDK mutation, and unsupported routes;
- no broker, generic anonymizer/plugin framework, automatic declassification, policy DSL, service mesh, hedged calls, cross-provider response cache, arbitrary endpoint, or dormant future provider/tool code.

The owner approved the normative design on 2026-08-16. It is recorded in `session-context/2026-08-15-target-architecture-decisions.md`.

## Final major-step cross-review: failure containment, operations, and release scope

Date: 2026-08-16

Both reviewers returned a **conditional go for additive scoped operational fences plus typed desired/observed reconciliation**. They rejected a global workflow/state-machine runtime and Kubernetes-style CRD/controller platform.

### Joint corrections

1. Use distinct `EDGE_CAPTURE`, `CORE_INGEST`, `PROCESS`, `EGRESS`, and `DELIVER` capabilities. Administrative mutation is a per-action step-up permit plus absence of an emergency fence, not an open gate.
2. Fences are additive denies with fixed scopes/issuers/proof. No actor opens a gate or clears another issuer's fence. Operational state never grants Trust authority.
3. Overseer may add fences, stop/restart registered units, and clear only its own transient fences after fresh proof. Security/integrity/recovery/migration/cutover/generation fences are sticky and owner-cleared.
4. Gateway/core owns semantic commands/effects/Trust leases; Overseer/control owns desired/observed lifecycle and reconciliation leases; systemd owns processes; workers own private checkpoints; Edge owns unaccepted durable events.
5. Exactly one coordinator owns retry for each operation. A transmitted or possibly transmitted side effect becomes terminal `external_effect_unknown` unless stable idempotency or authoritative reconciliation proves retry safety. Restart never resets it.
6. Health is a timestamped, evidence-sourced, generation-bound fact set with explicit `unknown`, not a boolean. Health is read-only; actions/probes are separately recorded.
7. Config/policy loading is pure and strict; activation is explicit compare-and-swap. Overseer detects drift/fences/restarts but never rewrites state.
8. Replace generic Markdown/LLM runbook interpretation with a small typed reconciler set whose success requires observed postconditions.
9. Delete executable features outside release scope but migrate their history as inert protected evidence.

### First-release recommendation

- ship WhatsApp and Telegram only if both pass identical core contracts, plus owner CLI;
- ship explicit modality matrices, reactive memory/reminders, named persistent workloads, selected provider routes/tools, local recovery, and typed Overseer control;
- delete Discord/Feishu, HTTP/webhook control, external telemetry, generic runbooks/event buses, unsupported routes/tools, and compatibility/host paths;
- omit consciousness, autonomous speak-up, and persona-evolution execution; preserve static persona and migrate historical state inertly. Later proactivity becomes an ordinary leased persistent workload.

### Joint release blockers

- signed release allowlist; clean non-editable install and strict artifact manifest;
- full clean-clone CI/type/lint/package/architecture/config/residual gates;
- common channel capture/audience/media/delivery/receipt/replay/reconnect/degradation contracts;
- complete fence/reopen/control-loss/failure-matrix and retry/unknown tests;
- crash/fault injection across every durability/effect/control boundary;
- two lossless, non-widening migrations and one blank-root recovery;
- 24-hour expected-load soak and at least two hours at twice expected peak;
- quiesced fenced cutover with forward-only commitment;
- 14-day/100-interaction post-cutover observation plus channel/modality/workload/failure exercises, three verified generations/restores, one blank-root recovery, zero unexplained divergence/access widening/integrity failure, and explicit disposition of all unknown effects before old-bundle destruction.

The operational proposal is approved. The earlier recommendation to omit autonomous consciousness/speak-up from the architecture program is superseded by the owner's 2026-08-16 correction and the follow-up cross-review below. Persona evolution remains omitted completely as an executable capability.

## Major-step cross-review: required proactivity and persona-evolution removal

Date: 2026-08-16

The owner required consciousness and speak-up to remain target capabilities even if postponed, while explicitly permitting persona evolution to be omitted completely. Both independent reviewers returned a **conditional go** for rebuilding proactivity as a standard persistent workload and a **no-go** for retaining the legacy implementation in parallel.

### Timing verdict

Both reviewers recommend a mandatory Phase 2 immediately after core stabilization unless the owner requires proactive continuity on cutover day. The reactive core may be called stable first, but the overall architecture program remains incomplete until the Proactivity Workload is live across its approved scope and passes its observation gate. This milestone belongs in the normative design and implementation plan with an owner, start trigger, and objective completion gates; dormant legacy code is not an acceptable reminder.

The owner approved this recommendation on 2026-08-16: core cutover first, followed by mandatory Proactivity Phase 2. Day-one proactive continuity is not a cutover requirement.

If day-one continuity is required, the safe first form is proposal-only with action-bound owner approval. Otherwise activation proceeds after core stabilization through inert/shadow, proposal-only, and narrowly approved autonomous stages. No stage widens automatically.

### Joint corrections

1. `assistant.proactivity` has no ambient owner read-all authority. Every observation grant is source-domain and purpose specific; source ACL/classification taints derived observations, memory candidates, and proposals. Shared-group output never inherits owner-private cross-domain context.
2. A proposal carries content and provenance, not delivery authority. Membership, identity, audience, revocation, policy, classification, budget, cooldown, fences, and delivery lease are freshly resolved immediately before the effect.
3. Trust/Gateway, not the workload or Overseer, issues or renews its lease. The workload has no direct core/projection/channel access, channel/model credentials, arbitrary host mounts, or unrestricted network.
4. Budgets, proposal IDs, approvals, delivery intents, attempts, receipts, and unknown outcomes are canonical/transactional; private checkpoint state cannot be the sole dedupe or effect authority.
5. Current `sent` speak-up rows are dispatch evidence, not confirmed delivery: legacy code marks them sent after publishing to an in-memory bus. Migration must reconcile platform/archive receipts or classify them as inert `external_effect_unknown`; they are never replayed.
6. Pending proposals, approval codes, timers, scheduler state, old desired-running state, and legacy leases migrate cancelled/inert. No historical authority becomes a Phase 2 command.
7. The replacement uses ordinary Workload, Trust, Controlled Egress, Memory, Action, Delivery, and Evidence contracts. It does not recreate its own bus, memory database, approval framework, scheduler, or transport behind a workload label.

### Persona-history disposition

Remove every persona-evolution execution path. Preserve one owner-signed static persona generation and prove historical files cannot influence runtime materialization. Preserve exact content/diff and provenance for applied changes and proposals actually shown to the owner; hashes alone cannot explain an applied change.

Routine no-proposal scans, duplicate renderings after normalization, scratch/model intermediates, caches, and unused drafts without unique evidence may be deleted after every source artifact receives a manifest-backed disposition. Feature execution tests/docs disappear; migration/reconciliation tests and generic inert evidence contracts remain only where recovery still depends on them.

### Hard gates added by the reviewers

- clean-release scan finds no legacy consciousness middleware/bootstrap/timer/provider/delivery/config path and no persona auto-apply surface;
- owner signs the exact shared-safe static persona generation and digest;
- two rehearsals reconcile proactivity/persona state, ACLs, domains, classifications, provenance, and effect status with zero unexplained loss or widening;
- restart/restore/lease expiry starts proactivity inert and never revives a pending or unknown legacy effect;
- deterministic proposal IDs and canonical budgets/deduplication survive crash and duplicate workload instances;
- fault injection spans observation, proposal commit, approval, authorization, intent, transport send, timeout, receipt, restart, and restore;
- typed status reports lease, observed domains, budget, backlog, last proposal/send/receipt, unknown effects, and fences without message content;
- shadow and proposal-only stages pass before narrow autonomous activation;
- the proactivity-specific observation gate covers at least 14 consecutive days and 100 trace-complete proposals/effects with zero cross-domain, wrong-audience, unauthorized, or duplicate delivery.

## Final specification cross-review

Date: 2026-08-16

The consolidated normative candidate is `docs/superpowers/specs/2026-08-16-yeoman-target-architecture-design.md`. Both reviewers initially returned **no-go as an implementation baseline** because several individually reasonable clauses still permitted conflicting implementations. These were specification defects, not rejections of the architecture.

Required corrections incorporated into the candidate:

1. The first non-migration live Edge event, exact producer cutoff, and `target_committed` marker are one atomic core transaction. After it the legacy release can never execute; the sealed bundle is inert migration evidence only.
2. A terminal `external_effect_unknown` is never automatically redispatched. Verified same-key resubmission is reconciliation while still `dispatched`, not retry from unknown.
3. Evidence inherits every source domain, audience intersection, classification, and grant; inspection is freshly authorized and cannot leak hidden-domain existence through shared output, health, logs, errors, or trace IDs.
4. One fixed local Secret/Key Authority owns credentials, channel sessions/ratchets, wrapping keys, epochs, rotation/revocation, and protected blank-root recovery. The current lifecycle-journal head is independent of older backup generations.
5. Logical modules own schemas/repositories/migration definitions. Storage owns transaction/blob/registry/order primitives only. A fixed backup unit obtains typed owner snapshots; Overseer observes, while restore/prune remain operator actions.
6. `legacy_unknown` is encrypted but non-materializable outside an explicit owner-private/operator audit. Rehearsal provider disclosure is synthetic/sanitized or fully Trust/Controlled-Egress authorized and recorded.
7. First-release suppression is owner-only, object/domain-bound, and step-up authorized; bulk/cross-domain suppression also binds a reviewed impact manifest.
8. Public build and private activation manifests are separate. The Core Release contains no proactivity package/config/unit; Phase 2 is the next feature milestone after the exact Core Stabilization Gate.
9. Proactivity observations/checkpoints are per-domain; taste is ordinary derived memory and cannot mutate persona/policy/system instructions. The 100-trace gate counts silence/denials without incentivizing sends but also requires receipt-backed coverage of every enabled destination/action class.
10. WhatsApp and Telegram each have an exclusive session/cursor handoff contract, durable media/cutoff evidence, and a separately signed pre-commit rollback importer outside the runtime artifact.
11. Declassification may change handling class/route only. It never adds a source domain or audience; owner-private context cannot thereby become shared output.
12. Migration deduplication is domain/key-epoch scoped and preserves every original identity, ACL, audience, provenance edge, and receipt.

The first focused re-review left two blockers: semantic/effect gates could open before the irreversible marker on a quiet channel, and owner step-up could still be misread as permission to decrypt `legacy_unknown`. The final candidate therefore:

- keeps processing, egress, delivery, timers, reminders, workload launches, and semantic/admin mutation fenced until the first non-migration live Edge event, its producer cutoff, and `target_committed` marker commit atomically; and
- permits only metadata/ciphertext-commitment audit of `legacy_unknown`. Plaintext requires a separately reviewed provenance-resolution operation with independent authority evidence and a new classified object version.

After these corrections, both the software-architecture and security reviewers returned **GO with no remaining architecture, contract, or safety blocker**. The owner approved the consolidation on 2026-08-16; it is the normative architecture until a reviewed successor replaces it. These session notes remain implementation-history evidence only and are excluded from the final released documentation set.

## Major-step cross-review: Data lifecycle and indefinite retention

Date: 2026-08-16

Both reviewers returned a **conditional go for indefinite default retention with erasure-ready provenance**. Immutable-forever-without-lifecycle is too rigid; a full retention/legal-hold/erasure policy engine is premature before the owner chooses actual deletion rights and periods.

### Joint corrections

1. “Indefinite” means retained until an explicit future policy changes it, not every subsystem byte forever and not undeletable.
2. Canonical indefinite evidence includes message/media content and relationships, identity/audience decisions, deliveries/receipts, Trust generations, canonical memory/provenance, executed model/tool/workload inputs/results/effects, admitted transforms, and recovery/lifecycle receipts. Authentication/session material is never raw-message evidence.
3. Preserve exact governed semantic disclosure and accepted result once. Use ordered immutable object refs plus a versioned serializer; retain a credential-stripped serialized body only when exact reconstruction is otherwise impossible. Do not create SDK/wire/debug shadow archives.
4. Keep content availability, recall eligibility, and provenance/version relationships orthogonal. `superseded` is an edge, not a lifecycle state; suppression is not erasure.
5. Suppression removes derived memory from every ordinary recall/materialization path while preserving raw sources and protected content. `/forget` must state suppression truthfully or be removed.
6. Erasure lineage includes every material derivative. Prior declassification widens disclosure but never proves independence from a source.
7. No user-facing canonical erase operation ships in first release. Lifecycle/key/provenance structure and synthetic non-materialization tests ship so a later reviewed policy does not require canonical-schema redesign.
8. A monotonic lifecycle journal can guarantee current-installation and supported-restore non-resurrection, not forensic erasure from older generations holding usable keys. The released architecture must say so.
9. Indefinite retention is an operator capacity obligation. Reserve capture/evidence capacity, remove only rebuildable state, fence work/effects under pressure, and never silently purge or falsely acknowledge durable capture.

### Joint release blockers

- remove active 30-day reply purge, media age/delete-after-transform paths, enabled purge runbooks, arbitrary Overseer DB deletion/snapshots, misleading soft-delete language, application log-rotation code, and stale retention documentation;
- prove immutable raw/transform objects, canonical classifications, complete provenance, exact bounded disclosure evidence, and no credential/debug duplicates;
- migrate active, hidden/deleted, expired, snapshot/backup-only, and quarantined objects with explicit receipts and report already-purged legacy gaps;
- test suppression across every projection/materialization route and synthetic erasure across mixed-source/declassified derivatives;
- apply the newest lifecycle journal before restore key access and fail closed when it is unavailable/stale;
- fault-test capacity watermarks, capture acknowledgement, backup/migration peaks, evidence commit, and outbound fencing;
- keep owner read-all constrained to owner-private/operator restore/export/audit contexts.

### Required simplifications

Omit a retention DSL, legal holds, compliance portal, scheduled canonical deletion, generic tombstone/cascade service, backup-rewrite engine, forensic-erasure claim, and non-owner deletion policy. The first release needs truthful suppression, erasure-ready storage/provenance, and capacity safety—not a speculative compliance platform.

The owner approved the normative design on 2026-08-16. It is recorded in `session-context/2026-08-15-target-architecture-decisions.md`.

## Implementation-plan cross-review

Date: 2026-08-16

The approved architecture was decomposed into one orchestration file and seven milestone plans under `docs/superpowers/plans/`. The context rule is deliberate: each implementation task reads the normative sections named by the orchestrator, the current milestone plan, current source/tests, and only the immediately preceding safe handoff plus its transitive accepted-contract registry. Completed plan bodies and conversational history are not architectural authority.

The final plan bundle is identified by the canonical command:

```bash
sha256sum docs/superpowers/plans/2026-08-16-yeoman-rework-*.md | sort | sha256sum
```

Final bundle SHA-256: `e614e1b92e1fc7eb9aef33909edbca2e830554f2763347a8f137730614cc0067`

Normative specification SHA-256: `8201c5986e66292147e8682af506bf1978abe5338d56572b0395bf3d42affbfa`

The software architect and security engineer initially returned NO-GO on earlier digests. Their blockers drove executable corrections rather than prose-only waivers:

1. Every source/test/manifest/operator-document change now has a failing test where applicable, an implementation boundary, green verification, and a clean commit before review, signing, activation, deployment, rehearsal, observation, or milestone handoff.
2. The signed Milestone 01 receipt is imported into inert Evidence before the one-time FIDO2 first-owner enrollment ceremony creates owner authority.
3. The public-web enforcement service has exact socket/process/package/systemd/Overseer ownership, dedicated identity, lifecycle, status, fence, and typed failure contracts.
4. Modified migration tooling is rebuilt and re-signed; target compatibility is a separate commit; both final migrations and blank-root restore run against the exact final pre-cutover source commit.
5. The complete clean release is proved and signed only after release, real-state recovery, and cutover tooling commits are final. Real protected activation remains inert, both independent real recovery-key copies are exercised, and activation-dependent gates are re-signed and rerun.
6. The stabilization evaluator and inert retirement command are delivered through a reviewed, owner-authorized signed forward release before the 14-day/100-trace Core cohort starts.
7. Proactivity has Workloads-owned DDL/repository/migration state, a green provisional lane that cannot merge/install/activate before accepted Core stabilization, staged activation, and an exact 14-day/100-decision runtime cohort.
8. Final documentation cleanup may not invalidate that cohort: runtime-source closure, runtime manifest section, executable artifact, activation, and every behavioral cohort component must remain byte-identical. Only the pre-reviewed documentation inventory may change, and the exhaustive manifest is re-signed.
9. The terminal protected `m07` receipt body is immutable before post-cleanup review. Separate reviewer receipts and the final signed tag bind the released commit, normative spec, observed runtime/cohort digests, final documentation manifest, accepted-contract digest, and `next_milestone=null` without a circular mutable receipt.

Final independent decisions:

- Software architecture reviewer task `01a00731-aa0d-7591-9382-2c52becddbae`: **GO**, exclusively bound to the final bundle and normative-spec digests above.
- Security engineering reviewer task `01a00731-aa5c-7232-93c3-d165eabc5d58`: **GO**, independently reproducing both digests and finding no remaining blocking dependency inversion, uncommitted source boundary, authority/invalidation gap, or receipt/cutover inconsistency.

This review authorizes execution of the plan, not mutation of the current live runtime. Runtime cutover remains a later explicit milestone with its own owner step-up and both reviewer gates.

## Major-step cross-review: Execution Capsules

Date: 2026-08-16

Both reviewers returned a **conditional go for one Bubblewrap/systemd Execution Capsule backend**. Browser, shell/code execution, parsing/conversion, public-web retrieval, and risky external tools belong in capsules. Deterministic Gateway code, authorized in-Gateway model/media egress, and channel transport do not.

### Joint corrections

1. The current Bubblewrap wrappers are not the target: Gateway shell and browser share the host network; browser state/controller and host mounts are too broad; Overseer maintains a separate arbitrary-command sandbox.
2. Converge on one fixed host-controlled launcher and immutable registered definitions. Callers and Overseer pass only typed inputs/job IDs, never commands, paths, mounts, flags, environment, systemd properties, or network destinations.
3. Verify a dedicated/dynamic deployed worker identity. A user-manager process under the Yeoman UID must not be described as separate-UID protection; its escape consequence is a Yeoman-user compromise.
4. Bubblewrap can enforce `none`, but not `public_web` by itself. Public-web capsules remain in an isolated namespace whose only external conduit is a short-lived authenticated capability to a supervised DNS/connection/redirect enforcement point. `--share-net` is forbidden.
5. The enforcement point is an SSRF/destination boundary, not DLP. Only authorized material enters a public-web job. Authenticated browsing, uploads, forms, and mutations require fixed-destination plus separate effect authorization.
6. Chromium's inner sandbox remains active. Unauthenticated profiles are ephemeral; authenticated state is isolated per registered platform identity/account/purpose/domain. Cookies are hard-deny authentication material.
7. Persistent capsules have service/initiator/domain identity, renewable leases, registered encrypted state, stable action IDs, and inert restart/restore behavior. Restart never renews authority or blindly replays unknown effects.
8. Capsule output is untrusted and enters only through a bounded encrypted quarantine/import path and a new Trust decision.
9. The capsule boundary does not protect against kernel, Bubblewrap, systemd, browser, launcher, Gateway, root, or capsule-definition-authority compromise. Rootless OCI has substantially the same shared-kernel limitation; microVM/separate-host isolation is a later threat-model decision.

### Joint release blockers

- single-launcher/zero-alternate-path scan and launch attestation;
- deployed UID/namespace/seccomp/cgroup/mount/IPC/descriptor escape tests;
- adversarial no-network, fixed-destination, and proxy-only public-web matrix;
- browser sandbox/profile/cookie/upload/download-quarantine tests;
- resource/fork/output/decompression/capacity tests;
- lease/revocation/restart/upgrade/recovery fencing;
- crash/idempotency and explicit-unknown effects;
- persistent volume schema/key/backup/inert-restore tests;
- result import limits and downstream Trust authorization;
- fail closed when any mandatory isolation/network primitive is unavailable.

### Required simplifications

Delete the bespoke shell/browser/Overseer sandboxes, shared networking/state, host fallback, arbitrary commands/mounts, browser self-start/local HTTP control, and drifted Docker/Podman contract. Omit OCI/microVM backend code, image lifecycle, generic schedulers or policy languages, dynamic container properties, in-capsule package installation, public-web uploads, and dormant workload types.

The owner approved the normative design on 2026-08-16. It is recorded in `session-context/2026-08-15-target-architecture-decisions.md`.
