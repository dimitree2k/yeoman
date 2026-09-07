# TradingAgents ↔ Hermes ↔ Yeoman Implementation Plan

> **Status:** Revised 2026-09-08 following owner-reviewed architecture feedback.
> Documentation revision only; implementation, migration, remote installation,
> paid experiments and service exposure require separate authorization.
>
> **Spec:** `docs/superpowers/specs/2026-09-07-tradingagents-hermes-yeoman-design.md`
>
> **Historical baseline:** `docs/superpowers/specs/2026-09-07-tradingagents-hermes-yeoman-baseline.md`

## Goal and initial scope

Keep Yeoman on Host A; move Hermes to Host B alongside upstream TradingAgents.
Preserve A2A on both edges, with independently configurable local/remote policy:
remote bearer-token authentication over encrypted private transport, or explicit
loopback-only local access with optional token. Placement changes configuration,
not research schemas or worker identity.

Initial route: Yeoman -> Hermes `yeoman-bridge` -> `tradingagents-research`.
Initial operation: `analyze`, one stock ticker, one execution worker. Start with
an owner-authorized on-demand request, then Yeoman-owned scheduling. A direct
Yeoman→TradingAgents route is permitted as a separately tested slice for fixed
reports where a Hermes model relay adds no value. Never automatically use both
routes or switch routes on an uncertain timeout.

Yeoman's existing worker key remains `hermes`; `/yeoman-bridge` is its served
route and `yeoman-bridge` its Hermes profile. Keep the profile generic, not weekly
or stock-specific. Yeoman owns chat policy/persona, conversational memory and
final delivery. Hermes/TradingAgents retain only isolated worker/research state.

## Global constraints

- Preserve existing uncommitted changes; no reset, unrelated cleanup or automatic staging.
- Do not install TradingAgents as a production runtime on the RPi.
- Import the named upstream project through a separate thin adapter; no fork,
  replacement, graph copy or direct Hermes-internal imports in Yeoman.
- No trades, broker credentials, channel tools, arbitrary callbacks or shared
  Yeoman/Hermes home mounts in the specialist.
- No secrets, private reports or raw chat memory in Git, prompts or ordinary config.
- Use supported Hermes profile/configuration commands; verify supported secret
  references and restriction features instead of inventing configuration keys.
- Skills and Agent Cards are guidance/advertisement, not security enforcement.
- No new reverse Yeoman listener or Hermes-owned scheduling in the initial slice.
- Tests use real imports/behavior with isolated homes and fake providers where
  appropriate; mocks do not substitute for the separately authorized live run.
- Every runtime/external change requires scoped backup, read-back and rollback.
- The baseline remains historical; do not rewrite it to pretend the new topology
  was already verified. These planning paths are currently Git-ignored: resolve
  deliberate tracking during implementation handoff, never silently force-add.

## Task 0 — Authorization, deployment facts and version gate

No runtime changes before explicit implementation authorization.

Record:

- Actual Host B address, encrypted private-network path and service/container form.
- Deployment matrix for each edge: logical name, URL, authorized origin/path,
  local/remote mode, secret reference, caller identity and listener/firewall policy.
- Separate Yeoman→Hermes and Hermes→specialist credentials where remote; a separate
  direct-caller credential only if that route is enabled.
- Pinned TradingAgents revision/dependencies, adapter/A2A SDK and compatible
  Hermes/Yeoman versions. Recheck review source observations against these pins.
- Explicit authorization and budget for installation and the cost-capped experiment,
  chosen provider/model, analysis-date semantics and research-state retention.
- Secret resolution/rotation mechanism and previous working configuration snapshot.

Retain the specified one-ticker, one-worker, Yeoman-scheduler defaults. Do not
turn obvious defaults into additional owner decisions. Reconcile the existing
network-separation/deferred-A2A gates from the earlier worker design before migration.

**Acceptance:** deployment/security facts and paid-work limits are recorded;
no ambiguous routing, credentials or unapproved service exposure remains.

