# Yeoman target architecture and migration design

Date: 2026-08-16

Status: Owner-approved normative architecture.

This document is the sole normative architecture design and supersedes:

- `session-context/2026-08-15-architecture-audit.md`
- `session-context/2026-08-15-target-architecture-decisions.md`
- `session-context/2026-08-15-architecture-cross-review.md`

Those working notes remain audit inputs during planning/migration but are excluded from the released tree with every other superseded architecture document.

Reviewers:

- Software architecture: `Yeoman Architecture Migration Review`, task `01a00731-aa0d-7591-9382-2c52becddbae`
- Security engineering: `Yeoman Security Architecture Review`, task `01a00731-aa5c-7232-93c3-d165eabc5d58`

## 1. Executive decision

Yeoman remains one coherent personal assistant with multiple authorized users, channels, platforms, modalities, model providers, and temporary or persistent workloads. It does not become a generic multi-tenant agent platform.

The target is a **modular conversational core plus a supervised workload fabric**:

- The Gateway is the conversational data plane and owns a small latency-critical Turn Kernel.
- Edge adapters preserve native channel semantics and durably capture events before processing.
- Trust is the only authority for identities, domains, audiences, grants, data handling, and execution envelopes.
- Memory is multi-layered, versioned, provenance-complete, and domain-isolated by default.
- Controlled Egress owns bounded model and media-provider disclosure.
- Delivery owns channel effects, stable idempotency, and platform receipts.
- Risky executables and tools run in lightweight Bubblewrap-based Execution Capsules.
- Persistent or independently supervised behavior runs as registered workloads.
- Overseer remains the Kubernetes-like control plane, while systemd remains the machine supervisor.
- Evidence connects every input, decision, model/tool attempt, memory mutation, workload transition, outbound effect, and receipt.

The replacement is built in an isolated Git worktree and a fresh state root, not as a permanent `v2`, `next`, or compatibility package inside the repository. The final released tree contains only the replacement. Old executable code, flags, configuration, tests, dependencies, docs, diagrams, and superseded specifications are absent before cutover.

The reactive core is cut over first. Consciousness and speak-up return as mandatory **Proactivity Phase 2** after core stabilization. The redesign is not complete until that workload is active and proven. Persona evolution is removed permanently as executable product behavior.

## 2. Product boundaries and terminology

### 2.1 Product identity

Yeoman supports:

- WhatsApp and Telegram as first-release channel contracts;
- owner-private operation through a local authenticated CLI;
- authorized non-owner participants under channel and destination policy;
- text, replies/quotes, mentions, reactions, documents, images, audio, and voice where the channel capability matrix declares support;
- selectable model/provider profiles constrained by capability, data class, destination, cost, and policy;
- immediate turns, reminders, one-shot tool jobs, and registered persistent workloads;
- later insertion of reviewed transforms such as anonymization without redesigning the turn path.

Yeoman does not provide arbitrary tenant onboarding, arbitrary code plugins, arbitrary provider endpoints, a workflow programming platform, a public control API, or Kubernetes-compatible APIs.

### 2.2 Core terms

- **Principal:** immutable Yeoman identity for a human, service, workload, or authenticated operator session.
- **Platform identity:** native user/account identifier bound to a platform account epoch.
- **Context domain:** one conversation lineage and its memory/disclosure boundary. Two groups are two domains even when their members are identical.
- **Audience:** the conservative set of people/endpoints that can receive a disclosure, distinct from intended recipients and confirmed readers.
- **Execution envelope:** immutable, expiring authorization evidence for one turn, action, or workload lease.
- **Canonical:** required to explain behavior, preserve user data, recover state, or prove authority/effects.
- **Projection:** rebuildable candidate/index/operational state that never grants authority.
- **Fence:** an additive operational deny. It can reduce availability but cannot grant Trust authority.
- **External effect unknown:** an operation that may have reached an external system but lacks authoritative reconciliation. It is terminal for automatic retry.

## 3. Architectural principles and invariants

The following are release invariants:

1. No message is processed without durable interaction evidence.
2. No external side effect exists without a prior canonical intent and a later receipt, failure, or explicit unknown outcome.
3. Every message preserves who sent what to whom, on which account and conversation, and whether it was new, replied, quoted, mentioned, threaded, edited, deleted, or reacted to.
4. Identity linkage never implies memory sharing.
5. A shared destination never inherits owner read-all authority.
6. Background workloads use the same Trust, egress, memory, action, and delivery boundaries as reactive turns.
7. Models and tools receive only objects authorized for that exact attempt.
8. Raw interactions, derived memory, operational state, and causal evidence remain logically distinct even when atomic metadata shares one database.
9. Unknown identity, domain, audience, membership, classification, route, generation, or grant fails closed. Safe raw capture may continue into quarantine.
10. Restart, restore, rollback evidence, or an old desired-running flag never renews authority.
11. Search results, model output, tool output, and workload output are untrusted candidates; none can mint permissions or cause an effect without a new decision.
12. No legacy runtime path or dormant unsupported module survives in the released artifact.

## 4. Logical planes and dependency direction

### 4.1 Edge Plane

Channel adapters translate native events into versioned canonical interaction commands while preserving native payloads and semantics. Each adapter/account owns a durable Edge spool when the upstream platform cannot guarantee replay.

Edge may capture while downstream processing is fenced. It cannot authorize memory, invoke models, or deliver assistant output. It submits authenticated, typed, size-bounded, idempotent records through the core ingress port and retains them until core commit acknowledgement.

Where the platform permits acknowledgement control, Edge acknowledges or advances the native durable checkpoint only after its raw envelope/media staging and producer sequence are durable. If the platform does not permit that guarantee, status records the exact loss/duplication risk instead of claiming `capture_committed`.

### 4.2 Interaction Plane

The Interaction module records immutable communication facts:

- native platform, channel account/epoch, conversation, message, sender, and recipient identifiers;
- canonical principal/domain resolution and the exact generations used;
- intended and conservatively reachable audiences;
- inbound/outbound direction and native event type;
- reply, quote, mention, thread-root, edit, delete, reaction, and media edges;
- original payload/blob references and accepted transforms;
- delivery intent, attempts, stable client ID, platform ID, receipts, read evidence, failure, or unknown outcome;
- workload/turn/action/trace links.

Inbound interaction metadata and raw blob references commit before turn processing. Outbound intent commits before transport dispatch.

### 4.3 Trust Plane

Trust is a logical in-Gateway boundary with three cohesive responsibilities:

1. identity, domain, membership, and grant registry;
2. pure deterministic authorization evaluation;
3. execution-envelope and decision persistence.

Trust does not become a network service, general IAM product, policy DSL, or external rules engine.

### 4.4 Execution Plane

The Execution Plane contains:

- the Turn Kernel for bounded reactive orchestration;
- the Workload Fabric for registered temporary and persistent work;
- the Action coordinator for consequential commands/effects;
- the Capsule launcher for risky executables.

The Turn Kernel coordinates typed ports. It does not own provider SDKs, channel transports, arbitrary storage queries, sandbox construction, or lifecycle reconciliation.

### 4.5 Memory Plane

Memory stores versioned derived knowledge with immutable sources, domain visibility, classification, grants, and lifecycle. It never replaces raw interaction history.

### 4.6 Control Plane

Overseer compares typed desired and observed lifecycle state, adds operational fences, requests fixed systemd actions, monitors capacity/backups/generations, and reports evidence. It never decides conversational meaning, issues Trust grants, renews workload authority, calls models directly, rewrites configuration, or opens another component's fence.

### 4.7 Evidence Plane

Evidence correlates the facts needed to explain an action: interaction, execution envelope, authorization, materialized objects, transforms, model/tool attempts, memory operations, workload transitions, delivery, receipt, failure, and recovery. Consequential evidence is canonical; verbose debug telemetry is bounded operational data.

Every Evidence record and protected evidence blob inherits all source domains, the audience intersection, highest classification, and grant constraints of the facts it describes. Evidence inspection is freshly Trust-authorized. Cross-domain read-all exists only in owner-private/operator audit. Shared output, health, logs, errors, and ordinary denials cannot reveal another domain's trace existence, identifiers, metadata, or content. Trace rows use opaque object references instead of copying protected content into a generally readable trace table. Periodic tamper-evident checkpoints bind the append history without requiring every local row to carry an independent signature.

Each owning module writes its own causal evidence in the transaction that owns the underlying fact. The Evidence module correlates and authorizes protected inspection; it does not become a universal write service or god repository for Interaction, Trust, Egress, Delivery, Memory, or Workload state.

