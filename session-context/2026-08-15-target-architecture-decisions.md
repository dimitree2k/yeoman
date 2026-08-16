# Yeoman target architecture decisions

Date: 2026-08-15

Status: Rolling session note. This captures approved architectural decisions while the full target design and migration strategy are still being developed and cross-reviewed.

Related audit: `session-context/2026-08-15-architecture-audit.md`

## Product identity

- Yeoman is one coherent personal assistant, not a general multi-tenant assistant platform.
- It serves multiple authorized people across multiple channels, platforms, and modalities.
- It runs both temporary and persistent workloads.
- Model/provider choice must remain flexible and policy-controlled.
- Traceability is a core product requirement, not optional diagnostics.
- The architecture must support later modules such as anonymization without redesigning the turn path.

## Approved architectural direction

Use a **modular core plus supervised workload fabric**:

- The Gateway remains the conversational data plane and contains the small latency-critical Turn Kernel.
- Persistent, expensive, failure-prone, resource-heavy, or independently deployable work runs as supervised workloads.
- Overseer remains the Kubernetes-like control plane. It observes desired versus actual state and reconciles services, workloads, deployments, configuration, policy generations, and storage health.
- Overseer is not in the critical path of an ordinary reply and does not decide conversational meaning.
- systemd remains the machine-level process supervisor used by Overseer.
- Avoid a general microservice mesh, distributed broker, dependency-injection framework, or full Kubernetes clone.

## Target logical planes

1. **Edge Plane** — channel adapters preserve native platform semantics and translate them into canonical interaction events.
2. **Interaction Ledger** — authoritative raw communication history and message relationships.
3. **Trust Plane** — identity resolution, context-domain resolution, audience calculation, capability policy, and data/model constraints.
4. **Execution Plane** — Turn Kernel plus the supervised Workload Fabric.
5. **Memory Plane** — authorization-aware working, session, situation, durable, and distilled memory.
6. **Control Plane** — Overseer reconciliation and owner-approved mutation.
7. **Evidence Plane** — correlated internal traces, decisions, tool/model calls, mutations, and delivery receipts.

## Execution envelope

Every live turn and background workload receives an immutable execution envelope containing at least:

- canonical principal
- channel and destination context domain
- audience
- owner-private versus shared context
- granted capabilities
- accessible memory domains
- data classifications and handling requirements
- permitted model/tool routes
- effective policy/config generations
- trace ID

Modules do not choose their own permissions, memory reach, models, or trace behavior.

## Memory isolation and owner authority

- A conversation/group is a memory security domain.
- The same person appearing in multiple groups does not connect those groups' facts.
- Canonical contact/entity identity is separate from memory visibility.
- Facts remain isolated to their origin domain by default.
- Cross-domain access requires an explicit owner-created grant.
- Only the owner has read-all authority.
- Owner-wide access is available only through owner-private or authenticated operator contexts.
- A shared destination remains restricted to its own disclosure permissions even when the owner initiated the request.

Memory must separate independent dimensions:

- lifetime: turn, session, situation, durable, distilled
- meaning: episodic, semantic, procedural, emotional, reflective
- visibility: origin domain, owner-private, explicitly shared

A memory's semantic type must never determine its visibility.

Every durable memory must retain immutable provenance back to source interactions and transformations.

## Interaction and message provenance

All channel messages must be traceable across inbound and outbound directions.

The canonical interaction record must retain:

- platform/channel/conversation/message identifiers
- canonical sender principal and original platform identity
- destination and audience
- direction and event type
- replies, quoted-message edges, thread roots, mentions, edits, deletions, and reactions
- raw message content
- media and transformations
- delivery attempts and platform receipts
- originating or consuming workload trace
- links to derived memory, tool calls, and outbound responses

Inbound interactions are durably recorded before processing. Outbound interactions are recorded as deterministic intents before delivery and completed with actual platform receipts afterward.

Raw messages and media are retained indefinitely for now. Future retention, deletion, tombstone, and cryptographic-erasure behavior must be an explicit policy decision rather than an implicit expiry.

## Evidence versus derived state

- The Interaction Ledger records what happened.
- The Trace Ledger records why Yeoman acted.
- Memory records derived knowledge.
- Memory is not the historical source of truth.
- Incorrect memories may be corrected or superseded without rewriting interaction history.

## Non-negotiable invariants

1. No message is processed without a durable interaction record.
2. No side effect exists without a trace and receipt.
3. Identity linkage never implies memory sharing.
4. Owner-wide reading never overrides a shared destination's disclosure boundary.
5. Background workloads use the same authorization path as live turns.
6. Models and tools receive only the context authorized for that execution.
7. Overseer controls lifecycle, never conversational meaning.
8. Raw interactions, derived memory, and execution traces remain separate but correlated.

## Approved migration and cutover direction

Approved by the owner on 2026-08-15 after independent software-architecture and security reviews. The initial long-lived strangler/canary proposal was rejected. Downtime is acceptable, legacy and target memory policies have incompatible security semantics, and the released repository must contain no stale legacy code or documentation.

Recommended direction:

- build the replacement in an isolated source worktree/branch and fresh state root
- compare it through deterministic offline replay and side-effect-free provider evaluation
- stop destructive retention behavior before the preservation baseline
- rehearse the complete state migration at least twice from independent consistent snapshots
- build the final release from a clean clone with legacy code, flags, config keys, tests, docs, and superseded specs already removed
- perform one quiesced cutover while Gateway, Overseer, cron, extractors, backfills, and all other state writers are stopped
- keep channels in durable capture-only mode and outbound delivery fenced during migration
- migrate the final delta, boot the new stack with external I/O fenced, and run synthetic verification
- make the first live interaction consumed by the new Gateway the irreversible commitment point
- before commitment, rollback restores the signed old release and untouched old state and replays the durable ingress inbox
- after commitment, recovery is forward-only unless a separately verified reconciliation migration can preserve every post-cutover interaction
- keep old executable/state only as an encrypted external rollback bundle; never ship it in the released source tree or runtime Git

The first live interaction consumed by the new Gateway is approved as the irreversible commitment point. Before that interaction, rollback restores the signed old release and untouched old state and replays the durable ingress inbox. After it, recovery is forward-only unless a separately verified reconciliation migration preserves every post-cutover interaction, intent, receipt, memory mutation, and trace.

The exact gates and findings are recorded in `session-context/2026-08-15-architecture-cross-review.md`.

## Reviewer coordination

- Software architecture: Codex task `01a00731-aa0d-7591-9382-2c52becddbae` (`Yeoman Architecture Migration Review`)
- Security engineering: Codex task `01a00731-aa5c-7232-93c3-d165eabc5d58` (`Yeoman Security Architecture Review`)
- Both reviews were read-only and made no source/runtime changes.
- These tasks should be consulted again at each major design and implementation gate.

## Decisions still open after migration approval

- the live observation threshold before deleting the encrypted external rollback bundle
- provider/tool/telemetry trust classifications and the future anonymization boundary
- remote/off-host backup, capacity, and future deletion controls for indefinite retention
- the exact first-release channel and workload scope; unsupported legacy paths must be deleted rather than left dormant

## Approved Trust Plane design

Approved by the owner on 2026-08-15 after both the software-architecture and security reviewers returned a conditional go and their corrections were incorporated. The Trust Plane remains a logical in-Gateway boundary with three cohesive responsibilities, not a network service or general IAM framework:

1. identity and membership registry
2. pure deterministic authorization evaluator
3. envelope and authorization-decision persistence

### Identity and domain model

- `Principal` is an immutable Yeoman identifier for a human, service, workload, or operator role/session.
- `PlatformIdentity` keys include platform, channel account/tenant, native identity, and native account epoch. A platform identity has at most one active principal link per epoch.
- Identity links are evidence-bearing, assurance-rated, time-versioned, reversible, and independently revocable. Conflicting active links quarantine privileged authority.
- Linking identities never merges memory scopes or context domains and never rewrites historical ACLs.
- Historical interactions retain native identities plus the identity-link and policy generations used at event time.
- `ContextDomain` is an immutable but retireable security boundary for one channel conversation lineage. Ordinary membership changes keep the same domain; material platform re-key/recreation creates a new domain with an explicit lineage edge.
- Actor, requester, service/workload, step-up operator, destination, and audience are represented as a typed initiator chain and separate destination/audience records.

### Membership and disclosure

- Store each normalized membership snapshot once as an evidence-rated `MembershipVersion`; interactions reference it and retain message-specific native recipients and unresolved-recipient evidence.
- Membership evidence records source, observation time, completeness, assurance, and platform account epoch.
- Authorization uses the conservative platform-reachable audience, not receipt-confirmed readers. Intended audience, reachable audience, unresolved recipients, delivery receipts, and read receipts are separate facts.
- Membership expansion is prospective. New members do not automatically receive old protected or unclassified group history. Removed members lose future assistant-mediated access. Material explicitly classified as releasable to future members may follow a separate rule.
- Missing or partial membership permits durable ingress capture but blocks protected historical retrieval, shared disclosure, mutation, and unapproved external egress.
- The core rule is conjunctive:

  `permit = source-domain access AND outbound-disclosure permission AND action/data/route permission`

