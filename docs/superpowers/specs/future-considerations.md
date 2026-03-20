# Future Considerations

Items deliberately deferred from Phase 2–4 implementation plans. Each represents a meaningful architectural upgrade worth revisiting when the preceding phases are stable.

---

## 1. Workspace Pattern (Phase 5+)

**Problem it solves:** Phases 2–3 use path deny-lists (`.env`, `secrets/`, `.git/`, etc.) and bubblewrap `--tmpfs` masking to keep the LLM away from sensitive files. This is "default allow + masking" — correct, but exhaustive. A forgotten path breaks the security guarantee.

**The shift:** When a runbook triggers, the overseer creates an ephemeral workspace (`/tmp/yeoman-ws-{uuid}/`) and copies only the files the runbook declares it needs into it. Tools (`read_file`, `write_file`, `shell`) are chrooted to this workspace. Sensitive files are never accessible because they were never placed there.

**Result:** Deny-lists become unnecessary. Secret exfiltration and `.git/hooks` RCE are architecturally impossible regardless of what the LLM requests.

**When to introduce:** Phase 5, after self-evolution (Phase 4) is stable and runbooks have a mature context requirements schema. Requires runbooks to declare their inputs explicitly — a natural fit for the governance model Phase 4 establishes.

---

## 2. Typed DB API (Phase 5+)

**Problem it solves:** `query_db` exposes a raw SQL interface. Even with `mode=ro` URI connections, the attack surface is large. The LLM can construct arbitrary queries, exfiltrate data via SELECT, or attempt schema introspection.

**The shift:** Deprecate `query_db` in favor of domain-specific typed functions:

```python
get_error_rates(domain: str, window_hours: int) -> ErrorRateResult
get_memory_summary(salience_above: float, domain: str | None) -> MemorySummary
get_session_count(channel: str, since_hours: int) -> int
```

The LLM has no SQL interface at all — it calls typed functions that return structured results. SQL injection is impossible.

**When to introduce:** After runbook vocabulary stabilizes (Phase 4+). The full set of DB queries the LLM actually needs becomes clear once several LLM runbooks are in production.

---

## 3. Ephemeral DB Replica

**Problem it solves:** An alternative to `mode=ro` if runbooks need complex SQL that SQLite disallows in read-only mode (e.g., `CREATE TEMP TABLE`, certain CTEs). Also provides mutation isolation: the LLM can run `UPDATE` on a throwaway copy without risk.

**The shift:** Before passing a DB to the agent, use SQLite's backup API to copy it to `/tmp/copy-{uuid}.db`. The LLM queries the replica. Production data is untouched regardless of the query.

**Relationship to Typed DB API:** If the Typed DB API is adopted, this item becomes moot. If raw SQL access is retained for flexibility, ephemeral replicas are the right safety mechanism.

**When to introduce:** If a runbook requires SQL that `mode=ro` blocks and a typed function is too rigid. Evaluate on-demand.

---

## 4. A2A Protocol (Agent2Agent)

**Problem it solves:** Yeoman currently has no standard way to expose itself as a callable service for external agents, or to delegate sub-tasks to specialist agents (e.g., a research agent, a code review agent, a financial data agent).

**What A2A provides:** An open standard (Google, April 2025; 100+ org partners as of early 2026) for agent-to-agent communication: capability discovery, structured task delegation, and result exchange — complementary to MCP (which is tool access, not agent-to-agent).

**When to introduce:** Phase 5+, if/when:
- Yeoman needs to delegate to external specialist agents (research, code review, finance)
- The gateway or overseer should be discoverable by other agents on the network
- Yeoman scales beyond a single box and the overseer becomes a multi-instance control plane

**Not needed for:** Internal overseer↔gateway communication (Unix sockets are sufficient for local IPC). Single-owner, single-box deployments.

---

## 5. Container Graduation

**Described in:** Parent spec `docs/superpowers/specs/2026-03-18-overseer-design.md` (Section 11, Layer 3).

**Summary:** Each service (gateway, overseer, bridge) becomes a rootless podman container. Unix socket communication and shared storage are preserved via volume mounts with identical permissions. The overseer gains `podman` access and becomes the container orchestrator. systemd units become `podman-generate-systemd` units.

**When to introduce:** When the system outgrows a single Pi or needs to run multiple agent instances. Also a natural complement to the Workspace Pattern (containers make workspace isolation even cleaner).