### 4.8 Dependency rule

The permitted direction is:

```text
Edge -> Core ingress
Turn Kernel -> Trust, Memory, Controlled Egress, Action, Delivery ports
Memory -> owned core tables/blob API + Trust checks
Controlled Egress -> registered provider adapters + Evidence
Action/Delivery -> registered effect/channel adapters + Evidence
Workers -> authenticated Workload API only
Overseer -> typed status/control ports + systemd fixed units
Projections -> core-owned transactional SQLite outbox/work queue; candidate IDs back to authorized callers
```

Edge, Overseer, workers, and projections never open `core.db`. No logical module issues arbitrary SQL against another module's tables.

The projection/change mechanism is the bounded transactional outbox/work queue committed with canonical changes in `core.db`; it is not a second event bus or broker.

## 5. Source and deployment structure

### 5.1 Development isolation

The rework uses a separate source worktree, for example `~/Documents/yeoman-rework`, and a fresh non-live state root. The old live services continue from the current release until the quiesced cutover. The new worktree uses final package names and final contracts from its first commit.

Do not create permanent `yeoman_v2`, `legacy`, compatibility, mirror, or dual-write packages. Temporary migration readers live in an allowlisted migration package and cannot serve runtime traffic. They are removed from the final artifact when no longer part of supported recovery.

### 5.2 Final package ownership

The final source tree should converge on this ownership shape:

```text
packages/gateway/yeoman_gateway/
  app/             composition and process entrypoints
  edge/            typed channel ingress ports and account coordination
  interaction/     canonical interaction commands and queries
  trust/           identity, domains, membership, grants, decisions
  turn/            small reactive Turn Kernel
  memory/          capture, versioning, recall, suppression
  actions/         semantic intents/effects and retry ownership
  egress/          classification, route policy, provider adapters
  delivery/        channel effect adapters and receipts
  evidence/        causal records and protected inspection
  workloads/       workload commands, leases, and IPC contracts
  storage/         connection/transaction primitives, blobs, registry, migration ordering
  secrets/         narrow Secret/Key Authority client and service entrypoint
  status/          authenticated redacted local status

packages/workloads/yeoman_workloads/
  runner/          fixed worker process contract
  capsule/         one fixed Bubblewrap/systemd launcher
  definitions/     immutable allowlisted capsule/workload definitions
  reminders/       named reminder workload
  proactivity/     created only when Phase 2 implementation begins

packages/overseer/yeoman_overseer/
  reconcile/       typed desired/observed reconcilers
  control/         control.db ownership and status exchange
  status/          redacted health facts

packages/bridge/   WhatsApp native transport bridge
packages/shared/   wire schemas and narrow dependency-free primitives only
```

This is a logical target, not permission to create empty framework folders. A directory/module is added only with a shipped responsibility. Shared code must remain narrow; business rules stay with their owner.

Architecture tests enforce import ownership, network ownership, credential ownership, core write ownership, systemd ownership, and capsule-launch ownership.

Interaction, Trust, Memory, Actions, Egress, Delivery, Evidence, and Workloads each own their schema, repositories, queries, and migration definitions. `storage/` owns only connection/transaction coordination, blob mechanics, the typed storage registry, and global migration ordering; it does not centralize business repositories.

## 6. Trust and information-flow design

### 6.1 Identity and domains

A platform identity key includes platform, channel account/tenant, native identity, and native account epoch. Each active identity link is evidence-bearing, assurance-rated, time-versioned, reversible, and independently revocable. Conflicting privileged links quarantine authority.

Identity links do not merge domains, memories, ACLs, or history. A context domain represents one conversation lineage. Ordinary membership changes retain the domain; a material platform recreation/re-key creates a new domain with an explicit lineage edge.

Actor, initiating requester, workload/service principal, step-up operator, destination, and audience are distinct typed records.

### 6.2 Membership and audiences

Normalized membership snapshots are stored once as evidence-rated versions with source, observation time, completeness, assurance, and account epoch. Interactions reference the membership version used and preserve message-specific native recipients and unresolved evidence.

Authorization uses conservative reachable audience, not only intended recipients or confirmed readers. Missing/partial membership permits durable capture but blocks protected historical retrieval, shared disclosure, mutation, and unapproved external egress.

Membership expansion is prospective: a new member does not automatically gain assistant-mediated access to protected historical group content. Removed members lose future assistant-mediated access. Any rule that releases content to future members requires an explicit classification and grant.

### 6.3 Owner authority

Only the owner has read-all authority, and only when both are true:

- the owner identity is verified with sufficient assurance; and
- the destination is positively owner-private, with a reachable audience containing only owner and assistant endpoints.

Owner-wide reading does not authorize disclosure to a group, provider, tool, telemetry sink, or export. Each destination and egress attempt is separately authorized.

Identity linking/revocation, cross-domain grants, bulk export/enumeration, migration/cutover, restore, key/config/policy activation, suppression administration, future erasure, and other designated high-risk mutations require a short-lived, replay-resistant local step-up assertion bound to the exact action, target/diff, nonce, trace, and expiry. The trusted local interface uses a passkey, hardware-backed key, or equivalent phishing-resistant mechanism. There is no reusable admin mode.

Emergency recovery uses separately stored offline recovery material, rotates affected authority after use, and creates its own canonical audit trail. Recovery material never grants conversational owner-private status by itself.

### 6.4 Grants and derivation

Cross-domain grants are explicit, owner-authorized, non-transitive, purpose/workload-bound, destination/audience-bound, versioned, time-bounded, and revocable. Operations distinguish `read_raw`, `read_derived`, `summarize`, `quote`, `send`, `persist_in_destination`, and `export`.

Derived content retains every source edge, the highest source classification, the intersection of source audiences, and all source-domain/grant constraints. A transform such as anonymization may change handling class/tag or route eligibility only through a distinct recorded declassification decision. Declassification never adds a source domain, widens an audience, or permits a shared destination to use owner-private context; those changes require a separate explicit cross-domain/destination grant. Model output cannot declassify itself.

### 6.5 Execution envelopes

Every turn, model/tool attempt, child action, and persistent workload lease receives an immutable narrowing envelope containing principal/initiator chain, source domains/resources, destination/audience, capabilities, handling classes/tags, allowed routes, use/deadline/cost limits, and identity/membership/grant/policy/config/provider generations.

Envelopes are historical evidence, not perpetual permission. Revalidation occurs before materialization, sensitive recall, provider/tool egress, mutation, delivery, and after waits. Children may only narrow authority. Unknown or stale authority fails closed.

## 7. Memory architecture

### 7.1 Memory layers

Memory has independent axes rather than one overloaded type:

| Axis | Values |
|---|---|
| Lifetime | working turn, session, situation/workload, durable, distilled |
| Meaning | episodic, semantic, procedural, emotional, reflective |
| Visibility | origin domain, owner-private, explicit grant |
| Recall state | eligible, suppressed |
| Content state | available, quarantined, erasure_pending, erased |

Semantic type never determines visibility. The same person in two groups never causes memory sharing.

- **Working memory** is one bounded authorized turn materialization and is not an independent durable source of truth.
- **Session memory** summarizes a bounded sequence within one domain and generation.
- **Situation/workload memory** supports a named continuing task and carries the task's exact domain/purpose lease.
- **Durable memory** stores versioned facts or preferences with sources and review state.
- **Distilled memory** stores higher-level derived patterns while retaining every contributing source and restriction.

### 7.2 Capture and versioning

Raw interactions are immutable source evidence. A memory extractor first commits an attempt intent, calls a provider outside the database transaction, makes its result blob durable, then atomically commits the memory version, source/transform/ACL/classification/grant edges, current-version relationship, projection work, and attempt result.

Corrections create new versions linked by `corrects` or `supersedes`; they never rewrite raw history. Missing-blob, invalid-key, pending, quarantined, erasure-pending, or erased objects are not materializable or indexable.

### 7.3 Recall

Recall is a two-stage process:

1. an encrypted projection returns bounded candidate object IDs and non-authoritative hints;
2. canonical Trust state revalidates each candidate before content decryption/materialization.

Projection filters improve efficiency but never authorize. Projection failure may produce a bounded, explicitly authorized canonical fallback or a no-recall degraded mode; it never permits an unbounded direct database scan.

Owner-private cross-domain recall is allowed only under verified owner read-all. Any response destined for a shared domain is rematerialized under that destination's disclosure rules.

