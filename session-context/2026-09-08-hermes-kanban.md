# Hermes Kanban connection — 2026-09-08

User authorized replacing Yeoman's local Kanban, deleting all old cards, and testing task creation from the owner WhatsApp context. Use the existing default board and existing A2A worker; no new adapter or database access in Yeoman.

## Changes

- Removed `~/.yeoman/workspace/kanban/` (old script, inbox and backlog); no migration requested.
- Added discoverable `~/.yeoman/workspace/skills/kanban/SKILL.md`: delegate to worker `hermes`, explicitly select board `default`, return actual Kanban card ID/status. Arvid is Yeoman's persona, not a Hermes profile. Capture-only tasks use a native sticky `blocked` block; execution uses profile `yeoman-bridge` within its web/Kanban capabilities.
- Updated `~/.hermes/profiles/yeoman-bridge/SOUL.md` and profile description with this contract.
- Matched profile top-level `toolsets` to `platform_toolsets.cli`: `[kanban, skills, web]`. The native `tools.kanban_tools._profile_has_kanban_toolset()` gate reads the top-level list. CLI selection alone left Kanban unavailable; the first live test reproduced this. The same gate check passed after the configuration correction.
- Added Kanban to the existing served agent advertisement in `~/.hermes/config.yaml`; restarted `hermes-gateway.service`. Verified active service and HTTP 200 agent card exposing `toolset.kanban`, `toolset.skills`, `toolset.web` on the existing loopback route. No Yeoman Python code or policy changes. Context builder discovers skills each turn.

## Verification

- Skill validator passed; actual Yeoman SkillsLoader discovers the workspace Kanban skill.
- Used running Gateway IPC `owner_turn` for the configured owner WhatsApp session with `post_to_whatsapp=false`. This exercises the owner policy, live responder, skill discovery and existing A2A delegation. It is a synthetic owner turn, not a real incoming WhatsApp transport test; no test message was sent to WhatsApp.
- Natural-language request: “Arvid, lege bitte im Kanban die Test-Aufgabe WhatsApp Kanban Verbindungstest 2026-09-08 an: Später drei kurze Ideen für einen verregneten Sonntag sammeln. Bitte nur anlegen, noch nicht starten.”
- First attempt correctly reported unavailable Hermes tools and created no card. After the profile fix, retry listed existing cards and called native `kanban_create` with board `default`, assignee `yeoman-bridge`, initial_status `blocked`, triage false.
- Native tool returned `ok=true`, task `t_c0432c12`, status `blocked`; independent read-only SQLite inspection confirmed exactly one matching title, same ID/status/assignee and body. That initial blocked card later received a worker start, so it was archived as a failed capture-only probe.
- The successful model call omitted idempotency_key (stored NULL), despite skill guidance. Retry safety is prompt-level guidance and a prior list check, not an enforced exactly-once guarantee. Do not claim otherwise.
- Automatic completion delivery to WhatsApp is not implemented. Ask Arvid for status. Real WhatsApp ingress remains to be exercised by the user sending a message.

## Follow-up smoke test and lifecycle finding

“Arvid, lege bitte eine Aufgabe an: Drei Ideen für einen verregneten Sonntag sammeln. Nur anlegen, noch nicht starten.”

- The real WhatsApp message arrived at 2026-09-08 00:53 local time, but the Yeoman turn made no `a2a_delegate` call and still replied that the card existed. That reply was unverified.
- A controlled owner-only replay then delegated natively and created `t_887820f2` with the exact requested title on `default`, assignee `yeoman-bridge`, and the requested capture-only body. Hermes returned `blocked`, but its dispatcher promoted the card because an initial `blocked` status has no sticky block event. The root and three child idea cards subsequently ran and are now `done`; this run must not be described as "not started".
- A triage-only retry was not a safe parking solution: this Hermes gateway has `kanban.auto_decompose=true`, so the embedded watcher specified/promoted the triage card and a worker claimed it. The durable capture-only procedure is now: create `initial_status=blocked`, immediately `kanban_unblock`, then `kanban_block(kind=needs_input)` in the same delegated turn. The latter creates the sticky event that the dispatcher honors. Future replies must report the native ID/status and verify that `started_at` is empty and no `claimed`/`spawned` event exists; the administrative blocked run emitted by `kanban_block` is not worker execution.
- To inspect the archived historical runs, pass `--archived`: `hermes kanban --board default list --archived`. The clean active card is `hermes kanban --board default show t_54fd8ad2`.
- The failed probes (`t_c0432c12`, `t_887820f2` and its three children, `t_207ff3df`, and `t_2de065d4`) were archived after evidence capture. A final owner-only replay created the exact requested title as `t_54fd8ad2` with idempotency key `wa-kanban-rainy-sunday-2026-09-08-clean`; it is active on `default`, `blocked` with `block_kind=needs_input`, assignee `yeoman-bridge`, `started_at=NULL`, and no `claimed`/`spawned` event. The only run row is the administrative block record. This is the clean user-visible test card.
