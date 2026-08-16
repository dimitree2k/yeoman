# WhatsApp Rotation Pre-owner Smoke Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add separate protected owner-turn evidence, post-relink phone device-inventory attestation, and a direct one-attempt Bridge self-chat smoke controller that never overclaims delivery.

**Architecture:** Do not use Gateway IPC `send_message`: it reports queue acceptance as `delivered: true`, loses Bridge `{to,messageId}`, and may retry ambiguous timeouts. Gateway stays stopped. With the owner explicitly told not to scan, first start the pinned Bridge-only helper so it starts Bridge and creates owner-only QR; then connect/authenticate the direct protocol-v3 observer and prove ready; only then may the owner open/scan QR. The observer captures every message event through relink/smoke as encrypted raw-event artifacts plus normalized provenance receipts. The controller reads held no-follow Baileys `creds.json` for `me.id`/LID and makes at most one direct send.

**Tech Stack:** Python 3 standard library, WebSocket client already used by `scripts/whatsapp_qr_reconnect.py`, `scripts/incident_evidence_lib.py`, `pytest`, Ruff.

## Global Constraints

- Protected owner records and receipts live only at `/home/dm/.local/share/yeoman-program-evidence/whatsapp-session-incident-2026-08-16/rotation`. Program keys stay in program-key roots. Stdout exposes only phase and public HMAC/ciphertext/signature commitments.
- There is no linked-device API. The controller does not claim to enumerate devices. Post-relink inventory is a protected owner-phone attestation that says intended Yeoman is present, no unknown device appears, and retains observed count/labels only inside encryption.
- The primary orchestrator obtains each exact `HostUserTurn(thread_id, completed_turn_id, user_message_item_id, timestamp, exact_content)` through trusted `codex_app__read_thread`. CLI receives bounded JSON only over inherited private FD/stdin, never argv/environment, and HMACs/encrypts it. There is no opaque fallback: missing/reread-mismatched host tuple refuses. Before every destructive transition, both standing reviewers independently reread the exact reference and confirm statement/predecessor; this is explicit host-API trust evidence, not nonrepudiation. Owner attestations are exact versioned statements/templates, not free-form content accompanied by independent booleans. Record code derives asserted fields from `exact_content`; a protected uniqueness journal refuses reuse of one host item across kinds. Pre-action records are distinct `owner-all-devices-revoked-v1` then predecessor-linked `owner-quarantine-authorized-v1`; `phone-ready-v1` is a third record after quarantine; `owner-device-inventory-v1` is a fourth post-relink host turn whose canonical content is parsed into inventory fields and bound to the verified current-auth fingerprint.
- After current-auth fingerprint and device inventory, `build_smoke_expectation` verifies the current-auth/inventory/observer-ready graph, recomputes stable `canonical_auth_tree_v1`, requires its HMAC/serialization to match the protected current-auth receipt, then reads held `creds.json`, computes normalized self-JID HMAC, and writes encrypted `SmokeExpectation`. Immediately before send, repeat the same stable canonical auth-tree check plus the self-JID check. A changed/unstable tree fails before network I/O. No JID is exposed. Derive `clientMessageId = sha256("yeoman-whatsapp-incident-smoke-v1" || incident_id || rotation_nonce)[:32]`. `rotation_nonce` and one fixed harmless smoke-text template live only in that expectation; the owner must quote-reply to that one message. There is no random retry identifier.
- The state comparator consumes a sealed, typed protected-evidence graph rather than trusting IDs embedded in one self-contained chain. Fixed kinds are `whatsapp-rotation-observer-ready-v1`, `whatsapp-rotation-raw-bridge-event-v1`, `whatsapp-rotation-normalized-observer-event-v2`, `whatsapp-rotation-smoke-expectation-v2`, `whatsapp-rotation-smoke-intent-v2`, `whatsapp-rotation-smoke-attempt-v2`, `whatsapp-rotation-smoke-bridge-acceptance-v2`, `whatsapp-rotation-smoke-inbound-reply-v2`, `whatsapp-rotation-smoke-terminal-v2`, and `whatsapp-rotation-observer-close-v2`. Every link is an authenticated `EvidenceCommitment`, never a decoded dictionary or caller-supplied ID. `observer-close-v2` is the only successful state-comparator input and seals readiness, capture boundaries, exact event count, first/final normalized-event commitments, smoke acceptance, optional inbound-reply commitment, and the terminal state value `accepted_no_reply` or `inbound_reply_observed`.
- Each normalized event binds its contiguous sequence, ready commitment, predecessor normalized-event commitment, raw-artifact commitment, and protected normalized provenance: sender, recipient, channel/account, event timestamp, observed timestamp, message ID, reply-to message ID, sorted mentions, direction, new-vs-reply relation, content/media type, and content commitment. The raw protected artifact retains the complete Bridge envelope. Public output contains none of these values. `expected_inbound_reply` is derived only when the normalized event's `reply_to_message_id` equals the message ID in the recursively verified Bridge-acceptance receipt. Every other event is retained as `unsolicited_inbound`, including a legitimate reply to some other message; its reply provenance is never erased or rejected.
- The Bridge-acceptance receipt binds the exact expectation, intent, durable attempt, accepted destination, message ID, request ID, channel/account, and acceptance timestamp. The optional inbound-reply receipt binds that acceptance and the exact normalized-event commitment. Publication is an acyclic DAG: ready -> raw/event chain and expectation -> intent -> attempt -> acceptance -> optional inbound reply -> observer close. A successful close is published last, binds ready, acceptance, optional inbound reply, first/final event heads, exact count, full-window completeness, and the terminal state value; no completed-capture terminal record points back to it. `smoke-terminal-v2` is only for pre-close `external_effect_unknown` or `capture_failed`, is terminal NO-GO, and can never be represented by a complete close. `accepted_no_reply` is valid only with the matching acceptance plus a complete close covering the entire bounded window. A missing, malformed, disconnected, dropped, overflowed, or publication-failed chain is `capture_failed`/NO-GO, never an empty successful chain.
- A successful close has ordered ready/open/close timestamps. `event_count == 0` iff both first/final event commitments are null; that is valid only for a complete `accepted_no_reply` window. `event_count > 0` requires both heads, first sequence `1`, final sequence equal to `event_count`, and an exactly contiguous predecessor traversal. `inbound_reply_observed` requires a non-null inbound receipt referencing one event in that exact chain. Unknown/capture-failed outcomes have no successful close.
- Under a short dedicated private `.smoke-one-shot` registry lock, reserve a keyed nonce identifier globally for this incident before any protected intent or attempt publication. The append-only/fsynced reservation binds incident, expectation commitment, nonce identifier, deterministic client message ID, state, and prior journal receipt. Persist protected intent and attempt, then atomically/fsync advance the reservation to `attempt_durable` before network; release the lock before network/observer wait. Any existing nonce entry—including a crash after reservation or intent but before durable attempt—burns that nonce forever and never sends again: return its authenticated terminal receipt when present, otherwise terminal `external_effect_unknown`. Corrupt/partial journals fail closed. Post-network publication failure leaves the durable reservation, so later calls cannot send. Observer raw-artifact serialization uses an independent lock.

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
- `record_device_inventory_turn(source: HostUserTurn, owner_predecessor: EvidenceCommitment, current_auth: EvidenceCommitment) -> EvidenceCommitment`