### 7.4 Suppression and future erasure

First release supports accurately named, reversible, domain-scoped suppression. Suppression removes derived memory from conversational recall, projections, background materialization, models, tools, and ordinary workloads while retaining protected canonical content and raw sources. `/forget` must be renamed/redefined truthfully or removed.

First-release suppression is an owner-only mutation. An individual suppression requires a verified owner-private/operator context, action-bound step-up naming the exact memory objects and domains, and a canonical receipt. Bulk or cross-domain suppression additionally requires a reviewed object manifest and impact summary bound to the same step-up. A non-owner may request owner review but cannot execute suppression. No suppression/deletion right is inferred from authorship, membership, identity linking, shared visibility, or model output.

First release defines provenance, key, and lifecycle seams for future erasure but exposes no canonical erase API, scheduled deletion, legal-hold engine, or deletion DSL. Future erasure requires a separate owner-approved policy and security review.

## 8. Physical data architecture

### 8.1 Authoritative stores

| Store | Contents | Writer |
|---|---|---|
| `core.db` | Interaction, Trust, Evidence, Delivery, Action, Workload command/effect, config/policy generations, and canonical memory metadata/provenance | Gateway core storage owner only |
| encrypted blob store | raw native payloads/messages/media, immutable transforms, memory content, large governed model/tool inputs/results, non-secret generation snapshots | restricted blob API |
| Edge spool/outbox | native events not yet accepted by core, producer epochs/sequences, staged blobs, transport recovery | one owner per producer/account |
| search/vector projection | rebuildable candidate indexes and non-authoritative hints | projection worker |
| `control.db` | Overseer desired/observed lifecycle, reconciliation leases/history, redacted evidence outbox | Overseer only |
| workload-local store | declared private checkpoints/internal state only | registered workload |
| Secret/Key Authority store | encrypted provider credentials, channel platform sessions/ratchets, object/domain key-encryption keys, credential/key epochs, rotation/revocation evidence | fixed local Secret/Key Authority only |

Logical modules retain owned tables, repositories, APIs, migrations, and tests inside `core.db`. Co-location is chosen because one Gateway writer must atomically commit causally coupled facts. It is not permission for a monolithic storage abstraction.

Canonical memory content remains in encrypted blobs. A separate canonical memory database is considered only after a measured independent-writer, key/lifecycle, performance, backup, or corruption-isolation requirement. No dormant dual-write design ships.

### 8.2 Durability

Authoritative SQLite uses local durable filesystems, WAL, `synchronous=FULL`, `foreign_keys=ON`, bounded busy timeout, strict `PRAGMA user_version` migrations, checkpoint/capacity alarms, and short transactions containing no external I/O.

A blob becomes referencable only after stage, encryption, hash verification, fsync, atomic no-replace rename, and directory fsync. Orphan encrypted blobs are recoverable garbage; a committed missing blob is an integrity failure.

Inbound capture is at-least-once. Canonical identity combines platform, channel account/epoch, native domain/conversation, native event ID, and event kind. Where the platform has no stable ID, Edge persists a source UUID and producer sequence once. Same ID/same digest is idempotent; same ID/different digest preserves both payloads, quarantines processing, and alarms.

### 8.3 Blob and key policy

Represent raw, normalized, transcribed/OCR, resized, anonymized, and provider-normalized content as separate immutable objects with transform edges. Never overwrite raw bytes.

Use opaque or domain/key-epoch-scoped keyed object identifiers, randomized authenticated encryption, per-object data keys, and wrapped domain/key-epoch keys. Do not use global plaintext-hash paths or global cross-domain deduplication that reveals equality or frustrates future erasure.

Credentials, platform authentication, session ratchets, cryptographic/recovery keys, authorization headers, TLS captures, and ephemeral signed URLs are not raw-message evidence and never enter general model contexts, traces, or blobs intended for ordinary inspection.

### 8.4 Secret/Key Authority

One fixed local `yeoman-secret-authority.service` is the sole owner of the protected secret/key store. It has no model access, conversational role, public listener, or arbitrary network capability. Authenticated role-limited Unix-socket operations resolve or update one named credential/session/key capability at a time, bind access to process identity and generation, and emit protected receipts. Gateway Controlled Egress, channel Edge/Delivery adapters, the backup coordinator, and recovery tooling receive only their exact allowed operation; no caller can enumerate or bulk-export plaintext secrets.

Active key-encryption material is loaded through an OS-protected systemd credential or kernel-backed local facility. Offline recovery material is separate from the host and backup ciphertext. Rotation creates a new epoch and rewrap plan; revocation immediately invalidates capability issuance and fences affected Edge/Egress/Delivery routes. Platform session updates use typed compare-and-swap generations so a stale Bridge cannot revive old ratchet/session state.

Backup includes an encrypted typed snapshot of the secret/key store and wrapped key objects, never offline root recovery material or plaintext credentials. Blank-root recovery installs the signed service first in recovery-only mode, authenticates the newest independent lifecycle-journal head, uses one separately stored offline recovery copy to unlock/verify the protected store, reconciles credential/key epochs and revocations, records the recovery, and keeps all Edge/Egress/Delivery capabilities fenced until explicit owner release. Compromise recovery rotates affected provider/channel credentials and key epochs before normal service.

### 8.5 Typed IPC

Every non-core producer uses authenticated, role-limited, versioned IPC with producer epoch/sequence, idempotency key, allowed record type/target, size/depth/concurrency limits, and replay/conflict behavior. IPC accepts no raw SQL, caller-chosen tables/paths, caller-minted Trust decision, arbitrary command, or systemd property.

## 9. Controlled Egress and model flexibility

### 9.1 Boundary

One logical `ControlledEgress` module inside Gateway owns bounded, latency-sensitive first-party chat, embedding, vision/OCR, ASR, and TTS provider calls. It contains strict registered adapters and is the only Gateway path that resolves model-provider credentials.

Web access, browsing, market/calendar integrations, shell/browser automation, backfills, persistent media processing, and other network-risky or long-running tools run as supervised workloads. Channel transport remains a separate Delivery boundary. Overseer never calls a model.

First release has no Egress Broker process. A narrow executor port and canonical attempt contract permit later extraction only if a real boundary appears: untrusted executable adapters, multiple OS trust domains, third-party credential use, independently auditable network enforcement, or a requirement to hide provider credentials from Gateway compromise.

### 9.2 Handling model

Data handling is independent of domain and audience:

1. `public`
2. `protected`
3. `restricted`
4. `host_only`

Unknown classification becomes `restricted + unclassified` and has no first-release external route. Hard-deny tags include `credential`, `platform_auth`, `cryptographic_key`, `recovery_material`, and `session_ratchet`. Other tags include direct identifiers, raw history, location, health, financial, voice, and biometric data.

Effective handling is the maximum source class, union of tags, intersection of source audiences, and conjunction of domain/grant restrictions. Owner read-all cannot override a route denial or hard-deny tag.

### 9.3 Route profiles

Model selection remains flexible through strict, immutable, owner-approved profiles. Each enabled profile declares stable processor identity, provider tenant/account, capability/modalities, exact network destination, processor chain, allowed handling classes/tags, required transforms, retention/training/jurisdiction posture, credential alias, budgets, retry/cancellation rules, and trace policy.

A caller may request an installed profile preference but cannot submit an endpoint, header, credential, tenant, or unregistered model string. Missing, ambiguous, stale, unsupported, or unauthorized profile data fails closed. Availability cooldown never creates authorization or selects an unapproved provider.

The selected credential alias resolves as late as possible for one attempt. Credentials are never bulk-loaded into process-global environment, mutable SDK globals, execution envelopes, core rows, blobs, prompts, logs, errors, or traces, and concurrent requests cannot inherit one another's provider state.

This permits model/provider changes by explicit configuration generation without hard-wiring the conversational architecture to one vendor.

### 9.4 Attempt flow

1. Commit an `ActionIntent` naming purpose, capability, source selectors, constraints, initiating envelope, and budget without claiming a concrete provider.
2. Revalidate Trust and materialize the exact authorized source objects.
3. Classify the complete payload, including system/persona instructions, history, memory, attachments, transforms, tool schemas/results, and derived request body.
4. Apply any required registered transform and commit its immutable object/provenance. First release has no anonymizer and no automatic declassification.
5. Resolve one strict profile and commit the egress decision, exact object manifest, narrowing child envelope, and `ProviderAttemptIntent`.
6. Resolve only that profile's credential alias and call the provider outside every core transaction.
7. Commit result, failure, cancellation, or explicit unknown with route/processor/endpoint/generation, timing, usage/cost, source/transform references, and available receipt.
8. Treat output as tainted untrusted data. A new decision is required before tool use, memory persistence, or delivery.

