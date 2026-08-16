# Task 4 pre-owner quarantine and identity report

Scope: `/home/dm/Documents/yeoman-migration-toolkit` only. All executions used disposable pytest directories and synthetic crypto fixtures. No live runtime, service, key, QR, network, message, or auth operation was invoked.

## TDD evidence

- RED Task 1: `uv run pytest -q tests/shared/test_whatsapp_auth_quarantine.py` produced 19 failures because `scripts/whatsapp_auth_quarantine.py` was absent.
- RED Task 2: `uv run pytest -q tests/shared/test_whatsapp_auth_quarantine.py -k current_fingerprint` produced 6 failures because `fingerprint_current` was absent.
- Mutation: omitting the quiescence/Overseer stopped-state fault proof made the quiescence case fail (1 failed, 18 deselected); restored.
- Mutation: returning success after old-tree deletion failure made the deletion case fail (1 failed, 18 deselected); restored.
- Mutation: omitting current-vs-old inequality made the equality case fail (1 failed, 19 deselected); restored.

## Final verification

- `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: 38 passed.
- `uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: all checks passed.
- `git diff --check`: no output.

## Fix round 3: evidence-gate alignment and state handoff

- Shared quiescence handoff contract: `yeoman-quiescence-receipt-v1`, with exactly two ordered checks, a bounded `intercheck_elapsed_ms`, fixed units `yeoman-bridge.service`, `yeoman-gateway.service`, and `yeoman-overseer.service`, each limited to `inactive|failed` with `main_pid: 0`; fixed `gateway_socket_absent`, `bridge_gateway_socket_absent`, and `bridge_port_3001_absent` booleans; and `overseer_respawn_observed: false`. `quiescence_receipt_fields` and `validate_quiescence_receipt` are exported by the common evidence library for the future `quiesce_and_record` controller.
- Quiescence is intentionally independent of owner turns and authorization predecessors. Owner revocation, owner authorization, and phone-ready instead use the common authenticated owner-record reader, which cross-checks the protected artifact with its `.owner-sources` request/record HMAC, artifact name, byte count, source, fields, kind, and predecessor.
- The controller now rejects generic protected-record fabrication for owner approvals and phone-ready; phone-ready requires exact `{phone_ready: true}` and the verified quarantine receipt as predecessor.
- A crash after protected PREPARED artifact publication but before its active index remains recoverable from captured state. Recovery finds only the nonce-derived sibling, requires its exact private empty form, reuses the deterministic PREPARED artifact, then publishes the active PREPARED index before the one intended exchange.
- Test-only wrappers were removed. Public calls construct a separate fixed-root production runtime, while the disposable core retains synthetic dependency injection. Test runtime construction rejects normalized production-path aliases. Active and replacement directories require exact mode `0700`.

### Fix-round-3 verification

- RED: common owner-journal verification and the shared quiescence contract initially failed (2 failures before implementation).
- Added synthetic tests for generic owner-record fabrication, exact quiescence shape drift, prepared-artifact/index fresh-core recovery without a second sibling, mode `0500`/`000`/`0750`, normalized `../` production aliases, and substituted transaction stats.
- `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: 70 passed.
- `uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: all checks passed; `git diff --check` produced no output.

## Fix round 4: quiescence and cleanup provenance

