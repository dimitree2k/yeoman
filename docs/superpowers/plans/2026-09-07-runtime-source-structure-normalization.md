# Yeoman Runtime and Source Structure Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Die Struktur von `/home/dm/.yeoman` (private Runtime) und `/home/dm/Documents/yeoman` (änderbarer Source) so vereinheitlichen, dass jeder persistente Zustand genau einen Besitzer und einen nachvollziehbaren Ablageort hat. Veraltete Dokumentation, doppelte Datenpfade und unnötige Caches sollen kontrolliert entfernt werden, ohne den laufenden WhatsApp-Betrieb oder relevante Arbeit aus `c/turn-engine-v2` zu verlieren.

**Architecture:** Zwei klar getrennte Ebenen bleiben erhalten: Source-Code und Tests liegen ausschließlich in `/home/dm/Documents/yeoman`; produktiver, privater und veränderlicher Zustand liegt in `/home/dm/.yeoman`. Die Migration erfolgt quiesced und evidence-first: erst Besitz und aktive Nutzung belegen, dann passend zum Speicherformat sichern (SQLite WAL-aware, Session-State als Markdown-Dateien), anschließend Pfade ändern, Services kontrolliert neu laden und zuletzt alte Pfade mit einer begrenzten Retention archivieren. `main` bleibt die Integrationsbasis; Inhalte aus `c/turn-engine-v2` werden vor jeder Bereinigung über Commit-, Datei- und Laufzeitbezug bewertet.

**Tech Stack:** Python 3.12/`uv`, TypeScript/Node.js für Bridge, SQLite mit WAL, user-level systemd, Git, `pytest`, `ruff`, `npm test`, `rg`.

**Spec:** Dieses Dokument ist die kombinierte Spec und der Implementierungsplan: `docs/superpowers/plans/2026-09-07-runtime-source-structure-normalization.md`.

## Global Constraints

- `/home/dm/Documents/yeoman` ist der einzige Ort, an dem Source-Code geändert wird. `/home/dm/.yeoman` bleibt private Runtime-/Konfigurations-/Datenablage.
- Keine laufende Datenbank, WAL-Datei, Media-Datei, Policy-Datei oder Service-Unit wird gelöscht, bevor aktive Nutzung, Backup, Referenzen und Rollback geprüft sind.
- Keine manuellen Änderungen an installierten Artefakten unter `~/.local/share/uv/tools/yeoman-gateway` oder an generierten Bridge-Artefakten unter `~/.yeoman/var/cache/bridge`. Änderungen erfolgen im Source und werden über den vorgesehenen Deploy-/Restart-Weg ausgerollt.
- Die aktiven Einträge bleiben zunächst unverändert: `data/contacts/contacts.db`, `data/inbound/chat_registry.db`, `data/inbound/reply_context.db`, `data/memory/memory.db`, `data/media/document_cache.db`, `data/policy/response_pauses.json`, `policy.json`, `policy/audit/` und `var/media/`.
- SQLite-Migrationen berücksichtigen Datenbank, `-wal` und `-shm`; während Backup und Move laufen keine schreibenden Gateway-/Bridge-Prozesse.
- Ein grüner Testlauf beweist keine Runtime-Korrektheit. Nach jeder produktiven Pfadänderung müssen zusätzlich systemd-Zustand, PID-/Socket-/Port-Bindings, Datenbankintegrität und frische Start-/IPC-Evidence geprüft werden.
- Der bestehende Quarantäneordner `/home/dm/Documents/yeoman-cleanup-archive-20260907/runtime-redundant-20260907` wird als Recovery-Archiv behandelt. Er ist kein neuer produktiver Datenpfad und wird erst nach der definierten Retention entfernt.
- `pinchtab` bleibt während dieser Normalisierung erhalten. Seine Stilllegung ist ein separates Folgevorhaben; Task 7 beschreibt dessen Vorbereitung und ist kein Abschlusskriterium dieser Migration.
- `var/` enthält veränderliche Betriebsdaten, nicht ausschließlich regenerierbare Artefakte. Nur bestätigte Caches gelten als regenerierbar; Logs und Medien erhalten eigene Aufbewahrungs- und Löschregeln.
- Ausführung bestätigt: Die Änderungen laufen direkt auf `main`; Services dürfen für quiesced Migrationen unterbrochen werden; die Recovery-Retention beträgt 14 Tage ab der letzten erfolgreichen Post-Cutover-Prüfung (geplantes Ende 2026-09-21, Europe/Berlin); die bestätigte Session-State-Konfiguration wird nach `data/memory/session-state/` migriert.
- Rollback-Entscheidung: Ein Rollback darf neue Daten seit dem Cutover verlieren. Vor einem Rollback entscheidet der Betreiber selbst, welche neuen Daten gesichert oder anderweitig aufgelöst werden; die Migration verspricht keine automatische Zusammenführung oder Replay dieser Daten.