Every retry/fallback is a distinct attempt. Fallback cannot change destination trust, audience, input scope, retention, processor posture, or budget without rematerialization and a new decision. Hedged calls and cross-provider response caches do not ship.

### 9.5 Future anonymization seam

The insertion point is between authorized materialization/classification and route resolution. A future anonymizer receives an expiring narrowing envelope and exact protected objects, produces a separate immutable transform object plus mapping/provenance, and cannot itself widen disclosure. A distinct deterministic or owner-authorized declassification decision may change only handling class/tag and route eligibility. It cannot widen source-domain access or audience; that requires a separate explicit grant, and owner-private material cannot thereby become speakable in a shared destination.

No generic transform/plugin framework, anonymization code, automatic declassification, or dormant provider route ships in first release.

## 10. Workload Fabric and Execution Capsules

### 10.1 Workload contract

A workload begins with a canonical owner-authorized `WorkloadCommand`, immutable registered definition, service and initiating principals, context domain/purpose, constraints, and renewable Trust lease. Core owns semantic command/effect state. Overseer owns desired/observed lifecycle projection. systemd owns processes and cgroups. The workload owns only declared private checkpoint state.

Temporary and persistent workloads use the same contract. A long runtime or a restart does not widen or renew authority. Loss of Trust/control communication causes effectful work to become inert when the lease expires.

### 10.2 Capsule backend

Risky executables run through exactly one Linux implementation: a fixed systemd template launching a non-root Bubblewrap sandbox under a dedicated/dynamic worker identity with cgroup, resource, namespace, mount, capability, and seccomp hardening.

Capsules are lightweight containment, not an OCI platform. Do not add Docker/Podman, images, registry, backend plugins, a generic scheduler, or Kubernetes-like workload resources. A microVM or separate host is considered only after a demonstrated hostile-native-code or kernel-isolation requirement.

The boundary limits a child from unauthorized files, processes, credentials, sockets, state, and resources. It does not protect against compromise of the host kernel, Bubblewrap, systemd, browser sandbox, Gateway, root, or capsule-definition authority. Deployed UID isolation must be proven rather than inferred.

### 10.3 Immutable definitions

A caller references an installed `CapsuleDefinition`; it never supplies executable paths, shell strings, host paths, mounts, environment, Bubblewrap flags, systemd properties, seccomp policy, credentials, or network destinations.

A definition fixes executable/build digest, typed arguments, lifecycle, mounts/materialization, network profile, optional credential alias, state schema, resource/output limits, and policy/config generation. Missing any required isolation primitive disables the tool. There is no host or unsandboxed fallback.

### 10.4 Filesystem and results

Capsules receive only staged job-specific inputs or safe read-only descriptors derived from authorized object IDs. They cannot access source/runtime homes, core/blob/control databases, unrelated workload state, systemd/D-Bus, host sockets, devices, or arbitrary paths. Home/tmp are private; writable state is tmpfs or one declared quota-bounded volume. Mount creation is race-safe against path and symlink replacement.

Output enters a bounded encrypted quarantine/import path. Count, bytes, type/MIME, structure, decompression ratio, and classification are checked before canonical import, chaining, memory, or delivery. Downloads are parsed in a fresh no-network capsule. Result data never mints authority.

### 10.5 Lifecycle classes

- `inline_trusted`: not a capsule; pure deterministic core code with no external executable.
- `ephemeral_capsule`: fresh private home/tmp/state for a bounded job/session; destroyed on completion or TTL. Default for unauthenticated browsing, parsing, conversion, shell/code, and one-shot tools.
- `persistent_capsule`: registered autonomous or authenticated integration with immutable service principal, instance ID, domain/purpose, renewable lease, and one encrypted workload/domain/key-epoch state volume.

Persistent state is never the sole copy of messages, memories, commands, effects, or receipts. Each volume declares owner, schema, compatibility, migration, backup, integrity, retention, quota, and key policy. Credentials and browser cookies are separate hard-deny material.

Restore, reboot, rollback, definition upgrade, or state migration starts persistent work inert until integrity, compatibility, pending-effect reconciliation, current authority, and a fresh lease pass.

### 10.6 Network profiles

- `none`: private/unshared network namespace with no external path; default.
- `fixed_destination`: exact protocol, scheme, host, port, purpose, account, redirect, class, and short-lived capability for an authenticated integration.
- `public_web`: unauthenticated public retrieval using only public or separately approved query material.

A public-web capsule has no host/default route. Its only conduit is an authenticated short-lived capability to one supervised enforcement point over a deliberately exposed private transport. DNS and connection establishment occur at that point. It validates and pins addresses, reauthorizes redirects, and blocks raw/direct sockets, inbound listeners, bypass proxies, unsafe schemes, loopback, link-local, private/ULA/CGNAT, multicast/reserved/unspecified, metadata, host/LAN/control networks, encoded-address tricks, DoH, QUIC, WebRTC, and unapproved protocols/ports.

The choke point is an SSRF/destination boundary, not DLP. Trust must authorize the query/object released to any public recipient. Form filling, uploads, authenticated browsing, and mutations require exact destination grants and separate effect authorization.

### 10.7 Browser state

Unauthenticated browsing uses a fresh ephemeral profile. Authenticated browsing is a registered persistent integration isolated by platform identity, account, purpose, and context-domain policy. No global shared browser profile exists.

Cookies/session tokens are `platform_auth` secrets and never enter prompts, models, logs, traces, results, other profiles, or other capsules. Chromium's internal sandbox and site isolation remain enabled; `--no-sandbox` is forbidden. Browser control uses inherited/private authenticated IPC rather than a detached host-local HTTP controller.

## 11. Reactive message and effect flows

### 11.1 Inbound turn

```text
native event
  -> Edge durable spool and raw staged blob
  -> canonical interaction/domain/audience commit
  -> Trust envelope
  -> bounded Turn Kernel materialization
  -> optional Memory recall
  -> optional Controlled Egress attempt(s)
  -> response candidate
  -> fresh Trust and audience resolution
  -> durable Delivery intent
  -> channel dispatch
  -> platform ID/receipt or explicit unknown
```

The interaction transaction includes reply/quote/mention/thread/media relationships and a processing work item. Message semantics are not reconstructed later from plain text when native relationships exist.

### 11.2 Delivery

Every send/react/edit/delete or other channel mutation has a deterministic stable client/effect ID and canonical intent before dispatch. Delivery revalidates destination, reachable audience, membership, identity links, grants, classification, policy/config, and `DELIVER` fences immediately before the effect.

While an effect remains `dispatched`, a platform-documented same-key idempotent resubmission may be used by the same coordinator as reconciliation; it is not a retry from terminal unknown. Once an effect is classified `external_effect_unknown`, it is never automatically redispatched. Only authoritative query/receipt reconciliation or explicit owner disposition can close it. Restart preserves unknown as unknown.

The reply/quote/mention/thread target used for delivery is an explicit canonical relationship. A newer ambient message cannot silently retarget an already authorized reply.

### 11.3 Model/tool result to action

Provider/tool output is recorded as an attempt result but remains tainted data. It can propose a tool call, memory candidate, or response; it cannot choose an unregistered route, widen a source selector, mint a grant, or dispatch an effect. Each next step receives a fresh authorization decision and intent.

## 12. Failure containment and operational control

### 12.1 Additive capabilities

Effective operational permission is:

`Trust authorization AND current execution lease AND no applicable fence`

The fixed capabilities are:

1. `EDGE_CAPTURE`
2. `CORE_INGEST`
3. `PROCESS`
4. `EGRESS`
5. `DELIVER`

Administrative mutation is not an open capability. It uses an action-bound owner step-up plus absence of an emergency admin fence.

Fences are additive scoped denies with fixed hierarchy: global, channel/account, provider/tool route, registered workload, destination/action. Each records issuer/sequence, cause/evidence, generation vector, issue/recheck time, stickiness, clearance authority, and proof predicate. No actor clears another issuer's fence. Expiry/stale proof remains closed.

Security, integrity, restore, migration, cutover, and generation-mismatch fences are sticky and owner-cleared after current proof. Overseer may always move the system safer, but cannot grant Trust, renew Trust leases, or reopen a sticky fence.

