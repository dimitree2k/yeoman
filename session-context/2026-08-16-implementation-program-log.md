# Yeoman implementation program log

Status: Active implementation ledger for major-turn decisions and expert cross-review.

## Authority and operating rules

- Normative architecture: `docs/superpowers/specs/2026-08-16-yeoman-target-architecture-design.md`, SHA-256 `8201c5986e66292147e8682af506bf1978abe5338d56572b0395bf3d42affbfa`.
- Orchestration: `docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md`.
- Current milestone: `docs/superpowers/plans/2026-08-16-yeoman-rework-01-release-baseline.md`.
- Plan bundle reviewed GO: SHA-256 `e614e1b92e1fc7eb9aef33909edbca2e830554f2763347a8f137730614cc0067`.
- The live runtime at `/home/dm/.yeoman` remains authoritative and must not be mutated by build tasks.
- Implementation uses isolated worktrees, TDD, one implementer at a time, a task-scoped review after each task, and a broad milestone review before handoff.
- Important decisions, blockers, commit ranges, verification evidence commitments, and reviewer rulings are appended here. Conversation history is not authority.

## Standing major-turn reviewers

- Agent A, senior software engineer: task `01a009b3-e740-7972-992b-5d63d6066b8c`.
- Agent B, senior security architect: task `01a009b3-f495-7a90-8e50-8a22d4d306d2`.
- Every message to either standing reviewer must explicitly select `gpt-5.6-luna`.
- Superseded architecture-review tasks `01a00731-aa0d-7591-9382-2c52becddbae` and `01a00731-aa5c-7232-93c3-d165eabc5d58` are retired from implementation communication.

## What counts as a major turn

Request both standing reviews after each accepted milestone task or earlier when a task changes a cross-milestone contract, authority boundary, migration/recovery invariant, release signature, or cutover state. Each request names the exact commit range, task brief, implementation report, verification/review package, and this log. Both reviewers return commit-bound GO/NO-GO and any required adjustment before the next task.

## Implementation entries

### 2026-08-16 — Program start

- Owner established the implementation goal and required Luna for standing experts and implementation workers.
- Agent A and Agent B were created as same-directory tasks; all of their active turns will be sent with explicit Luna model override.
- Milestone 01 preflight begins. No target/runtime mutation has occurred in this implementation run yet.
- Tool constraint to track: the current in-process collaboration API advertises only `gpt-5.6-sol` and `gpt-5.6-terra` for spawned subagents. Before dispatching implementation work, the controller must either obtain a Luna-capable task mechanism or record the blocker rather than silently using a different model.

### 2026-08-16 — Milestone 01 preflight

- Controller read the complete orchestration plan, Milestone 01 plan, its required normative sections, and `AGENTS.md`; an SDD workspace, Task 1 brief, and recovery ledger now exist under the plan-scoped ignored workspace.
- Source fact: `/home/dm/Documents/yeoman` is a normal checkout on `c/turn-engine-v2` at `6f96c19a1fdf04ca777fe28d9ca831fbbd4482a8` with 50 staged source/test paths plus this untracked program log. Existing linked worktrees are unrelated and were left untouched.
- Preservation gate: every dirty path needs an explicit owner-approved disposition before the preservation commit/tag. No source path has been committed, excluded, reverted, or copied by this implementation run.
- Signature gate: repository Git config names no signing key, GnuPG reports zero local secret keys, and neither `age` nor `rage` is installed. The approved plan requires a pinned offline Ed25519 public-key fingerprint, an authorized signing operation, and two separately stored age recovery identities before the signed tag and encrypted bootstrap evidence.
- Worker-model gate: standing Agent A and Agent B acknowledged their read-only Luna review contracts. The collaboration subagent API still offers only Sol and Terra, so no implementation subagent has been dispatched under a substituted model.
- Runtime safety: `/home/dm/.yeoman`, its services, and all live state remain untouched.

### 2026-08-16 — Preservation-candidate audit while owner gates remain open

- Candidate index: 50 staged paths, 3,507 additions and 186 deletions before the privacy correction; current binary cached-diff SHA-256 `7d94039ee62bd91d6735cbfe4b293db41a6c7be4c6fdedc060273c21f5ddd56c` and cached raw-index SHA-256 `8e88c630cc63a3f1e053c9df9f53a167cf84d38b8ae35611723201989f8817c3`.
- Privacy correction: two newly added real channel/participant fixture identifiers in `tests/gateway/test_implicit_bot_address.py` were replaced with obvious synthetic identifiers. No behavior changed; the file's 25 tests pass. Existing historical identity fixtures remain a source-history privacy gap and must never enter the target release artifact or public handoff.
- Verification passed: `git diff --cached --check`; `uv run ruff check .`; 210 tests covering every modified Python test file; 868 tests under the supported `tests/gateway`, `tests/shared`, and `tests/overseer` collections; and all 23 Bridge tests including its TypeScript build.
- Repository-wide `uv run pytest -q` is not a valid green baseline: collection stops on 19 root-level stale tests importing the absent historical `yeoman` package. This is recorded as preservation debt; the target release plan already requires removal of old tests and a clean full collection rather than treating this failure as success.
- The AGENTS-prescribed mypy slice reports 13 errors in the existing large policy/responder adapters. One staged removal of a required boundary ignore was corrected; the remaining current errors are not represented as green and will not be used to justify baseline acceptance.
- Credential-like added literals are confined to explicit test fixtures such as redaction tests; no private-key marker was found. This heuristic scan is evidence, not a substitute for the signed exhaustive source inventory.
- No preservation commit/tag or implementation worktree has been created. Owner disposition, signing/recovery material, and Luna worker availability remain open gates.

