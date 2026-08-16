# WhatsApp Rotation Pre-owner Smoke Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add separate protected owner-turn evidence, post-relink phone device-inventory attestation, and a direct one-attempt Bridge self-chat smoke controller that never overclaims delivery.

**Architecture:** Do not use Gateway IPC `send_message`: it reports queue acceptance as `delivered: true`, loses Bridge `{to,messageId}`, and may retry ambiguous timeouts. Gateway stays stopped. With the owner explicitly told not to scan, first start the pinned Bridge-only helper so it starts Bridge and creates owner-only QR; then connect/authenticate the direct protocol-v3 observer and prove ready; only then may the owner open/scan QR. The observer captures every message event through relink/smoke as encrypted raw-event artifacts plus normalized provenance receipts. The controller reads held no-follow Baileys `creds.json` for `me.id`/LID and makes at most one direct send.

**Tech Stack:** Python 3 standard library, WebSocket client already used by `scripts/whatsapp_qr_reconnect.py`, `scripts/incident_evidence_lib.py`, `pytest`, Ruff.

## Global Constraints

- Protected owner records and receipts live only at `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`. Program keys stay in program-key roots. Stdout exposes only phase and public HMAC/ciphertext/signature commitments.
- There is no linked-device API. The controller does not claim to enumerate devices. Post-relink inventory is a protected owner-phone attestation that says intended Yeoman is present, no unknown device appears, and retains observed count/labels only inside encryption.
- The primary orchestrator obtains each exact `HostUserTurn(thread_id, completed_turn_id, user_message_item_id, timestamp, exact_content)` through trusted `codex_app__read_thread`. CLI receives bounded JSON only over inherited private FD/stdin, never argv/environment, and HMACs/encrypts it. There is no opaque fallback: missing/reread-mismatched host tuple refuses. Before every destructive transition, both standing reviewers independently reread the exact reference and confirm statement/predecessor; this is explicit host-API trust evidence, not nonrepudiation. Pre-action records are distinct `owner-all-devices-revoked-v1` then predecessor-linked `owner-quarantine-authorized-v1`; `phone-ready-v1` is a third record after quarantine; `owner-device-inventory-v1` is a fourth post-relink host turn.
- After current-auth fingerprint, `build_smoke_expectation` recomputes stable `canonical_auth_tree_v1`, requires its HMAC/serialization to match the protected current-auth receipt, then reads held `creds.json`, computes normalized self-JID HMAC, and writes encrypted `SmokeExpectation`. Immediately before send, repeat the same stable canonical auth-tree check plus the self-JID check. A changed/unstable tree fails before network I/O. No JID is exposed. Derive `clientMessageId = sha256("yeoman-whatsapp-incident-smoke-v1" || incident_id || rotation_nonce)[:32]`. `rotation_nonce` and one fixed harmless smoke-text template live only in that expectation; the owner must quote-reply to that one message. There is no random retry identifier.
- Persist `intent`, `attempt`, `bridge_acceptance`, and `inbound_reply` as protected records with sender, recipient, message, reply, channel, timestamp, and prior-receipt-HMAC provenance. Raw Bridge message events are full encrypted protected artifacts; no raw/content/JID is public. Retain and schedule them for import into the target immutable event log before incident-tool/evidence disposition.
- Under a short dedicated one-shot registry lock, atomically/fsync reserve intent then attempt keyed by incident+nonce+clientMessageId before network, then release that lock before network/observer wait. An existing attempt forever forbids a new send: return its terminal/acceptance receipt when present, otherwise terminal `external_effect_unknown`. Observer raw-artifact serialization uses an independent lock. Observer disconnect/drop/overflow writes a protected `capture_failed` receipt when evidence publication remains available, is terminal, and blocks incident close; `accepted_no_reply` requires the observer remained connected/complete for the entire bounded window.

---

### Task 1: Implement separate owner turns and post-relink inventory attestation