- Source-domain access comes only from the same domain, owner-private read-all, or an explicit cross-domain grant. Being a person who appears in both domains is never sufficient.

### Execution authority

- The Trust Plane is the only issuer of immutable execution envelopes and authorization decisions.
- Immutable envelopes are historical evidence, not perpetual authority. They contain issue/expiry time, authentication assurance, identity-link, membership, grant, policy, config, provider-policy, and revocation generations.
- A revoked link, grant, membership, or policy generation invalidates later uses without rewriting the historical envelope.
- Revalidation occurs at security boundaries: context materialization, high-risk retrieval, provider/tool egress, mutation/persistence, delivery, and after long waits. Group audience is resolved again immediately before send.
- Long-lived workloads use renewable, narrowly scoped leases. Background work has a service/workload principal and initiator chain; it never defaults to owner authority.
- Child envelopes may only narrow domains, resources, actions, tools, audiences, data classifications, provider routes, lifetime, and use count.
- Tool instances contain no shared mutable authority.
- Unknown or malformed identity, domain, audience, classification, scope, route, or grant data fails closed, while the raw inbound evidence is still durably captured.

### Owner authority and step-up authentication

- Normal bounded owner conversation and cross-domain retrieval require both a verified owner platform identity and a positively computed owner-private destination whose reachable audience contains only the owner and assistant endpoints.
- Owner retrieval does not authorize provider/tool/telemetry egress. Egress is independently checked against the highest classification in the materialized context.
- Identity link/revoke, grants, exports, deletion, migration/cutover, key/config/policy changes, bulk enumeration, and designated highly sensitive retrieval require a short-lived, replay-resistant step-up assertion from a trusted local interface using a passkey, hardware-backed key, or equivalent.
- Step-up assertions bind to the exact action, targets/diff, nonce, trace, and expiry; there is no reusable global admin mode.
- Emergency recovery uses offline recovery material, rotates affected authority, and creates a separate audit trail.

### Grants and derived data

- Cross-domain grants are owner-authorized, explicit, non-transitive, purpose/workload-bound, destination/audience-bound, time-bounded, versioned, and revocable.
- A grant names exact source domains/resource selectors, data classes, operations, destination/audience, workload policy ID, issuer, reason, validity, and version.
- Operations distinguish at least `read_raw`, `read_derived`, `summarize`, `quote`, `send`, `persist_in_destination`, and `export`. Permission to read or summarize does not imply permission to persist or disclose.
- Derived memories, summaries, and model results retain every source edge, the most restrictive classification, the intersection of source audiences, and all domain/grant constraints. Anonymization may relax restrictions only through a separate recorded declassification decision.
- No grant is inferred from identity linking, overlapping group membership, semantic memory type, current legacy reach, or model output.

### Evidence and migration consequences

- Authorization decisions are append-only and reference exact envelope/rule/grant generations, returned object IDs or a digest, allowed scopes/routes, and denial reason.
- Trace scope identifiers and denials use protected opaque references so ordinary callers cannot discover hidden domains. Periodic tamper-evident checkpoints are sufficient; every local row need not be signed.
- Historical WhatsApp audience membership cannot be reconstructed from current rosters. Migrated records preserve known origin domains but mark missing historical audiences as `legacy_unknown`/`membership_unknown`.
- Unknown legacy audience or origin remains domain-protected or owner-private/no-provider quarantined. Current contact merges, semantic scopes, sessions, grants, and background authority are not imported as target ACLs.
- Every legacy object still receives a migration receipt and is never silently dropped or widened.

Review tasks:

- Software architecture: `01a00731-aa0d-7591-9382-2c52becddbae`
- Security engineering: `01a00731-aa5c-7232-93c3-d165eabc5d58`

## Approved physical data architecture

Approved by the owner on 2026-08-16 after both reviewers recommended **A-prime: one transactional core plus genuinely separate owner/failure domains**. This preserves logical layer boundaries without creating cross-database sagas between facts that must be atomic.

Rejected alternatives:

- Separate physical Interaction, Trust, Evidence, Delivery, and Memory databases would create intent/receipt choreography and reconciliation without a different writer or key boundary.
- One monolithic database for all state would mix Gateway, Edge, Overseer, projections, blobs, and workload-private state and enlarge contention and corruption scope.
- PostgreSQL, a broker, generic event store, ORM, distributed transaction manager, or synchronous remote replication are not justified for first-release scale.

### Physical ownership

| Store | Authoritative contents | Writer |
|---|---|---|
| `core.db` | Interactions and causal edges; identity/domain/membership/grant state; envelopes and authorization decisions; consequential model/tool/egress intents and results; delivery intents/attempts/receipts; workload commands and effects; config/policy generations; canonical memory version metadata, provenance, ACL/classification/grant taint, supersession/tombstones; protected blob metadata | Gateway core storage owner only |
| Edge spool/outbox | Native events not yet accepted by core, producer epoch/sequence, raw-event staging, and transport idempotency/receipt recovery | One per producer process/account where upstream durable replay is insufficient |
| Search/vector projection | Rebuildable FTS/vector candidates and copied non-authoritative hints | Gateway projection worker |
| `control.db` | Overseer desired/observed lifecycle state, leases, reconciliation history, durable evidence outbox | Overseer only |
| Encrypted blob store | Raw native events/messages/media; immutable transformations; memory content; large model/tool requests/results; non-secret config/policy snapshots | Restricted blob API; producers cannot choose final paths |
| Workload-local store | Only genuinely private internal workload state with declared owner/schema/backup/migration | Registered workload owner; central commands/effects remain in core |

Interaction, Trust, Evidence, Delivery, Workload, and Memory remain separate logical modules, table/API ownership boundaries, and migration modules even when their causally coupled canonical records share `core.db`. No module may issue arbitrary SQL against another module's tables.

### Why canonical memory belongs in core for first release

- Gateway is the sole writer for both the source interaction/authorization evidence and memory creation.
- Atomic co-location allows a memory version to become visible together with every source edge, ACL, classification, grant taint, transformation/decision trace, supersession state, and projection work item.
- Memory plaintext and large derived content remain outside SQLite as encrypted immutable blobs. The core keeps protected references/digests and metadata.
- Search/FTS/vector data is sensitive but disposable and can never authorize access. It returns candidate IDs; canonical Trust state is rechecked before materialization.
- A separate canonical memory database should be considered only after a measured independent-writer, key/erasure-lifecycle, performance, backup/restore, corruption-isolation, or retention need. The reviewed intent/applied-pending/receipt protocol is the future split design, but dormant split/dual-write code must not ship now.

### Durability and atomic flows

- Authoritative SQLite stores use local durable filesystems, WAL, `synchronous=FULL`, `foreign_keys=ON`, bounded busy timeouts, explicit checkpoint/disk alarms, and strict `PRAGMA user_version` migrations. Transactions remain short and contain no provider, platform, blob, or IPC wait.
- A durable blob is created before a core reference: stage, encrypt, hash/verify, fsync file, atomic no-replace rename, fsync directory, then commit the reference. Orphan encrypted blobs are safer than committed missing references.
- Inbound capture is at-least-once. Canonical IDs include platform, Yeoman channel-account/epoch, native conversation/domain ID, native event ID, and event kind. Where a stable native ID is absent, the Edge persists a source UUID/producer epoch and sequence once.
- Replaying the same canonical ID and digest is idempotent. The same ID with a different digest preserves both payloads, quarantines processing, and alarms; history is never updated in place.
- The inbound core transaction commits the interaction, reply/mention/media relationships, membership-version reference, raw blob reference, and processing work item together.
- Every model/tool/external action has an authorization decision and intent before the call, followed by a result/receipt or explicit `unknown` state.
- Every outbound send has a deterministic stable client ID and intent before delivery. Audience/authority is resolved again immediately before send. Timeout reuses the same ID only where platform idempotency is safe; otherwise reconcile or require owner action rather than blindly resend.
- Memory extraction records provider intent first, calls the provider outside a transaction, makes the result/content blob durable, then atomically commits extraction evidence, canonical memory version, every source/ACL edge, active visibility, projection work, and intent completion. Replay uses deterministic creation keys.
- `pending`, `quarantined`, `erasure_pending`, missing-blob, or invalid-key memory is never materializable or indexable.
- Edge/Overseer/workload producers never open `core.db`; they submit typed, authenticated, role-limited, versioned, idempotent IPC records and retain durable outboxes until a core commit acknowledgment.