---

## 1. Zielbild

### Runtime: `/home/dm/.yeoman`

```text
~/.yeoman/
├── config.json                 # private Runtime-Konfiguration
├── policy.json                 # effektive Root-Policy
├── policy/
│   └── audit/                  # Policy-Änderungsjournal
├── secrets/                    # private Credentials, niemals in Git
├── data/                       # langlebiger, fachlicher Zustand
│   ├── contacts/               # contacts.db
│   ├── inbound/                # chat_registry.db, reply_context.db
│   ├── memory/                 # memory.db und später session-state
│   ├── media/                  # persistente Media-Metadaten/Caches
│   └── policy/                 # langlebiger Policy-Zustand
├── workspace/                  # user-/sessionbezogener Arbeitszustand
├── var/                        # veränderliche Betriebsdaten
│   ├── cache/                  # regenerierbar nach Prüfung der Eingaben
│   ├── logs/                   # Betriebsbelege mit eigener Retention
│   └── media/                  # incoming/outgoing WhatsApp Media
├── run/                        # einheitlicher Zielort für Socket/PID/Lock
└── backups/                    # explizit benannte Recovery-Sicherungen
```

`run/` ist das Zielbild für alle Runtime-Handles. Derzeit liegt der aktive Gateway-Socket bereits unter `run/`, während PIDs und Locks teilweise unter `var/run/` liegen. Diese Vereinheitlichung ist deshalb eine koordinierte Source-, Config-, systemd- und Restart-Migration und keine kosmetische Umbenennung.

### Source: `/home/dm/Documents/yeoman`

```text
yeoman/
├── packages/{shared,gateway,overseer,bridge}/
├── tests/{shared,gateway,overseer}/
├── docs/
│   ├── architecture/
│   ├── guides/
│   ├── security/
│   └── superpowers/{plans,specs}/
├── session-context/            # historische/evidenzbasierte Sitzungsnotizen
├── scripts/
├── .superpowers/               # ignorierter Scratch-/Tool-Zustand
├── .venv/                      # lokale Entwicklungsumgebung
└── .worktrees/                 # temporär; neue Worktrees bevorzugt außerhalb
```

### Namenskonventionen

- Runtime-Verzeichnisse tragen stabile, semantische lower-case-Namen: `data`, `var`, `run`, `secrets`, `backups`, `workspace`.
- SQLite-Dateien und maschinenlesbare Runtime-Dateien verwenden `snake_case`: `chat_registry.db`, `reply_context.db`, `document_cache.db`.
- Source-Dokumente verwenden lower-kebab-case und bei zeitbezogenen Dokumenten den Präfix `YYYY-MM-DD-slug.md`.
- Dauerhafte Pläne liegen unter `docs/superpowers/plans/`, Spezifikationen unter `docs/superpowers/specs/`; `session-context/` bleibt für historische Audits und Sitzungsbelege getrennt.
- Sicherungen werden nach Artefakt gruppiert, etwa `backups/config/`, `backups/policy/`, `backups/migrations/`, mit Zeitstempel `YYYY-MM-DDTHHMMSS±HHMM`.
- Branches bleiben `c/topic`. Neue externe Worktrees verwenden `c-topic` als Verzeichnisnamen.
- Keine Modellnamen, Branch-Namen oder Chatnamen in produktiven Runtime-Pfaden, sofern sie nicht fachlich zwingend sind.

## 2. Ownership-Invarianten

