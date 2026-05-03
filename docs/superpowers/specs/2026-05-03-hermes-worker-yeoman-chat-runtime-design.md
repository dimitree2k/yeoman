# Hermes Worker And Yeoman Chat Runtime Design

Status: Draft for review
Date: 2026-05-03
Owner: Tim

## 1. Purpose

Define a practical split between Hermes and Yeoman.

Hermes should become the worker, developer, tool runner, and broad agent
platform. Yeoman should remain the chat-focused runtime that owns WhatsApp
behavior, social timing, voice behavior, disclosure-aware memory, policy, and
the final decision about whether anything should be said in a chat.

The goal is to reduce Yeoman's generic agent-platform surface without weakening
the Yeoman-specific behavior that makes it valuable.

## 2. Current Context

Yeoman already has a disciplined chat runtime:

- deterministic inbound pipeline in `packages/gateway/yeoman_gateway/core/orchestrator.py`
- per-chat policy and persona resolution
- WhatsApp bridge, voice handling, reply archive, and quoted-message context
- semantic memory with chat/user/global scopes
- disclosure metadata and render-time memory gates
- consciousness burst/lull/cron behavior
- group-state and outcome-learning direction
- overseer and native tools for selected runtime work

Hermes is a broader maintained agent product:

- CLI/TUI and gateway surfaces
- broad skills ecosystem
- MCP support
- coding/research workflows
- background jobs and long-running sessions
- multiple execution environments
- plugins and memory providers
- product/research features that move faster than Yeoman should

The systems overlap, but they optimize for different failure modes. Hermes
optimizes for breadth and agent work. Yeoman optimizes for predictable chat
behavior under local policy.

## 3. Mental Model

```text
Yeoman = chat brain / social runtime
Hermes = worker / developer / tool runner
```

Yeoman receives chat input and decides whether to reply, ignore, remember,
delegate, or schedule. Hermes performs heavy work when Yeoman or the owner asks
for it.

```text
human or group chat
  -> Yeoman policy, memory, context, social decision
    -> answer directly
    -> stay silent
    -> delegate bounded task to Hermes
         -> Hermes performs work
         -> Hermes returns result
    -> Yeoman decides how, when, and whether to present result
```

Hermes should not become another participant making independent social decisions
inside the same WhatsApp chats.

## 4. Non-Goals

- Do not replace Yeoman with Hermes.
- Do not let Hermes own Finanzgruppe or other WhatsApp group behavior.
- Do not run both systems as independent bots in the same chat account.
- Do not share raw memory databases between Hermes and Yeoman.
- Do not let Hermes bypass Yeoman's disclosure, policy, or voice rules.
- Do not port all Yeoman behavior into a large Hermes addon.
- Do not duplicate STT, WhatsApp, or voice pipelines in Hermes when Yeoman
  already owns them.
- Do not use Hermes as a workaround around Yeoman policy.

## 5. Responsibilities Kept In Yeoman

Yeoman remains authoritative for chat-native behavior:

| Concern | Reason |
|---------|--------|
| WhatsApp runtime | Yeoman already owns the bridge, contacts, group IDs, reply archive, and delivery semantics. |
| STT/TTS and voice rules | Yeoman already handles audio suitability, voice length, and chat-specific voice behavior. |
| Reply timing | Burst, lull, cron, quiet windows, and direct reactive replies are social decisions. |
| Policy engine | Access, tool allowlists, persona, model route, and per-chat overrides must remain deterministic. |
| Disclosure-safe memory | Raw chat memory has owner/group/private boundaries that Hermes should not flatten. |
| Conversation context | Quoted-message context, ambient windows, and stale-context rails are chat-specific. |
| Consciousness | Proactive speakups and outcome learning decide if Yeoman should enter a chat. |
| Group-state learning | This is Yeoman's product direction, not a generic worker-agent feature. |
| Final chat rendering | Hermes may produce a result, but Yeoman decides wording, voice/text, and timing. |

## 6. Work Moved Or Frozen In Yeoman

Yeoman should stop expanding generic agent-platform work where Hermes is stronger.

Move or freeze these categories:

| Category | Yeoman direction | Hermes direction |
|----------|------------------|------------------|
| Coding and repo work | Keep only minimal chat command/delegation surfaces. | Own long-running development, repo inspection, CI triage, PR work. |
| Generic research | Delegate heavy research; keep chat summarization only when needed. | Own browser/MCP/research sessions and artifacts. |
| MCP ecosystem | Do not rush broad MCP into normal chat. | Use Hermes-native MCP first. |
| Broad integrations | Avoid custom Yeoman adapters for every external service. | Use Hermes skills/plugins/MCP. |
| General automation cron | Keep chat-native rituals and reminders. | Own audits, reports, research jobs, file organization, server checks. |
| Multi-platform expansion | Avoid adding Slack, Matrix, email, Teams, etc. unless chat policy truly needs them. | Let Hermes cover broad platforms. |
| Large skill ecosystem | Keep small runtime-specific Yeoman skills. | Let Hermes manage broad skill libraries and curation. |
| Heavy terminal/browser work | Keep only what Yeoman needs to operate itself. | Own rich terminal/browser workflows. |

This is initially a product boundary, not a code deletion pass. First route work
to Hermes in practice; delete or simplify Yeoman code only after the split proves
stable.

## 7. Hermes-Native Use Cases

Hermes should be preferred when the task is not primarily a social chat decision:

- inspect Yeoman code and propose or implement changes
- debug CI, run tests, open PRs, prepare release notes
- research a topic over time and produce a concise report
- use MCP servers for GitHub, databases, filesystem, browser, or internal APIs
- run recurring audits and operational checks
- maintain skills for repeated workflows
- work on a remote machine or Raspberry Pi as an always-on worker
- manage long-running background sessions

Yeoman may initiate these tasks, but Hermes owns execution.

## 8. Delegation Boundary

The first integration should be narrow and explicit.

Yeoman-to-Hermes contract:

```json
{
  "task_id": "uuid",
  "requested_by": "owner|chat",
  "source_channel": "whatsapp",
  "source_chat_id": "optional",
  "task": "plain-language bounded task",
  "context": "sanitized bounded context",
  "constraints": {
    "no_chat_send": true,
    "max_runtime_seconds": 1800,
    "allowed_repos": ["yeoman"],
    "requires_approval_for_mutation": true
  }
}
```

Hermes-to-Yeoman result contract:

```json
{
  "task_id": "uuid",
  "status": "completed|failed|needs_input",
  "summary": "short result for Yeoman to decide how to present",
  "details_ref": "optional artifact or session reference",
  "suggested_memory": [
    {
      "content": "stable fact worth remembering",
      "scope_hint": "global|chat|user",
      "confidence": 0.8
    }
  ],
  "requires_owner_review": false
}
```

Hermes should return results to Yeoman or the owner, not directly to group chats,
unless a later explicit policy permits a specific direct path.

## 9. Memory Boundary

Yeoman and Hermes should be interoperable at the API boundary, not compatible by
sharing one memory store.

Yeoman memory is chat-governance data. It contains:

- scope type and scope key
- channel, chat ID, sender ID, and source message IDs
- semantic sectors and kinds
- salience and confidence
- embeddings and lexical search state
- `meta_json` disclosure metadata
- render-time gates that can hide or guard sensitive memories
- learned proactive speakup taste and group outcome patterns

Hermes memory is worker-agent data. It should contain:

- project and repository facts
- environment and machine facts
- tool workflows
- MCP and skill usage notes
- coding/research task state
- owner preferences for worker behavior

Do not sync all memory both ways.

Allowed memory flows:

```text
Hermes asks Yeoman for memory:
  Hermes calls a Yeoman memory query API
  Yeoman performs scoped recall and disclosure rendering
  Yeoman returns sanitized memory text
  Hermes uses it as task context, not as permanent global memory by default
```

```text
Hermes discovers a stable fact:
  Hermes returns suggested_memory to Yeoman
  Yeoman classifies disclosure with its own rules
  Yeoman writes or rejects the memory
```

Forbidden memory flows:

- Hermes reading Yeoman SQLite directly.
- Hermes writing Yeoman memory directly.
- Hermes Curator mutating Yeoman memory records.
- Automatic two-way memory sync.
- Exporting raw group chat archives into Hermes memory.
- Letting Hermes use owner-only or taboo memories outside Yeoman's renderer.

## 10. A Possible Hermes Addon

A Hermes addon can make sense only if it is thin.

Good addon shape:

- `yeoman_status`
- `yeoman_recent_chat_context`
- `yeoman_safe_memory_search`
- `yeoman_policy_read`
- `yeoman_trigger_reply_once`
- `yeoman_submit_delegation_result`

Bad addon shape:

- reimplement Yeoman's policy engine inside Hermes
- port Yeoman consciousness into Hermes
- let Hermes own WhatsApp delivery decisions
- let Hermes directly mutate Yeoman memory
- expose raw chat archives as a generic Hermes knowledge base