### Blob, projection, IPC, and failure security

- Preserve raw, normalized, transcribed/OCR, resized, anonymized, and provider-normalized representations as separate immutable objects with transformation provenance; never overwrite raw bytes.
- Avoid global plaintext-hash filenames/deduplication because they leak equality across memory domains and weaken future erasure. Use opaque or domain/key-epoch-scoped keyed identifiers, randomized AEAD, per-object data-encryption keys wrapped by domain/key-epoch keys, and authenticated metadata.
- Protect projections like memory: encrypted storage, narrow access, and canonical authorization after candidate selection.
- IPC authenticates producer identity, allowed record types/targets, schema/protocol version, producer epoch/sequence, idempotency key, size/depth/concurrency limits, and replay/conflict handling. No raw SQL, arbitrary paths, table names, or caller-minted Trust decisions.
- Disk high watermark stops expensive work and alerts. Critical watermark prioritizes durable capture where possible, stops acknowledgement/processing, fences outbound effects, and never silently purges retained data.
- Core/blob corruption fences processing and effects while safe Edge capture continues. Health checks are read-only; no automatic repair.
- Consequential causal/security evidence stays in core. Verbose debug spans and scheduler chatter remain operational telemetry so Evidence does not become an unbounded trace monolith.

### Storage governance, backup, and recovery

- A static typed storage registry declares logical name, path, owner, schema version, encryption/key policy, backup, integrity, retention, and migration policy.
- One backup coordinator creates a named backup generation with producer cutoffs, SQLite backup-API snapshots, unconsumed Edge/outbox segments, referenced encrypted blobs, schema/config/policy generations, key epochs, pending/unknown intents, and a signed/MACed manifest.
- Every referenced local blob/hash/key is verified before snapshot closure. The signed/MACed manifest is the generation's final commit marker.
- Search/vector projections are not backed up; restore rebuilds them and verifies candidate authorization.
- Recovery keys/escrow are stored separately from backup ciphertext and are restore-tested. A signed/MACed monotonic lifecycle journal is applied before key access during every supported restore and prevents normal restoration from reactivating suppressed/erased objects. It does not make an older generation forensically undecryptable to an operator who possesses usable historical recovery keys.
- Blank-root restores begin with providers, tools, outbound delivery, cron, and Overseer reconciliation fenced. Counts, hashes, causal edges, blob reachability, pending intents, producer cutoffs, and storage integrity pass before external I/O.
- Cutover/migration still uses stopped writers for one cross-store-consistent generation.

### Approved first-release local backup and recovery policy

Approved by the owner on 2026-08-16 after software-architecture and security cross-review.

- First release uses local backups only. Remote/off-host backup, remote transport, RPO monitoring, schedules, and configuration are explicitly deferred and must not ship as dormant code.
- Git contains the exact signed/tagged source release, migrations, schemas, recovery tooling, documentation, defaults, and sanitized non-secret templates only.
- Live/effective config and policy containing identities, chat IDs, grants, routes, or operational generations remain encrypted state, not source Git.
- No live memory export, raw interaction/media, database, spool, encrypted blob, backup generation, key, or real manifest enters any source/runtime/private Git repository.
- Every release/schema capable of writing live state must be recoverable from Git before activation.
- RPO 0 applies to `capture_committed`/acknowledged state across process crashes, service restarts, reboots, and power loss while the host and fsync-honoring local filesystem remain recoverable.
- First release makes no recovery or RPO claim for primary-disk destruction when all copies share it, theft, fire, total host loss, malicious destruction/compromise of every local copy, or other total loss of local media. Remote disaster recovery is a later reviewed module.
- systemd owns automatic process restart. Overseer observes/reconciles desired lifecycle, requests bounded actions, and fences outbound/capture-only mode. Neither selects nor restores an older state generation automatically.
- Create at least one coordinated local generation every 24 hours and immediately before schema migrations, upgrades, cutover, or other high-risk state changes.
- Prefer an encrypted, versioned repository on a separate physical disk. A same-disk repository protects only selected logical-corruption/operator-error cases and must not be described as a separate failure domain.
- The backup covers the entire recovery unit: `core.db`, closed Edge spools/outboxes, `control.db`, registered canonical workload stores, encrypted blobs, storage registry, schema/config/policy generations, erasure ledger, producer cutoffs, pending/unknown intents, and key-escrow references.
- Rebuildable search/vector projections, caches, ordinary operational logs, deployment caches, contaminated logs, and temporary files are excluded.
- A generation is successful only after SQLite backup-API completion, manifest authentication, database quick/integrity and foreign-key checks, schema compatibility, blob reachability/decryption, key availability, erasure-generation validation, and pending intent/receipt reconciliation.
- Backup keys/recovery material survive independently of the primary data disk. Keep at least two separately stored recovery-key copies and test them.
- The newest generation is restored and reconciled automatically into disposable storage at least daily with every external model/tool/delivery/control effect disabled.
- Before first release, after storage-schema/key-management changes, and at least quarterly, perform a blank-root recovery rehearsal using only documented inputs. Rebuild projections and prove identity, authority, storage, pending-effect, and blob reconciliation before outbound release.
- Retain at least 14 daily generations plus the last verified pre-migration/pre-upgrade generation until its observation gate closes. Never prune the last verified restore point; pruning requires step-up operator authority and emits an audit receipt.
- Restore is an authenticated operator workflow. It selects a known-good generation, verifies/reconciles it, restores leases/locks/jobs/authority inert, records the recovery, boots capture-only, and deliberately releases outbound behavior.
- Health reports the destination/failure-domain class, newest completed generation, newest verified disposable restore, watermark age, capacity, and integrity/key/blob failures.

This owner decision narrows “no memory may be lost”: it remains absolute for migration/cutover and for durably captured state while the local recovery media survive. It is not a promise against destruction of all local media.

## Cross-reviewed proposal: Controlled Egress and Model Fabric

Status: approved by the owner on 2026-08-16 after software-architecture and security cross-review.

### Boundary choice

- First release uses one logical `ControlledEgress` boundary inside Gateway for bounded, latency-sensitive, first-party model, embedding, vision/OCR, ASR, and TTS requests.
- Web access, browsing, market/calendar integrations, shell/browser automation, persistent media processing, backfills, autonomous/repeated work, and other network-risky or long-running tools run as supervised workloads. They start with network denied and receive an expiring child envelope plus OS-enforced destination/network capability.
- Channel receive/send adapters remain a separate Delivery boundary. Overseer alert transports use fixed destinations and metadata-minimal payloads. Neither is treated as model/tool egress.
- Overseer never invokes a model provider directly. Model-assisted operations are authorized supervised workloads; Overseer only observes, requests, reconciles, restarts, and fences lifecycle.
- Do not build an Egress Broker process, broker IPC, compatibility route, or transport abstraction in first release. Keep typed canonical egress records and a narrow executor port so the boundary can be extracted later without carrying dormant code.

This choice assumes Gateway contains only trusted, reviewed, first-party executable code. No downloaded/dynamic provider adapter, arbitrary skill code, untrusted plugin, browser, shell, or user program executes inside Gateway. A separate policy-enforcing broker becomes mandatory if credentials must be hidden from Gateway compromise, untrusted executable extensions gain provider access, multiple OS trust domains appear, third-party workloads need provider credentials, independently auditable egress becomes a requirement, or enforceable per-provider network isolation cannot otherwise be achieved.

### Orthogonal information model

Trust continues to own origin domain, owner-private status, authorized audiences, grants, and disclosure destination. These are not sensitivity levels.

Controlled Egress uses a small handling lattice:

1. `public`
2. `protected` — ordinary non-public data, allowed only on explicitly approved processor routes
3. `restricted` — sensitive or uncertain data, denied externally by default and allowed only by an exact route policy, potentially after a required transform
4. `host_only` — no external model, tool, telemetry, or channel disclosure

Unknown or malformed classification becomes `restricted + unclassified`, for which first release defines no external route. Effective handling is the maximum source class, union of source tags, intersection of source audiences, and conjunction of all source-domain/grant constraints.

Handling tags include `direct_identifier`, `raw_history`, `precise_location`, `health`, `financial`, `voice`, `biometric`, and immutable hard-deny tags for `credential`, `platform_auth`, `cryptographic_key`, `recovery_material`, and `session_ratchet`. Hard-deny objects are not materializable into a general-purpose model context, including a local model. Owner read-all cannot override provider denial or a hard-deny tag.

Tags may be added conservatively. Removing a class/tag or widening an audience requires a separately authorized, recorded declassification decision. Transcription, OCR, summarization, embedding, resizing, or anonymization does not implicitly declassify its source.

### Canonical request and evidence flow