| Bereich | Kanonischer produktiver Ablageort | Nicht mehr zulässige Zweitablage |
|---|---|---|
| Kontakte | `data/contacts/contacts.db` | Root- oder `data/inbound/contacts.db`-Kopien |
| Chat-/Inbound-Index | `data/inbound/chat_registry.db` | `data/inbound/inbound.db`, alte Archive als aktive DB |
| Reply-Kontext | `data/inbound/reply_context.db` | ad-hoc Kopien außerhalb von `data/inbound/` |
| Memory | `data/memory/memory.db` | Root `data/memory.db` |
| Session-State | künftig `data/memory/session-state/` | `workspace/memory/session-state/` |
| Policy | `policy.json` plus `policy/audit/` | `data/policy/audit/` als zweites Journal |
| WhatsApp-Media | `var/media/incoming/whatsapp` und `var/media/outgoing/whatsapp` | Root `media/`, `data/media/` für Binär-Media |
| Runtime-Handles | künftig `run/` | verstreute aktive Dateien unter `var/run/` |
| Source-Pläne/Specs | `docs/superpowers/{plans,specs}/` | neue Dateien direkt unter `docs/plans/` oder beliebigen Parallelordnern |


Ein Ablageort ist kein Besitzer. Task 1 ergänzt für jede Zeile oben den konkret verifizierten Writer und den maßgeblichen Pfadhelper beziehungsweise Config-Key. Die folgende Tabelle definiert die dafür verbindlichen Felder und Lebenszyklen; noch ungeprüfte Code-Zuständigkeiten werden nicht als bestätigt ausgegeben.

| Bereich | Zuständige Komponente / Writer | Kanonischer Resolver / Config-Key | Haltbarkeit | Löschbedingung |
|---|---|---|---|---|
| Kontakte | Gateway `build_gateway_runtime()` → `ContactsService`/`ContactsStore` | `get_operational_data_path()/contacts/contacts.db` | Langlebiger fachlicher Zustand | Nur nach belegter Ablösung, Sicherung und abgelaufener Recovery-Frist |
| Chat-/Inbound-Index | Gateway `build_gateway_runtime()` → `ChatRegistry` | `get_operational_data_path()/inbound/chat_registry.db` | Langlebiger fachlicher Zustand | Nur nach belegter Ablösung, Sicherung und abgelaufener Recovery-Frist |
| Reply-Kontext | Gateway `build_gateway_runtime()` → `InboundArchive` | `get_operational_data_path()/inbound/reply_context.db` | Langlebiger fachlicher Zustand | Nur nach belegter Ablösung, Sicherung und abgelaufener Recovery-Frist |
| Memory | Gateway `MemoryService` → `MemoryStore` | `Config.memory.db_path`, aktuell `data/memory/memory.db` | Langlebiger fachlicher Zustand | Nur nach belegter Ablösung, Sicherung und abgelaufener Recovery-Frist |
| Policy | Gateway `PolicyLoader`/`PolicyAdminService` → `PolicyAuditStore` | `get_policy_path()` → `policy.json`; Audit unter `policy/audit/` | Policy und Journal dauerhaft | Nur nach belegter Ablösung, Sicherung und abgelaufener Recovery-Frist |
| Media-Metadaten | Gateway `build_gateway_runtime()` → `DocumentCache` | `get_operational_data_path()/media/document_cache.db` | Cache mit fachlichen Referenzen | Kein aktiver Nutzer; Wiederaufbau aus verfügbaren Eingaben, Retention beachten |
| Session-State | Gateway `SessionStateStore`, aufgerufen durch `MemoryService` | `memory.wal.stateDir`; Default `DEFAULT_SESSION_STATE_DIR`, Auflösung über `get_session_state_path()` | Langlebige Markdown-PRE/POST-Einträge | Alte Ablage erst nach Inhaltsprüfung, zwei Neustarts und Recovery-Frist |
| WhatsApp-Media | Bridge `media_paths.ts` und Gateway Media-Storage | `MEDIA_INCOMING_DIR`/`MEDIA_OUTGOING_DIR`, aktuell `var/media/.../whatsapp` | Potenziell einzigartige Nutzdaten; Typ-Retention aus Media-Config | Eigene Retention/`delete_*`-Regeln und Prüfung verbleibender Referenzen |
| Logs | Gateway-/Bridge-/Overseer-Logger und systemd `StandardOutput` | `get_logs_path()` bzw. effektive Unit-/Logger-Konfiguration | Betriebsbelege, nicht regenerierbar | Keine Löschung in dieser Migration; bestehende Betreiber-/Log-Retention |
| Caches | Gateway `WhatsAppRuntime.ensure_runtime()` | Source-Bridge → `var/cache/bridge/`; nur aktuelles Ziel ist Eingabe | Regenerierbar aus Source und Dependencies | Kein aktiver Nutzer; Wiederaufbau aus Source möglich |
| Runtime-Handles | Gateway, Bridge und Overseer | `get_run_path()` → `~/.yeoman/run/`, abgestimmte IPC-/Unit-Konfiguration | An Prozesslebensdauer gebunden | Besitzer beendet, keine offenen Handles; Neuerzeugung beim Start |
| Source-Pläne/Specs | Maintainer im Source-Repository | `docs/superpowers/{plans,specs}/` gemäß `AGENTS.md` | Dauerhafte Architektur-/Planungsdokumentation | Explizite Archivierungsentscheidung; Historie und Links erhalten |

