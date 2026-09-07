# TradingAgents ↔ Hermes ↔ Yeoman Design

Status: Revised after owner-reviewed architecture feedback; implementation not authorized
Date: 2026-09-07
Revision: 2026-09-08
Owner: Dimi

## 1. Purpose and decisions

Expose upstream TradingAgents as a bounded A2A research specialist. Yeoman
remains the sole authority for chat policy, persona, conversational memory,
final presentation and channel delivery. Hermes remains a general-purpose
research worker/orchestrator through the existing `yeoman-bridge` profile.

The owner intends to move Hermes to the server running TradingAgents. Host
placement is deployment configuration, not part of the research contract.
Keep A2A on both local and remote connections; do not introduce ACP or replace
A2A with a separate local protocol merely because two services share a host.

This revision authorizes neither implementation nor installation, migration,
credential creation, network exposure, paid experiments or scheduled delivery.
The baseline document remains historical evidence of the pre-review state;
its Host-A-Hermes assumptions are superseded here.

## 2. Logical roles and primary deployment

- **Host A:** `moltypython`, retaining Yeoman in the intended deployment.
- **Host B:** server running TradingAgents and the relocated Hermes installation.
- **Yeoman:** owner authorization, sanitized task context, chat memory/policy,
  initial scheduling, result acceptance and final delivery.
- **Hermes:** bounded research orchestration in `yeoman-bridge`; no channel tools.
- **TradingAgents adapter:** separate thin service importing the pinned upstream
  `tradingagents` package and calling `TradingAgentsGraph.propagate(...)`.
- **Specialist identity:** `tradingagents-research`, independent of host name.

```text
Host A: Yeoman request / approved schedule
    |
    | A2A across hosts: dedicated bearer token + encrypted private path
    v
Host B: Hermes served route /yeoman-bridge -> profile yeoman-bridge
    |
    | loopback A2A: token optional under explicit local trust policy
    v
Host B: tradingagents-research adapter -> upstream TradingAgents
    |
    | structured research result, or durable task handle + polling
    v
Hermes -> Yeoman policy / conversational memory / rendering / delivery
```

Yeoman's existing worker key is `hermes`; its served route and target profile
are `yeoman-bridge`. Preserve these identifiers during migration. The adapter
has its own logical worker name. Names, schema and request semantics do not
change when URLs or authentication modes change.

Each edge independently supports loopback or remote A2A. Also test the previous
placement (Yeoman and Hermes on A, specialist on B) and all-local integration
fixtures. Separate containers are not automatically loopback-local even on one
physical host.

## 3. Scope and authority

### Goals

- Keep the TradingAgents runtime off the RPi.
- Support on-demand research, scheduled reports and later comparisons/watchlists
  without cadence-specific profiles or protocols.
- Preserve A2A interoperability across process, framework and host boundaries.
- Return bounded structured research with honest provenance and failure state.
- Make topology changes configuration-only after boundary controls are implemented.
- Keep the adapter thin and upstream dependencies independently upgradeable.

### Non-goals

- Replacing or forking the named TradingAgents project, or copying its graph into Hermes.
- Replacing Yeoman or adding a second messaging-account/channel owner.
- Broker integration, automatic orders, portfolio mutation or financial-advice claims.
- Sharing raw Yeoman memory, chat archives, channel credentials or home directories.
- Public endpoint exposure, arbitrary callbacks, ACP, a broad reverse-control API,
  or a new distributed queue platform for the first slice.

Call outputs research reports/digests. Neither successful execution nor a
research signal is authority to trade or deliver a message.

Yeoman owns conversational/user memory and disclosure decisions. Hermes may
retain isolated worker task/session state. TradingAgents may retain its own
research memory, caches, checkpoints and reports under an explicit retention
policy; upstream graph execution itself reads/writes research state. These are
not Yeoman memory and must not be synchronized into it automatically. Yeoman
alone decides whether research becomes conversational memory or channel output.

## 4. Routing and triggers

Select exactly one route in trusted orchestration configuration before starting:

```text
route = hermes_orchestrated | yeoman_direct
```

- `hermes_orchestrated`: open-ended research, supplementary web research,
  comparisons and synthesis where Hermes adds value. This is the initial
  integration route for proving the shared worker boundary.
- `yeoman_direct`: permitted for fixed parameterized reports where Hermes would
  merely relay a request/result. Enable as a separate tested slice, with its own
  peer identity and remote credential. Do not make another model call solely to
  forward JSON. Until enabled, use the explicit orchestrated route and record
  its extra model cost rather than claiming a cost-free relay.

