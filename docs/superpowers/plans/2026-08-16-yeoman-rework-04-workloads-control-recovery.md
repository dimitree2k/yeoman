# Yeoman Rework Milestone 04: Workloads, Control, and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add registered temporary/persistent workloads, one hardened Capsule backend, typed Overseer reconciliation, truthful local status, and coordinated local backup/recovery.

**Architecture:** Core owns semantic workload commands/effects, Overseer owns desired/observed lifecycle in `control.db`, systemd owns processes/cgroups, and workers own only declared private checkpoints. Risky executables run through one immutable Bubblewrap/systemd launcher; local backup is a separate fixed coordinator and restores always boot fenced/inert.

**Tech Stack:** Python 3.14, systemd user units/transient scopes, Bubblewrap, Linux namespaces/cgroups/seccomp, Unix sockets, SQLite/WAL, encrypted blobs, pytest/fault tests, Ruff, and mypy.

## Global Constraints

- Read the orchestration file, normative §§4.4, 4.6-4.8, 6, 8.1-8.5, 10, 12-16, and 19, plus `m03-handoff.json`.
- Overseer remains Kubernetes-like lifecycle reconciliation but never decides conversation, grants Trust, calls models, rewrites config, renews leases, restores backups, or clears another issuer's fence.
- systemd is the only machine/process supervisor.
- Exactly one Capsule backend ships: fixed systemd template plus non-root Bubblewrap.
- Callers cannot supply executable paths, shell strings, host paths, mounts, environment, Bubblewrap flags, systemd properties, credentials, or network destinations.
- Missing isolation primitive disables the definition; no host/unsandboxed fallback exists.
- Browser/public-web work runs only inside approved Capsules.
- First release implements local backup only; remote backup code/config/schedule does not exist.
- Restore/prune/activation are owner step-up operations and never automatic Overseer actions.
- The Core Release contains only named reminder and required tool workload definitions, not arbitrary definitions or proactivity.
- Real channel sessions, provider credentials, key-encryption keys, and migrated protected state remain unactivated until Task 9 proves encrypted Secret Authority backup, both independent recovery-key copies, current lifecycle-head recovery, and fenced blank-root restore.

---

## File map

- `packages/workloads/pyproject.toml` — canonical worker package.
- `packages/workloads/yeoman_workloads/runner/contracts.py`, `client.py`, `service.py` — fixed authenticated worker loop.
- `packages/workloads/yeoman_workloads/definitions/models.py`, `registry.py` — immutable installed definitions.
- `packages/workloads/yeoman_workloads/capsule/definition.py`, `launcher.py`, `result_import.py` — single Capsule contract/backend.
- `packages/workloads/yeoman_workloads/capsule/network.py`, `proxy.py` — `none`, `fixed_destination`, and `public_web` enforcement.
- `packages/workloads/yeoman_workloads/capsule/browser.py` — ephemeral/persistent browser state policy.
- `packages/workloads/yeoman_workloads/reminders/service.py` — named owner-requested reminder workload.
- `packages/gateway/yeoman_gateway/workloads/models.py`, `schema.py`, `repository.py`, `service.py`, `ipc.py` — canonical command/lease/effect API.
- `packages/gateway/yeoman_gateway/backup/contracts.py`, `coordinator.py`, `restore.py`, `prune.py` — fixed local backup owner and operator workflows.
- `packages/overseer/yeoman_overseer/control/models.py`, `schema.py`, `repository.py` — `control.db` ownership.
- `packages/overseer/yeoman_overseer/reconcile/base.py`, `services.py`, `capacity.py`, `backups.py`, `workloads.py`, `generations.py` — fixed typed reconcilers.
- `packages/gateway/yeoman_gateway/status/models.py`, `server.py` — module-owned redacted fact schema and authenticated Gateway status socket.
- `packages/overseer/yeoman_overseer/status/snapshot.py`, `client.py`, `server.py` — authenticated Gateway client, redacted aggregation, and operator-facing local snapshot socket.
- `packages/overseer/yeoman_overseer/app/main.py` — staged status/reconciliation process entrypoint.
- `deploy/systemd/yeoman-workload@.service`, `yeoman-capsule@.service`, `yeoman-web-egress.socket`, `yeoman-web-egress.service`, `yeoman-overseer.service`, `yeoman-backup.service`, `yeoman-gateway.service` — fixed units.
- `tests/workloads/`, `tests/capsule/`, `tests/overseer/`, `tests/backup/`, `tests/recovery/`, and `tests/systemd/`.