- The shared state handoff now has crash-safe `record_quiescence_receipt` internals and verifier semantics: a dedicated descriptor-rooted `.quiescence-receipts` pending/final journal binds the exact v1 artifact, count, and commitment. Generic protected records cannot satisfy quarantine gates.
- The exact quiescence schema retains two ordered samples and requires a positive 1000..5000 ms interval, fixed endpoint absence booleans, fixed systemd units, `main_pid: 0`, and no Overseer respawn. Revocation is chained to the verified quiescence receipt and authorization to revocation.
- Owner verification now checks public ciphertext/signature proofs before bounded per-chunk decrypt accumulation. Cleanup writes durable `cleanup_verified` only after old identity, stable security stat, and canonical HMAC match; exchanged recovery re-proves before cleanup and verified partial cleanup can resume only on the held old inode/name.
- `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: 76 passed. Ruff and `git diff --check` passed.

## Fix round 5: adapter contract start

- Added the reusable injected live-quiescence adapter contract with fixed bridge-only stop command, ordered exact systemd samples, no output surface, endpoint predicates, and the existing bounded quiescence schema. Its unit test uses only synthetic runner, endpoint, and sleeper callables.
- Production quarantine now calls the fixed adapter before entering the transaction and writes an `auth-quarantine-live-quiescence-v1` protected receipt, binding its commitment into the transaction and PREPARED evidence. This path was not executed during verification.
- Quiescence verification now reuses the bounded protected-record reader; pending receipt publication fsyncs its journal and holds a per-record nonblocking lock through pending, artifact, and final publication. Synthetic concurrent-writer coverage passes. The full common/quarantine run completed with 78 passing tests and no warnings, with Ruff and diff checks clean.

### Fix-round-5 completion

- Commits: `abca1a1`, `bf162b9`, `1247bb0`, `890505d`, `0ce7ad3`, `5ce4b7b`, `3d52870`, `7fcd815`, and `8b316b1`.
- The fixed public-only live-quiescence adapter/receipt path was never invoked during verification. All tests used disposable evidence roots and fake crypto; no live service, socket, port, auth, key, or network access occurred.
- Shared reader mutation: temporarily removed the detached-signature verification before decrypt. `test_owner_verification_rejects_bad_signature_before_decrypt` failed because verification no longer raised; the check was restored.
- Final verification: `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py` — 88 passed with no warnings; Ruff passed; `git diff --check` had no output; toolkit worktree clean.

## Fix round 6

- Commits: `9e50336`, `ab38948`, `818963d`, `313ec3b`, `a504ae0`, and `39ee258` plus the final cap regression commit.
- Existing artifact verification now checks detached signatures before decrypt and aborts at the expected byte count. The live adapter uses synthetic-injectable timing and fail-closed endpoint semantics; public live probes carry random nonce data and are journaled before auth open with bounded history.
- Fresh public recovery regression proves a second disposable probe preserves the immutable PREPARED live receipt while appending history; capped history refuses before auth open. No production adapter/live path was invoked.
- Final verification: 94 focused common/quarantine tests passed with no warnings; Ruff and `git diff --check` passed.
- Final live-probe crash coverage: `211ea1a` injects after live receipt artifact publication but before history state. A fresh disposable public-wrapper invocation publishes a distinct replacement receipt, reaches quarantine, and retains the prior artifact. Final suite: 95 passed with no warnings; no live calls.
- Mutation: temporarily removed `runtime.live_quiescence_history.append(runtime.live_quiescence)` and ran `uv run pytest -q tests/shared/test_whatsapp_auth_quarantine.py -k public_recovery_appends`; it failed with history length `0 != 2`. The exact append was restored, then the full focused suite passed: 95 tests, no warnings; Ruff and `git diff --check` clean.

## Fix round 7

- Commits: `cad4a47`, `47655a6`, `680a3ef`, `0f0b547`, `b1586c1`, `7cfb71f`, `a48722d`, `b6e47d0`, `c5fc5cf`, and `ac882fd`.
- Fixed adapter timing, gate-before-probe ordering, fixed systemctl path, bounded child cleanup, and fake-process refusal coverage were all exercised without a real service command.
- Final verification: 105 focused tests passed with no warnings; Ruff and `git diff --check` passed. No live calls occurred.
- Mutation proof: skipping public pre-adapter gate verification made `public_invalid_gates` fail with adapter calls `1 != 0`; restored. The deterministic timer mutation yields 199ms instead of 1200ms and fails; restored.
- Lifecycle mutation: removing `_reap_process(process)` from the bounded-reader failure path made `bounded_show_reader_reaps_on_overflow` fail with `terminated == 0`; restored. Final verification: 106 focused tests passed with no warnings; Ruff and `git diff --check` clean; toolkit worktree clean. No live calls occurred.

## Fix round 8

- `9476b50 fix(security): guarantee live probe child reap` changes kill fallback to an unconditional final `wait()` after kill, refusing only on an actual OS reap error instead of silently returning. Focused suite: 106 passed; Ruff and diff checks clean; no live calls.
- `68839a3 test(security): prove final child reap` asserts the fake lifecycle `[0.2, None]`: timed wait, kill, final unbounded wait, and reaped state. Mutation replacing final wait with a silent return made the test fail because only `[0.2]` was recorded; restored. Final suite: 106 passed, no warnings; Ruff/diff clean; no live calls.

## Fix round 2: exact exchange-window recovery and receipt binding

- PREPARED is now durable before `renameat2`: the protected journal binds the PREPARED commitment, old artifact and HMAC, old identity plus stable stat facts, and the exact replacement name, identity, and stat facts. (Directory ctime is deliberately excluded from transaction stat facts because `renameat2` itself changes it.)
- Fresh PREPARED recovery opens only the fixed active and recorded replacement names. It authenticates the PREPARED record and compares its payload to the journal. If the names show that exchange already happened, it fsyncs the parent, records EXCHANGED, and performs cleanup only; it never calls `renameat2` again.
- Existing malformed transaction state now refuses instead of starting a new transaction: missing active index within an existing journal, malformed JSON, symlink, oversized content, wrong mode, and wrong index schema/version are all fail-closed.
- Gate records require separate, complete `HostUserTurn` sources; exact true owner fields; exact predecessors; and a versioned two-sample Bridge/Gateway/Overseer quiescence shape with no Overseer respawn.
- DONE journals now bind the real `auth-quarantine-receipt-v1` commitment. Fingerprinting reloads and authenticates that exact receipt and requires an exact payload match; a substituted receipt is refused.

### Fix-round-2 verification

- RED additions initially failed: injected post-`renameat2`/pre-EXCHANGED crash recovery, an existing journal without its active index, false owner approval records, and a substituted DONE receipt (5 failures before implementation).
- Mutation: disabling the already-exchanged PREPARED branch made the crash-window test fail (1 failed, 41 deselected); restored.
- Mutation: replacing the protected receipt read with the copied journal payload made the substituted-receipt test fail (1 failed, 41 deselected); restored.
- `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: 61 passed.
- `uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: all checks passed.
- `git diff --check`: no output.

## Public commitments

- `4b9ce7a feat(incident): add non-restorable auth quarantine`
- `b637bfb feat(incident): verify rotated auth identity`

The controller produces only fixed result phases and public `EvidenceCommitment` values; this report contains no auth material or auth-derived HMAC values.

## Fix round 1: durable production boundary

- Added `5bd93ab fix(security): make auth quarantine crash durable`.
- Added the minimal common-library `_read_protected_record` compatibility primitive. The controller needs a bounded, dual-recipient decrypt-and-verify read of an exact protected record to authenticate owner gates and resume a protected journal after a new process; a commitment alone does not reveal its schema/payload.
- The fixed public entrypoints construct literal production auth/evidence roots and do not consume any mutable test runtime. Tests use explicit `_TestCore` and `*_for_test` dependency injection only.
- The protected journal has an opaque descriptor-rooted index and protected state records. It persists nonce, planned deterministic artifact binding before streaming, captured/prepared/exchanged/done states, identities, and the protected quarantine receipt binding. Fresh synthetic cores recover from each tested crash window without RAM state.
- Deletion now uses no-follow held descriptors plus identity/stat rechecks immediately before unlink/rmdir; component-by-component auth opening rejects symlinked parent components.
- Evidence-publication failure returns a non-success result with no receipt commitment rather than a counterfeit value; the durable exchanged journal permits fresh cleanup-only recovery.

### Fix-round verification

- RED additions: protected-record reader, mutable-public-path bypass, fresh-core recovery, component symlink, race, nested deletion, and receipt-publication failure cases initially failed before their implementations.
- Mutation: omitting stopped/respawn proof made the quiescence case fail (1 failed, 30 deselected); restored.
- Mutation: omitting the real post-unlink deletion failure made the deletion case fail (1 failed, 30 deselected); restored.
- Mutation: omitting current-vs-old inequality made the equality case fail (1 failed, 30 deselected); restored.
- `uv run pytest -q tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: 51 passed.
- `uv run ruff check scripts/incident_evidence_lib.py scripts/whatsapp_auth_quarantine.py tests/shared/test_incident_evidence_lib.py tests/shared/test_whatsapp_auth_quarantine.py`: all checks passed.
- `git diff --check`: no output.

## Independent acceptance

- Final toolkit head: `68839a3 test(security): prove final child reap`, with implementation fix `9476b50 fix(security): guarantee live probe child reap` immediately below it.
- A fresh Terra security reviewer re-read the complete common-evidence and quarantine/identity implementation and tests after round 8 and returned **ACCEPT**, with no remaining Critical or Important issue.
- Independent verification passed 106 focused tests without warnings, scoped Ruff over the four reviewed files, and `git diff --check`; the toolkit worktree was clean. A broader repository Ruff invocation still reports unrelated pre-existing I001/F401 findings in `tests/shared/test_tracing.py`; those are outside this module and are not represented as green.
- Controller verification independently reproduced 106 focused passes, scoped Ruff success, clean diff, and clean toolkit status. The explicit reap test requires wait timeouts `[0.2, None]` and `reaped=True`; removing the final wait fails that test.
- No production adapter, service, socket, port, authentication tree, key, runtime state, operator-evidence root, QR, message, memory, archive, or receipt was invoked or mutated. Owner interaction and live rotation remain NO-GO.