- [ ] **Step 1: Write RED owner-evidence tests**

```python
def test_pre_action_gate_refuses_generic_or_combined_approval():
    assert load_owner_gate(protected_generic_approval()) is None
    assert load_owner_gate(protected_combined_checkbox()) is None
    assert load_pre_action_gate(record_revocation_then_authorization()) is not None
```

Add tests that phone readiness before quarantine, inventory before relink/fingerprint, altered signatures, wrong predecessor kind, or count/labels/JID disclosure fails. Use fake structured `codex_app__read_thread` tuples; reject missing/reread-mismatched/duplicate/combined/reused host sources. Prove a correctly signed turn with content that does not exactly assert the recorded action fails, inventory fields are derived only from canonical host content, and a swapped current-auth commitment fails. No test infers owner identity.

- [ ] **Step 2: Implement fixed schemas and ordering**

```python
def record_all_devices_revoked_turn(source: HostUserTurn, predecessor: EvidenceCommitment | None) -> EvidenceCommitment:
    require_exact_owner_statement(source, "YEOMAN_ROTATION_ALL_DEVICES_REVOKED_V1")
    return record_owner_turn("owner-all-devices-revoked-v1", {"all_devices_revoked": True}, source, predecessor)

def record_quarantine_authorized_turn(source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment:
    require_exact_owner_statement(source, "YEOMAN_ROTATION_QUARANTINE_AUTHORIZED_V1")
    require_owner_predecessor(predecessor, "owner-all-devices-revoked-v1", {"all_devices_revoked": True})
    return record_owner_turn("owner-quarantine-authorized-v1", {"quarantine_authorized": True}, source, predecessor)

def record_phone_ready_turn(source: HostUserTurn, predecessor: EvidenceCommitment) -> EvidenceCommitment:
    require_verified_quarantine_receipt()
    require_exact_owner_statement(source, "YEOMAN_ROTATION_PHONE_READY_V1")
    require_owner_predecessor(predecessor, "owner-quarantine-authorized-v1", {"quarantine_authorized": True})
    return record_owner_turn("phone-ready-v1", {"phone_ready": True}, source, predecessor)
```