## Task 1 — Early bounded upstream experiment and contract mapping

After Task 0 authorization, prepare an isolated experiment on Host B, not a public
service. Pin/install upstream and configure only the approved provider/data keys.

- Exercise one ticker with bounded debate/risk rounds and concurrency one.
- Map accepted fields to actual upstream API/config: company/date/asset arguments,
  `max_debate_rounds`, and `max_risk_discuss_rounds`.
- Reject unsupported `lookback_days`, `depth` and `output_language`; do not silently
  accept them. Final presentation language remains Yeoman's responsibility.
- Document date cutoff/timezone/non-trading-day behavior and historical data limits.
- Inspect returned structure and available provenance; record absent source URLs
  or timestamps honestly. Do not manufacture citations to fit the proposed schema.
- Identify upstream cache, research-memory, checkpoint and report writes; isolate
  them and document retention. No raw conversational memory input.
- Measure graph/model/data time, total time, result size and actual/estimated cost;
  account for retries and possible additional reflection work.

Record all waiting limits across Yeoman, Hermes and the proposed adapter, including
headroom for model orchestration and response validation. A single fast run is not
proof of reliable tail latency; bounded repeat measurements require budget coverage.
Choose synchronous only when measurements justify fitting the entire chain. Default
to durable A2A task polling for long work; do not simply inflate nested HTTP waits.
Task 7 validates this choice on the complete route before production.

**Acceptance:** real bounded evidence and field mappings exist, or the blocker is
reported. No fake timings, guessed costs or mock-only production decision.

## Task 2 — Harden A2A connection boundaries before remote enablement

### Yeoman client/configuration

Likely files:

- `packages/gateway/yeoman_gateway/a2a/client.py`
- `packages/gateway/yeoman_gateway/a2a/registry.py`
- `packages/shared/yeoman_shared/config/schema.py`
- Focused tests in `tests/gateway/` and `tests/shared/`

Implement with behavior tests first:

- Remote worker configuration requires a secret reference and a non-empty resolved
  token; missing credentials fail before any discovery or task network request.
- Explicit local no-token mode remains loopback-only. No automatic auth downgrade.
- Validate discovered endpoints against configured origin/path or explicit operator
  allowlist. Reject another origin, unexpected served profile, TLS downgrade,
  credential-bearing URL and unsafe redirects before forwarding token/task data.
- Preserve stable logical worker key and bound-context policy; model text cannot
  choose URLs, credentials, routes or arbitrary persistent context IDs.
- Separate connect/read waits from true execution deadlines; bound response bytes
  before parsing and retain truthful unknown/nonterminal state on caller timeout.

### Hermes inbound and outbound

- Verify how the installed version restricts Yeoman's peer to `/yeoman-bridge`.
  Test the same credential against root/default and another profile/tenant. If native
  routing cannot enforce scope, use a dedicated narrow listener or authenticated
  path-restricting proxy; do not expose the broad default listener as a shortcut.
- Verify enforceable named-peer restrictions on outbound A2A. Current general
  `a2a_call` accepts URLs; adding one configured peer is insufficient.
- If no suitable native restriction exists, implement a small typed research
  tool/plugin backed by A2A, with configured-peer dispatch and deterministic schema
  validation. Do not expose unrestricted discovery/call/fan-out in this profile.
- Verify profile-scoped secret resolution, destination binding and redirect handling
  in that outbound path too. Do not assume Yeoman's fix secures Hermes' client.

**Acceptance:** tests cover both edges in local and remote modes, missing/wrong/
rotated tokens, endpoint/profile changes, redirects and arbitrary model-supplied
URLs. Yeoman credentials cannot authorize Hermes root/other profiles. General
Hermes A2A capability remains available outside the bounded worker profile.

## Task 3 — Thin specialist adapter and durable execution

Keep service code separate from the upstream checkout, importing its pinned package.
Use the pinned compatible A2A SDK where suitable; do not invent a parallel job API.