1. Commit an `ActionIntent` with initiating principal/envelope, purpose, requested capability, source selectors, constraints, and budget. It contains no concrete provider claim.
2. Trust revalidates authority and the materializer obtains the exact authorized source objects. The complete payload is classified locally/deterministically: system/persona instructions, history, memories, attachments, media transforms, tool schemas/results, and derived request body—not only the newest message.
3. If policy requires a transformation, write its immutable encrypted blob and source/transform provenance. First release has no anonymizer and no automatic declassification.
4. Resolve a strict, versioned route from capability and constraints. A caller or owner may prefer an installed profile, but cannot supply a URL, header, credential, tenant, or unregistered model string, and policy may reject the preference.
5. Commit the egress decision, exact materialized object/transform references, one-use narrowing child envelope, complete pre-authorized route set, and one `ProviderAttemptIntent`.
6. Resolve only the selected credential alias and make the call outside every core transaction.
7. Commit a typed result, failure, cancellation, or explicit `unknown` outcome with exact route/processor/endpoint/policy generation, credential generation reference, source/transform references, timing, usage/cost, and available receipt.
8. Every retry or fallback is a distinct attempt under current authorization. A fallback requiring a stricter transform is re-materialized and re-decided; no hedge sends the payload to multiple providers.
9. Treat provider/tool output as untrusted, tainted data inheriting every source restriction and route provenance. It cannot grant authority, select a route, invoke a tool, persist memory, or deliver a message without the corresponding new Trust decision.

### Strict route profiles

Every enabled model/tool profile declares:

- stable profile and processor identity, execution locality, provider tenant/account, capability and modality;
- exact scheme/host/port/path or supervised public-web network policy, redirect/proxy/DNS behavior, and destination constraints;
- allowed handling classes/tags and required transforms;
- operator-reviewed retention, training, jurisdiction, and subcontractor/aggregator processor posture;
- credential alias and credential generation reference, never credential value;
- context/size, timeout, concurrency, retry, cancellation, cost, and latency budgets;
- content/metadata trace policy and policy/config generations.

Aggregator routes describe the actual permitted processor chain, not only the HTTP gateway host. Missing/unknown fields, stale generations, absent credentials, ambiguous processor identity, or no authorized route fail closed. Availability cooldown never creates authorization.

Credentials are not loaded wholesale into process-global environment or mutable SDK globals. The selected alias resolves as late as possible, never appears in an envelope, core row, blob, log, error, prompt, or trace, and cannot bleed across concurrent requests.

### Network and telemetry enforcement

- Clean-release architecture tests allow network clients, provider SDKs, DNS/socket calls, and network subprocesses only in enumerated channel adapters, Controlled Egress provider adapters, and supervised-worker packages.
- Supervised workers cannot open `core.db`, arbitrary blobs, other workers' state, or unrelated credentials. Fixed integrations receive exact destination grants. A public-web worker may reach public Internet only and must be blocked from loopback, link-local, private/control networks, metadata services, Unix sockets, unapproved ports, proxy-environment bypass, unsafe redirects, and DNS rebinding at an enforceable sandbox/network boundary.
- If a required worker cannot be constrained at the OS/network boundary, that tool does not ship.
- Consequential Evidence remains local in encrypted core/blobs. Operational logs are metadata-minimal. First release deletes external Langfuse implementation, configuration, dependency, tests, documentation, and call sites rather than leaving them disabled.

### Release gates

- Static clean-clone scans prove there is no external-call path, dynamic executable plugin load, credential resolution, arbitrary socket, `curl`, or provider SDK outside the allowlist.
- Strict config rejects unknown/shadowed/unsupported profiles and proves every enabled profile is reachable, versioned, and owner-reviewed; missing credentials never select another provider.
- A policy matrix covers every class/tag/profile combination. Unknown classification, stale authority, owner read-all plus route denial, mixed `public + host_only`, mixed-domain input, and hard-deny material all fail correctly before content decryption/egress.
- Sentinel tests prove credentials, authentication/ratchet/recovery material, hidden-domain content, and identifiers never appear in the wrong provider traffic, worker IPC, logs, errors, traces, retries, or fallbacks.
- Crash injection around action intent, materialization, transform durability, attempt intent, network write, provider response, and receipt yields only absent, pending, completed, or explicit-unknown states; no call lacks a committed decision/attempt.
- Stalled calls prove no SQLite transaction spans network I/O and core capture remains available.
- Fallback, redirect, DNS-rebinding, private-address, proxy, cost/context escalation, revocation, and credential cross-talk tests fail closed.
- Multimodal tests prove chat, embedding, OCR/vision, ASR, and TTS use the same decision lifecycle and that every transform remains a separately protected, provenance-linked blob.
- Output-taint tests prove prompt-injected results cannot add domains/audiences/providers/tools or bypass separate memory and delivery authorization.
- Worker tests prove default network denial, exact capability/destination reachability, inability to open canonical stores/credentials, result size/type validation, and lease/revocation enforcement.
- Network capture proves no first-release external telemetry connection while local consequential evidence remains complete.

### Delete or omit from first release

Delete provider inference by keyword/key prefix/arbitrary API base, first-credential fallback, process-global provider mutation, direct calls from memory/media/responder/tool modules, external Langfuse, Overseer model calls, raw provider exceptions exposed to chats, unsupported provider/profile/channel paths, and dual legacy/Controlled Egress routes.

Deliberately omit the broker process and IPC, generic provider/plugin discovery, generic anonymization/transform framework, automatic declassification, OPA/policy DSL, service mesh, dynamic route optimizer, hedged/ensemble calls, cross-provider response cache, user-supplied endpoints, and dormant tools/providers/channels. Define only the typed canonical egress contracts and the transform/declassification evidence seam required by current flows.

## Cross-reviewed proposal: Execution Capsules

Status: approved by the owner on 2026-08-16 after software-architecture and security cross-review.

### One lightweight backend

- Browser automation, shell/code execution, document conversion, archive/document/media parsing, public-web retrieval/scraping, and every risky or externally executable tool run in an **Execution Capsule**.
- First release implements exactly one Linux backend: a fixed systemd capsule template that runs a non-root Bubblewrap sandbox under a dedicated/dynamic worker identity with cgroup/resource hardening.
- Pure deterministic Gateway operations remain inline. Authorized bounded model/embedding/vision/ASR/TTS calls remain behind Controlled Egress. Channel transport remains behind Delivery. These are not put into capsules merely for uniformity.
- This is not an OCI container platform. Do not add Podman/Docker, images/registry, backend plugins, a generic scheduler, or Kubernetes-like workload APIs. Rootless OCI still shares the host kernel and would primarily add packaging/lifecycle machinery. A microVM or separate host is considered only for hostile multi-tenant/native-code workloads or a requirement to survive a namespace/kernel escape.

The capsule boundary limits an untrusted child process from reaching unauthorized files, processes, networks, credentials, state, or resources. It does not protect against compromise of the Linux kernel, Bubblewrap, systemd, browser sandbox, fixed launcher, Gateway, host root, or the capsule-definition authority. The exact deployed UID arrangement must be verified; if capsules share the Yeoman host UID, that weaker escape consequence must be stated and is not called separate-UID isolation.

### Fixed launch authority and definitions

- The caller submits a typed job referencing an immutable registered `CapsuleDefinition`; it never supplies an executable path, shell string, host/container path, mount, environment variable, Bubblewrap flag, systemd property, seccomp policy, credential, or network destination.
- A definition fixes executable and build digest, typed argument schema, lifecycle class, mounts/materialization rules, network profile, optional credential alias, state contract, resource/output limits, seccomp/capability policy, and policy/config generation.
- Gateway commits the Workload command/intent in core. Overseer may reconcile/start/stop only registered job IDs through the fixed systemd template. systemd owns the process, worker UID, cgroup, restart, timeout, and kill behavior. Neither Overseer nor model output constructs or interprets an arbitrary command.
- Capsule launch evidence binds job/envelope, service and initiating principals, context domain, definition/build/binary, mounts, network capability, credential generation, state/key generation, seccomp/resources, and policy/config generations.
- Missing Bubblewrap, namespaces, deployed UID isolation, seccomp, cgroups, mount policy, network enforcement, or other required primitive disables the affected tool. There is no unsandboxed fallback.

### Filesystem, IPC, and result boundary

- Capsules receive only capsule-specific staged inputs or safely materialized read-only descriptors derived from authorized object IDs. No arbitrary bind mount, shared workspace, source tree, runtime home, core/blob/control database, host D-Bus/systemd socket, host network socket, device tree, or unrelated workload state is reachable.
- Mount creation is race-safe against path/symlink replacement. Writable storage is tmpfs or one declared quota-bounded state/output volume; home and tmp are private.
- Browser control uses an inherited/private authenticated IPC capability, not a detached controller listening on host-local TCP.
- Capsule output goes into a bounded encrypted quarantine/import path. Counts, bytes, MIME/type, decompression ratio, structure, and classification are checked before a result can enter canonical blobs, trigger another tool, persist memory, or be delivered.
- A downloaded object is parsed in a fresh no-network capsule before import. Capsule results are always untrusted data and never mint authority.