## Task 1: Implement canonical workload commands, leases, and runner IPC

**Files:**

- Create: Gateway workload files and worker runner files from the file map.
- Test: `tests/workloads/test_commands.py`, `test_leases.py`, `test_runner_ipc.py`, `test_restart_inert.py`

**Interfaces:**

- Consumes: owner-authorized `WorkloadCommand`, installed `WorkloadDefinition`, service/initiator principals, exact domain/purpose, constraints, and renewable Trust lease.
- Produces: canonical workload transition/effect records and one authenticated bounded worker API; no database handle crosses IPC.

- [ ] **Step 1: Write failing command/lease tests**

Assert temporary and persistent work use the same command; definition/instance/principal/domain/purpose/generation are immutable; children narrow the initiating envelope; lease expiry makes effectful work inert; restart/restore/duplicate runner starts inert; old desired-running state cannot renew authority.

- [ ] **Step 2: Write role-limited IPC tests**

Assert producer epoch/sequence, idempotency, allowed record type/target, size/depth/concurrency, replay/conflict, and lease generation are mandatory. Reject raw SQL, caller paths, arbitrary actions, Trust decisions, provider/channel credentials, and cross-instance checkpoint access.

- [ ] **Step 3: Run failing tests**

```bash
uv run pytest tests/workloads -q
```

Expected: FAIL on missing workload packages.

- [ ] **Step 4: Implement the command/runner boundary**

Core stores semantic command/effect/budget/lease state and emits a bounded outbox record. Runner polls only its authenticated instance, revalidates before every effect request and after waits, and stores only definition-declared private checkpoints. Canonical messages, memory, effects, and receipts never reside solely in workload state.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/workloads -q
uv run mypy packages/gateway/yeoman_gateway/workloads packages/workloads/yeoman_workloads/runner
git add packages/gateway/yeoman_gateway/workloads packages/workloads tests/workloads pyproject.toml uv.lock
git commit -m "feat(workloads): add leased registered worker contract"
```

## Task 2: Implement immutable Capsule definitions and the one launcher

**Files:**

- Create: `definitions/models.py`, `definitions/registry.py`
- Create: `capsule/definition.py`, `capsule/launcher.py`, `capsule/result_import.py`
- Create: `deploy/systemd/yeoman-capsule@.service`
- Test: `tests/capsule/test_definitions.py`, `test_launcher.py`, `test_filesystem.py`, `test_result_import.py`, `test_missing_primitive.py`

**Interfaces:**

- Consumes: installed `CapsuleDefinitionId`, typed argument object, staged authorized input refs, and one narrowing lease.
- Produces: bounded quarantined `CapsuleResult` plus launch/exit/import evidence.

- [ ] **Step 1: Write failing definition tests**

Assert a definition fixes executable/build digest, typed arguments, lifecycle, mounts/materialization, network profile, optional credential alias, state schema, resource/output limits, and policy/config generation. Reject caller-provided shell, executable, paths, mounts, environment, flags, properties, credentials, endpoints, or raw bytes outside staged objects.

- [ ] **Step 2: Write deployed isolation tests**

Launch a fixture and assert non-root distinct UID, private process/mount/network/IPC/user namespaces as required, dropped capabilities, `NoNewPrivileges`, seccomp policy, cgroup CPU/memory/PID/I/O limits, private home/tmp, read-only staged inputs, no source/runtime/core/blob/control access, no host/systemd D-Bus or sockets, no devices, and no sibling process visibility. Missing `bwrap`, user namespace, seccomp, UID isolation, or unit hardening must disable the definition.

- [ ] **Step 3: Write result quarantine tests**

Reject count/byte/MIME/structure/decompression-limit violations, symlink/hardlink/device/socket output, changed inode, and classifier failure. Parse downloads in a fresh `network=none` capsule; admitted output remains tainted and cannot chain, persist, or deliver without a new decision.

- [ ] **Step 4: Run failing tests**

```bash
uv run pytest tests/capsule/test_definitions.py tests/capsule/test_launcher.py tests/capsule/test_filesystem.py tests/capsule/test_result_import.py tests/capsule/test_missing_primitive.py -q
```

Expected: FAIL on missing Capsule implementation.

- [ ] **Step 5: Implement one fixed backend**

The launcher resolves only installed definitions, creates race-safe job directories beneath the state root, stages descriptors by object ID, invokes the fixed systemd template, and records exact build/definition/generation/resource facts. No Docker, Podman, OCI image, backend plugin, generic scheduler, detached controller, or direct `bwrap` path outside the launcher is added.

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/capsule -q
git add packages/workloads/yeoman_workloads/definitions packages/workloads/yeoman_workloads/capsule deploy/systemd/yeoman-capsule@.service tests/capsule
git commit -m "feat(capsule): add fixed Bubblewrap execution backend"
```

