# Security Policy

## Reporting a Vulnerability

If you discover a security issue in nanobot:

1. Do not open a public issue.
2. Open a private GitHub security advisory or contact maintainers directly.
3. Include:
   - Impact and affected component
   - Reproduction steps
   - Suggested remediation (if available)

Target initial response time: within 48 hours.

## Security Model (Current)

nanobot uses a layered security model:

1. Policy layer (`~/.nanobot/policy.json`)
   - Controls who can talk, when the bot replies, and which tools can run.
   - Supports per-channel and per-chat overrides.
2. Runtime security middleware (`security.*` in `~/.nanobot/config.json`)
   - Staged checks for input, tool calls, and optional output sanitization.
3. Tool guardrails
   - `exec` has deny-pattern filters, timeout, output truncation, and optional sandbox isolation.
4. OS-level hardening
   - File permissions, non-root runtime, process/network isolation.

## Secret Management

Critical rules:

- Never commit API keys or bridge tokens.
- Assume `~/.nanobot/config.json` contains sensitive material.

Recommended permissions:

```bash
chmod 700 ~/.nanobot
chmod 600 ~/.nanobot/config.json
chmod 700 ~/.nanobot/whatsapp-auth
```

Notes:

- Provider API keys are stored as plain text in config by default.
- For stronger production security, use a secret manager or environment-injection workflow.
- Rotate keys after suspected exposure.

## Access Control (Policy, Not `allowFrom`)

`channels.*.allowFrom` has been removed from `config.json`.
Access control now lives in `~/.nanobot/policy.json`.

Key controls:

- `whoCanTalk`: `everyone | allowlist | owner_only`
- `whenToReply`: `all | mention_only | allowed_senders | owner_only | off`
- `blockedSenders`: explicit deny list
- `allowedTools`: allow-all or allowlist + deny list
- `toolAccess`: per-tool sender ACL

Example:

```json
{
  "owners": {
    "telegram": ["123456789"],
    "whatsapp": ["+1234567890"]
  },
  "channels": {
    "whatsapp": {
      "default": {
        "whoCanTalk": { "mode": "allowlist", "senders": ["+1234567890"] },
        "whenToReply": { "mode": "mention_only", "senders": [] },
        "allowedTools": {
          "mode": "allowlist",
          "tools": ["list_dir", "read_file", "web_search", "web_fetch"],
          "deny": []
        }
      }
    }
  }
}
```

Legacy migration:

```bash
nanobot policy migrate-allowfrom --dry-run
nanobot policy migrate-allowfrom
```

## Admin Command Security

Deterministic slash admin commands (`/policy ...`, `/reset`) are restricted:

- Channel scope: WhatsApp
- `/policy`: owner DM only
- `/reset`: owner-only (WhatsApp)

Built-in protections:

- Admin command rate limiting (`runtime.adminCommandRateLimitPerMinute`, default `30`/minute)
- Optional confirm gate for risky commands (`runtime.adminRequireConfirmForRisky`)
- Policy mutation backups + append-only audit log under:
  - `~/.nanobot/policy/audit/policy_changes.jsonl`
  - `~/.nanobot/policy/audit/backups/`

## Tool and Execution Security

### `exec` tool

Current protections:

- Dangerous command deny patterns (for example disk wipe/fork bomb patterns)
- Timeout (default: `60s`)
- Output truncation (10,000 characters)
- Optional per-session sandbox isolation via Bubblewrap (`tools.exec.isolation.*`)

Recommended for production:

- Enable `tools.exec.isolation.enabled=true`
- Keep `tools.exec.isolation.failClosed=true`
- Keep mount allowlist outside repo (default `~/.config/nanobot/mount-allowlist.json`)

### File tools

- Path traversal protections are enforced.
- Workspace restriction can be enforced with `tools.restrictToWorkspace=true`.
- `security.strictProfile=true` forces workspace restriction and enables fail-closed exec isolation.

## Network and Channel Security

- External provider calls use HTTPS endpoints.
- WhatsApp bridge defaults to local binding (`127.0.0.1:3001`) with token authentication.
- Keep bridge auth state protected in `~/.nanobot/whatsapp-auth` (mode `0700`).

If you expose bridge or gateway outside localhost:

- Put it behind authenticated reverse proxy
- Enforce TLS
- Restrict source IPs/firewall egress and ingress

## Data Privacy and Storage

Sensitive local data may exist in:

- `~/.nanobot/config.json` (API keys/tokens)
- `~/.nanobot/policy.json` (owner IDs, ACLs)
- `~/.nanobot/sessions/*.jsonl` (conversation history)
- `~/.nanobot/memory/memory.db` (long-term memory)
- `~/.nanobot/inbound/reply_context.db` (message archive)
- `~/.nanobot/logs/*` (runtime logs)

Operational guidance:

- Limit host access to the nanobot user.
- Define retention/rotation policy for logs and memory databases.
- Treat prompts and chat history as sensitive data.

## Dependency Security

Run regular vulnerability checks:

```bash
pip install pip-audit
pip-audit
```

For WhatsApp bridge dependencies:

```bash
cd bridge
npm audit
npm audit fix
```

Also keep `litellm` and websocket-related dependencies current.

## Known Limitations

Current limitations to account for:

1. No global inbound message rate limiting across all channels.
2. Secrets are plain text in config by default.
3. No automatic session expiration model.
4. Security rules are deterministic/pattern-based and can miss novel attacks.
5. Exec/file protection is defense-in-depth, not a complete sandbox unless isolation is enabled.
6. Audit trail is strong for policy admin mutations, but not a full SIEM-grade security event pipeline.

## Production Hardening Checklist

Before production deployment:

- [ ] Run as non-root dedicated user
- [ ] `~/.nanobot` permissions hardened (`700`)
- [ ] `~/.nanobot/config.json` permissions hardened (`600`)
- [ ] Owners and per-chat ACLs configured in `policy.json`
- [ ] High-risk tools (`exec`, `spawn`) denied or tightly scoped
- [ ] Exec isolation enabled and fail-closed
- [ ] Logs and policy audit files monitored
- [ ] Dependency audit integrated into update cadence
- [ ] Key rotation and incident runbook documented

## Incident Response

If compromise is suspected:

1. Revoke and rotate API keys and bridge tokens immediately.
2. Review runtime logs and policy audit history.
3. Inspect `~/.nanobot/policy.json` for unauthorized ACL/persona/tool changes.
4. Review `~/.nanobot/sessions/` and memory DB for prompt-injection persistence.
5. Update dependencies and redeploy from a trusted baseline.
6. Report confirmed vulnerabilities to maintainers.

## Updates

Last updated: 2026-02-14

Project references:

- This repository advisories: `/security/advisories`
- This repository releases: `/releases`
- Upstream project: https://github.com/HKUDS/nanobot

## License

See `LICENSE`.