Implement:

- Research-only Agent Card and explicit supported-method/auth metadata.
- Per-edge local/remote authentication rules from the spec; per-caller authorization
  for submission, task lookup/list/cancel and result/artifact access.
- Strict versioned schema parsing, unknown-field rejection and field mapping from
  Task 1. Bounds on ticker/date/operation/rounds, input/output bytes and admission.
- Canonical structured result with per-ticker outcomes, evidence IDs/records,
  retrieval/data timestamps or explicit absence, warnings/errors and disclaimer.
- Terminal status mapping including partial, rejected, failed, timed out,
  cancelled and expired; A2A lifecycle state is distinct from research completeness.
- Atomic durable admission keyed by authenticated caller + `request_id`, with a
  fingerprint of validated request content, stable `run_id` and task mapping.
- Same key/same payload returns the existing run; changed payload conflicts.
  Simultaneous duplicates and restart must not launch duplicate expensive work.
- Bounded task store/result retention with replay policy after cleanup; reconcile
  interrupted runs rather than blindly restarting them.
- One cancellable execution worker/process with an enforced execution deadline,
  bounded provider calls/retries, explicit backpressure and resource limits.
- Per-run/per-period spend admission and accounting with conservative in-flight
  reservations; record billing lag/estimates and use provider caps where available.
  Logging cost after completion alone does not enforce a hard cap.
- Redacted logs, health/readiness and orderly shutdown/restart behavior.

Even a synchronous first slice must persist idempotency/run state before execution.
An HTTP timeout is an unknown execution outcome, not confirmed termination.
Cancellation is not successful until local work stops; already submitted provider
requests may still complete/be billed and this limitation must be surfaced.

For long-running work, implement A2A `SendMessage` submission, `GetTask` polling
and `CancelTask` with persisted lifecycle state. Advertise only tested capabilities.
A small durable store and one worker are enough initially; no distributed queue stack.

Tests without live credentials:

- Valid request reaches a fake graph with exact mapped arguments/config.
- Unsupported fields, unsafe tickers, dates and over-limit input reject.
- Missing/invalid remote auth rejects; explicit local trust behavior is tested.
- Provider failure, partial data, missing provenance and malformed/oversize output
  do not become a complete verified report.
- Concurrent duplicates, changed payload, service restart, cleanup/replay and
  interrupted-run reconciliation preserve admission guarantees.
- Executor deadline/cancel stops local work; caller disconnect alone does not
  falsely mark the run stopped. Budget exhaustion prevents new provider dispatch.
- Report text containing instructions is treated as inert data and cannot cause
  tool calls, callbacks, routing changes or delivery.

**Acceptance:** real adapter/SDK contract and lifecycle tests pass with fake providers;
no claims of hard runtime/spend limits unsupported by executable enforcement.

## Task 4 — Hermes research capability and end-to-end task transport

Configure only the approved `yeoman-bridge` profile through supported Hermes paths:

- Preserve intended web/skills capability; keep terminal, file mutation, messaging,
  arbitrary delegation and unrelated toolsets disabled.
- Enable only the enforced A2A-backed research capability established in Task 2.
  Agent Card projection and actual callable tools must be checked separately.
- Configure `tradingagents-research` as a stable named peer. In the primary
  deployment use loopback A2A; a remote placement uses a separate required secret.
- Update generic research guidance to build only supported parameters, treat
  evidence as untrusted and preserve failure/partial/provenance limitations.
- Carry Yeoman's originating request ID unchanged. Persist outer request/task to
  specialist run/task mappings in code; do not rely on an LLM recreating handles.
- Preserve specialist JSON as a canonical structured artifact through deterministic
  code, with optional Hermes synthesis separate. No model-only JSON round-trip.
  If artifacts use references, enforce retrieval ownership, origin and size limits.
- For asynchronous operation, extend every participating hop: Hermes must expose
  a durable outer task linked to the specialist task, and Yeoman must be able to
  retrieve it after waiting ends/restart. Do not poll by spending an LLM turn.
  Confirm cancellation propagation and partial failures across both edges.