The first three owner records require their exact fixed versioned acknowledgement text. `owner-device-inventory-v1` requires exact canonical JSON under `YEOMAN_ROTATION_DEVICE_INVENTORY_V1`; it is only recorded after relink/current-fingerprint verification and includes `intended_yeoman_present`, `no_unknown_devices`, `observed_count`, and `labels` parsed from that host content inside ciphertext. It binds the verified current-auth fingerprint and contains no self JID. Every record has a unique user-turn source commitment, actual user-turn timestamp, exact predecessor-kind/HMAC, and signature verification; actual user content remains encrypted. Maintain a protected source-commitment uniqueness journal so the same host item cannot authorize two kinds.

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
- Modify: `scripts/incident_evidence_lib.py`
- Modify: `scripts/whatsapp_rotation_smoke.py`
- Modify: `scripts/whatsapp_rotation_state.py`
- Modify: `tests/shared/test_incident_evidence_lib.py`
- Modify: `tests/shared/test_whatsapp_rotation_smoke.py`
- Modify: `tests/shared/test_whatsapp_rotation_state.py`

**Interfaces:**
- `BridgeEventObserver.start_before_qr() -> EvidenceCommitment` (`observer-ready-v1`)
- `BridgeEventObserver.capture_window() -> CaptureWindow` (in-memory controller handle, never comparator evidence)
- `BridgeEventObserver.close(acceptance, inbound_reply, terminal_state) -> EvidenceCommitment` (`observer-close-v2`)

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

Test readiness before QR, authenticated protocol-v3 event filtering, encrypted full raw-event artifact retention, normalized provenance receipt, disconnect, drop, overflow, and ordering. Every normalized event test covers sender, recipient, channel/account, event/observed timestamp, message ID, reply-to, sorted mentions, direction, new-vs-reply, content/media type, and content commitment. Add a minimal fixed-kind `verify_protected_artifact(kind, commitment)` to the common library: it revalidates the commitment, HMAC/framing, dual-recipient decryption equality, ciphertext signature, and artifact byte commitment without returning or parsing raw payload bytes. Test invalid/mismatched/non-artifact commitments, swapped kinds, unequal decryptions, and output leakage. Assert no raw event/content/JID reaches stdout.

- [ ] **Step 2: Implement the observer inside the smoke module**

```python
async def start_before_qr(self) -> None:
    await self.connect_authenticated_protocol_v3()
    await self.require_ready()
    self.capture_all_message_events = True
```

With the owner explicitly instructed not to scan, start the pinned Bridge-only helper first and wait for its owner-only QR marker; then connect/authenticate/prove observer-ready and publish `observer-ready-v1`; only then allow QR open/scan. Capture every protocol-v3 `message` event from observer-ready through smoke closure. For each event, first write its exact raw frame/envelope bytes as `raw-bridge-event-v1`, then write a contiguous predecessor-linked `normalized-observer-event-v2` that binds the raw commitment and full normalized provenance. Retain all expected/unsolicited events and schedule them for import into the target immutable event log before incident-tool/evidence disposition. Close successful capture with `observer-close-v2` published last under the acyclic rules above. If observer disconnects, drops an event, overflows, loses a publication, or cannot seal the chain, record terminal capture failure when possible and block completion; never claim complete capture. Gateway stays stopped and no Gateway DB is read.

