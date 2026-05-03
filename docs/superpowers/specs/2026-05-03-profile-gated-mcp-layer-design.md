# Profile-Gated MCP Layer Design

Status: Future implementation spec
Date: 2026-05-03
Owner: Tim

## 1. Purpose

This document defines how Yeoman should add Model Context Protocol support later
without turning every chat response into a large MCP-enabled prompt.

The goal is ecosystem access with strict activation controls. MCP should behave
like a capability pack: dormant by default, activated only by policy, explicit
commands, or task shape, and exposed to the model as the smallest useful tool
set.

## 2. Problem

Yeoman has a native `ToolRegistry`, deterministic policy resolution, and
chat-scoped tool allowlists. That makes native tools efficient and predictable,
but every external service currently needs a custom adapter.

MCP solves the adapter problem for long-tail integrations, but it creates three
risks if added naively:

- token growth from importing many remote tool schemas
- latency from remote MCP discovery and tool calls
- policy bypass if remote tools are exposed outside Yeoman's existing controls

The layer must optimize for normal chat staying lean.

## 3. Non-Goals

- Do not replace existing native Tavily, filesystem, message, voice, ops, or
  memory tools.
- Do not expose all tools from an MCP server globally.
- Do not allow remote MCP servers to write, send, or mutate state unless an
  explicit Yeoman policy profile permits it.
- Do not implement autonomous OAuth setup inside casual chat.
- Do not use MCP as a workaround around Yeoman policy.

## 4. Activation Model

MCP is inactive unless a resolver selects an MCP profile before the LLM call.

Activation inputs:

- explicit owner/admin command such as `/mcp tavily map ...`
- responder task classification such as `deep_research`, `docs_lookup`, or
  `admin_integrations`
- channel and chat policy
- overseer runbook context
- configured profile allowlist

Default behavior:

```text
normal chat -> no MCP server listed -> no MCP schema tokens
research/admin task -> selected MCP profile -> only profile tools exposed
```

## 5. Profiles

Profiles are the unit of exposure.

Example configuration shape:

```json
{
  "mcp": {
    "enabled": true,
    "profiles": {
      "tavily_research": {
        "server": "tavily",
        "allowedTools": ["search", "extract", "map"],
        "channels": ["whatsapp", "cli"],
        "chatIds": ["owner"],
        "requireApproval": "read_only_actions",
        "maxOutputChars": 12000
      },
      "github_admin": {
        "server": "github",
        "allowedTools": ["list_pull_requests", "get_pull_request", "list_checks"],
        "channels": ["cli", "overseer"],
        "requireApproval": "mutations"
      }
    },
    "servers": {
      "tavily": {
        "transport": "streamable_http",
        "url": "https://mcp.tavily.com/mcp/",
        "auth": {
          "type": "bearer",
          "env": "TAVILY_API_KEY"
        }
      }
    }
  }
}
```

The exact schema can change during implementation, but the policy boundary must
remain profile-based.

## 6. Architecture

Add an MCP adapter layer beside native tools:

```text
PolicyEngine.resolve_policy(...)
  -> allowed native tools
  -> allowed MCP profiles

McpProfileResolver
  -> decides active profile for this turn, if any

McpClientManager
  -> connection lifecycle
  -> tool discovery cache
  -> auth/header construction

McpToolAdapter
  -> converts selected remote tools into Yeoman Tool definitions
  -> executes remote tool calls
  -> applies output caps and error normalization

ToolRegistry
  -> native tools + selected MCP tool adapters for this turn
```

Remote tool names should be namespaced before registration:

```text
mcp_tavily_search
mcp_tavily_extract
mcp_github_list_pull_requests
```

Namespacing avoids collisions with native tools and makes policy/audit logs
clear.

## 7. Token Controls

The implementation must keep token growth measurable and bounded.

Required controls:

- no MCP tools in normal chat unless activated
- profile-level `allowedTools`; never import a whole server by default
- cache discovered schemas outside the prompt where the provider path allows it
- expose compact wrapper descriptions rather than raw verbose server docs when
  Yeoman adapts tools locally
- cap remote tool output by profile
- prefer query-focused extraction over full crawl output
- store large outputs by reference for later retrieval instead of injecting full
  content into the next model call
- log approximate schema-token and output-token budgets per turn

Budget targets:

| Path | MCP schema budget | Tool output budget |
|------|-------------------|--------------------|
| normal chat | 0 tokens | 0 tokens |
| Tavily research | small profile only, 1-3 tools | 8k-12k chars before summarization |
| overseer/admin | selected profile only | profile-specific cap |
| crawl/import | no final chat injection by default | stored artifact plus summary |

## 8. Latency Controls

Required controls:

- startup or lazy schema discovery with TTL cache
- per-server connect timeout
- per-tool execution timeout
- profile-specific max runtime
- graceful fallback to native tools when available
- structured error messages that do not trigger repeated retry loops

Remote MCP is acceptable for research, admin, and overseer work. It is not the
default for low-latency casual replies.

## 9. Policy And Safety

MCP tools must pass through Yeoman policy.

Policy dimensions:

- channel and chat ID
- read-only vs mutation capability
- owner approval requirement
- remote host allowlist
- output cap
- allowed argument domains for web-like tools
- audit logging

Mutation tools must require either explicit command context or an approval
boundary. Read-only tools may be automatic in owner/admin contexts if the profile
allows it.

MCP roots are advisory, not enforcement. Yeoman must enforce filesystem and
runtime boundaries itself.

## 10. First Implementation Slice

When MCP becomes serious, implement in this order:

1. Add config schema for servers and profiles.
2. Add `McpClientManager` for streamable HTTP remote servers.
3. Add `McpToolAdapter` for selected, read-only tools.
4. Add profile activation for CLI/overseer only.
5. Add token/output budget logging.
6. Pilot with Tavily remote MCP using only `search`, `extract`, and `map`.
7. Add WhatsApp/admin activation only after CLI/overseer behavior is stable.
8. Add mutation approval support later.

## 11. Acceptance Criteria

- A normal WhatsApp reply includes no MCP tool definitions.
- An explicit MCP-enabled research command exposes only the configured profile
  tools.
- Remote tool outputs are capped and logged.
- Tool execution appears in the same audit/log path as native tools.
- Policy validation can list known MCP tool names for the active profiles.
- Disabling MCP config removes all MCP tools without code changes.
- Tavily native tools remain available and are preferred for common search paths.

## 12. Open Questions

- Whether to use provider-native remote MCP support where available or implement
  a local MCP client that adapts tools into Yeoman function calls.
- Whether schema discovery should happen at gateway startup or first activation.
- Where large remote outputs should be stored: session artifacts, memory import
  queue, or a new tool-result store.
- How much MCP should be allowed in non-owner group chats, if any.