### Lifecycle classes

The design has one implementation and three lifecycle classes:

1. `inline_trusted` is not a capsule: pure deterministic core code only, with no arbitrary executable or external tool.
2. `ephemeral_capsule`: fresh private home/tmp and state for one job or short bounded session; destroyed on completion/TTL. This is the default for unauthenticated browsing, parsing, conversion, shell/code, and one-shot tools.
3. `persistent_capsule`: a registered autonomous or authenticated integration with an immutable service principal, workload instance ID, initiating-principal chain, context domain/purpose, renewable narrow Trust lease, and one encrypted workload/domain/key-epoch state volume.

Persistent state is private checkpoint/internal state, never the sole copy of canonical message, memory, command, effect, or receipt history. Every volume declares owner, schema, compatibility, migration, backup, integrity, retention, quota, and key policy. Credentials/cookies are separately protected `credential`/`platform_auth` material and never become model-visible state.

Restart does not renew authority. Lease/policy/identity/membership/credential revocation immediately fences network and new effects and then terminates or quarantines work as required. A reboot, restore, rollback, definition upgrade, or state migration starts persistent work inert until integrity, compatibility, pending-effect reconciliation, current authority, and a fresh lease pass.

### Network profiles

- `none` is the default and uses a private/unshared network namespace with no external path.
- `fixed_destination` is for authenticated/sensitive integrations and binds exact protocol, scheme, host, port, purpose, identity/account, redirects, data class, and short-lived capability.
- `public_web` is only for unauthenticated public retrieval and may transmit only public or separately approved query material. Form filling, uploads, authenticated browsing, and other state changes require an exact destination-specific grant and a separate effect decision.

A `public_web` capsule has no host/default Internet route. Its only conduit is an authenticated, short-lived capability to one supervised network enforcement point through a deliberately exposed private transport. DNS resolution and connection establishment occur there. The enforcement point validates and pins every DNS answer/connection, reauthorizes every redirect, and denies direct/raw sockets, inbound listeners, proxy bypass, unsafe schemes, IPv4/IPv6 loopback, link-local, private/ULA/CGNAT, multicast/reserved/unspecified, metadata services, host/LAN/control networks, alternate address encodings, DoH, QUIC, WebRTC, and unapproved protocols/ports. Failure disables the tool.

The network choke point is an SSRF/destination boundary, not DLP: arbitrary public recipients may be attacker-controlled. Trust must therefore authorize every object/query released into the job. Redirects never widen destination or data authority.

### Browser state

- Unauthenticated browsing starts with a fresh ephemeral browser profile per job/session.
- Authenticated browsing is a separately registered persistent integration whose profile/cookies are isolated by platform identity, account, purpose, and context-domain policy. There is no global shared authenticated browser profile.
- Cookies/session tokens are hard-deny `platform_auth` material: not exposed to prompts, models, logs, traces, capsule results, other profiles, or other capsules.
- Chromium renderer/site isolation remains enabled; Bubblewrap does not replace the browser's own sandbox and `--no-sandbox` is forbidden.
- Public browsing cannot upload protected data. Authenticated mutations/forms/uploads require fresh effect and destination authorization immediately before the action.

### Release gates

- Clean-clone architecture scan proves every risky executable uses the single launcher and no direct `bwrap`, detached subprocess, `systemd-run`, arbitrary shell/host fallback, network client, or duplicate sandbox remains.
- Deployed-identity preflight and escape tests prove namespace, UID, cgroup, seccomp, mount, descriptor/environment, process-tree, device, IPC/socket, state, and credential isolation. Symlink/mount races and inherited-descriptor attacks fail.
- A network matrix adversarially proves `none`, `fixed_destination`, and proxy-only `public_web` against raw TCP/UDP, DNS, redirects, rebinding, proxy bypass, encoded addresses, IPv4/IPv6, metadata/internal/control targets, DoH/QUIC/WebRTC, and disallowed ports.
- Browser tests prove active Chromium sandboxing, fresh ephemeral profiles, principal/account/domain isolation of persistent profiles, cookie non-disclosure, no protected upload via public-web, and quarantine of every download.
- Resource tests bound fork bombs, CPU/memory/PID/FD use, tmpfs/disk/log/output growth, decompression bombs, hangs, and whole-cgroup termination. Capacity pressure queues or rejects work; it never preempts an active effect merely to free a slot.
- Revocation/restart/upgrade/recovery tests prove stale leases, profiles, credentials, identities, policy generations, effects, and browser sessions never reactivate automatically.
- Crash/idempotency tests distinguish not launched, pending, completed, and external-effect-unknown without blind replay.
- Persistent-state tests prove schema/key migration, coordinated backup, blank-root inert restore, state isolation, and safe failure on missing/incompatible state.
- Result-import tests enforce type/size/count/classification and a new Trust decision before chaining, memory, or delivery.

### Delete or omit from first release

Replace the separate Gateway shell, browser, and Overseer Bubblewrap builders with the single launcher. Remove `--share-net`, broad `/dev`/`/sys`/home/source/runtime exposure, caller-selected mounts/commands, detached localhost browser control, shared browser profiles, Gateway browser self-start, host-execution fallback, and Overseer arbitrary LLM shell/test execution. Remove drifted Docker/Podman/container documentation and packaging when OCI is not supported.

Do not ship an OCI/microVM backend interface, images/registry/builder, generic scheduler/container API, arbitrary per-job firewall/namespace language, dynamic mounts/environment/seccomp, in-capsule package installation, shared browser state, public-web uploads, or dormant capsule/tool types.

## Cross-reviewed proposal: Data lifecycle and indefinite retention

Status: approved by the owner on 2026-08-16 after software-architecture and security cross-review.

### Retention meaning and canonical set

“Indefinite” means retained until an explicit future owner-approved policy changes it; it does not mean undeletable forever or that every byte emitted by every subsystem becomes permanent evidence.

First release retains these canonical records indefinitely:

- original inbound and outbound message-bearing native envelopes, exact message/media objects, edits/deletes/reactions, and reply/quote/mention/thread relationships;
- native IDs, sender/platform identity resolution, destination, intended and conservative reachable audience, membership version, and delivery/read evidence used at event time;
- delivery intent, stable client ID, every attempt, receipt, and explicit-unknown outcome;
- Trust envelope/decision, applicable grants, and identity/membership/policy/config/provider generations;
- canonical memory versions, source/ACL/classification/grant-taint edges, corrections/supersession, recall/lifecycle evidence, and accepted derived content;
- executed model/tool/workload action and attempt decisions, exact governed input/result objects, consequential state transitions/effects, receipts, and unknown outcomes;
- every normalized/OCR/transcript/thumbnail/extraction or other transform actually admitted into a model, tool, memory, response, or consequential decision, with immutable source/transform edges;
- migration receipts, backup manifests, integrity outcomes, suppression receipts, and future erasure receipts.

Raw channel evidence explicitly excludes platform/session authentication, ratchets, credentials, ephemeral signed URLs, authorization headers, TLS/packet captures, and transport secrets. Presence, typing, reconnect, keepalive, and protocol frames are canonical only if they causally affect product behavior.

Rebuildable search/vector projections, caches, scratch, temporary staging, discarded browser subresources, duplicate SDK/wire serialization, routine scheduler/reconcile chatter, and ordinary operational logs are noncanonical and bounded. Edge/outbox segments compact only after verified core acceptance and replay reconciliation. Operational logs may rotate after 14 days only after content hardening proves every consequential fact is in core and no message/media/credential/session content is logged.

### Model and tool disclosure evidence

Each executed attempt retains exactly one governed canonical disclosure record:

- an ordered request manifest of source/transform object IDs, tool schema/typed arguments, profile/destination, effective parameters, serializer/adapter release, policy/config generations, size, and digest;
- the exact transformed semantic prompt/query/attachment objects disclosed, excluding credentials;
- an exact credential-stripped serialized request body only when immutable objects plus the versioned deterministic serializer cannot reproduce it byte-for-byte;
- the exact bounded normalized response/result admitted into Yeoman, plus any partial content that caused an action;
- exact typed effect request, provider/platform request ID and receipt for side effects.

Do not retain credentials/headers, TLS/packet data, hidden provider reasoning, raw SDK internals, duplicate streaming chunks, discarded browser resources, stack traces, or indefinite raw error pages. A rejected/oversized input that never becomes an admitted result may be represented by its source receipt, ciphertext/object digest, byte count, typed rejection, and bounded protected diagnostic.

This gives exact causal traceability without creating shadow archives of the same sensitive content.

### Three orthogonal lifecycle mechanisms

Do not use one mixed lifecycle enum.