The caller-provided `route` field is correlation metadata, not authorization.
Neither a model nor report content can change destinations or credentials.
Never automatically switch route after failure/timeout: the original run may
still be executing. A deliberate resubmission requires reconciliation of that
run and a new policy decision, not a blind alternative-path retry.

Initial scope: owner-authorized on-demand requests, then Yeoman-owned scheduling;
`analyze`, one stock ticker, one execution worker. Cadence is schedule metadata,
not part of the worker name or research schema.

Hermes-owned scheduling remains a later slice. Prefer Yeoman polling an
explicitly authorized durable task/result collection with cursor, ownership,
acknowledgement and replay semantics. Do not assume a local Yeoman IPC return
path exists after migration. No reverse listener is required for the initial
flow. Callbacks, if later needed, require a separate security decision.

## 5. Connection security and isolation

### 5.1 Independent policy per edge

Deployment configuration records logical peer, endpoint, allowed origin/path,
authentication mode, secret reference, connect/read limits and execution policy.
These are design concepts; do not invent unsupported Hermes configuration keys.
Verify supported secret resolution and restriction mechanisms during implementation.

- **Local:** explicitly loopback-only listener and client destination; bearer token
  optional. No automatic auth downgrade. Loopback trusts processes able to reach
  that listener, not a particular Unix user. On a shared/untrusted host use tokens
  and suitable service/network isolation even for local calls.
- **Remote:** dedicated bearer token mandatory plus TLS with verification or an
  encrypted private overlay such as Tailscale. Private addressing alone does not
  encrypt bearer credentials. Bind only to the chosen interface and firewall
  access to intended peers. No unauthenticated non-loopback mode.
- Missing/empty required token fails locally before any request. Server rejects
  missing, invalid, revoked/rotated or wrong-peer credentials. If expiry is part
  of the selected secret mechanism, enforce and test it explicitly.
- Keep secrets in profile/service secret storage, not Git, prompts, skills,
  request bodies, ordinary config literals or conversational memory.

Use separate remote credentials for `yeoman -> hermes-yeoman-bridge`,
`hermes-yeoman-bridge -> tradingagents-research`, and optional
`yeoman-direct -> tradingagents-research`. Only provision credentials needed by
the selected placements/routes. The orchestrated route does not require Yeoman
to possess the specialist credential.

### 5.2 Authentication is not destination authorization

- Enforce named-peer access in executable code, not a skill or Agent Card.
  Hermes' general `a2a_call` accepts direct URLs; enabling the toolset and listing
  one configured peer does not prohibit arbitrary destinations.
- Prefer an existing enforceable peer restriction if verified; otherwise expose
  a small typed research tool/plugin backed by A2A. Keep unrestricted A2A
  discovery/call/fan-out unavailable in this bounded profile. The general Hermes
  installation retains its A2A capabilities.
- Validate Agent Card advertised endpoints against the configured origin and
  authorized path, or an explicit operator-maintained allowlist. Reject origin
  changes, unexpected profile paths, HTTPS downgrades and unsafe redirects before
  forwarding credentials or task data. Test both clients, not just discovery.
- Authorize Yeoman's identity for `/yeoman-bridge` only, not Hermes' broad root
  agent or other tenants/profiles. If native routing cannot enforce that scope,
  use a narrowly exposed listener or authenticated path-restricting proxy; do
  not widen the default listener and assume peer authentication isolates profiles.
- Agent Cards may be public under an explicit metadata policy; do not assume a
  successful Card/health read proves task authorization. Test task submission,
  lookup, list, cancellation and artifact ownership separately.

### 5.3 Service isolation

Run the adapter as a dedicated service account or isolated container with its
own upstream installation, cache/log/report directories and provider keys. No
Yeoman/Hermes home mounts or channel credentials. Bound resources and egress to
required providers where practical. Keep terminal, file mutation, messaging and
arbitrary delegation disabled in `yeoman-bridge`; preserve its intended web and
skills capabilities. A profile alone is not an OS sandbox.

## 6. Research contract

### 6.1 Protocol and identity

Use an explicitly pinned/tested A2A version and compatible SDK/client versions.
The specialist Agent Card advertises research only, supported methods and actual
auth requirements. Do not advertise task operations until implemented and tested.
Use A2A task operations for lifecycle, not a second custom job API. JSON carried
in message text is acceptable for initial compatibility; validate it as data.

### 6.2 Initial request

```json
{
  "schema_version": "tradingagents.research.v1",
  "operation": "analyze",
  "route": "hermes_orchestrated",
  "request_id": "opaque-owner-or-yeoman-id",
  "tickers": ["NVDA"],
  "asset_type": "stock",
  "analysis_date": "2026-09-07",
  "max_debate_rounds": 1,
  "max_risk_rounds": 1
}
```

