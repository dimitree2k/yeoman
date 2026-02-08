# Container Isolation Analysis for AI Agents

## Overview

This document captures the essential learnings from analyzing nanoclaw's container isolation architecture and how to apply it to similar projects like nanobot.

## Core Concepts

### 1. Container Isolation as Primary Security Boundary

Instead of relying on application-level permission checks, container isolation limits the attack surface by:
- **Process isolation**: Container processes cannot affect the host
- **Filesystem isolation**: Only explicitly mounted directories are visible
- **Non-root execution**: Runs as unprivileged user (uid 1000)
- **Ephemeral execution**: Fresh environment per invocation (`--rm`)

### 2. Persistent Memory with Ephemeral Containers

Containers are ephemeral, but **bind mounts** persist data on the host:

```
Host Path                    Container Path         Purpose
─────────────────────────────────────────────────────────────
groups/{group}/              /workspace/group/      Agent's long-term memory
data/sessions/{group}/       /home/node/.claude/    Session state
data/ipc/{group}/            /workspace/ipc/        IPC communication
```

When container writes to `/workspace/group/memory.md`, it actually writes to `./groups/main/memory.md` on the host via bind mount.

### 3. Mount Security (Critical)

**Allowlist stored OUTSIDE project root** - containers cannot modify security config:
- Location: `~/.config/nanoclaw/mount-allowlist.json`
- Never mounted into containers
- Validates all mount requests before container spawn

**Validation steps:**
1. Blocked patterns check (`.ssh`, `.aws`, `.env`, `id_rsa`, etc.)
2. Symlink resolution (prevents traversal attacks)
3. Allowed root verification
4. Container path validation (rejects `..` and absolute paths)

### 4. Concurrency Management

**Per-group serialization**: Only ONE container per group at a time.
**Global concurrency limit**: Max N containers across all groups (default: 5).

Messages arriving while container is running are queued and batched.

## Platform Comparison

| Platform | Best Approach | Notes |
|----------|---------------|-------|
| Raspberry Pi 4 | `bubblewrap` or `unshare` | Lightweight, no daemon |
| macOS | Docker Desktop or Apple Container | Full container isolation |
| Linux server | Docker + gVisor | Strongest isolation |
| Production VPS | Docker with resource limits | Battle-tested |

## Key Files from nanoclaw

| File | Purpose |
|------|---------|
| `src/container-runner.ts` | Container lifecycle & mount building |
| `src/mount-security.ts` | Allowlist validation logic |
| `src/group-queue.ts` | Per-group serialization & concurrency |
| `docs/SECURITY.md` | Security architecture documentation |

## Implementation Patterns

### Pattern A: Per-Message Container (nanoclaw approach)
- Maximum isolation
- Higher latency (~2s spawn overhead)
- Simple mental model

### Pattern B: Session-Bound Container Pool
- Container per active session
- Dies after idle timeout (e.g., 10min)
- Lower latency for active sessions
- More complex state management

### Pattern C: Subprocess Isolation (bubblewrap)
- Lightweight namespaces
- ~50ms startup vs ~500ms for Docker
- Linux-only
- Good for resource-constrained devices

## Security Checklist

- [ ] Allowlist stored outside project root
- [ ] Symlink resolution before validation
- [ ] Blocked patterns for sensitive paths
- [ ] Non-root execution in container
- [ ] Resource limits (memory, CPU)
- [ ] Per-group/session isolation
- [ ] Credential filtering (only necessary env vars)
- [ ] Graceful shutdown handling