### 2026-08-16 — Goal blocked at owner-controlled bootstrap gates

- The same three gates remain unresolved for three consecutive goal turns: implementation-subagent model authority, disposition of the audited 50-path staged candidate, and authorization/destinations for offline program-signing and age recovery identities.
- Current collaboration workers cannot satisfy the explicit Luna requirement: the subagent interface exposes only Sol and Terra. Agent A and Agent B remain idle Luna reviewers and have not been used outside their standing contract.
- Current source authority is program-log commit `5ebfa0119d9bbd0edc7810b25cdd93e18d974afb`; the staged candidate digest remains `7d94039ee62bd91d6735cbfe4b293db41a6c7be4c6fdedc060273c21f5ddd56c` across 50 paths. No preservation tag or target/toolkit worktree exists.
- `age`/`rage` remain unavailable and GnuPG has zero local secret keys. No key material was generated and no recovery location was guessed.
- Runtime and memory remain untouched. Resume Task 1 only after the owner answers the three numbered decisions recorded in the prior handoff.

### 2026-08-16 — Bootstrap gates resolved; Task 1 resumed

- Owner authorized Terra for implementer/task-review subagents while Agent A and Agent B remain Luna, approved the audited 50-path preservation candidate, and approved local signing/recovery-key setup.
- `age` 1.2.1 was installed from the Debian package repository. No key material has entered Git, either worktree, or runtime state.
- The host exposes one physical disk only. Two independent age recovery identities will be created in separate permission-restricted locations outside Git/worktrees/runtime, but they share the disk failure domain. This is an explicit local-only bootstrap limitation; one identity must later move to separate local media and be exercised before claiming separate-media recovery.
- Task 1 resumes from controller base `5fb178ba5b37cb3ee2c6df050545134e65c99bda`; the candidate diff commitment is unchanged.

### 2026-08-16 — Milestone 01 Task 1 accepted after fix round 1

- Preservation commit `bdc4c8c9e33602c5c8abd69f054703469d212dab` contains exactly the owner-approved 50-path candidate. Source branch commit subject is `chore(release): preserve pre-rework source baseline`.
- The accepted annotated SSH-signed tag `yeoman-preservation-2026-08-16` is tag object `eb7dede1fbbb52db3ee6ff098f069a35d95067eb` and peels to the preservation commit. Its signature verifies against the pinned Ed25519 program key.
- Initial review rejected a deterministic reconstruction as proof of the old running Bridge. Fix round 1 performed an owner-approved coordinated preservation fence: Overseer and Bridge stopped in order, exact preservation artifacts populated the managed cache through the supported runtime manager, a new Bridge started under systemd, authenticated health proved protocol v3/running/connected, Overseer returned, and Gateway remained continuously active.
- Exact deployed `dist/index.js` SHA-256 is `4b2f95fce746e5728f1090aba26567d89df2a091782f78ac0564fac066da6736`; exact managed-runtime fingerprint is `8d4df04de24190b0b9f512a93d1971731a6176c32b089df118499581f711884d`.
- The protected deployment receipt remains outside Git/worktrees/runtime. Public commitments are plaintext `dea931ef742dead91fba023dc1489489320c7db2be30e594f7ee7615496132e4`, dual-recipient ciphertext `e961c56654b75d293cdff471581128b9f418616d396028b44adacd2753133eb7`, and detached signature `d13be4a2f3a8b2a50b168d4e41a879da04801a0728175224623c823ad9f23896`. Both age identities decrypt to the same commitment and the signature verifies; plaintext is absent.
- `/home/dm/Documents/yeoman-rework` on `c/yeoman-architecture-rework` and `/home/dm/Documents/yeoman-migration-toolkit` on `c/yeoman-migration-toolkit` are clean at the preservation commit.
- Scoped task re-review verdict: all three findings addressed; Task 1 accepted. No Task 2 work has started.

### 2026-08-16 — Security incident requiring standing-review disposition

- During Task 1 diagnostics, the bundled `/home/dm/.codex/skills/yeoman-runtime/scripts/recent_logs.sh bridge` helper unexpectedly emitted historical Baileys session-key material into the implementer tool transcript. The helper had been treated as redaction-safe by its skill contract, but current evidence disproves that guarantee for Bridge logs.
- The implementer stopped broad-log use immediately. No exposed value was copied into Git, task reports, receipts, session notes, or other retained files. This note intentionally contains no secret, identifier, or raw log excerpt.
- WhatsApp remains connected, but no credential/session rotation or QR relink was performed because that is a separate destructive authorization boundary. Task 2 is held until Agent B classifies the exposure and recommends exact containment; Agent A must also assess the diagnostic/process correction. The unsafe helper must not be used again in this program unless independently repaired and validated.