---

## 3. Implementation Tasks

Ausführung in getrennten Etappen: Tasks 1–4 legen Baseline, Dokumentation und Backup-Ordnung fest. Tasks 5 und 6 sind jeweils eigenständig zu prüfende Pfadmigrationen. Task 8 schließt diese Normalisierung ab; das separate Pinchtab-Folgevorhaben aus Task 7 blockiert diesen Abschluss nicht.

### Task 1: Baseline einfrieren und Herkunft aus `c/turn-engine-v2` dokumentieren

**Files:**

- Modify: `docs/superpowers/plans/2026-09-07-runtime-source-structure-normalization.md` only for recorded evidence and checked-off execution steps.
- Read/verify: `/home/dm/Documents/yeoman`, `/home/dm/.yeoman`, `/home/dm/.config/systemd/user/yeoman-gateway.service`, `/home/dm/.config/systemd/user/yeoman-bridge.service`, `/home/dm/.config/systemd/user/yeoman-overseer.service`, `/home/dm/.config/systemd/user/yeoman-pinchtab.service`.
- Create: a dated migration evidence note under `session-context/` only when execution starts.

- [x] Record the exact `main` and `c/turn-engine-v2` commit IDs, branch status, worktrees, and the file/commit range containing A2A, owner-turn observability, and Turn Engine V2 work.
- [x] Compare `main...c/turn-engine-v2` by commit and path, classify each changed area as required-now, potentially-reusable, historical, or unverified, and preserve the classification in a dated evidence note.
- [x] Capture active systemd units, launcher paths, `YEOMAN_HOME`, socket paths, bridge media environment, open runtime file descriptors, and current database sizes/checksums.
- [x] Complete the ownership/lifecycle table with verified writer callsites, effective path resolvers/config keys and retention conditions for every store; record unresolved entries before the affected migration proceeds.
- [x] Confirm that `/home/dm/.yeoman` and `/home/dm/Documents/yeoman` are independently clean or explicitly record every pre-existing modification before any migration step.
- [x] Do not delete or merge the branch based on source-only evidence; retain `c/turn-engine-v2` until all potentially-reusable changes have been cherry-picked, merged, or explicitly declared obsolete.

**Acceptance:** A reviewer can identify which branch contains each relevant A2A/owner-turn-observability change, which files are live, and which state is safe to touch. No service, policy, database, or media path changes in this task.

### Task 2: Runtime documentation auf den tatsächlichen Betrieb korrigieren

**Files:**

- Modify: `/home/dm/.yeoman/CLAUDE.md` in the separate runtime repository.
- Modify: `/home/dm/Documents/yeoman/CLAUDE.md` and any source documentation found by the path scan.
- Verify: `docs/architecture/`, `docs/guides/`, `docs/security/`, `session-context/`.

- [x] Replace stale claims that the active Gateway runs from a repository `.venv` with the verified launcher/deployment model using the uv-installed Gateway and source-controlled Bridge deployment.
- [x] Document the actual ownership split: source in `/home/dm/Documents/yeoman`, private state in `/home/dm/.yeoman`, active Gateway socket in `~/.yeoman/run/`, and Bridge media under `~/.yeoman/var/media/`.
- [x] Classify references to `data/policy/audit`, `var/run`, root `media/`, and `workspace/data` against current usage. Document still-active paths as active until their respective cutover; distinguish current state from target layout.
- [x] Add a short “do not edit generated/runtime installation directly” rule and the approved deploy/restart workflow.
- [x] Run `rg` over both trees and correct current operational documentation only. Leave config, service units and path helpers unchanged in this task; change them during the relevant migration. Preserve historical snapshots and evidence, adding a dated clarification or link where needed.