### 12.2 Failure matrix

| Failure | Fence | Still available | Reopen proof |
|---|---|---|---|
| provider/model unavailable | scoped `EGRESS(route)` | capture, local work, approved other routes/delivery | current route/credential/generation checks and dwell; no widening |
| capsule/network control unavailable | scoped `EGRESS(tool/profile)` | core turns and other routes | isolation/network preflight |
| channel delivery failure | scoped `DELIVER(account/destination)` | inbound capture/core and other channels | transport readiness plus pending-attempt reconciliation |
| channel inbound failure | scoped `EDGE_CAPTURE(account)` | other channels and committed work | reconnect plus producer continuity |
| projection failure | scoped `PROCESS(recall)` or no-recall mode | canonical storage/bounded authorized fallback | clean rebuild and authorization tests |
| core/blob/evidence uncertainty | downstream capabilities and emergency admin | independently safe Edge capture only | sticky integrity/recovery/reconciliation proof |
| Trust/config/policy mismatch | protected processing/egress/delivery and admin | raw capture/quarantine | explicit activation and matching loaded generations |
| disk pressure | progressively background work, egress/delivery, core | reserved capture while durable | sustained headroom; sticky if write ambiguity occurred |
| Overseer/control unavailable | no new launch/reopen; leases expire inert | already-authorized bounded interactive work | current control sync/outbox reconciliation |
| backup late/failed | block risky upgrade/migration | ordinary traffic degraded | new verified generation |
| external effect unknown | exact action/destination | unrelated work | authoritative reconciliation or owner disposition |
| security incident | scoped/global downstream and admin | Edge only where independently safe | incident-specific owner proof |

### 12.3 Retry ownership

Each operation ID has exactly one retry owner. Process restart resumes reconciliation; it is never semantic retry.

Durable states are `not_dispatched`, `dispatched`, `succeeded`, `failed_permanent`, `cancelled_or_expired`, and `external_effect_unknown`. Trust `denied` is separate from operational failure.

Pure reads/computation may retry within the same authorized disclosure, route, cost, and deadline envelope, with a new recorded attempt. Capsules may retry private computation/checkpoints but never independently retry externally visible browser actions, uploads, purchases, integration writes, or messages. Action/Delivery owns those effects centrally.

For provider/model calls, an uncertain first attempt remains permanently recorded with its disclosed object manifest, route, possible cost, and outcome. A later attempt is a new attempt under the original action's remaining disclosure, retention, cost, use-count, and deadline limits and requires current authorization; it never overwrites or reclassifies the first unknown attempt.

Typed errors expose only a safe code/message, protected evidence reference, owner, retry safety, and optional retry time. Raw provider/platform errors and stack traces never become assistant content.

## 13. Configuration, startup, health, and reconciliation

### 13.1 Immutable generations

Release, configuration, policy, storage schema, provider policy, keys, and control state have explicit ordered generations and digests. Loading is pure and strict: no writes/backups, environment mutation, implicit migration, fallback to defaults after invalid content, ignored unknown keys, or activation.

Activation is compare-and-swap against the expected prior generation and records an owner-authorized generation receipt. Overseer detects drift, fences, and restarts fixed units; it never rewrites active content.

### 13.2 Startup order

Every process preflights executable/build, release/config/policy/schema/key/control generations, database/blob integrity, active fences, Edge cutoffs, leases, and pending/unknown effects.

The stack boots fenced and opens only after current proof:

1. verified `EDGE_CAPTURE` per account/spool;
2. `CORE_INGEST`, Edge replay/deduplication, and producer cutoff reconciliation;
3. `PROCESS` after canonical/Trust/projection readiness;
4. scoped `EGRESS` after route/tool/isolation proof;
5. scoped `DELIVER` after audience/delivery/receipt reconciliation.

Restore/cutover/integrity/security startup requires deliberate sticky-fence clearance. Persistent workloads renew authority separately and start inert.

### 13.3 Health truth contract

Expose one authenticated, redacted local status snapshot over typed Unix-socket control. Unauthenticated liveness reveals only that a process answers. Remove dormant HTTP control/health/metrics/webhooks.

Every health fact includes measurement time, evidence source, status including `unknown`, freshness/SLO, expected/observed build and generation, last success/failure, and collection error. Stale evidence is unknown, never green.

Status covers per-plane readiness/integrity/connectivity; effective fences; loaded digests; queue/backlog/lag/drop counts; last Edge/core/model/tool/delivery commits; pending/unknown effects; disk/WAL/blob growth and reserve; latest backup/verified disposable restore; and workload desired/observed/lease state. Detailed metrics contain no message content or chat/principal labels.

Health reads are side-effect free. Provider probes, test sends, restores, and repairs are explicit recorded actions.

### 13.4 Overseer reconcilers

Replace generic Markdown/LLM runbook interpretation with a small typed set for registered services, storage/capacity, backups, workloads, and active generations. Human runbooks remain documentation.

Reconciler outcomes distinguish `not_evaluated`, `no_action`, `attempted`, `succeeded`, `failed`, `unknown`, and `escalated`. Success requires a fresh observed postcondition with the expected unit/build/generation.

Overseer controls only fixed registered systemd units and definitions. It never accepts arbitrary shell, mounts, paths, environment, prompts, or model-generated operational commands.

## 14. Retention, lifecycle, and capacity

### 14.1 Indefinite canonical retention

First release retains canonical records indefinitely until an explicit future owner-approved policy changes that rule. Indefinite does not mean undeletable forever and does not make every operational byte canonical.

Canonical retained data includes:

- original message-bearing native envelopes, raw messages/media, edits/deletes/reactions, and reply/quote/mention/thread relationships;
- native IDs, identity/domain/audience/membership evidence used at event time;
- delivery intents, attempts, receipts, read evidence, and unknown outcomes;
- Trust envelopes/decisions, grants, and relevant generations;
- canonical memory versions, content, source/ACL/classification/grant edges, corrections, suppression, and lifecycle evidence;
- admitted transforms and governed model/tool/workload inputs/results that caused or informed behavior;
- consequential state/effect/recovery/migration/backup receipts.

Noncanonical bounded data includes rebuildable projections, caches, scratch, temporary staging, discarded browser subresources, duplicate SDK/wire encodings, routine reconcile/scheduler chatter, and hardened metadata-only operational logs.

Edge/outbox segments compact only after verified core acceptance and cutoff reconciliation. Operational logs may rotate after 14 days only after tests prove all consequential facts are canonical and logs contain no message/media/credential/session content.

### 14.2 Exact governed disclosure evidence

Each executed model/tool attempt retains one canonical governed disclosure record:

- ordered source/transform object manifest, tool schema/typed arguments, route/profile, effective parameters, serializer/adapter release, generations, size, and digest;
- exact transformed semantic prompt/query/attachments disclosed, excluding credentials;
- an exact credential-stripped wire body only when immutable objects plus a versioned deterministic serializer cannot reproduce it;
- exact bounded normalized result admitted into Yeoman, including partial content that caused an action;
- exact typed external effect, provider/platform request ID, and receipt.

Do not retain credentials/headers, packet/TLS data, hidden provider reasoning, raw SDK internals, duplicate chunks, indefinite raw error pages, or stack traces as canonical content.

### 14.3 Orthogonal lifecycle

Use three independent dimensions:

1. content availability: `available`, `quarantined`, `erasure_pending`, `erased`;
2. recall eligibility: `eligible`, `suppressed`;
3. provenance/version edges: `source_of`, `transformed_from`, `corrects`, `supersedes`.

Suppression is not erasure. Supersession is not deletion. Provenance edges survive lifecycle changes.

The signed/MACed monotonic lifecycle journal is applied before key access and projection rebuild during every supported restore. It prevents conforming restoration from reactivating suppressed/erased objects, but it does not claim forensic erasure from an old backup whose operator still possesses usable historical keys. Yeoman also cannot erase copies already delivered to channels, people, providers, or exports.

### 14.4 Capacity behavior

Indefinite retention is an operator capacity obligation, never a silent-purge license.