1. **Content availability:** `available`, `erasure_pending`, `erased`, or `quarantined`.
2. **Memory recall eligibility:** `eligible` or `suppressed`.
3. **Provenance/versioning:** immutable `supersedes`, `corrects`, and source/transform edges.

A correction creates a new immutable current version. The older version remains protected historical evidence and is excluded from ordinary current-memory selection through version edges, not destruction.

Suppression is reversible. It removes a derived memory immediately from conversational recall, FTS/vector projections, background materialization, models, and ordinary tools while leaving plaintext protected in canonical storage and raw source history untouched. Access is limited to explicitly authorized owner-private audit, reactivation, or future erasure-planning paths. First-release `/forget` must either become an accurately named, domain-scoped suppression operation or be removed; it may not claim deletion.

`erasure_pending` immediately blocks decryption/materialization, indexing, derivation, model/tool use, export, and delivery while a crash-recoverable provenance plan fences pending effects. `erased` retains only opaque object identity, non-plaintext/ciphertext commitment and size, causal/provenance shape, authorization/lifecycle receipt, and known residual-copy status. `quarantined` content exists but cannot enter normal processing because integrity, identity, audience, classification, or provenance is insufficient.

Supersession, suppression, and erasure never sever provenance. A declassification decision may widen disclosure but does not make a derivative independent of an erased source. Mixed-source derivatives must be erased or independently regenerated from unaffected sources.

### First-release erasure scope and honest guarantee

First release ships object-level encrypted content, granular keys, complete provenance, the lifecycle fields above, projection purge/rebuild, an erasure-generation field in backup manifests, and supported-restore reconciliation. It ships no user-facing canonical erase API, scheduled canonical deletion, background cascade engine, or non-owner deletion policy.

Any future canonical/bulk erasure requires a separately approved policy, owner-private action-bound step-up, complete provenance plan, explicit residual-copy accounting, and a new cross-review. Overseer may detect, recommend, and fence but never authorize or execute erasure.

Before a suppression or future erasure reports success, the signed/MACed monotonic lifecycle journal advances and is copied to every approved local recovery medium. A supported restore applies the newest journal before content-key access or projection rebuild and fails closed if the required current journal head is missing or stale. This provides current-installation and conforming-restore non-materialization.

It does not guarantee forensic/adversarial cryptographic erasure from retained historical backups. Such a guarantee requires a later choice among verified destruction/rewrite of every affected generation, a separately maintained non-rollbackable key authority, hardware-backed revocation, or coarse key-epoch destruction with an accepted blast radius. Yeoman also cannot erase copies already delivered to people/platforms/providers or exported outside managed storage; lifecycle receipts enumerate them as outside local control.

### Capacity and failure policy

Indefinite retention creates an explicit operator capacity obligation. It is never solved through silent eviction.

- Reserve capacity exclusively for core transactions, Edge capture metadata/spools, receipts, alarms, and lifecycle evidence. Quota projections, logs, scratch, workload state, backups, migration staging, and blob staging so they cannot consume that reserve.
- Monitor physical/logical bytes, object counts, growth rate, WAL/checkpoint/backup/migration peaks, and forecasted time to exhaustion—not only percentage free.
- At a soft watermark, delete only proven-rebuildable projections/scratch, stop speculative transforms, and throttle background work.
- At a hard watermark, stop new nonessential workloads and fence outbound effects; enter capture-only mode.
- At a critical watermark, raw capture continues only if blob and core references can commit durably. Where the platform permits, do not acknowledge or advance the durable checkpoint. Where it does not, record an explicit degraded/loss-risk state and never claim `capture_committed`.
- Never issue an outbound effect if its intent/result/receipt evidence cannot still be committed.
- Same-disk backup growth may never consume the primary recovery reserve.

### Release gates

- Clean-clone scans find no canonical age/salience purge, delete-after-transform behavior, arbitrary SQL deletion, unmanaged DB snapshot, misleading soft-delete wording, or stale 7/30-day retention contract across code/config/tests/docs/diagrams.
- Every enabled channel durably captures immutable raw message/media and relationship/audience evidence before processing and before acknowledgement where controllable.
- Migration reconciles every extant active, soft-deleted, expired, backup/snapshot-only, and quarantined object; already-purged content becomes an explicit unrecoverable provenance gap.
- Canonical/noncanonical rules are enumerated for every channel, provider, tool, transform, and workload.
- Suppression tests cover SQL/FTS/vector recall, context materialization, models/tools, background work, projection rebuild, owner-private audit, and reactivation.
- Synthetic future-erasure tests prove `erasure_pending`/`erased` never materialize and crash recovery is idempotent across mixed-source derivatives, caches, workload state, pending effects, and declassified objects.
- Restoring an older generation with a newer lifecycle journal proves conforming non-resurrection and fails closed on stale/missing journal authority while documentation states the adversarial-old-backup limitation.
- Disk-pressure fault tests cover every watermark, blob staging, core commit, Edge acknowledgement, backup growth, projection purge, capture-only transition, and evidence-write failure without silent purge or false acknowledgement.
- Exact bounded model/tool disclosure evidence is sufficient to reconstruct/prove behavior while secret scans show no authentication material or duplicate shadow trace.
- Restore, export, and operator inspection continue to enforce owner read-all only in owner-private/operator contexts.

### Delete or omit from first release

Delete reply-archive purge/config/startup calls, canonical-media retention/delete-after-transform settings, enabled media/memory purge runbooks, Overseer direct DB copy/delete tools, application-managed log rotation, misleading `is_deleted`/`soft_delete` naming, and every stale retention test/README/diagram/spec. Use bounded journald only after log hardening.

Deliberately omit a retention DSL, legal holds, subject/compliance portal, scheduled canonical deletion, generic tombstone service, background erasure engine, backup-rewrite engine, forensic-erasure claim, and any non-owner deletion-right policy.

## Cross-reviewed proposal: Failure containment and operational control

Status: awaiting owner approval. Both reviewers returned a conditional go for one small additive-fence model plus typed desired/observed reconciliation.

### Operational permission and fences

Operational controls can only reduce availability; they never grant data authority. Effective permission is:

`Trust authorization AND current execution lease AND no applicable operational fence`

Use five fixed capabilities:

1. `EDGE_CAPTURE` — accept native channel events into a durable account/producer spool.
2. `CORE_INGEST` — commit canonical interactions/blobs/relationships/work items.
3. `PROCESS` — construct turns, retrieve/materialize memory, and start authorized computation.
4. `EGRESS` — disclose to a specific model route, tool, or capsule network profile.
5. `DELIVER` — send/react/mutate through a specific channel account/destination.

Administrative mutation is not a normally open capability. Each config activation, policy change, migration, restore, suppression, future erasure, cutover, or destructive operation receives a single-action, short-lived owner-private step-up permit and must also be free of an applicable emergency admin fence.

Fences are additive and scoped, never mutable global booleans. A fence records capability, fixed scope hierarchy (global; channel/account; provider/tool route; registered workload; destination/action), issuer and issuer-local monotonic sequence, cause, evidence reference, observed release/config/policy/storage/Trust/control generation vector, issue time, stickiness, recheck time, explicit clearance authority, and proof predicate.

No actor sets a gate open or clears another issuer's fence. Edge may fence its own capture; Gateway storage/Trust may fence core ingest/processing; Controlled Egress fences affected routes; Delivery fences affected channel accounts/destinations; Overseer adds resource/lifecycle/dependency fences and may physically stop registered units. Each automatic issuer clears only its own transient fence after fresh affirmative proof and stable dwell. Expiry or stale evidence remains closed.

Integrity, security, generation mismatch, recovery, migration, and cutover fences are sticky. They require owner-private action-bound step-up plus referenced current proof. Overseer can always move the system to a safer state but can never reopen a sticky fence, broaden Trust, renew a Trust lease, or create conversational authority.

### Failure containment matrix