**Acceptance:** Current documentation accurately distinguishes active paths, migration targets and retired paths. Historical evidence remains intact. No effective config or service changes occur in this task.

### Task 3: Backups, Caches und Quarantäne sauber benennen

**Files:**

- Modify: runtime-only paths under `/home/dm/.yeoman/backups/` and the existing quarantine archive.
- Modify: `.gitignore` or runtime ignore rules only if the verified backup layout requires it.
- Verify: `config.backup.*`, `policy.backup.*`, `.mypy_cache/`, `.ruff_cache/`, stale empty directories, and archive metadata.

- [x] Create `backups/config/`, `backups/policy/`, and `backups/migrations/` with owner-only permissions where secrets or private policy are involved.
- [x] Move root-level backup files into the appropriate artifact directory using the timestamp convention; preserve original bytes and record source/target checksums.
- [x] Remove only regenerable caches after confirming no active process has them open and no deployment script treats them as input.
- [x] Keep the existing redundant-state quarantine read-only for the retention period; record its contents, creation time, and planned expiry instead of treating it as a live fallback.
- [x] Record a concrete recovery-window duration and expiry timestamp for each migration/archive before any deletion. Backups remain until all post-cutover checks pass and that window expires. If no duration is recorded, retain the artifact.
- [x] Record separate retention and deletion conditions for logs and media, including remaining consumers/references; do not apply cache cleanup rules to them.

**Acceptance:** Root-level backup clutter is gone, no active component points at moved backups, and every retained recovery artifact has an owner, purpose, timestamp, and expiry.

### Task 4: Dokumentationshierarchie konsolidieren

**Files:**

- Move: `docs/plans/2026-03-09-langfuse-tracing-design.md` to `docs/superpowers/specs/2026-03-09-langfuse-tracing-design.md`.
- Move: `docs/plans/2026-03-09-langfuse-tracing.md` to `docs/superpowers/plans/2026-03-09-langfuse-tracing.md`.
- Modify: links and references found by `rg` after the moves.
- Keep: `session-context/` as a separate evidence/history area with its existing README and naming guidance.

- [x] Confirm both destination names are free and inspect Git history before moving the files.
- [x] Use a tracked move where source files are tracked; these two Langfuse files were ignored/untracked, so they were moved with `mv`, inbound links were scanned, and explicit `.gitignore` exceptions make the intended files trackable.
- [x] Adopt `docs/superpowers/plans/` and `docs/superpowers/specs/` as the current canonical plan/spec locations; do not create a second parallel hierarchy during this cleanup.
- [x] Keep generated `.superpowers/` scratch output out of durable documentation; archive only reviewed, meaningful results into `docs/` or `session-context/`.
- [x] Define a future decision point for a tool-agnostic `docs/{plans,specs,decisions,operations}` layout rather than mixing it into this migration.

**Acceptance:** There is one active location for plans and one for specs, historical evidence remains distinguishable, and all moved documents retain any available Git history and working links.

### Task 5: Runtime-Handles auf `~/.yeoman/run/` vereinheitlichen

**Files:**

- Modify: `packages/shared/yeoman_shared/utils/helpers.py` (`get_run_path`).
- Modify: `packages/shared/yeoman_shared/config/schema.py` and all Gateway/Overseer/Bridge call sites that derive PID, lock, or socket paths.
- Modify: relevant tests under `tests/shared/`, `tests/gateway/`, and `tests/overseer/`.
- Modify: affected Gateway/Bridge/Overseer systemd units and runtime IPC configuration discovered by the inventory, during the controlled rollout.
- Modify: matching runtime/source documentation.