- Reserve space for core transactions, Edge capture metadata/spools, receipts, alarms, and lifecycle evidence.
- Quota projections, logs, scratch, workload state, backup staging, migration staging, and blob staging so they cannot consume the reserve.
- Monitor physical/logical bytes, object counts, growth, WAL/checkpoint/backup peaks, and forecast time to exhaustion.
- Soft watermark: delete proven rebuildable scratch/projections, stop speculative transforms, and throttle background work.
- Hard watermark: stop nonessential workloads and fence outbound effects; enter capture-only mode.
- Critical watermark: capture only if raw blob and canonical reference can commit durably. Do not acknowledge/advance upstream checkpoints where avoidable. Otherwise record explicit loss risk and never claim `capture_committed`.
- Never issue an effect if its intent/result/receipt evidence cannot still commit.
- Same-disk backup growth cannot consume the primary recovery reserve.

## 15. Local backup and recovery

### 15.1 First-release boundary

First release implements local backup only. Remote/off-host backup, remote transport, remote schedules, and remote RPO claims do not ship even as dormant code.

Git stores signed/tagged source releases, migrations, schemas, recovery tooling, current documentation, defaults, and sanitized templates. It never stores live/effective config or policy, identities/chat IDs/grants, memory, raw interactions/media, databases, spools, blobs, backup generations, keys, or real manifests.

RPO 0 applies to state already acknowledged as `capture_committed` across process crash, service restart, reboot, and power loss while the local fsync-honoring media remains recoverable. There is no first-release promise against destruction/compromise/theft/fire affecting every local copy.

### 15.2 Generation contents and ownership

A fixed `yeoman-backup.service` systemd unit is the sole backup coordinator. It requests typed snapshot/cutoff operations from each registered store owner, copies already-encrypted blobs and secret-store snapshots, authenticates and owns the generation manifest, and has no authority to inspect plaintext content or choose a restore. Overseer observes schedule, capacity, integrity, and postcondition evidence only. Restore and pruning are owner-step-up operator workflows, not Overseer actions.

One backup coordinator creates a named coordinated generation containing:

- SQLite backup-API snapshots of `core.db`, `control.db`, and every registered workload-local store whose definition declares state necessary for recovery, whether or not that private checkpoint is canonical product history;
- closed/unconsumed Edge spools and durable outboxes with producer cutoffs;
- all referenced encrypted blobs;
- storage registry, schema/config/policy/provider/key generations;
- the lifecycle-journal head known when the generation closes;
- pending/unknown intents and effect reconciliation state;
- key-escrow references, never unprotected key values;
- authenticated manifest committed last.

Rebuildable search/vector projections, caches, ordinary logs, deployment caches, and temporary files are excluded.

Create a generation at least every 24 hours and immediately before migrations, upgrades, cutover, or other high-risk state changes. Prefer encrypted versioned storage on a separate physical local disk. Same-disk backup is documented as a weaker failure domain.

Normal online generations are reconcilable high-watermark generations: each store snapshot records its own cutoff, and the closed manifest proves how Edge/outbox/core/control/workload state reconciles across those cutoffs. Only pre-migration and cutover generations claim one cross-store-consistent point, and those require all relevant writers to be quiesced.

Retain at least 14 daily generations plus the latest verified pre-migration/pre-upgrade generation until its observation gate closes. Never prune the last verified restore point. Pruning requires owner step-up and emits a receipt.

Recovery keys survive independently of the primary data disk. Keep at least two separately stored local recovery-key copies, exercise both through disposable restores, and never place either in Git or beside the only backup ciphertext it protects.

The monotonic lifecycle journal is a separate current recovery authority, replicated to every approved recovery medium independently of historical backup generations. Suppression or future erasure reports success only after the new head is durable on every approved medium. Restore must obtain and authenticate the newest known journal head before opening any selected generation; an older generation's embedded head is evidence only. Missing, stale, or conflicting current lifecycle authority makes restore fail closed, preventing a historical backup from resurrecting a later suppression/erasure.

### 15.3 Verification and restore

A generation succeeds only after manifest authentication, SQLite backup completion, quick/integrity/foreign-key checks, schema compatibility, blob reachability/decryption, key availability, lifecycle-generation validation, producer cutoff reconciliation, and pending intent/receipt reconciliation.

Restore the newest generation into disposable storage and reconcile it at least daily with all external models, tools, delivery, and control effects disabled. Perform a blank-root rehearsal before first release, after storage/key changes, and at least quarterly using only documented inputs and the signed release.

Restore is an authenticated operator action. It selects and verifies a known generation, restores leases/locks/jobs inert, boots capture-only, rebuilds projections, proves identity/authority/storage/effect/blob reconciliation, records the recovery, and deliberately releases downstream capabilities.

systemd restarts processes automatically. Overseer reconciles desired lifecycle and may fence. Neither automatically chooses nor restores an older data generation.

## 16. First-release product scope

### 16.1 Core release ships

- WhatsApp and Telegram, each passing the same Edge, Trust, Delivery, Evidence, replay, reconnect, and degradation contracts.
- A channel-specific modality matrix; unsupported modalities are explicit rather than implied.
- Local authenticated owner/operator CLI and action-bound step-up.
- Reactive turns, multi-layer memory capture/recall, truthful suppression, and owner-requested reminders.
- Strict owner-approved model/provider profiles and only named required capsule tools.
- Browser/public web only through the approved capsule/network boundary.
- Local backup/restore, typed status, systemd supervision, and typed Overseer reconciliation.
- Canonical migration evidence for every legacy state object.

### 16.2 Core release does not ship

- Discord or Feishu adapters, schema, config, dependencies, tests, or docs;
- dormant HTTP control/health/webhook/metrics APIs;
- external telemetry such as Langfuse;
- generic Markdown/LLM runbook execution;
- alternate event/IPC buses;
- unsupported provider/tool/route code;
- arbitrary persistent workload definitions;
- host-execution or unsandboxed fallback;
- generic broker, workflow/state-machine platform, CRDs/controllers, service mesh, HA cluster, distributed consensus, or control UI;
- retention/deletion DSL, legal-hold portal, canonical erase API, auto-restore, remote backup, or automatic declassification;
- persona evolution;
- legacy/compatibility paths kept “for flexibility.”

Two different manifests prevent private activation data from entering Git or the public build artifact:

- the non-secret **build manifest** names files/digests, packages, schemas, migrations, adapter/profile/tool/workload definition types, systemd templates, and current documents;
- the encrypted **activation manifest/config generation** names real channel accounts/tenants, credential aliases, enabled provider profiles, destinations, workload instances, grants, and effective policy/config generations.

The owner signs/activates both at their appropriate boundary. Anything not in the build manifest is absent from the artifact; anything not in the encrypted activation generation cannot become live.

## 17. Mandatory Proactivity Phase 2

### 17.1 Product commitment

Consciousness and speak-up are required target behavior. They are intentionally absent during the accepted **gate-bounded** gap between core cutover and completion of the Core Stabilization Gate in §18.7. They are then delivered as one registered persistent **Proactivity Workload**. The architecture program remains incomplete until it is live in the owner-approved destination scope and passes its observation gate.

Phase 2 implementation may proceed while the core stabilizes, but its activation begins immediately when the Core Stabilization Gate passes; no unrelated product milestone may be inserted first. If the gate remains blocked, status must show both the blocking core evidence and that the architecture program remains incomplete rather than silently converting proactivity into backlog.

The old consciousness/speak-up implementation is never retained as a compatibility runtime during the gap.

### 17.2 Authority and state

The workload principal is `assistant.proactivity`. It receives exact source domains, purpose, observation set, model routes, budget, and renewable lease. It never inherits owner read-all or aggregates across groups because the same people appear in them.

It may:

- observe explicitly granted canonical event/object references;
- request bounded model/tool attempts through standard ports;
- create observations, preferences, and memory candidates that each retain their exact source-domain, audience, classification, and grant constraints;
- commit typed expiring `SpeakUpProposal` objects;
- maintain declared private checkpoint state.

It may not open canonical databases/projections, hold channel/model credentials, choose raw network destinations, grant authority, persist canonical memory directly, or deliver messages directly.

Owner-private aggregation requires an explicit owner-private purpose/grant and can produce only owner-private output. It cannot feed a proposal for a shared destination. Observations and checkpoints use per-domain compartments and keys; there is no global taste/observation store.

Canonical budget consumption, proposal ID/content/source manifest, approvals, delivery intent/attempt/receipt/unknown state, cooldown, and deduplication survive restart and duplicate workload instances. A proposal is content, not authority.

Immediately before delivery, Trust and Delivery re-resolve destination, membership/audience, reply/mention relationship, identity/grant revocation, classification, current generations, quiet windows, budget/cooldown, fences, and lease. Restore/restart begins inert and never replays a sent or possibly sent proposal.

### 17.3 Behavior inventory