Upgrade `compare_v2` to accept only the sealed successful `observer-close-v2` commitment. It recursively verifies fixed protected kinds and predecessor links from the final normalized-event head, requires ready-before-capture, contiguous sequence/count and exact first/final heads, authenticates every raw-artifact reference and normalized field set, and validates the exact expectation -> intent -> attempt -> Bridge acceptance -> optional inbound reply -> close DAG. Failure-only `smoke-terminal-v2` records are not successful comparator inputs. Fabricated self-consistent IDs, a swapped acceptance, missing provenance fields, a wrong raw commitment, broken head/count/order/readiness/completeness, plain dictionaries, and the retired self-contained observer-chain-v1 all return `mismatch`. Classification is derived after verification: only an event replying to the accepted smoke message is expected; every other event is unsolicited even when it legitimately replies to another message.

- [ ] **Step 3: Verify and commit**

Run focused tests/Ruff, temporarily discard one unsolicited event and prove RED coverage fails, restore, then commit:

```bash
git add scripts/incident_evidence_lib.py scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
git commit -m "feat(incident): capture rotation bridge events"
```

### Task 3: Implement direct one-attempt smoke and bounded receipt observation

**Files:**
- Modify: `scripts/whatsapp_rotation_smoke.py`
- Modify: `scripts/whatsapp_rotation_state.py`
- Modify: `tests/shared/test_whatsapp_rotation_smoke.py`
- Modify: `tests/shared/test_whatsapp_rotation_state.py`

**Interfaces:**
- `run_smoke(expectation: EvidenceCommitment, observer_ready: EvidenceCommitment) -> SmokeResult`
- `build_smoke_expectation(current_auth: EvidenceCommitment, inventory: EvidenceCommitment, observer_ready: EvidenceCommitment) -> EvidenceCommitment`
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

Add tests with synthetic `creds.json` for non-regular/wrong-owner/wrong-mode/missing `me.id`, device-suffix normalization, unsupported JID domain, identity-HMAC mismatch, malformed protocol-v3 envelope, mismatched returned destination, accepted causal quote-reply, and no string containing `delivered`. Add crash/manual-rerun cases after nonce reservation, after intent before attempt, after durable attempt before network, after network before response, after response before acceptance receipt, and every protected receipt publication failure; each proves a second send is forbidden. Reject the same nonce with a distinct expectation/receipt/client ID, concurrent same-nonce calls, partial/corrupt journals, and invalid prior journal receipts.
Also prove the short one-shot registry lock is released before network/observer wait, observer artifact serialization uses a different lock, any existing incomplete reservation/intent/attempt returns terminal unknown without send, and accepted-no-reply is rejected if the observer close is missing, mismatched to acceptance, or incomplete during its bounded window.
Test exact publication order and predecessor direction for accepted-no-reply and inbound-reply-observed. Prove a complete zero-event accepted-no-reply close requires both heads null; every nonzero count requires first/final heads, first sequence 1, final sequence equal to count, and contiguous traversal. Reject invalid timestamp order, inbound success without its bound event, any successful close for `external_effect_unknown`/`capture_failed`, and every predecessor that points to a record published later.

- [ ] **Step 2: Implement self-identity-bound direct request**