- [x] Add or update tests first so the canonical run root, path derivation, and legacy-path rejection are explicit.
- [x] Enumerate every use of `get_run_path`, `var/run`, `gateway.sock`, `overseer.sock`, PID files, and lock files with `rg`; include service units and operational scripts.
- [x] Implement one canonical path helper rooted at `~/.yeoman/run/`; keep the active Gateway socket name stable unless a collision-free migration requires otherwise.
- [x] Prepare a runbook that records prior service states, pauses restart-capable supervisors/schedules, and stops Overseer, Gateway and Bridge as applicable. Include Overseer even if currently inactive; verify all affected processes have exited and released their locks and sockets.
- [x] Update code/config/service paths under quiescence and record the changes. Do not copy or restore PID, lock or socket files as durable state: owning processes recreate them after restart. Remove stale handles only after their old owners have exited. Run `systemctl --user daemon-reload` after unit changes.
- [x] Restore previously active services in dependency order (Bridge, Gateway, then Overseer/supervision); leave previously inactive services inactive. Verify systemd state, process command lines, socket ownership, IPC request/response, Bridge port, fresh log timestamps and exclusive lock behavior.
- [ ] Remove `var/run/` only after a zero-reference scan and after the rollback window expires.

**Acceptance:** Exactly one active runtime-handle root exists, all components agree on it, and a controlled restart recreates the expected socket/PID/lock artifacts without stale `var/run` dependencies.

**Retention hold:** The now-empty `var/run/` directory remains until the shared 14-day recovery expiry on 2026-09-21; it is not an active handle root.

### Task 6: Session-State von `workspace/memory` nach `data/memory` migrieren

**Files:**

- Modify: `packages/shared/yeoman_shared/config/defaults.py` (`DEFAULT_MEMORY["wal"]["state_dir"]`) and `packages/shared/yeoman_shared/config/schema.py` as needed for `.memory.wal.stateDir` resolution.
- Modify: `packages/shared/yeoman_shared/utils/helpers.py`, `packages/gateway/yeoman_gateway/memory/session_state.py`, `packages/gateway/yeoman_gateway/memory/service.py`, bootstrap, and all callers discovered by `rg`.
- Modify: tests covering state directory resolution, preservation of Markdown PRE/POST records, restart persistence, and path ownership.
- Modify: runtime documentation and effective config after the code change is deployed.

- [x] Inventory the effective `stateDir` and its existing resolution (`workspace / state_dir`). Define the new default relative to `YEOMAN_HOME`, falling back to `~/.yeoman`; preserve explicit absolute overrides and workspace-relative resolution of explicit relative overrides unless the rollout deliberately migrates that configured path. Record every source/target mapping; reject collisions rather than merging directories silently.
- [x] Add tests for the default target, alternate `YEOMAN_HOME`, alternate workspaces and explicit absolute/relative overrides. Prove that the migrated production configuration neither reads nor recreates the old workspace path.
- [x] Quiesce all session-state writers. Back up the complete Markdown directory with a filename/size/checksum manifest and verify byte-for-byte preservation, including PRE/POST entries. This store is not SQLite and has no SQLite WAL/SHM files or database replay step.
- [x] Deploy the source change, move the state atomically into `data/memory/session-state/`, update effective config, and preserve a rollback copy under `backups/migrations/`.
- [x] Restart and exercise a session-state read/write/recovery path; verify no new legacy files appear and that existing memory behavior is unchanged.
- [ ] Retire the old `workspace/memory/session-state/` only after the recovery window and a second clean restart.

**Acceptance:** Session state has one owner under `data/memory`, survives restart/recovery tests, and the old workspace path is neither read nor recreated.

**Retention hold:** The legacy directory is absent from the productive runtime, while its read-only recovery copy remains under `backups/migrations/` until 2026-09-21; final archive deletion and the second clean restart are intentionally deferred to that date.

### Task 7: Separates Folgevorhaben — Pinchtab kontrolliert stilllegen

Die folgenden Schritte gehören in einen separaten Stilllegungsplan und Ausführungszeitraum. Im Rahmen der Struktur-Normalisierung dürfen Referenzen inventarisiert werden; Browser-Verhalten, Autostart, Services und Daten bleiben erhalten. Die folgenden Abnahmekriterien gelten erst für das Folgevorhaben.

**Files:**