A comprehensive Yeoman addon for Hermes would likely become a maintenance trap.
Hermes moves quickly, and Yeoman's important behavior depends on local runtime
rules. The safer long-term architecture is a small bridge between two systems.

## 11. Raspberry Pi Deployment Notes

Hermes can be useful on a Raspberry Pi 4 with 8GB RAM if it uses hosted models.
It should not be expected to run serious local LLM inference.

Recommended Pi profile:

- 64-bit Raspberry Pi OS
- SSD preferred over SD card
- 2-4GB swap for installs and updates
- hosted LLM provider with at least 64K context
- skip Hermes STT/voice extras because Yeoman already owns STT/TTS
- do not enable Hermes WhatsApp if Yeoman owns WhatsApp
- install only the Hermes surfaces needed for worker/developer tasks
- prefer API-key providers over headless OAuth where possible

The Pi is a good always-on Hermes worker for Telegram/CLI, repo work, scheduled
jobs, MCP, and reports. It is not a good target for local model serving or heavy
browser automation without separate testing.

## 12. First Practical Architecture

Start with no deep runtime integration.

Phase 0: Operational split

- Run Hermes separately from Yeoman.
- Disable or do not configure Hermes WhatsApp.
- Do not install Hermes voice/STT extras.
- Keep Hermes memory separate.
- Use Hermes manually as Yeoman's developer and worker.

Phase 1: Manual delegation convention

- Owner asks Yeoman for work.
- Yeoman replies with a concise task packet or owner manually gives Hermes the
  task.
- Hermes works and returns a result.
- Yeoman or owner decides what to say in chat.

Phase 2: Thin bridge

- Add a Yeoman-side delegation interface.
- Add a Hermes-side Yeoman tool/addon with read-only status and safe-memory
  access.
- Add result submission back to Yeoman.
- Keep group delivery behind Yeoman policy.

Phase 3: Policy-gated automation

- Allow Yeoman to delegate specific classes of owner-approved work.
- Allow Hermes to return artifacts and memory suggestions.
- Add owner approval for mutations, code commits, deploys, or chat sends.
- Add audit logs on both sides.

Phase 4: Prune Yeoman generic surfaces

- Freeze or remove Yeoman features that Hermes reliably covers.
- Keep chat-native tools and runtime operations inside Yeoman.
- Document replaced surfaces and revert paths.

## 13. Safety And Failure Handling

Required controls:

- Every delegation has a task ID.
- Hermes cannot send to chats by default.
- Hermes cannot read raw Yeoman memory.
- Yeoman validates all Hermes results before chat delivery.
- Owner approval is required for mutations until proven safe.
- Timeouts and failed tasks produce owner-visible status, not group spam.
- Hermes results are summarized before chat injection.
- Large artifacts stay by reference.
- All cross-system calls are logged with task ID, requester, and action.

Failure behavior:

| Failure | Behavior |
|---------|----------|
| Hermes offline | Yeoman says the worker is unavailable only to the owner or falls back to a direct answer. |
| Hermes task timeout | Yeoman keeps chat silent unless the owner asked for status. |
| Hermes returns unsafe/private data | Yeoman disclosure and policy gates suppress or summarize it. |
| Hermes needs credentials/OAuth | Ask owner out-of-band; do not continue in group chat. |
| Hermes wants to mutate repo/runtime | Require owner approval or a dedicated policy profile. |
| Hermes result is too long | Store artifact by reference and inject a concise summary only. |

## 14. Acceptance Criteria

The design is successful when:

- Yeoman remains the only system deciding when and how to speak in WhatsApp.
- Hermes can do meaningful worker/developer tasks without joining Yeoman chats.
- Yeoman memory is never shared as raw storage.
- Safe Yeoman memory access goes through Yeoman's existing disclosure renderer.
- Hermes task results return through Yeoman or owner review before group delivery.
- Generic Yeoman tool expansion slows down because Hermes covers those workflows.
- Chat-native Yeoman behavior does not become slower or more complex.

## 15. Open Decisions Before Implementation

These should be settled before writing code:

- Which transport should the thin bridge use first: local CLI, HTTP, socket, or
  MCP-style tool?
- Should Yeoman delegate only from owner DMs at first?
- Which first use case should prove the split: Yeoman repo work, CI debugging,
  weekly research, or ops audit?
- Should Hermes run on the Raspberry Pi or on the development machine first?
- What exact owner approval boundary is required for Hermes code edits,
  commits, deploys, or chat delivery?