## Task 3: Enforce network profiles and browser state isolation

**Files:**

- Create: `capsule/network.py`, `capsule/proxy.py`, `capsule/browser.py`
- Create: `deploy/systemd/yeoman-web-egress.socket`, `deploy/systemd/yeoman-web-egress.service`
- Test: `tests/capsule/test_network_none.py`, `test_fixed_destination.py`, `test_public_web.py`, `test_browser.py`, `test_network_bypass.py`
- Test: `tests/systemd/test_web_egress_unit.py`, `tests/capsule/test_proxy_failure.py`

**Interfaces:**

- Consumes: installed network profile and short-lived destination capability.
- Produces: supervised connection decision/receipt from the fixed `yeoman-web-egress.service`; Capsule itself has no host/default route.

- [ ] **Step 1: Write `none` and `fixed_destination` tests**

Prove `none` has no external path. For fixed destinations, bind protocol/scheme/host/port/purpose/account/redirect/class/capability and reject destination/IP/generation changes, redirects outside policy, inbound listeners, and direct DNS/sockets.

- [ ] **Step 2: Write public-web bypass tests**

Cover raw TCP/UDP, DNS, IPv4/IPv6 literals, encoded addresses, redirects, DNS rebinding, loopback, private/ULA/CGNAT/link-local/multicast/reserved/unspecified, metadata/control/LAN, DoH, QUIC, WebRTC, unsafe schemes, and unapproved ports. Every connection must traverse one authenticated supervised enforcement point that resolves, validates, pins, and reauthorizes redirects.

Parse the fixed socket/service units and prove the Workloads package owns one private socket-activated process with public network access but no core/blob/control/workload-state access, no model/channel credential role, no arbitrary listener/config, a dedicated dynamic identity, resource limits, and authenticated short-lived destination capabilities. The Capsule launcher passes only one connected private transport descriptor; it exposes no host/default route or alternate proxy address.

Systemd owns socket creation, activation, restart throttling, and teardown; the Workloads package owns only the fixed proxy protocol and process implementation. The socket is inaccessible outside the fixed Capsule launcher identity. Milestone 04 Task 6 registers the socket/service with Overseer for observation and fixed restart only; Overseer cannot mint destination authority or alter proxy policy. Unit stop, crash, activation-loop, socket-owner/mode drift, stale proxy generation, malformed response, or lost authenticated connection must close `EGRESS(tool/public_web)`, terminate or safely fail the affected attempt, and require a fresh service/generation postcondition before reopening.

- [ ] **Step 3: Write browser isolation tests**