**Acceptance:** capability inspection and actual execution agree; arbitrary peer
selection is blocked; result payload/IDs survive unchanged; long tasks, when needed,
work across the full route rather than just inside the specialist.

## Task 5 — Deployment and migration boundary

Deploy only after applicable Task 2–4 tests pass and Task 0 authorization covers it.

- Use dedicated adapter account/container, supported pinned runtime and private
  provider secrets. No Yeoman/Hermes homes or channel credentials mounted.
- Relocate Hermes/profile/shared-skill dependencies through the approved migration
  procedure. Verify profile paths/permissions and model/secret resolution on B;
  do not assume old absolute symlinks survive migration.
- Configure Yeoman→Hermes on the encrypted private path with a dedicated peer
  credential and only the authorized served route accessible.
- Configure Hermes→specialist loopback-only in the shared network namespace.
  For separate container networks, use an explicitly secured network deployment,
  not a misleading “same host therefore unauthenticated” assumption.
- Bind/firewall each listener according to its actual callers. Do not expose the
  specialist to Host A unless the direct route is explicitly enabled.
- Verify service restart, readiness, shutdown, resource bounds, retention and
  sanitized observability. Supervise services with systemd/container tooling.
- Verify remote unauthorized peer access, token rotation and secret-free reporting.
  A public Card, if chosen, reveals only approved metadata; task auth is mandatory.

**Acceptance:** receiving-side profile identity and task authorization are verified,
not just Card/health success. Local specialist is unreachable off-host; remote
Hermes accepts only intended peers/paths. Existing Yeoman worker still works.

## Task 6 — Yeoman result policy, triggers and optional direct route

- Keep delegation owner-policy-gated and preserve bound session/context rules.
- Send only explicit research parameters and necessary sanitized task context,
  never full chat history or raw conversational memory.
- Validate canonical response schema, request/run/task IDs, ticker/date, truthful
  status, size, evidence references and disclaimer before rendering/storage.
- Keep suggested synthesis separate from source data; do not hide missing evidence
  or treat a valid disclaimer as proof of factual correctness.
- Start with owner-requested execution. For scheduling, persist due request IDs,
  selected route, task handles, progress and delivery acknowledgement in Yeoman.
- Distinguish research completion from the separate policy-gated delivery effect;
  test duplicate schedule suppression, restart pickup and delivery idempotency.
- Reconcile unknown task state before retry. Never automatically switch route or
  provider after an unavailable/timeout response.

Optional separately enabled direct route:

- Add a separate named specialist worker and caller credential when remote.
- Select `route=yeoman_direct` before execution for fixed parameterized jobs.
- Apply identical input/result validation, budget, task and delivery guarantees.
- Verify one request uses one route; the direct path avoids a needless Hermes
  model relay but does not bypass Yeoman policy or A2A security.

Hermes-owned schedules remain deferred. A later slice should use Yeoman polling
an authorized durable result collection, with ownership/cursor/acknowledgement
and replay tests; do not assume local IPC or add a reverse listener now.

**Acceptance:** one authorized request and one manually observed schedule produce
one truthful result/delivery decision, with durable recovery and no direct
Hermes/TradingAgents channel output.

## Task 7 — Full-route measurement and verification matrix

Run checks in increasing scope:

1. Real-import unit/contract tests with fake providers for schema, auth and lifecycle.
2. Both-edge local/remote configuration tests using temporary service fixtures.
3. Real private-network tests of the selected deployed topology and profile identity.
4. Hermes runtime capability, destination restriction and canonical-result tests.
5. Yeoman owner-policy, validation, task recovery and delivery tests.
6. One authorized cost-capped live one-ticker end-to-end request using configured
   providers; record graph/model/data/transport/total time, output size and cost.
7. Failure injections: unavailable peer, provider failure, caller/read timeout,
   executor timeout, cancellation, restart, malformed output and budget exhaustion.