Semantic migration covers scheduled, burst, lull, and explicit/manual triggers; per-destination enablement/profile; quiet windows; daily/burst budgets; minimum gaps; approval/preview modes; reply targeting; outcome classification; and domain-scoped preference/taste learning. Taste/preference learning is ordinary derived memory with source ACL/classification and can never mutate the static persona, policy, system instructions, grants, or workload definition. The migration does not preserve legacy classes, prompts, event buses, JSON files, SQLite layout, or bootstrap wiring.

Live counts and hashes belong to the signed migration inventory rather than this normative contract. The inventory must be refreshed at each rehearsal and final cutover.

Legacy `sent` means dispatch evidence because the old code marks sent after in-memory queue publication, not after platform receipt. Migration reconciles each against outbound archive/platform IDs/receipts; unproven delivery becomes inert `external_effect_unknown` and is never replayed. Pending proposals, approval codes, timers, old desired-running state, scheduler state, and leases become cancelled/inert evidence, not Phase 2 authority.

### 17.4 Activation stages

Activation never widens automatically:

1. `inert_shadow`: authorized observation and sanitized deterministic replay comparison, with no external-effect proposal or delivery.
2. `proposal_only`: canonical expiring proposals; every delivery requires action-bound owner approval plus fresh Trust and Delivery authorization.
3. `narrow_autonomous`: only owner-approved destinations/action classes after the prior stages pass; every send still requires fresh authorization.

Phase 2 starts after the Core Stabilization Gate is proven, not merely after elapsed time. Only corrective, integrity, or security work may intervene before it as the next feature milestone. The Core Release build manifest contains no `proactivity/` package, entrypoint, config, unit, or dormant definition; a distinct Phase 2 build and encrypted activation manifest add them only when implementation begins.

Completion requires its migration, isolation, crash, restore, revocation, cross-domain, audience, and effect tests plus 14 consecutive days and at least 100 trace-complete proactive decisions with zero cross-domain, wrong-audience, unauthorized, or duplicate delivery. The 100 traces may include `no_action`, expired, denied, and deduplicated decisions as well as sends, so the gate creates no quota or incentive to speak. Silent decisions alone are insufficient: every enabled destination type and autonomous action class must also have at least one receipt-backed authorized delivery and outcome trace during staged activation.

### 17.5 Persona evolution removal

Persona evolution is absent from the executable product: no config key/schema, model route, schedule/job kind, CLI command, approval middleware, ledger, proposal writer, auto-apply path, feature tests, or support documentation.

The owner signs one exact static persona generation and digest. Before shared use, it is reviewed for destination-specific facts and hidden cross-domain instructions. No historical proposal or companion evolution file can affect runtime materialization.

Historical artifacts receive a manifest disposition. Preserve as generic protected inert evidence the effective persona generation, exact applied changes/diffs, proposals shown to the owner, source references, decision actor/context/time, before/after hashes, and enough content to explain which persona governed behavior. Hashes alone are insufficient for an applied change.

Routine no-proposal scans, duplicate renderings after normalization, scratch/model intermediates, caches, and unseen unused drafts with no unique evidence are noncanonical and may be deleted after proven disposition. Raw source conversations remain canonical in their original domains. Migration/reconciliation tests remain only while part of the recovery contract; persona-evolution execution tests and docs are removed.

## 18. Migration and cutover

### 18.1 Strategy

Use an isolated rewrite with offline comparison and one quiesced cutover. Do not run a long-lived shadow writer, dual canonical store, strangler, or channel canary because the old and new authorization/memory semantics are incompatible and the final product must be legacy-free.

The new source worktree and fresh state root remain disconnected from live channels/effects. Comparison uses deterministic synthetic/sanitized fixtures, local provider stubs, recorded authorized outputs, and explicit expected results. No rehearsal writes live memory, calls live delivery, or mutates live configuration. If captured private content is ever evaluated by an external provider, it must run through the target Trust/Controlled-Egress path under an exact authorized route and create canonical disclosure evidence; “side-effect-free” does not waive confidentiality.

The durable migration Edge format is source-native and versioned. Before cutover, tests prove that both the old rollback importer and new core importer can consume the same captured segment idempotently. This is migration compatibility tooling, not a second live Gateway path.

### 18.2 Preservation baseline

Before the first migration snapshot:

- stop active canonical retention purges, delete-after-transform paths, misleading soft-delete behavior, and arbitrary Overseer database cleanup;
- enumerate every database/table/file/blob/JSON log/spool/persona/config/policy/workload state source in `.yeoman` and relevant external local paths;
- classify every object as canonical, noncanonical, credential/auth material, rebuildable projection, or explicit legacy provenance gap;
- record counts, sizes, schemas, hashes, foreign-key relationships, ACL/domain/classification, producer cutoffs, and pending/unknown effects;
- produce a migration registry and authenticated source manifest.

Already deleted legacy data cannot be reconstructed. It is recorded as an explicit provenance gap, never silently treated as migrated.

### 18.3 Migration semantics

Each source object receives one receipt and disposition: preserved, normalized, deduplicated, quarantined, rebuildable/excluded, or explicit unrecoverable gap. Unknown origin, audience, membership, or classification maps to `legacy_unknown`: encrypted, quarantined, and non-materializable. It is excluded from ordinary recall, prompts, workloads, model/tool egress, memory derivation, delivery, and plaintext inspection. Owner-private/operator audit may inspect only metadata, ciphertext commitments, source location, schema, size, and migration evidence.

Plaintext becomes available only through a separately reviewed, action-bound `resolve_legacy_provenance` operation that supplies independent origin/audience/classification evidence, creates a new explicit classification/domain/grant decision and object version, and preserves the original quarantine record. Step-up proves the operator action; it never substitutes for missing data authority. Failed or incomplete resolution leaves the content non-materializable.

Migration deduplication is limited to the same domain and key epoch. It preserves every original object identity, source/provenance edge, ACL, audience, classification, digest, and migration receipt so each source is independently reconstructible. Raw interactions and protected derivatives never collapse across domains merely because plaintext or participants match.

Do not import current contact merges, shared semantic scopes, sessions, old grants, timers, approval codes, desired-running state, or background authority as target ACL/authority. Preserve historical native identities and known origin domains; mark missing historical audience as `legacy_unknown`/`membership_unknown`.

Two independent full rehearsals from consistent snapshots must reconcile every source object, raw blob, memory version, ACL/provenance edge, lifecycle state, effect, persona/proactivity record, configuration/policy generation, producer cutoff, and key reference with zero unexplained loss or widening. A blank-root restore from the migrated backup is also required.

### 18.4 Release construction and stale-code proof

The final release is built non-editably from a clean clone. It emits a strict manifest of files, dependencies, imports, units, routes, profiles, tools, schemas, config keys, migrations, docs, and package data.

Static allowlist scans and build/package inspection prove absence of:

- legacy namespaces/imports/entrypoints and compatibility branches;
- removed channel/provider/tool/workload/config paths;
- old database names used by runtime code;
- dual writers and direct cross-owner SQL;
- direct provider/network/credential paths outside registered owners;
- direct `bwrap`, arbitrary subprocess/systemd, or unsandboxed execution paths;
- stale docs/specs/diagrams/tests describing removed architecture;
- old service units, timers, install scripts, caches, and package artifacts.

Git history remains the historical code archive. The released checkout and runtime contain only current documentation and implementation. Superseded architecture specs, plans, session notes, diagrams, README sections, and operator procedures are deleted from the release branch rather than kept under an archive folder.

Migration readers, rollback importers, and old-schema tools are built as a separate signed **migration toolkit artifact** with no imports, entrypoints, units, package data, or installation path in the product runtime artifact. The toolkit is available offline for rehearsals and pre-commit rollback, cannot serve channel traffic or write target core outside the migration procedure, and is destroyed with the sealed legacy evidence after its recovery obligation ends.

### 18.5 Channel-session handoff

Exactly one process owns each live platform session or polling cursor at every point. A handoff receipt records platform/account epoch, old process/build and stop proof, last old native cursor/producer sequence/core acknowledgement, target Edge process/build, first target producer epoch/sequence, authentication/key generation, media cutoff, and acknowledgement mode.

For WhatsApp:

1. stop the old Bridge/Gateway consumer and prove the Baileys session owner is gone;
2. start the target WhatsApp Edge/Bridge in capture-only mode as the sole session owner using the approved migrated platform-auth generation;
3. durably spool each native event and stage/hash/fsync referenced media before any controllable acknowledgement or processing;
4. continue the recorded native/account epoch and start a new explicit target producer epoch/sequence linked to the old cutoff.