Assert unauthenticated browsing receives a fresh ephemeral profile; authenticated profile keys include platform identity/account/purpose/domain policy; cookies/tokens are `platform_auth` Secret Authority capabilities; profiles cannot cross instances; Chromium inner sandbox and site isolation remain active; `--no-sandbox` and host HTTP controllers are rejected.

- [ ] **Step 4: Run failing tests**

```bash
uv run pytest tests/capsule/test_network_none.py tests/capsule/test_fixed_destination.py tests/capsule/test_public_web.py tests/capsule/test_browser.py tests/capsule/test_network_bypass.py tests/capsule/test_proxy_failure.py tests/systemd/test_web_egress_unit.py -q
```

Expected: FAIL on missing network/browser controls.

- [ ] **Step 5: Implement and verify**

Use the inherited/private authenticated transport from Capsule to `yeoman-web-egress.service`, with no alternative route. Gateway Capabilities fences `EGRESS(tool/public_web)` when the service/socket, generation, DNS/connection enforcement, or fresh postcondition is unavailable; running jobs fail closed and return a typed `public_web_egress_unavailable` or `public_web_effect_unknown` result as appropriate. No automatic retry follows an unknown external effect. Status reports redacted readiness/generation/counts only. Trust authorizes every query/object released to the public recipient; uploads, forms, authenticated browsing, and mutations require exact destination grant plus effect intent.

```bash
uv run pytest tests/capsule tests/systemd/test_web_egress_unit.py -q
git add packages/workloads/yeoman_workloads/capsule deploy/systemd/yeoman-web-egress.socket deploy/systemd/yeoman-web-egress.service tests/capsule tests/systemd/test_web_egress_unit.py
git commit -m "feat(capsule): enforce destination and browser isolation"
```

## Task 4: Add the named reminder workload

**Files:**

- Create: `reminders/service.py`
- Modify: immutable definition registry and build manifest.
- Test: `tests/workloads/test_reminders.py`, `test_reminder_restart.py`, `test_reminder_delivery.py`

**Interfaces:**

- Consumes: owner-requested reminder command with domain/destination/action-bound authority and scheduled time.
- Produces: one leased trigger request routed through Trust, Action, and Delivery; no direct send.

- [ ] **Step 1: Write failing reminder tests**

Assert scheduled state is canonical, duplicate workers deduplicate, restore starts inert, expired/revoked/membership-changed reminders deny, unknown send never replays, and the worker cannot open databases or channel credentials.

- [ ] **Step 2: Implement the named definition and verify**

```bash
uv run pytest tests/workloads/test_reminders.py tests/workloads/test_reminder_restart.py tests/workloads/test_reminder_delivery.py -q
git add packages/workloads/yeoman_workloads/reminders packages/workloads/yeoman_workloads/definitions release/build-manifest.toml tests/workloads
git commit -m "feat(reminders): add leased owner reminder workload"
```

## Task 5: Establish `control.db`, fixed outcomes, and the Overseer fence issuer

**Files:**

- Create: `packages/overseer/pyproject.toml`
- Create: `control/models.py`, `control/schema.py`, `control/repository.py`
- Create: `reconcile/base.py`
- Test: `tests/overseer/test_control_db.py`, `test_fences.py`, `test_postconditions.py`

**Interfaces:**

- Consumes: registered desired state, authenticated fake observed facts, and the Gateway Capabilities issuer port.
- Produces: owned desired/observed/reconciliation history plus outcomes `not_evaluated`, `no_action`, `attempted`, `succeeded`, `failed`, `unknown`, or `escalated`.

- [ ] **Step 1: Write failing ownership and fence tests**

Assert only Overseer opens `control.db`; it cannot open core/blob/private workload state. The fixed issuer may add narrower fences through the Gateway port but cannot bypass validation, grant Trust, renew leases, rewrite config, clear another issuer's fence, or claim success without a fresh expected postcondition. Unknown/stale observation remains closed.

- [ ] **Step 2: Run the failing control tests**