**Files:**
- Create: `scripts/whatsapp_rotation_smoke.py`
- Create: `tests/shared/test_whatsapp_rotation_smoke.py`
- Create: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-03-report.md`

**Interfaces:**
- `record_all_devices_revoked_turn(source: HostUserTurn, predecessor: EvidenceCommitment | None) -> EvidenceCommitment`
- `record_quarantine_authorized_turn(source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment`
- `record_phone_ready_turn(source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment`
- `record_device_inventory_turn(intended_yeoman_present: bool, no_unknown_devices: bool, observed_count: int, labels: Sequence[str], source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment`

- [ ] **Step 1: Write RED owner-evidence tests**

```python
def test_pre_action_gate_refuses_generic_or_combined_approval():
    assert load_owner_gate(protected_generic_approval()) is None
    assert load_owner_gate(protected_combined_checkbox()) is None
    assert load_pre_action_gate(record_revocation_then_authorization()) is not None
```

Add tests that phone readiness before quarantine, inventory before relink, altered signatures, or count/labels/JID disclosure fails. Use fake structured `codex_app__read_thread` tuples; reject missing/reread-mismatched/duplicate/combined host sources. No test infers owner identity.

- [ ] **Step 2: Implement fixed schemas and ordering**

```python
def record_all_devices_revoked_turn(source: HostUserTurn, predecessor: EvidenceCommitment | None) -> EvidenceCommitment:
    return record_owner_turn("owner-all-devices-revoked-v1", {"all_devices_revoked": True}, source, predecessor)

def record_quarantine_authorized_turn(source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment:
    return record_owner_turn("owner-quarantine-authorized-v1", {"quarantine_authorized": True}, source, predecessor)

def record_phone_ready_turn(source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment:
    require_verified_quarantine_receipt()
    return record_owner_turn("phone-ready-v1", {"phone_ready": True}, source, predecessor)
```

`owner-device-inventory-v1` is only recorded after relink/current-fingerprint verification and includes `intended_yeoman_present`, `no_unknown_devices`, `observed_count`, and `labels` inside ciphertext. It contains no self JID. Every record has a unique user-turn source commitment, actual user-turn timestamp, predecessor HMAC, and signature verification; actual user content remains encrypted.

- [ ] **Step 3: Verify GREEN, mutate, and commit**

```bash
uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_smoke.py
uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_rotation_smoke.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_smoke.py
```

Temporarily accept a generic approval and prove the test fails. Restore it and commit:

```bash
git add scripts/whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_smoke.py
git commit -m "feat(incident): record rotation owner attestations"
```

### Task 2: Implement direct Bridge event observer before QR scan

**Files:**
- Modify: `scripts/whatsapp_rotation_smoke.py`
- Modify: `tests/shared/test_whatsapp_rotation_smoke.py`

**Interfaces:**
- `BridgeEventObserver.start_before_qr() -> None`
- `BridgeEventObserver.capture_window() -> EvidenceCommitment`
- `BridgeEventObserver.close() -> EvidenceCommitment`

- [ ] **Step 1: Write RED observer tests**

```python
async def test_observer_captures_every_message_event_and_refuses_drop():
    observer = await observer_for_test().start_before_qr()
    await observer.feed(message_event("expected"))
    await observer.feed(message_event("unsolicited"))
    assert observer.public_count() == 2
    await observer.feed_overflow()
    assert observer.terminal_state == "capture_failed"
```

Test readiness before QR, authenticated protocol-v3 event filtering, encrypted full raw-event artifact retention, normalized provenance receipt, disconnect, drop, overflow, and ordering. Assert no raw event/content/JID reaches stdout.

- [ ] **Step 2: Implement the observer inside the smoke module**

```python
async def start_before_qr(self) -> None:
    await self.connect_authenticated_protocol_v3()
    await self.require_ready()
    self.capture_all_message_events = True
```

With the owner explicitly instructed not to scan, start the pinned Bridge-only helper first and wait for its owner-only QR marker; then connect/authenticate/prove observer-ready; only then allow QR open/scan. Capture every protocol-v3 `message` event from observer-ready through smoke closure. For each event, write its complete raw envelope as a protected artifact and a normalized protected provenance receipt; classify quote causality only from `replyToMessageId`. Retain all expected/unsolicited events and schedule their import into the target immutable event log before incident-tool/evidence disposition. If observer disconnects, drops an event, or overflows, record terminal capture failure and block completion; never claim complete capture. Gateway stays stopped and no Gateway DB is read.

- [ ] **Step 3: Verify and commit**

Run focused tests/Ruff, temporarily discard one unsolicited event and prove RED coverage fails, restore, then commit:

```bash
git add scripts/whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_smoke.py
git commit -m "feat(incident): capture rotation bridge events"
```

### Task 3: Implement direct one-attempt smoke and bounded receipt observation

**Files:**
- Modify: `scripts/whatsapp_rotation_smoke.py`
- Modify: `tests/shared/test_whatsapp_rotation_smoke.py`

**Interfaces:**
- `run_smoke(expectation: SmokeExpectation) -> SmokeResult`
- `build_smoke_expectation(current_auth: EvidenceCommitment) -> EvidenceCommitment`
- States: `external_effect_unknown`, `capture_failed`, `accepted_no_reply`, or `inbound_reply_observed`.
- `SmokeExpectation` is protected and contains `incident_id`, `rotation_nonce`, fixed harmless `smoke_text`, and accepted post-relink whole-auth/self-identity HMACs; it contains no user-supplied JID.

- [ ] **Step 1: Write RED send/receipt tests**

```python
async def test_ambiguous_response_is_terminal_and_never_retried(fake_bridge):
    result = await run_smoke(expected_smoke(), bridge=fake_bridge.timeout_after_send)
    assert result.state == "external_effect_unknown"
    assert fake_bridge.send_count == 1
    assert receipt_kinds() == ["intent", "attempt", "external_effect_unknown"]

async def test_accepted_without_reply_is_not_unknown(fake_bridge):
    result = await run_smoke(expected_smoke(), bridge=fake_bridge.accept_no_reply)
    assert result.state == "accepted_no_reply"
    assert fake_bridge.send_count == 1
```

Add tests with synthetic `creds.json` for non-regular/wrong-owner/wrong-mode/missing `me.id`, device-suffix normalization, unsupported JID domain, identity-HMAC mismatch, malformed protocol-v3 envelope, mismatched returned destination, accepted causal quote-reply, and no string containing `delivered`. Add crash/manual-rerun cases before send, after network before response, after response before receipt, and every protected receipt publication failure; each proves a second send is forbidden.
Also prove the short one-shot registry lock is released before network/observer wait, observer artifact serialization uses a different lock, an existing incomplete attempt returns terminal unknown without send, and accepted-no-reply is rejected if the observer became incomplete during its bounded window.

- [ ] **Step 2: Implement self-identity-bound direct request**

```python
async def run_smoke(expectation: SmokeExpectation) -> SmokeResult:
    self_jid = extract_self_jid_from_held_creds(AUTH_DIR / "creds.json")
    require_bound_new_auth_identity(self_jid, expectation.current_auth_hmac, expectation.self_identity_hmac)
    cid = deterministic_client_message_id(expectation.incident_id, expectation.rotation_nonce)
    reservation = reserve_one_shot_before_network(expectation.incident_id, expectation.rotation_nonce, cid)
    if reservation.existing:
        return reservation.existing.terminal_or_unknown()
    return await run_reserved_once(expectation, self_jid, cid)

def reserve_one_shot_before_network(incident_id: str, nonce: str, cid: str) -> Reservation:
    with one_shot_registry_lock(incident_id, nonce, cid):
        if existing := load_existing_attempt(cid):
            return Reservation(existing=existing)
        fsync_protected_record("intent", cid)
        fsync_protected_record("attempt", cid)
        return Reservation(existing=None)

async def run_reserved_once(expectation: SmokeExpectation, self_jid: str, cid: str) -> SmokeResult:
    try:
        acceptance = await bridge_send_text_once(self_jid, expectation.smoke_text, cid)
    except AmbiguousSend:
        fsync_protected_record("external_effect_unknown", cid)
        return SmokeResult("external_effect_unknown")
    fsync_protected_record("bridge_acceptance", cid, acceptance)
    return await observer.wait_for_quote_or_timeout(acceptance.messageId)
```

`build_smoke_expectation(current_auth)` recomputes stable `canonical_auth_tree_v1`, requires its HMAC/serialization to match the current-auth receipt, then reads held no-follow `creds.json`, normalizes `me.id`, computes self-JID HMAC, and binds both values in encrypted expectation. Immediately before send, repeat the stable canonical auth-tree HMAC and `creds.json` read and require both bindings. It validates regular type/current owner/private mode, strips `:<device>` consistently with Bridge `normalizeJid`, and accepts only supported WhatsApp JID domains. It never writes/prints/stores clear JID outside protected receipts. Do not add any Bridge/Gateway command. `bridge_send_text_once` parses the actual authenticated protocol-v3 envelope: matching `type == "response"`/request id, `payload.ok is true`, then `payload.result.sent.to` and `payload.result.sent.messageId`; it accepts only `to == self_jid` and nonempty message id. It sends exactly one fixed-template `send_text`; the owner must quote-reply. The ready direct observer, not Gateway data, bounded-waits for an event whose `replyToMessageId == acceptance.messageId`; it may never send again. It writes `inbound_reply` from captured event provenance or `accepted_no_reply` only if observer capture stayed connected/complete throughout. Disconnect/drop/overflow yields terminal `capture_failed`, writes its protected receipt when possible, and blocks incident close. All receipts are protected records; clear JID/text is never public. If a post-send evidence write fails, existing attempt reservation still forces terminal unknown and never retry.

- [ ] **Step 3: Verify GREEN, mutate, and commit**

Run the focused tests and Ruff. Temporarily add a retry after `AmbiguousSend` and prove the test fails. Temporarily map accepted timeout to `external_effect_unknown` and prove the second test fails. Restore both and commit:

```bash
git add scripts/whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_smoke.py
git commit -m "feat(incident): add no-retry rotation smoke"
```

### Task 4: Report for integration and final task review

**Files:**
- Modify: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-03-report.md`

- [ ] **Step 1: Record synthetic-only evidence**

Record RED/GREEN/mutation counts and public commitments, with the explicit statement that no real Bridge, Gateway, archive, JID, message, or owner turn was touched. The orchestration plan runs integration first, then packages this report for the task review set.

## Execution Handoff

After Luna GO, the owner gate is recorded before quarantine, phone readiness is recorded after quarantine, inventory is attested after relink, and smoke makes exactly one direct self-chat attempt. `external_effect_unknown` needs owner disposition and is never replayed; `capture_failed` is incident-close NO-GO.