Reject unknown/unsupported fields, malformed dates, unsafe ticker/path components,
unsupported operations/assets and over-limit values. Preserve source identifiers;
reject invalid input rather than silently repairing it. No caller-supplied shell
commands, URLs, callback destinations, tools, channels, prompt templates or secrets.

Map each accepted field explicitly to the pinned upstream API/configuration:

- `tickers[0]`, `analysis_date`, `asset_type` map to the graph's company/date/asset
  arguments; document exchange, timezone, non-trading-day and data-cutoff semantics.
- Debate rounds map to `max_debate_rounds`; risk rounds map to upstream
  `max_risk_discuss_rounds`, subject to verified pinned-version behavior.
- `lookback_days`, `depth` and `output_language` are not silently accepted.
  They are omitted from v1 until an adapter mapping with behavioral tests exists.
  Yeoman controls final presentation language separately.
- An analysis date does not by itself guarantee historical point-in-time data
  or freedom from look-ahead bias; report provider limitations explicitly.

Server-owned limits override caller requests. Record configured provider/model
and upstream/adapter versions for reproducibility without exposing credentials.

### 6.3 Result and provenance

A terminal result includes schema version, request ID, stable run ID, status,
generation time, analysis date, per-ticker findings/outcomes, evidence records,
warnings/errors and the research-only disclaimer. Required terminal statuses:
`completed`, `partial`, `rejected`, `failed`, `timed_out`, `cancelled`, `expired`.
Partial output enumerates failed/missing ticker operations; it never silently
omits them. A2A task state and report status have an explicit tested mapping;
for example a finished task may contain a `partial` research report.

Each finding contains ticker, summary, risks, invalidation conditions where
supported, and evidence IDs. Each evidence record contains a stable ID, source
identity, source URL when available, retrieval time, applicable data timestamp,
and provenance availability/limitations. Unknown timestamps or URLs are null
with a reason, never model-invented. Freshness is source-specific, not just the
report generation time. Missing provenance remains visible and may lead Yeoman
to withhold publication; valid JSON/disclaimers do not establish accuracy.

Hermes preserves the validated specialist result as a structured artifact and
returns any synthesis separately. Do not depend on an LLM copying JSON perfectly;
transport/adapter code preserves the canonical payload or an authorized artifact
reference. Yeoman validates size, schema, request/run identity, status, ticker/date,
evidence references and disclaimer before deciding storage or delivery. Artifact
retrieval must apply ownership, size and endpoint restrictions too.

Research prose may contain instruction-like text. The testable guarantee is
that report text is never executed or treated as authority to change tools,
policy, routing, credentials or delivery—not that a filter removes every possible
instruction. No report content creates a callback/tool invocation by itself.

## 7. Execution lifecycle, idempotency and budgets

### 7.1 Measure before choosing the production waiting strategy

Run an authorized, cost-capped one-ticker experiment early, before committing to
synchronous production orchestration. Measure graph/model/data time, total time,
output size and cost; then measure the complete selected route. No estimates or
mock results substitute for this gate.

The current Yeoman client supports synchronous `SendMessage` only. Its worker
schema defaults to 120 seconds and caps configuration at 600 seconds; Hermes has
its own reply/client limits. These are separate waiting limits, not a proven
end-to-end execution deadline. Record the actual configured chain and headroom.

A synchronous first slice is allowed only if measured bounded work reliably fits
all waits. Long-running production work uses durable A2A tasks: submit with
`SendMessage`, retrieve with `GetTask`, and support explicit `CancelTask` semantics.
Extend every participating hop, not only the specialist. A small durable task
store and one execution worker are sufficient initially. Polling must not consume
an LLM turn per poll. No claim of asynchronous support before end-to-end tests.

### 7.2 Guarantees required even for synchronous work

- Atomically persist admission/idempotency before launching expensive work.
- Namespace by authenticated logical caller and `request_id`; store a canonical
  validated request fingerprint. Same key/same content returns the same run;
  same key/changed content rejects. In no-token loopback mode attribution is a
  deployment trust namespace, not verified per-process identity.
- Carry the originating request ID unchanged through orchestration. Persist the
  outer request/task to inner specialist task/run mapping; retries must not depend
  on an LLM remembering an earlier submission.
- Persist task/run status and bounded result references across restart. Reconcile
  interrupted runs rather than blindly restarting expensive provider work.
- A caller/read timeout means execution state is unknown until reconciled; it is
  not a terminal `timed_out` research result. That status requires the executor's
  enforced deadline and stopped local work. Already accepted provider calls may
  still complete/be billed; report this limitation.