```bash
uv run pytest tests/overseer/test_control_db.py tests/overseer/test_fences.py tests/overseer/test_postconditions.py -q
```

Expected: FAIL on missing Overseer control package.

- [ ] **Step 3: Implement the control owner and base reconciler**

Use WAL/FULL/foreign-key SQLite with module-owned schema. Reconciliation records desired generation, observed generation/freshness, lease, attempt, fixed action type, postcondition, and protected evidence reference. No arbitrary command, path, unit name, prompt, environment, or systemd property enters the model.

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/overseer/test_control_db.py tests/overseer/test_fences.py tests/overseer/test_postconditions.py -q
uv run mypy packages/overseer/yeoman_overseer/control packages/overseer/yeoman_overseer/reconcile/base.py
git add packages/overseer/pyproject.toml packages/overseer/yeoman_overseer/control packages/overseer/yeoman_overseer/reconcile/base.py tests/overseer/test_control_db.py tests/overseer/test_fences.py tests/overseer/test_postconditions.py pyproject.toml uv.lock
git commit -m "feat(overseer): own typed control state"
```

## Task 6: Add fixed service and generation reconcilers

**Files:**

- Create: `reconcile/services.py`, `reconcile/generations.py`
- Create: `deploy/systemd/yeoman-overseer.service`, `yeoman-workload@.service`, `yeoman-gateway.service`
- Test: `tests/overseer/test_service_reconciler.py`, `test_generation_reconciler.py`
- Test: `tests/systemd/test_registered_units.py`

**Interfaces:**

- Consumes: fixed unit/definition registry and authenticated fake build/generation facts.
- Produces: start/stop/restart attempts for only registered units and generation-drift fences with fresh postconditions.

- [ ] **Step 1: Write failing fixed-registry tests**

Register only Gateway, Secret Authority, backup, bridge/channel owners, workload template, Capsule template, public-web enforcement service/socket, and Overseer. Reject caller unit names/properties and all shell/LLM/runbook actions. Assert generation drift fences before restart and success only after expected PID/build/generation observation.

- [ ] **Step 2: Implement and verify**

```bash
uv run pytest tests/overseer/test_service_reconciler.py tests/overseer/test_generation_reconciler.py tests/systemd/test_registered_units.py -q
git add packages/overseer/yeoman_overseer/reconcile/services.py packages/overseer/yeoman_overseer/reconcile/generations.py deploy/systemd/yeoman-overseer.service deploy/systemd/yeoman-workload@.service deploy/systemd/yeoman-gateway.service tests/overseer/test_service_reconciler.py tests/overseer/test_generation_reconciler.py tests/systemd/test_registered_units.py
git commit -m "feat(overseer): reconcile fixed services and generations"
```

## Task 7: Add capacity, backup, and workload reconcilers

**Files:**

- Create: `reconcile/capacity.py`, `reconcile/backups.py`, `reconcile/workloads.py`
- Test: `tests/overseer/test_capacity_reconciler.py`, `test_backup_reconciler.py`, `test_workload_reconciler.py`

**Interfaces:**

- Consumes: registered resource/backup/workload desired state plus authenticated fake redacted facts until Task 8 wires production status.
- Produces: fixed throttle/fence/escalation requests, backup lateness evidence, and workload lifecycle requests; never restore/prune/lease renewal.

- [ ] **Step 1: Write failing reconciler tests**

Cover soft/hard/critical watermarks, backup late/failed, workload desired/observed/lease mismatch, control loss, stale facts, and every permitted outcome. Assert no deletion of canonical data, no backup selection/restore/prune, no Trust lease renewal, and no arbitrary workload definition.

- [ ] **Step 2: Implement and verify**

```bash
uv run pytest tests/overseer/test_capacity_reconciler.py tests/overseer/test_backup_reconciler.py tests/overseer/test_workload_reconciler.py -q
git add packages/overseer/yeoman_overseer/reconcile/capacity.py packages/overseer/yeoman_overseer/reconcile/backups.py packages/overseer/yeoman_overseer/reconcile/workloads.py tests/overseer/test_capacity_reconciler.py tests/overseer/test_backup_reconciler.py tests/overseer/test_workload_reconciler.py
git commit -m "feat(overseer): reconcile capacity backups and workloads"
```

## Task 8: Expose truthful authenticated local status and staged startup

**Files:**

- Create: `packages/gateway/yeoman_gateway/status/models.py`, `status/server.py`
- Create: `packages/overseer/yeoman_overseer/status/client.py`, `status/snapshot.py`, `status/server.py`, `app/main.py`
- Modify: `packages/gateway/yeoman_gateway/app/preflight.py`, `app/main.py`, `deploy/systemd/yeoman-overseer.service`
- Modify: `packages/overseer/yeoman_overseer/reconcile/services.py`, `generations.py`, `capacity.py`, `backups.py`, `workloads.py` to replace authenticated fakes with the production status client.
- Test: `tests/status/test_socket_auth.py`, `test_freshness.py`, `test_redaction.py`, `test_startup_order.py`, `test_side_effect_free.py`

**Interfaces:**

- Consumes: module-owned redacted health facts.
- Produces: one authenticated typed local snapshot; unauthenticated liveness returns process-answering only.

- [ ] **Step 1: Write failing truth-contract tests**

Every fact must include measurement time, evidence source, `ok/degraded/failed/unknown`, freshness/SLO, expected/observed build+generation, last success/failure, and collection error. Stale becomes unknown. Snapshot covers planes/fences/digests/queues/last commits/unknown effects/disk/WAL/blob/backup/restore/workload state without content, chat, principal, credential, or hidden-trace identifiers. Replace Tasks 5-7 authenticated fake collectors with this production Unix-socket client and rerun every reconciler outcome/postcondition test against it.

- [ ] **Step 2: Write startup-order tests**

Boot globally fenced; open verified `EDGE_CAPTURE`, then `CORE_INGEST` after cutoff reconciliation, `PROCESS` after core/Trust/projection, scoped `EGRESS` after route/capsule proof, and scoped `DELIVER` after audience/effect reconciliation. Restore/cutover/integrity/security fences require deliberate owner clearance; workloads start inert.

- [ ] **Step 3: Implement, verify, and commit**

```bash
uv run pytest tests/status tests/overseer -q
git add packages/gateway/yeoman_gateway/status packages/gateway/yeoman_gateway/app packages/overseer/yeoman_overseer/status packages/overseer/yeoman_overseer/reconcile tests/status tests/overseer
git add packages/overseer/yeoman_overseer/app/main.py deploy/systemd/yeoman-overseer.service
git commit -m "feat(status): expose redacted generation-aware health"
```

## Task 9: Implement coordinated local backup, restore, and pruning

**Files:**

- Create: Gateway backup files from the file map.
- Create: `deploy/systemd/yeoman-backup.service`
- Test: `tests/backup/test_generation.py`, `test_manifest.py`, `test_cutoffs.py`, `test_prune.py`
- Test: `tests/recovery/test_disposable_restore.py`, `test_blank_root.py`, `test_lifecycle_head.py`, `test_effect_reconciliation.py`

**Interfaces:**

- Consumes: typed snapshots/cutoffs from registered owners and already-encrypted objects.
- Produces: authenticated generation manifest committed last; backup cannot read plaintext or choose a restore.

- [ ] **Step 1: Write failing generation-content tests**

Require SQLite backup-API snapshots of core/control/recovery-required workload stores, closed/unconsumed Edge/outbox cutoffs, referenced encrypted blobs, storage/schema/config/policy/provider/key generations, independent lifecycle head, pending/unknown effects, key-escrow references, and manifest-last authentication. Exclude projections/caches/logs/deployment/temp.

- [ ] **Step 2: Write schedule/retention/failure-domain tests**

Require at least daily generation plus pre-migration/upgrade/cutover; at least 14 daily plus latest verified pre-change; never prune last verified restore; prune requires step-up/receipt. Assert same-disk capacity cannot consume primary reserve and no remote transport/config exists.

- [ ] **Step 3: Write restore tests**

Verify manifest, SQLite integrity/foreign keys/schema, blob reachability/decryption, keys, newest independent lifecycle head, cutoffs, and pending effects. Restore leases/locks/jobs inert, boot capture-only, rebuild projections, and deliberately release capabilities. Missing/stale/conflicting lifecycle authority fails closed. Exercise both separately stored recovery-key copies in independent disposable restores.

- [ ] **Step 4: Run failing tests**

```bash
uv run pytest tests/backup tests/recovery -q
```

Expected: FAIL on missing backup implementation.

- [ ] **Step 5: Implement local-only coordinator and operator workflows**

The service has typed snapshot clients and ciphertext copy permission only. Restore/prune commands require local action-bound step-up and never run from Overseer. Keep two independently stored local recovery-key copies outside Git and backup ciphertext; tests use disposable fixture keys, never live keys. Only after the signed encrypted secret-store generation and fenced blank-root restore pass may a separate activation step import real credential/session aliases and migrated protected-state key references.

- [ ] **Step 6: Verify and commit**

```bash
uv run pytest tests/backup tests/recovery -q
git add packages/gateway/yeoman_gateway/backup deploy/systemd/yeoman-backup.service tests/backup tests/recovery
git commit -m "feat(backup): add coordinated local recovery generations"
```

## Task 10: Prove capacity degradation and close Milestone 04

**Files:**

- Create: `tests/integration/test_capacity_fences.py`, `test_control_loss.py`, `test_workload_capsule_trace.py`, `test_backup_restore_trace.py`
- Create: `artifacts/program/handoffs/m04-handoff.json`
- Modify: orchestration state after review.

- [ ] **Step 1: Write and run capacity, control-loss, trace, and recovery integration tests**

Soft watermark removes only proven rebuildable scratch/projections and throttles background work. Hard stops nonessential workloads, fences effects, and enters capture-only. Critical captures only when raw blob+canonical reference are durable and otherwise records explicit loss risk without false acknowledgement. Control loss prevents new launch/reopen and lets leases expire inert.

```bash
uv run pytest tests/integration/test_capacity_fences.py tests/integration/test_control_loss.py tests/integration/test_workload_capsule_trace.py tests/integration/test_backup_restore_trace.py -q
```

Expected: PASS against committed Milestone 04 modules; any failure is fixed in the owning earlier task with a new commit and affected reruns.

- [ ] **Step 2: Commit the green integration contract**

```bash
git add tests/integration/test_capacity_fences.py tests/integration/test_control_loss.py tests/integration/test_workload_capsule_trace.py tests/integration/test_backup_restore_trace.py
git commit -m "test(integration): prove workload control and recovery"
```

- [ ] **Step 3: Run the complete gate**

```bash
uv run pytest tests/workloads tests/capsule tests/overseer tests/status tests/backup tests/recovery tests/systemd tests/integration tests/architecture -q
uv run ruff check .
uv run mypy packages/shared packages/gateway packages/workloads packages/overseer
uv build --all-packages
uv run python scripts/release/verify_artifact.py --manifest dist/build-manifest.json --dist dist
git diff --check
```

Expected: all pass; residual scans find no generic runbook execution, model-enabled Overseer, alternate sandbox, host fallback, dormant proactivity, auto-restore, or remote backup.

- [ ] **Step 4: Obtain reviewer GOs and advance**

Architecture reviews core/control/systemd ownership and workload semantics. Security reviews deployed Capsule/network/browser isolation, fence authority, secret exposure, status redaction, lifecycle restore, and backup privileges. After both GO:

```bash
git add artifacts/program/handoffs/m04-handoff.json artifacts/program/contracts/accepted-contracts.json docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md
git commit -m "docs(architecture): accept workload and recovery milestone"
```

Expected: next state is `READY_FOR_MILESTONE_05`.