- Inspect and then modify/remove as justified: `packages/gateway/yeoman_gateway/agent/tools/browse.py`, `packages/gateway/yeoman_gateway/skills/browser/SKILL.md`, `packages/gateway/yeoman_gateway/skills/browser/scripts/start.sh`, `packages/gateway/yeoman_gateway/skills/browser/scripts/monitor.py`, browser tool registration/allowlists, and related tests/docs.
- Modify/remove: `/home/dm/.config/systemd/user/yeoman-pinchtab.service` during the execution gate.
- Archive then remove: `/home/dm/.yeoman/pinchtab` and `var/logs/pinchtab.log` only after the retention decision.

- [ ] Inventory all source, policy, skill, service, schedule, log, and runtime references with `rg`; inspect recent usage and effective tool availability without changing live state.
- [ ] Decide from evidence whether any current chat, automation, or operator workflow still needs browser capability. If yes, document the replacement or keep a clearly bounded browser capability until replacement validation passes.
- [ ] Add an explicit unavailable-browser behavior and tests for the no-Pinchtab case before removing the implementation, so a request fails clearly rather than hanging or silently routing elsewhere.
- [ ] Disable browser autostart and stop/disable `yeoman-pinchtab.service` only in the controlled execution window after the inventory gate is green.
- [ ] Remove stale source scripts, registration, docs, and service references; run a zero-reference scan for `pinchtab`, `PINCHTAB_BIN`, and the old data path.
- [ ] Preserve the old Pinchtab directory in the migration archive through the recovery window, then delete it as a separate, explicitly recorded cleanup action.

**Acceptance:** No active process, service, policy, tool registry, schedule, or source path depends on Pinchtab; browser requests have tested, explicit behavior; the removal is reversible until the retention window expires.

### Task 8: Abschlussprüfung und Branch-/Runtime-Hygiene

**Files:**

- Verify all changed files in `/home/dm/Documents/yeoman` and `/home/dm/.yeoman`.
- Update the relevant dated evidence note under `session-context/`.

- [x] Run `git diff --check`, source formatting/linting, the focused path/migration tests, the complete Python test suite, and Bridge tests from the supported environments.
- [x] Run repository-wide `rg` checks for every retired path, duplicate database basename, `var/run`, `data/policy/audit`, root `media/`, and `workspace/data`. Classify matches: no active dependency on a retired path is allowed; historical notes, migration instructions and rejection tests may retain explicit references. Pinchtab references remain expected until its separate decommissioning.
- [x] Verify all active SQLite databases with read-only `PRAGMA quick_check`/appropriate integrity checks, compare expected row/file counts, and confirm no unexpected `-wal`/`-shm` files remain in retired locations.
- [x] Verify `systemctl --user` active state, process command lines, Gateway socket, Bridge port, media directories, and fresh logs after at least one clean restart.
- [x] Re-check `main` versus `c/turn-engine-v2`; retain or integrate the branch only after A2A and owner-turn-observability changes are accounted for and tests/evidence are attached.
- [x] Commit source changes separately from runtime state changes, with migration notes and rollback instructions; leave no accidental secrets, databases, media, or generated caches in the source repository.

**Acceptance:** Source and runtime each have one clear owner per artifact, all services are healthy, no required `c/turn-engine-v2` work is lost, and every deletion is backed by evidence and a recovery decision.

## 4. Explicitly deferred decisions

- A broad rename of Python packages or service names is out of scope; current package names remain stable.
- A tool-agnostic documentation hierarchy may be evaluated later, after the `superpowers` plan/spec convention is no longer needed.
- Pinchtab removal is a separate follow-up with its own capability decision, plan and verification. Its continued presence does not prevent completion of this normalization.
- Runtime Git history is not merged into the Source Git history. The two repositories remain separate unless a later architecture decision explicitly changes that boundary.

## 5. Final rollback rule

Every migration task must leave a dated, owner-only backup until its post-cutover checks and recorded recovery window are complete. Record the compatible prior code revision/deployment, config and service units together with the state backup. Rollback means stopping/quiescing affected services and supervision, restoring that compatible code/config/path/state, reloading changed systemd units, restarting in the documented order, and re-running the same health checks. Runtime handles are recreated, not restored from backup. A partial rollback that leaves two active owners for one artifact is not an accepted state.

**Owner decision (2026-09-07):** Restoring a backup may discard data written after that backup. The owner explicitly accepts this risk and decides before a rollback whether and how to preserve those newer data. Automatic reconciliation, lossless reverse migration, or a separate data-preservation approval gate is not required by this plan.