### 2026-08-16 — Major-turn review after Milestone 01 Task 1

- Agent A reviewed the durable Task 1 package with explicit `gpt-5.6-luna` and returned GO for Task 1, bound to preservation commit `bdc4c8c9e33602c5c8abd69f054703469d212dab` and signed tag object `eb7dede1fbbb52db3ee6ff098f069a35d95067eb`. Agent A requires Agent B's incident disposition before Task 2, permanent removal of the unsafe helper from the approved workflow, a transcript-safe diagnostic procedure with regression coverage, a diagnostic-safety gate in later reviews, explicit separation of controller-documentation commit `b2a5bf09186c5d705cbe1ebc13d4cc3455d518cb` from the immutable preservation baseline, and retirement of unrelated worktrees before final release.
- Agent B reviewed the same package with explicit `gpt-5.6-luna` and returned GO for Task 1 but NO-GO for Task 2. The exposure is HIGH / SEV-2 and must be treated as compromised WhatsApp cryptographic session state; severity escalates if current authentication or identity secrets are proven exposed.
- Mandatory containment is now the Task 2 entry gate: stop Bridge and Overseer while preserving Gateway and all memory/archive state; owner revokes linked WhatsApp sessions and performs a fresh QR relink; quarantine only old WhatsApp authentication/session state; restrict and seek deletion of the sensitive transcript where supported; perform a reviewed offline secret-scope scan without revealing raw values; quarantine/repair the unsafe helper with a redaction regression test; verify no unknown linked device and a changed session identity; revalidate health, send/receive, provenance, and memory integrity; retain same-disk-only recovery labeling until one age identity is moved to separate physical media.
- Immediate reversible containment was applied after the reviews: `yeoman-overseer.service` and `yeoman-bridge.service` are inactive/dead; `yeoman-gateway.service` remains active/running. No memory, raw-message, archive, receipt, authentication, or linked-device data was deleted or modified by this step.
- Owner-interactive authorization is required before revoking linked devices, quarantining the active authentication state, or performing the QR relink. No Task 2 implementation may begin until the incident-close evidence listed by Agent B is recorded.

### 2026-08-16 — Incident containment plan opened

- Re-verified current authority before continuing: controller branch at `090faa7c2b018ca500a987fe763167e82d48cdc4`; clean target and toolkit worktrees remain at preservation commit `bdc4c8c9e33602c5c8abd69f054703469d212dab`; Bridge and Overseer remain inactive while Gateway remains active.
- Source-only root-cause analysis found that `recent_logs.sh` streamed raw tail content through a short denylist. Nested Baileys cryptographic fields were outside that denylist, so the helper's stated redaction guarantee was structurally unsound. No live log or exposed value was reopened to establish this cause.
- Containment execution authority is `docs/superpowers/plans/2026-08-16-yeoman-whatsapp-session-incident-containment.md`. Task 1 removes all log-body emission and proves the metadata-only contract with controlled synthetic fixtures. Task 2 builds a metadata-only exposure-scope scanner. Task 3 remains owner-interactive for linked-session revocation and QR relink.
- The incident plan is controller documentation, not part of the immutable preservation baseline or target release. It must be reconciled into final operating evidence and removed with superseded implementation plans before the legacy-free release handoff.

### 2026-08-16 — Incident containment Task 1 accepted

- A fresh Terra implementer followed controlled-fixture RED-GREEN TDD and changed the installed `yeoman-runtime` diagnostic helper from denylist-redacted tails to a metadata-only boundary. `recent_logs.sh` candidate SHA-256 is `83a071a3d8972a26deda691a20755cb123ed27b5125db7a52c33686f45295044`; updated `SKILL.md` SHA-256 is `8e295cf7f2d2afb4fa36f0cc9c15c6cafe9928cb3aef202463f542bc45db46ad`.
- Initial task review found the production behavior compliant but rejected a regression test that banned only the complete synthetic token and line. Fix round 1 replaced that denylist assertion with a fully anchored stdout allowlist and mutation proof. Accepted test SHA-256 is `a98099196478223a1f2d16c7487f8a2890dfa5258637afa5190849c2eca5c261`.
- Fresh controller verification: one real subprocess regression test passed; skill validation passed; Bash syntax passed; no `tail` or redaction function remains; a temporary helper emitting one additional partial-body-fragment line was rejected. Scoped Terra re-review returned ADDRESSED, no new breakage, ACCEPT.
- No live log, journal, transcript, authentication state, message, memory store, or service was read or mutated during implementation/review. Bridge and Overseer remain inactive; Gateway remains active. Incident Task 2 may begin, but release-baseline Task 2 remains NO-GO.
