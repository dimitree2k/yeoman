# Yeoman WhatsApp Session Incident Containment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Baileys session-material transcript exposure without losing raw messages or memory, then reopen the Milestone 01 Task 2 gate.

**Architecture:** Remove log-body emission from the shared diagnostic helper instead of attempting an incomplete secret denylist. Scan the incident corpus offline with a deterministic metadata-only scanner, then rotate the WhatsApp linked session through the canonical QR helper while preserving all memory/archive stores and placing compromised authentication only into verified encrypted, non-restorable quarantine.

**Tech Stack:** Bash, Python 3 standard library, `unittest`, SHA-256, systemd user services, repository `whatsapp_qr_reconnect.py`, signed and age-encrypted incident evidence.

## Global Constraints

- Never print, copy, summarize, hash as a reversible encoding, or embed any matched secret value in a tool transcript, report, test result, receipt, or Git object.
- Never invoke `recent_logs.sh` against live log content until Task 1 is reviewed and accepted.
- Preserve every raw message, memory database, archive, receipt, and provenance record; authentication/session quarantine is the only permitted state replacement.
- Keep Bridge, Gateway, and Overseer stopped through the first-Luna-authorized quiesced v1/v2 baseline and owner gates. During relink only the pinned Bridge-only helper/direct observer may run; stop Bridge again before post-capture/compare. Keep Gateway stopped and storage unchanged.
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

### Task 3: Harden the canonical QR reconnect boundary

**Files:**
- Modify: `scripts/whatsapp_qr_reconnect.py`
- Create: `tests/gateway/test_whatsapp_qr_reconnect_script.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-3-report.md`

**Interfaces:**
- Consumes: empty prepared authentication directory, explicit owner-revocation acknowledgement, Bridge health.
- Produces: owner-only QR SVG plus allowlisted service/health metadata; never raw health/error/QR content and never a Gateway restart.

- [ ] **Step 1: Write failing orchestration and disclosure tests**

Prove that status/poll output is a compact allowlist, exception detail is suppressed, start refuses nonempty auth or absent owner acknowledgement, and neither start nor restore restarts Gateway.

- [ ] **Step 2: Run RED**

Run the new test module against the current helper and record failures for raw health output and Gateway restart behavior.

- [ ] **Step 3: Implement the minimal safe boundary**

Separate auth quarantine from QR startup. Require a prepared empty auth directory and explicit owner-revocation flag, remove automatic auth backup and Gateway-restart options from start, keep Overseer untouched, capture QR-render subprocess output, reduce health to protocol/connected/running fields, and collapse operational exceptions to a generic error marker.

- [ ] **Step 4: Verify GREEN and mutation resistance**

Run the exact test module, Ruff, the complete Gateway suite, and a mutation that restores a Gateway restart or raw `lastError` output.

- [ ] **Step 5: Review before owner interaction**

Obtain independent task review. Do not quarantine auth, revoke devices, start Bridge, or render a QR in this task.

### Task 4: Rotate and revalidate the WhatsApp linked session

**Pre-owner preparation:** Implement and accept the synthetic-only controller bundle in `docs/superpowers/plans/2026-08-16-whatsapp-rotation-prep-orchestration.md` and its `-01` through `-03` companion plans before requesting any owner confirmation. Its operator evidence is fixed under `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`; only runtime auth remains under `/home/dm/.yeoman`. First Luna GO authorizes only full quiescence and read-only v1 inspection/v2 baseline; mandatory second evidence-bound Luna GO is required before any owner turn. This Task remains owner-interactive; no authentication mutation, device revocation, QR rendering, Bridge start, or smoke send is authorized by the preparation bundle.

**Files:**
- Use: `scripts/whatsapp_qr_reconnect.py`
- Update: `session-context/2026-08-16-implementation-program-log.md`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-report.md`

**Interfaces:**
- Consumes: separate protected owner records for all-device revocation, quarantine authorization, then post-quarantine phone readiness; owner QR scan; current authentication state.
- Produces: verified encrypted non-restorable old-auth quarantine, fresh linked session, protocol-v3 connected health, changed opaque session fingerprint, protected owner-attested device inventory, send/receive causal receipt, and memory/archive integrity commitments.

- [ ] **Step 1: Revalidate the accepted v2 baseline and record host-backed owner turns**

Revalidate the accepted protected v2 baseline under full quiescence; do not create a redundant generic manifest. Only after mandatory second evidence-bound Luna GO, record `owner-all-devices-revoked-v1` from its exact host-backed owner turn, then predecessor-linked `owner-quarantine-authorized-v1` from a distinct host-backed owner turn. After verified quarantine, record third `phone-ready-v1` before observer/helper readiness and QR scan. After relink/fingerprint, record fourth `owner-device-inventory-v1` host-backed attestation. Never treat generic approval, one source turn, or a combined checkbox as these records.

- [ ] **Step 2: Use the accepted pre-rotation v2 commitments**

Use only the accepted quiesced v2 baseline commitments and encrypted adapter inventory. Do not read Gateway databases or create a generic replacement manifest; direct Bridge observer artifacts carry all relink-window raw message evidence.

- [ ] **Step 3: Revoke and relink**

Create a crash-safe encrypted non-restorable quarantine of old authentication after the two separate pre-action records. With the owner explicitly told not to scan, invoke the reviewed Bridge-only reconnect helper against prepared empty auth so it starts Bridge and creates the owner-only QR. Then start the direct authenticated observer and prove protocol-v3 readiness; only then may the owner scan. Keep QR payload out of chat and retain every observer event encrypted.

- [ ] **Step 4: Verify the new session**

Confirm host-attested intended/no-unknown device inventory, changed opaque session identity under the same `canonical_auth_tree_v1` serialization, observer-complete capture, authenticated protocol-v3 connected health, and one-shot causal smoke receipt. `capture_failed` is incident-close NO-GO. Stop Bridge before post-v2 capture/compare; Gateway/Overseer remain stopped.

- [ ] **Step 5: Close the incident gate**

Sign and dual-recipient-age-encrypt the incident-close receipt, record transcript retention/deletion disposition and residual uncertainty, then request explicit Luna Agent A/B review. Begin release-baseline Task 2 only after both approve entry.