```python
async def run_smoke(expectation: EvidenceCommitment, observer_ready: EvidenceCommitment) -> SmokeResult:
    protected_expectation = verify_bound_expectation(expectation, observer_ready)
    self_jid = extract_self_jid_from_held_creds(AUTH_DIR / "creds.json")
    require_bound_new_auth_identity(self_jid, protected_expectation.current_auth_hmac, protected_expectation.self_identity_hmac)
    cid = deterministic_client_message_id(protected_expectation.incident_id, protected_expectation.rotation_nonce)
    reservation = reserve_one_shot_before_network(expectation, protected_expectation.rotation_nonce, cid)
    if reservation.existing:
        return reservation.existing.terminal_or_unknown()
    return await run_reserved_once(expectation, protected_expectation, reservation.attempt, observer_ready, self_jid, cid)

def reserve_one_shot_before_network(expectation: EvidenceCommitment, nonce: str, cid: str) -> Reservation:
    with one_shot_registry_lock():
        nonce_key = keyed_nonce_identifier(INCIDENT_ID, nonce)
        if existing := load_any_nonce_reservation(nonce_key):
            return Reservation(existing=existing)  # never continue or send
        fsync_nonce_reservation(nonce_key, expectation, cid, state="reserved")
        intent = fsync_protected_record("whatsapp-rotation-smoke-intent-v2", expectation, cid)
        attempt = fsync_protected_record("whatsapp-rotation-smoke-attempt-v2", intent, cid)
        fsync_nonce_reservation(nonce_key, expectation, cid, state="attempt_durable", attempt=attempt)
        return Reservation(existing=None, attempt=attempt)

async def run_reserved_once(expectation, protected_expectation, attempt, observer_ready, self_jid: str, cid: str) -> SmokeResult:
    try:
        acceptance = await bridge_send_text_once(self_jid, protected_expectation.smoke_text, cid)
    except AmbiguousSend:
        fsync_protected_terminal("external_effect_unknown", expectation, attempt, observer_ready, cid)
        return SmokeResult("external_effect_unknown")
    accepted = fsync_bound_bridge_acceptance(expectation, attempt, acceptance)
    return await observer.wait_for_quote_or_timeout(accepted)
```

`build_smoke_expectation(current_auth, inventory, observer_ready)` verifies the exact post-relink inventory/current-auth/observer-ready graph, recomputes stable `canonical_auth_tree_v1`, requires its HMAC/serialization to match the current-auth receipt, then reads held no-follow `creds.json`, normalizes `me.id`, computes self-JID HMAC, and binds all values in encrypted `smoke-expectation-v2`. Immediately before send, repeat the stable canonical auth-tree HMAC and `creds.json` read and require both bindings. It validates regular type/current owner/private mode, strips `:<device>` consistently with Bridge `normalizeJid`, and accepts only supported WhatsApp JID domains. It never writes/prints/stores clear JID outside protected receipts. Do not add any Bridge/Gateway command. `bridge_send_text_once` parses the actual authenticated protocol-v3 envelope: matching `type == "response"`/request id, `payload.ok is true`, then `payload.result.sent.to` and `payload.result.sent.messageId`; it accepts only `to == self_jid` and nonempty message id. It sends exactly one fixed-template `send_text`; the owner must quote-reply. The ready direct observer, not Gateway data, bounded-waits for a normalized event whose `replyToMessageId == acceptance.messageId`; it may never send again. It writes `inbound-reply-v2` bound to the exact acceptance and normalized-event commitment, or `accepted_no_reply` only if a matching sealed observer close proves connected/complete capture for the entire bounded window. Disconnect/drop/overflow/publication failure yields terminal `capture_failed`, writes its protected terminal receipt when possible, and blocks incident close. All receipts are protected records; clear JID/text is never public. If a post-send evidence write fails, the durable attempt reservation still forces terminal unknown and never retry.

- [ ] **Step 3: Verify GREEN, mutate, and commit**

Run the focused tests and Ruff. Temporarily add a retry after `AmbiguousSend` and prove the test fails. Temporarily map accepted timeout to `external_effect_unknown` and prove the second test fails. Restore both and commit:

```bash
git add scripts/whatsapp_rotation_smoke.py scripts/whatsapp_rotation_state.py tests/shared/test_whatsapp_rotation_smoke.py tests/shared/test_whatsapp_rotation_state.py
git commit -m "feat(incident): add no-retry rotation smoke"
```

### Task 4: Report for integration and final task review

**Files:**
- Modify: `.superpowers/sdd/2026-08-16-yeoman-whatsapp-session-incident-containment/task-4-prep-03-report.md`

- [ ] **Step 1: Record synthetic-only evidence**

Record RED/GREEN/mutation counts and public commitments, with the explicit statement that no real Bridge, Gateway, archive, JID, message, or owner turn was touched. The orchestration plan runs integration first, then packages this report for the task review set.

## Execution Handoff

After Luna GO, the owner gate is recorded before quarantine, phone readiness is recorded after quarantine, inventory is attested after relink, and smoke makes exactly one direct self-chat attempt. `external_effect_unknown` needs owner disposition and is never replayed; `capture_failed` is incident-close NO-GO.
