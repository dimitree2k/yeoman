# Yeoman WhatsApp Session Incident Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Baileys session-material transcript exposure without losing raw messages or memory, then reopen the Milestone 01 Task 2 gate.

**Architecture:** Remove log-body emission from the shared diagnostic helper instead of attempting an incomplete secret denylist. Scan the incident corpus offline with a deterministic metadata-only scanner, then rotate the WhatsApp linked session through the canonical QR helper while preserving a timestamped authentication backup and all memory/archive stores.

**Tech Stack:** Bash, Python 3 standard library, `unittest`, SHA-256, systemd user services, repository `whatsapp_qr_reconnect.py`, signed and age-encrypted incident evidence.

## Global Constraints

- Never print, copy, summarize, hash as a reversible encoding, or embed any matched secret value in a tool transcript, report, test result, receipt, or Git object.
- Never invoke `recent_logs.sh` against live log content until Task 1 is reviewed and accepted.
- Preserve every raw message, memory database, archive, receipt, and provenance record; authentication/session quarantine is the only permitted state replacement.
- Keep `yeoman-bridge.service` and `yeoman-overseer.service` stopped until the owner-authorized QR relink starts; keep Gateway and storage unchanged.
- Task 2 of the release-baseline plan remains NO-GO until Agent A and Agent B approve the incident-close package.
- Terra is authorized for implementation/task-review workers. Every active standing Agent A/B review prompt must explicitly use `gpt-5.6-luna`.

---

### Task 1: Make the Yeoman runtime log helper metadata-only

**Files:**
- Modify: `/home/dm/.codex/skills/yeoman-runtime/scripts/recent_logs.sh`
- Modify: `/home/dm/.codex/skills/yeoman-runtime/SKILL.md`
- Create: `/home/dm/.codex/skills/yeoman-runtime/tests/test_recent_logs.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-1-report.md`

**Interfaces:**
- Consumes: `YEOMAN_LOG_DIR`, service name `gateway|bridge|overseer`, optional line-count argument for compatibility.
- Produces: file path, byte count, line count, modification time, and literal `content=suppressed`; never a log-body byte.

- [ ] **Step 1: Add the failing controlled-fixture test**

Create a temporary `bridge.log` containing a synthetic `chainKey` value. Run the real helper with `YEOMAN_LOG_DIR` set to the temporary directory. Assert exit code zero, the synthetic value and the entire fixture line are absent, and `content=suppressed` is present. The production change that makes this pass is removal of the `tail | redact` content path.

- [ ] **Step 2: Run the test against the current helper and record RED**

Run:

```bash
python -m unittest -v /home/dm/.codex/skills/yeoman-runtime/tests/test_recent_logs.py
```

Expected: FAIL because the controlled synthetic `chainKey` content appears in captured output. Do not print the captured subprocess output from the test.

- [ ] **Step 3: Replace content emission with metadata-only output**

Delete the denylist `redact()` pipeline and the `tail` invocation. Retain service validation, path discovery, line clamping for compatibility, and metadata output. After each metadata line, emit:

```text
content=suppressed
```

- [ ] **Step 4: Correct the skill contract**

Describe `recent_logs.sh` as metadata-only and explicitly require source-specific structured diagnostics for content. Remove any statement that calls it a redacted tail.

- [ ] **Step 5: Verify GREEN and validate the skill**

Run:

```bash
python -m unittest -v /home/dm/.codex/skills/yeoman-runtime/tests/test_recent_logs.py
python /home/dm/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/dm/.codex/skills/yeoman-runtime
```

Expected: all tests pass and skill validation succeeds. Record pre/post SHA-256 values and test output in the task report, without log content.

### Task 2: Produce a metadata-only exposure-scope report

**Files:**
- Create: `scripts/incident_secret_scope_scan.py`
- Create: `tests/shared/test_incident_secret_scope_scan.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-2-report.md`

**Interfaces:**
- Consumes: explicit regular-file paths and directories passed on the command line.
- Produces: JSON with scanner version, corpus-file count, corpus-byte count, corpus-manifest SHA-256, counts by secret category, unreadable-file count, and scan timestamp; never matched text, source lines, basenames, or clear paths.

- [ ] **Step 1: Write failing tests**

Use temporary files containing synthetic Baileys key-field names, common credential forms, and benign controls. Assert only counts and SHA-256 commitments appear, no fixture content or clear path appears, unreadable/non-regular inputs fail closed, and repeated scans of an unchanged corpus have the same manifest commitment.

- [ ] **Step 2: Verify RED**

Run the exact new test module and confirm failure because the scanner does not exist.

- [ ] **Step 3: Implement the minimum standard-library scanner**

Stream files as bytes, count fixed secret-category regex matches without retaining matched values, hash each file, and derive the corpus commitment from sorted opaque file identifiers plus file hashes. Bound optional ASCII-whitespace gaps at 128 bytes so the streaming overlap has a finite maximum. Open every path component and recursive child relative to held directory descriptors with no-follow checks. Emit one JSON object to stdout. Never emit exception data that could contain file content.

- [ ] **Step 4: Verify GREEN and mutation resistance**

Run the exact test module, then remove one redaction/count branch locally to prove a test fails, restore it, and rerun the test plus Ruff on the two files.

- [ ] **Step 5: Run the restricted incident scan**

Scan only the exact implementer transcript, Bridge log files, shell-history files, Task 1 reports/receipts, and WhatsApp authentication-store metadata authorized by the controller. Store only the JSON summary in the protected incident-evidence directory. Sign and encrypt the result; record ciphertext/signature/manifest commitments in the task report.

### Task 3: Rotate and revalidate the WhatsApp linked session

**Files:**
- Use: `scripts/whatsapp_qr_reconnect.py`
- Update: `session-context/2026-08-16-implementation-program-log.md`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-3-report.md`

**Interfaces:**
- Consumes: owner authorization, owner QR scan, current authentication state.
- Produces: timestamped old-auth quarantine, fresh linked session, protocol-v3 connected health, changed opaque session fingerprint, no unknown linked devices, send/receive receipt, and memory/archive integrity commitments.

- [ ] **Step 1: Obtain explicit owner authorization**

State that every existing linked device will be revoked/logged out and a phone QR scan is required. Do not proceed from general architecture approval alone.

- [ ] **Step 2: Record pre-rotation integrity commitments**

Hash manifests for raw-message, memory, archive, and receipt stores without reading content into stdout. Record only commitments and counts.

- [ ] **Step 3: Revoke and relink**

Use the canonical reconnect helper so it creates a timestamped authentication backup and owner-only stable SVG. Keep the QR payload out of chat. Have the owner revoke linked devices and scan the fresh QR.

- [ ] **Step 4: Verify the new session**

Confirm no unknown linked device, changed opaque session identity, authenticated protocol-v3 connected health, controlled send/receive receipt, and unchanged memory/archive commitments.

- [ ] **Step 5: Close the incident gate**

Sign and dual-recipient-age-encrypt the incident-close receipt, record transcript retention/deletion disposition and residual uncertainty, then request explicit Luna Agent A/B review. Begin release-baseline Task 2 only after both approve entry.