For Telegram:

1. stop the old poller and prove no second process uses the bot token/cursor;
2. start the target Telegram Edge as the sole poller from the recorded last accepted update offset;
3. durably spool the native update and media references before advancing the polling offset;
4. record the new producer epoch/sequence and reconcile Telegram redelivery/deduplication at the cutoff.

Before commitment, rollback stops target Edge ownership, closes/authenticates the spool segment, uses the separate signed migration toolkit to import/re-emit those native events and media into the untouched old release in original producer order with stable IDs, verifies old canonical acceptance, and reconciles every pending/unknown old and migration-time effect. Only then may old Delivery reopen and the old exclusive session/poller restart from the reconciled cursor. Rehearsals must prove this protocol for both channels, including reconnect, duplicate delivery, partial media, cursor ambiguity, platform-auth epoch change, and an unknown outbound effect. If exclusive takeover or rollback replay cannot be proven, cutover is blocked.

### 18.6 Quiesced cutover

1. Install sticky cutover fences.
2. Stop old Overseer, Gateway, cron, extractors, backfills, and every old writer; prove their PIDs/builds are gone.
3. Perform the per-channel exclusive handoff in §18.5 and keep target Edge capture-only; keep all external delivery/effects fenced.
4. Close a final authenticated old-state backup generation with exact producer cutoffs.
5. Migrate and reconcile the final delta.
6. Boot the new stack fully fenced and verify release/config/policy/schema/key/control generations, database/blob integrity, and effect state.
7. Open `CORE_INGEST`; replay/deduplicate migration-tagged Edge segments and reconcile cutoffs while `PROCESS`, `EGRESS`, `DELIVER`, timers, reminders, workload launches, and semantic/admin mutations remain fenced.
8. Wait for the first non-migration live Edge event. Commit that event, its exact producer cutoff, and the unique `target_committed` marker atomically. A quiet channel leaves the system at this gate; an owner may create an ordinary live channel interaction but no synthetic/migration record can satisfy it.
9. Only after the marker transaction succeeds, permanently remove pre-commit rollback authority and open `PROCESS`, then scoped `EGRESS`, then scoped `DELIVER` after their proofs. Pending processing for the marker event may then run.
10. Start Overseer last and prove it observes active generations rather than rewriting them.

The first non-migration live Edge event, its exact producer cutoff, and a unique `target_committed` generation marker commit atomically in one `core.db` transaction. Before that transaction, rollback restores the signed old release and untouched old state and replays Edge through §18.5. After it, recovery is forward-only; the legacy release may never execute and no rollback escape hatch exists. Any later release change is a forward migration that must preserve every post-cutover interaction, intent, receipt, memory mutation, and trace.

### 18.7 Core Stabilization Gate and sealed legacy bundle

The old release/state becomes an encrypted, offline, inert migration-evidence bundle. It is not a post-commit rollback runtime and is never installed, imported into production, executable, or visible to systemd after commitment.

Destroy it only after owner step-up and all of:

- 14 consecutive observation days and at least 100 trace-complete live interactions;
- every shipped channel/modality and DM/group authority path exercised;
- reconnect, restart, Edge replay, provider failure, capsule tool, reminder, and periodic workload proven;
- no unexplained count/hash/provenance divergence, access widening, stuck intent, unresolved integrity/security fence, or undisposed unknown effect;
- three verified post-cutover backup generations and disposable restores;
- one post-cutover blank-root recovery;
- current backup proven to contain every object whose only old copy was in the bundle;
- no unauthorized generation/fence change for twice the longest reconciliation interval.

Bundle destruction records its manifest and destroys its encryption key where practical without claiming unverifiable physical secure erase. Phase 2 reads only canonical migrated state, never the sealed bundle.

## 19. Verification and acceptance

### 19.1 Core release gates

Before cutover:

1. Owner signs the exact channel/modality/provider/tool/workload allowlist and static persona digest.
2. Clean clone installs from locked inputs and the strict release manifest matches the artifact.
3. Full test collection, CI, lint, type checks for public contracts, packaging/build, import ownership, strict config, and residual scans pass.
4. Every shipped channel passes common capture/audience/media/delivery/receipt/replay/reconnect/degradation tests plus its modality tests.
5. Every fence issuer/scope/reopen/generation/control-loss path and failure-matrix row passes.
6. Crash/fault injection covers blob/fsync/core/intent/model/tool/send/receipt/outbox/control boundaries and explicit unknown recovery.
7. Two complete migrations and one blank-root recovery prove lossless non-widening state preservation.
8. Expected-load soak runs 24 hours; twice measured peak runs at least two hours with bounded queues, WAL, blobs, backups, disk growth, health collection, and no loss/duplication.

### 19.2 Security and isolation gates

- Cross-domain property tests cover same-person/different-group, owner-private read-all, shared destination, membership expansion/removal, unknown membership, and revocation.
- Sentinel tests prove credentials, authentication/recovery material, hidden-domain content, and identifiers never appear in wrong provider traffic, IPC, logs, errors, traces, retry, or fallback.
- Deployed capsule escape tests cover filesystem, UID, process tree, descriptors/environment, systemd/D-Bus, devices, IPC/sockets, credentials, cgroups/seccomp, symlink races, and resource exhaustion.
- Network tests cover `none`, `fixed_destination`, and proxy-only `public_web` against raw TCP/UDP, DNS, redirects/rebinding, encoded addresses, IPv4/IPv6, metadata/private/control targets, DoH/QUIC/WebRTC, and disallowed ports.
- Browser tests prove active inner sandbox, ephemeral profiles, persistent profile isolation, cookie secrecy, no protected public-web upload, and download quarantine.
- Output-taint tests prove provider/tool results cannot widen domains, audiences, routes, actions, persistence, or delivery.

### 19.3 Data and recovery gates

- Canonical/noncanonical classification exists for every channel, provider, transform, tool, and workload.
- No active age/salience purge, delete-after-transform, arbitrary SQL cleanup, or misleading deletion contract remains.
- Suppression tests cover SQL/FTS/vector recall, context materialization, models/tools, background work, rebuild, owner audit, and reactivation.
- Synthetic future-erasure tests prove pending/erased content never materializes and old restores apply the current lifecycle journal or fail closed.
- Disk-pressure tests cover every watermark, reserve, Edge acknowledgement, blob/core commit, backup growth, and capture-only transition without false durability claims.
- Daily disposable restore and scheduled blank-root recovery prove keys, blobs, identities, authority, effects, cutoffs, and projections.

### 19.4 Completion definitions

The **Core Release** is complete after cutover and its sealed-bundle observation/retirement gate.

The **Architecture Program** is complete only after Proactivity Phase 2 reaches the owner-approved autonomous scope and passes its separate 14-day/100-trace observation gate.

## 20. Explicit non-goals

Do not implement speculative machinery for:

- a generic microservice/broker/event-store architecture;
- PostgreSQL/distributed transactions/consensus/HA cluster;
- Kubernetes CRDs/controllers/service mesh;
- a global workflow/state-machine runtime;
- a public control plane or control UI;
- generic model/tool/plugin/provider discovery;
- OCI/microVM backend interfaces before a demonstrated isolation need;
- dynamic mounts, commands, endpoints, systemd properties, or network policies;
- remote telemetry or remote backup in first release;
- arbitrary retention, deletion, legal-hold, or erasure policy engines;
- automated persona mutation or persona evolution;
- dormant code for future channels, providers, tools, anonymization, or compatibility.

Future modules extend typed seams only when an approved product requirement exists. They do not justify shipping empty frameworks today.

## 21. Implementation-program boundaries

This design is one normative architecture specification, but implementation is divided into reviewable delivery milestones rather than one giant code change:

1. release/build/architecture contracts and preservation baseline;
2. canonical storage, blobs, Trust, and migration framework;
3. Edge, Interaction, Turn Kernel, Memory, Controlled Egress, and Delivery;
4. Workload Fabric, Capsules, Overseer reconcilers, status, and local recovery;
5. WhatsApp/Telegram parity, full migration rehearsals, release proof, and cutover;
6. core stabilization and legacy-bundle retirement;
7. mandatory Proactivity Phase 2 and final program completion.

Each milestone must end with both software-architecture and security cross-review. No milestone may leave a second live authorization, storage, delivery, provider, capsule, or configuration path “temporarily” enabled.