- Define cancellation request versus confirmed termination, expiry, retention and
  replay protection after result cleanup. Do not allow cleanup to recreate a run
  accidentally under the same admitted request.
- Enforce runtime inside a cancellable worker/process, not just an HTTP timeout
  around a blocking graph call. Bound provider calls/retries and output size.
- Enforce per-run/per-period budgets before dispatching more provider work, account
  for in-flight work and reserve conservative headroom. Record actual versus
  estimated usage and provider billing lag. Do not claim a strict monetary cap
  from after-the-fact logging alone; use provider limits where available.
- Start concurrency at one, with bounded admission/backpressure. No blind retry,
  provider substitution or alternate-route fallback after uncertain failure.

## 8. Scheduling, recovery and delivery

Yeoman initially creates the generic request, records route/request/task mapping,
submits it, and retrieves the canonical result. Its scheduler persists due runs,
completion and delivery acknowledgements so restart/replay does not duplicate
research or channel output. Delivery remains a separate policy-gated effect.

An unavailable Yeoman or worker leaves a durable pending/failed/unknown state,
not a fabricated completed report. Reconcile work before retry. Log correlation
IDs, state transitions, durations and bounded usage metadata without tokens,
raw chat memory, full private watchlists or provider response bodies.

Hermes-owned schedules and callbacks remain deferred as described in section 4;
they are not prerequisites for this initial architecture.

## 9. Acceptance and rollout gates

1. Intended deployment has Yeoman on A, Hermes and TradingAgents on B; no
   TradingAgents production workload on the RPi.
2. Both edges pass local and remote configuration tests without research-code or
   schema changes; a real cross-host selected-route run verifies migration.
3. Remote credentials are mandatory and encrypted in transit; local no-token
   mode is loopback-only. Missing secret, wrong token, rotation, unauthorized
   network peer, endpoint changes and downgrade tests fail closed.
4. Yeoman cannot access Hermes root/other profiles; the bounded Hermes worker
   cannot select arbitrary A2A destinations. Card advertisement is not used as
   proof of runtime authorization.
5. Contract mapping, source provenance/absence, partial output, malformed/oversize
   output and injection-as-data tests pass.
6. Persistent duplicate suppression, changed-payload conflicts, restart recovery,
   execution deadlines, cancellation and budget behavior are verified. Async
   paths, when required by measurements, work through every participating hop.
7. One real cost-capped ticker run is verified at Yeoman: matching request/run/task
   correlation, ticker/date, truthful status, evidence limitations and disclaimer.
8. One manually observed Yeoman-owned scheduled run and replay/delivery checks pass
   before periodic enablement. Yeoman alone decides memory and final delivery.
9. Versions, service isolation, secret handling, retention and observability are
   recorded; compatibility tests guard upgrades without an upstream fork.
10. Rollback restores the previous working topology, endpoint/auth configuration
    and bounded profile capabilities, not an unconditional loopback configuration.
    Disable new research triggers/peers, reconcile in-flight work, preserve data,
    and verify the pre-existing Yeoman→Hermes worker still functions. Migration
    rollback and research-feature rollback are separate procedures.

## 10. Remaining implementation decisions

After explicit implementation authorization, resolve only environment-dependent
choices: actual Host B/private endpoint, service versus container deployment,
secret resolution/rotation, pin set, provider/model and approved experiment/spend
budget, analysis-date semantics and retention. Initial operation/ticker/concurrency
and scheduler defaults are already specified above. Measurements determine whether
the initial production slice needs asynchronous tasks; direct routing is a separate
enablement choice, not automatic fallback.

## 11. Evidence and superseded assumptions

Read alongside the original baseline and the earlier
`2026-09-07-hermes-bot-profile-a2a-design.md` and implementation plan. Their local
first-slice verification does not prove cross-host authorization or async behavior.
The earlier network-separation and deferred-A2A gates apply before migration.

Source observations from the review (recheck against pinned versions):

- Yeoman `packages/gateway/yeoman_gateway/a2a/client.py`: synchronous client;
  missing token currently yields no Authorization header; remote Card endpoint
  selection lacks configured-origin binding.
- Yeoman `packages/shared/yeoman_shared/config/schema.py`: worker timeout and
  `authTokenEnv` / `allowRemote` configuration.
- Yeoman `packages/gateway/yeoman_gateway/agent/tools/a2a.py`: bound channel calls
  deliberately do not accept model-controlled persistent context IDs.
- Hermes `plugins/platforms/a2a/tools.py`: `_resolve_peer` accepts direct URLs;
  ordinary peer configuration alone is not an outbound destination allowlist.
- https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a
- https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/trading_graph.py

These observations are implementation work items, not claims that the missing
controls already exist. This revision changes planning documents only.