8. Duplicate/replay/conflicting-payload tests and one manually observed schedule.
9. If direct routing is enabled, repeat relevant full-route checks without Hermes.

Required connection matrix for each applicable edge:

| Mode/scenario | Required outcome |
| --- | --- |
| Explicit loopback, no token | Works only under documented local trust policy |
| Loopback with token | Valid token works; invalid token rejected |
| Remote encrypted private path, valid peer token | Authorized route works |
| Remote missing/empty secret | Client fails before network request |
| Missing/wrong/rotated token at server | Task rejected |
| Unauthorized network peer | Listener/firewall policy blocks access |
| Card points elsewhere / wrong profile / unsafe redirect | No token or task forwarded |
| Yeoman credential used on Hermes root/other tenant | Rejected |
| Model supplies arbitrary peer URL | Rejected by bounded worker code |

At the receiving Yeoman side verify matching request/run/task correlation,
ticker/date, actual status, evidence limitations, warnings/errors, disclaimer
and separate optional synthesis. For long tasks verify polling/cancel/recovery
on both edges; an adapter-only async test is insufficient.

Compare complete-route measurements to every configured wait and the execution
budget. If synchronous headroom is not reliable, complete async work in Tasks 3–4
and rerun this gate before production. Keep progress pending until verified.

Run relevant repository quality gates using project guidance; record commands,
results and known blockers. Never claim mock results prove live provider behavior.

**Acceptance:** all applicable gates pass with receiving-side evidence, including
real cross-host execution; no success claim from process exit or Card fetch alone.

## Task 8 — Controlled rollout and topology-aware rollback

Rollout:

1. Complete authorized experiment and fake-provider boundary/lifecycle tests.
2. Deploy secured services without scheduled callers; verify readiness/task auth.
3. Verify migrated Yeoman→Hermes worker independently of TradingAgents.
4. Enable bounded research for one owner test and verify Task 7 live evidence.
5. Run one manually observed schedule and recovery/replay tests.
6. Enable periodic scheduling only after measured runtime/cost/failure acceptance.

Research-feature rollback:

- Disable research triggers and new specialist peer/capability.
- Reconcile/cancel in-flight runs; retain durable IDs to prevent accidental replay.
- Restore previous bounded worker capability and selected-route configuration.
- Preserve the working Yeoman→Hermes connection on its actual host/URL/auth mode.
- Revoke affected credentials if compromise is suspected; preserve upstream data
  and required audit/recovery state unless deletion is separately authorized.
- Verify the pre-existing general Yeoman→Hermes worker still works.

Migration rollback is separate: restore the captured previous working host,
endpoint, authorization, secret references and profile dependencies. Never
unconditionally restore loopback after Hermes has moved to another host. Do not
weaken authentication to regain connectivity. Verify both rollback paths with
safe probes and no channel posts.

## Definition of done

- [ ] Authorization, deployment matrix, pins and experiment budget recorded.
- [ ] Early real upstream measurement and exact field mappings recorded.
- [ ] Both-edge local/remote A2A security and served-profile restrictions verified.
- [ ] Bounded Hermes destination enforcement is executable, not prompt-only.
- [ ] Thin adapter contract, provenance and partial-result tests pass.
- [ ] Durable idempotency/conflict/restart/deadline/cancel/budget behavior verified.
- [ ] Canonical structured results and request/task mappings survive both hops.
- [ ] Required async operations work through every hop without LLM polling loops.
- [ ] Intended deployment and one real cross-host research run verified at Yeoman.
- [ ] One Yeoman-owned schedule, recovery and delivery replay checks verified.
- [ ] Optional direct route explicitly enabled/tested or clearly left deferred.
- [ ] Yeoman remains sole conversational-memory and final-delivery authority.
- [ ] Research-feature and migration rollback preserve the correct topology/auth.
- [ ] Documentation tracking is explicitly resolved for handoff; no secrets staged.