| Failure | Fence | Remains available | Reopen proof |
|---|---|---|---|
| Provider/model unavailable | scoped `EGRESS(route)` | capture, local processing, other routes, authorized delivery | current route/credential/policy checks and stable dwell; no fallback widening |
| Capsule/network control unavailable | scoped `EGRESS(tool/profile)` | core turns and other routes/tools | fresh isolation/network preflight |
| Channel outbound failure | scoped `DELIVER(account/destination)` | inbound Edge/core capture and other channels | transport readiness plus pending-attempt/receipt reconciliation |
| Channel inbound failure | scoped `EDGE_CAPTURE(account)` | other channels and existing committed work | reconnect plus durable sequence/cutoff continuity |
| Projection failure | scoped `PROCESS(recall projection)` or degraded no-recall mode | canonical storage and bounded authorized fallback | verified rebuild against current canonical generation and authorization tests |
| Core/blob/evidence integrity uncertainty | `CORE_INGEST`, `PROCESS`, `EGRESS`, `DELIVER`, emergency admin fence | safe isolated `EDGE_CAPTURE` only | sticky owner step-up after integrity/recovery/reconciliation proof |
| Trust/config/policy generation uncertainty | protected `PROCESS`, `EGRESS`, `DELIVER`, admin fence | raw Edge capture and conservative quarantine | sticky explicit activation and matching loaded digests from every process |
| Disk pressure | progressively background process, egress/delivery, then core ingest | reserved Edge capture while safely possible | sustained headroom plus integrity proof; sticky if write-loss ambiguity occurred |
| Overseer/control unavailable | no new launches/reopens; workloads expire inert | Gateway interactive traffic within already valid generations/leases | current control sync and outbox reconciliation |
| Backup late/failed | block high-risk upgrade/migration/restore activation | normal traffic continues degraded | new verified closed generation; never stop capture merely because daily backup is late |
| External effect unknown | fence exact action/destination | unrelated work | authoritative receipt/query reconciliation or owner disposition |
| Security incident | scoped/global downstream and admin fences | Edge capture only where independently safe | incident-specific step-up and proof |

Projection degradation never permits an unbounded direct canonical database scan that bypasses candidate, ACL, class, or result limits.

### Workload and supervision ownership

| Owner | Authoritative responsibility |
|---|---|
| Gateway/core | authorized `WorkloadCommand`, initiating principal/domain/purpose, execution-authority lease, semantic action/effect intent, result/receipt/unknown, accepted fence evidence |
| Overseer/control | desired instance projection, observed lifecycle, reconciliation ownership lease, restart/fence history, redacted health evidence outbox |
| systemd | actual process, PID/cgroup, watchdog, resource controls, start/stop/restart mechanics |
| Capsule/workload | private checkpoint and bounded internal progress only |
| Edge | durable native events not yet accepted by core, producer sequence/cutoff, transport recovery |

Each desired instance references one canonical authorized command and immutable registered unit/capsule definition; it contains no prompts, grants, credentials, arbitrary commands, systemd properties, paths, mounts, or environment. A reconciliation lease prevents duplicate Overseer work but cannot renew the Trust execution lease. Loss of core/control communication makes effectful workloads inert when their Trust lease expires.

`control.db` may rebuild desired projections from canonical commands after loss, but every instance starts stopped/inert and requires current authority. Historical observed/desired-running state is evidence, never restart permission.

Config and policy are strict immutable generations with independent ordered generation/digest pairs. Loading is pure: no write/backup, process-global environment mutation, implicit migration, fallback-to-default after invalid input, ignored unknown keys, or activation. Explicit compare-and-swap operator activation creates a signed generation record. Overseer detects loaded-generation drift, fences, and restarts fixed units; it never rewrites content.

### Retry and unknown ownership

Each operation ID has exactly one retry owner. Process restart resumes reconciliation; it is never a semantic retry.

Durable states are `not_dispatched`, `dispatched`, `succeeded`, `failed_permanent`, `cancelled_or_expired`, and `external_effect_unknown`; Trust `denied` is separate from operational failure.

- `not_dispatched` may retry only after current authorization.
- Pure reads/computation/model attempts may retry through the owning coordinator, with a new recorded attempt and unchanged authorized disclosure/route/cost/deadline envelope.
- An external write/send may retry automatically only when the destination provides verified idempotency and the identical stable key is reused.
- Timeout/cancellation after possible transmission becomes `external_effect_unknown` and is terminal for automatic retry.
- Unknown remains unknown through restart/restore. A replacement user request creates a new intent that explicitly accounts for the possible previous effect.
- Capsules retry private computation/checkpoints only. Externally visible browser actions, uploads, integration writes, purchases, messages, and other effects remain centrally owned by the Action/Delivery coordinator even if the capsule performs the syscall.
- Rate limiting records a specific `retry_after`; fallback never changes destination trust, audience, data scope, retention, or provider policy.

Typed errors expose a sanitized public code/message, internal evidence reference, owner, retry safety, and optional retry time. Provider exceptions, raw platform errors, and stack traces never become assistant content.

### Startup and recovery order

Every process start preflights executable/build ID, release/config/policy/schema/key/control generations, database/blob integrity status, active fences, Edge cutoffs, leases, and pending/unknown effects.

The stack boots fenced. Open capabilities only after current proof, in this order:

1. `EDGE_CAPTURE` for each verified account/spool.
2. `CORE_INGEST`, then replay/deduplicate Edge and reconcile producer cutoffs.
3. `PROCESS` after canonical/Trust/projection readiness.
4. scoped `EGRESS` after route/tool/isolation policy proof.
5. scoped `DELIVER` after audience/delivery/receipt reconciliation.

Restore, cutover, integrity, or security startup always requires deliberate sticky-fence clearance. An ordinary transient restart may clear only the restarting issuer's nonsticky dependency fence after its defined proof. Persistent workloads renew execution authority separately and start inert by default.

### Health and observability truth contract

Expose one typed, authenticated, redacted local status snapshot over the Unix-socket control path. Unauthenticated liveness may reveal only that a process answers. Remove the dormant HTTP health/control/metrics/webhook API.

Every health fact includes measurement time, evidence source, status including `unknown`, freshness/SLO, expected and observed build/generation, last success, last failure, and collection error. Stale evidence is `unknown`, never green. A derived ready/degraded/fenced label never replaces component facts.

Snapshots report per-plane liveness/readiness/integrity/connectivity, effective fences, loaded release/config/policy/storage/control digests, queue/backlog/lag/drop counts, last Edge/core/model/tool/delivery commits, pending/unknown effects, disk reserve/growth/WAL/blob state, newest completed backup and verified disposable restore, and persistent workload desired/observed/lease state. Metrics include process epoch because they may reset and are not canonical evidence.

Health checks are read-only. Provider probes, test sends, restores, and other effects are explicit recorded actions. Typed storage/status APIs replace arbitrary DB paths/SQL. Detailed metrics never label chat/principal IDs or content. Local Evidence stores consequential transitions; journald stores bounded metadata-only operational logs; alerts carry only failure class, component, correlation ID, and safe remediation reference.

Replace the generic Markdown/LLM runbook interpreter with a small enumerated set of typed reconcilers for registered services, storage/capacity, backups, workloads, and active generations. Human runbooks remain documentation. Reconciler outcomes distinguish `not_evaluated`, `no_action`, `attempted`, `succeeded`, `failed`, `unknown`, and `escalated`; `succeeded` requires a fresh observed postcondition with the expected unit/build/generation.

## Cross-reviewed proposal: First-release product scope and proof

Status: approved by the owner. The product scope was corrected on 2026-08-16: consciousness and speak-up remain required capabilities, delivered as mandatory Phase 2 after core stabilization; persona evolution is removed completely as an executable capability.

### Recommended first-release scope

Ship:

- WhatsApp and Telegram as hard contract obligations, plus the local owner/operator CLI. Telegram remains a release blocker until it satisfies the same Edge, Trust, Delivery, Evidence, media, replay, and receipt contracts as WhatsApp.
- An explicit per-channel capability matrix for text, reply/quote, reactions, image/document, audio/voice, and other claimed modalities; do not imply parity where a platform lacks it.
- reactive assistant turns; canonical memory capture/recall and accurately named suppression; owner-requested reminders; and a small allowlist of named persistent workload types.
- only selected owner-approved provider profiles/routes and named capsule tools used by shipped features; browser/public-web only through the approved capsule/network boundary.
- local backup/restore, typed status, and Overseer reconciliation.

Do not ship Discord or Feishu executable adapters/config/schema/docs/dependencies, dormant HTTP API/webhooks, external telemetry, generic Markdown/LLM runbooks, alternate event/IPC buses, unsupported providers/tools/routes, host-execution/compatibility paths, or generic workload definitions.

Consciousness and autonomous speak-up are **required target capabilities**, not speculative future work. Their current implementation is not copied into the new Gateway and is not retained as a compatibility runtime. They are rebuilt as one ordinary registered persistent **Proactivity Workload** in the Workload Fabric. If the replacement is not part of the core cutover release, it is the mandatory next milestone and the architecture program is not complete until it is live and accepted. The only permissible gap is an owner-approved, time-bounded period in which reactive assistance continues but proactive entry is fenced off.

The Proactivity Workload uses service principal `assistant.proactivity` and receives only explicit source-domain, purpose, observation-set, model-route, budget, and renewable execution-lease grants. It consumes canonical event and object references through Trust-authorized reads; it never inherits owner read-all merely because the owner operates the system. State and learning remain domain-scoped by default, so two WhatsApp groups containing the same people cannot silently share observations, outcomes, or learned taste.

The workload may create private observations, domain-scoped memory candidates, and a typed `SpeakUpProposal`. A proposal is not delivery authority. Immediately before every outbound effect, the Action/Delivery coordinator re-resolves the principal, destination, audience/membership, reply/mention context, content classification, current policy and grants, fences, budget, quiet hours, cooldown, deduplication key, and delivery lease. Every trigger, governed input, model call, proposal, authorization decision, send attempt, external-effect-unknown state, platform ID, receipt, reply/quote/mention relationship, and later outcome is connected by canonical provenance. Restore or restart begins inert until a fresh lease is issued and never auto-replays a sent or possibly sent proposal.

The workload has no direct `core.db`, projection, channel transport, channel/model credential, arbitrary host path, or unrestricted-network access. Trust/Gateway, not the workload or Overseer, issues and renews its execution lease from canonical owner-authorized state. Canonical budget consumption, proposal identity, approval, delivery intent, attempt, receipt, and unknown outcome live transactionally in core; a workload-local checkpoint cannot be the only deduplication or effect record.

The live behavior inventory to reconcile includes scheduled, burst, lull, and explicit/manual triggers; per-destination enablement and profiles; quiet windows; daily and burst budgets; minimum gaps; approval/preview modes; outcome classification; and domain-scoped preference/taste learning. Parity is semantic and safety-focused, not a requirement to preserve the current class structure, prompts, JSON files, or SQLite schemas.

All existing consciousness/speak-up records are migrated losslessly before the old files are retired. At the 2026-08-16 design snapshot the live stores contained 2,323 proposal/decision rows, including 326 `sent` records, and 180 taste-distillation fingerprints; these are inventory evidence, not fixed cutover counts. The old implementation marks a proposal `sent` after publishing it to an in-memory outbound bus, not after a durable platform receipt. Therefore legacy `sent` means dispatch evidence only. The migration reconciles each row against authoritative outbound archives/platform IDs/receipts; anything not proven delivered becomes an inert `external_effect_unknown` historical effect and is never replayed.

The final migration reconciles counts, content/blob hashes, status, trigger, destination domain, proposal-to-send provenance, outcomes, approvals, and source ACL/classification. Pending proposals, approval codes, timers, desired-running flags, and old scheduler/lease state become retired or cancelled evidence, not new execution authority. Only conservatively mapped cooldown/deduplication facts and explicitly defined private checkpoint state may seed the new workload. Ambiguous legacy rows become restricted inert evidence with an explicit provenance gap, never globally recallable authority.

Persona evolution is removed completely from the released executable product: no scheduler job, config schema, model route, CLI command, approval middleware, ledger, proposal writer, auto-apply path, tests for that feature, or docs/specs presenting it as supported behavior remain. The new release carries one explicit owner-approved static persona generation. Because the current persona may contain previously learned material, its release candidate must pass a disclosure review proving that it contains no destination-specific facts or hidden cross-domain instructions before it is usable in shared contexts.

Removing persona evolution does not waive the no-data-loss and traceability rules. Every old database row and proposal artifact receives a manifest-backed disposition: preserved, normalized, deduplicated, quarantined, or explicitly excluded as noncanonical operational material. Preserve the exact effective static persona generation; exact content/diff for every applied proposal or proposal shown to the owner; source references; approval/denial/expiry actor, context, and time; before/after hashes; and enough evidence to explain which persona governed historical behavior. Hashes alone are insufficient for an applied change.

Routine scans that produced no proposal or consequential decision, duplicate rendered proposal files after unique content is normalized, scratch/model intermediates, caches, and unseen rejected/expired drafts with no unique provenance are noncanonical and may be deleted after the migration manifest proves their disposition. Raw source conversations remain canonical under their original domains and are referenced instead of duplicated. Preserved persona history remains inert generic protected evidence, not a persona module, recall source, or executable authority. At the design snapshot there were 102 scans and 11 proposals (5 applied and 6 expired); final cutover uses fresh inventory and hash reconciliation. Once import, disposition, and restore are proven, the old feature-specific database/files, runtime code, config, feature tests, and product documentation are deleted with the rest of the sealed legacy bundle. Migration/reconciliation tests and a generic inert evidence schema remain only as long as they are part of the recovery contract.

Two release shapes were evaluated:

1. include the minimal safe Proactivity Workload in the core cutover and make its acceptance gates part of the first release; or
2. cut over the reactive core first, keep proactivity fenced, then deliver it as a mandatory immediate stabilization milestone before declaring the redesign complete. **Chosen by the owner on 2026-08-16.**

Preserving the legacy consciousness/speak-up implementation in parallel is not valid because it creates a second authorization, memory, retry, and delivery path and violates the zero-stale-runtime requirement.

The software-architecture and security reviewers recommended option 2, and the owner approved it. Its activation is deliberately staged with no automatic widening:

1. `inert_shadow`: authorized observation and deterministic/sanitized replay comparison only; no proposals with external effect and no delivery.
2. `proposal_only`: durable expiring proposals; each delivery requires action-bound owner approval plus fresh Trust and Delivery authorization.
3. `narrow_autonomous`: only owner-approved destinations and action classes after shadow/proposal-only proof; every send still receives fresh authorization.

Phase 2 begins after the core satisfies its post-cutover stabilization gate, not merely after elapsed time. Completion requires the normal proactivity migration/crash/isolation/restore tests plus a proactivity-specific 14-day/100-trace observation gate with zero cross-domain, wrong-audience, unauthorized, or duplicate delivery. The architecture program remains formally incomplete until the owner-approved destination scope is live and that gate passes.

### Release proof

Before cutover:

1. Owner signs the exact channel/modality/provider/tool/workload allowlist.
2. A clean clone installs non-editably from locked inputs and emits a strict manifest of every file, dependency, build ID, route, unit, schema, config, and doc.
3. Full test collection, mandatory CI, lint, type checking for all public contracts, packaging/build tests, architecture/import ownership rules, strict unknown-config rejection, and zero-stale-path scans pass.
4. Every shipped channel passes one common capture/audience/media/delivery/receipt/replay/reconnect/degradation contract suite plus its explicit modality tests.
5. Every fence issuer/scope/reopen/stale-generation/control-loss path and every failure-matrix row passes without unauthorized reopen or blind retry.
6. Crash/fault injection covers each blob/fsync/core/intent/call/send/receipt/outbox/control-projection boundary, disk/corruption/network/provider/channel/capsule/revocation failure, and explicit unknown recovery.
7. Two independent full migrations from consistent snapshots reconcile every source object, raw blob, memory version, ACL/provenance edge, lifecycle state, pending effect, and producer cutoff with zero unexplained loss or access widening.
8. One blank-root local recovery uses only the signed release, documented recovery inputs, and backup; all effects remain fenced.
9. Run at expected load for 24 hours and at twice measured expected peak for at least two hours with bounded queues, WAL, blobs, backups, disk growth, health collection, latency, and no loss/duplication.

### Cutover and sealed-bundle retirement

Install sticky cutover fences; stop old Overseer/timers and every old writer; retain verified Edge capture only; close the final old backup generation/cutoffs; migrate/reconcile the delta; boot the new stack fully fenced; verify all generations/integrity; open `CORE_INGEST`, replay/deduplicate Edge, then `PROCESS`, scoped `EGRESS`, and scoped `DELIVER`; start Overseer last and prove it observes rather than rewrites active generations.

The atomic commitment marker is the first live interaction committed by the new Gateway. Before it, rollback may restore untouched old state and replay Edge capture. After it, recovery is forward-only; the old bundle is migration evidence and must never start as a runtime.

Destroy the sealed legacy bundle only after owner step-up and all of:

- 14 consecutive observation days and at least 100 trace-complete live interactions;
- every shipped channel/modality and DM/group authority path exercised end-to-end;
- reconnect, process restart, Edge replay, provider failure, capsule tool, reminder, and each periodic workload proven without loss/duplicate effect;
- no unexplained canonical count/hash/provenance divergence, stuck intent, access widening, unresolved integrity/security fence, or undisposed external-effect-unknown;
- at least three successive verified post-cutover backup generations and disposable restores;
- one verified post-cutover blank-root recovery;
- current backup proven to contain every object whose only previous copy may be in the old bundle;
- no unauthorized config/policy/fence generation change for at least twice the longest reconciliation interval.

Long-period workloads are time-shift tested before release rather than extending observation indefinitely. Bundle destruction records its exact manifest and destroys its encryption key where practical, without claiming unverifiable physical secure erase.

### Deliberate omissions

Do not build a global workflow/state-machine runtime, CRD/controller platform, generic retry/circuit-breaker mesh, control-plane UI, HA cluster, distributed consensus, service mesh, remote telemetry, arbitrary SQL health, auto-restore, post-commit auto-rollback, or stale executable modules for future flexibility. The first release uses systemd, one typed Overseer reconciliation loop, additive scoped fences, canonical effect records, and one truthful local health snapshot.
