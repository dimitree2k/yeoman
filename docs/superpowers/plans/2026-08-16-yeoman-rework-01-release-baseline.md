# Yeoman Rework Milestone 01: Release Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a signed preservation baseline, a separately buildable migration toolkit, and a clean installable target source skeleton that cannot execute legacy behavior.

**Architecture:** Freeze the exact old source/state inputs before deletion, then build the target in an isolated worktree using canonical package names. Release construction is allowlist-based: the public build manifest declares every shipped file and type, while private activation is a separately encrypted generation.

**Tech Stack:** Git worktrees, Python 3.14, uv/hatchling, Pydantic 2, SQLite read-only inspection, JSON Schema, pytest, Ruff, mypy, SHA-256, an offline Ed25519 program-signing key, age-encrypted bootstrap evidence with two separately stored local recovery identities, and the local Secret/Key Authority for target receipts from Milestone 02 onward.

## Global Constraints

- Read the orchestration file and normative §§1-5, 14-16, 18.1-18.4, and 19-21 before starting.
- Work only in `~/Documents/yeoman-rework`, except the explicitly separate `~/Documents/yeoman-migration-toolkit` worktree and read-only preservation inspection of `~/.yeoman`.
- Do not mutate, deploy, restart, or stop the live runtime in this milestone except an owner-approved preservation fence or coordinated backup operation.
- No private state, source paths containing personal identity, chat/account IDs, policy values, credentials, or activation values enter Git.
- The target runtime artifact contains no old package implementation, migration reader, compatibility import, old test, or old documentation.
- The target skeleton starts globally fenced and has no channel, provider, tool, workload, delivery, or model capability.
- All commits use Conventional Commits.
- Before any snapshot or tag, pin the offline Ed25519 signing public-key fingerprint and age recipient set in the owner-authorized bootstrap action; signing/decryption identities remain outside Git, both worktrees, runtime state, and the only preservation ciphertext location.

---

## File map

### Temporary migration-toolkit worktree

- `pyproject.toml` — standalone locked toolkit build with no target runtime dependency.
- `src/yeoman_migration_toolkit/cli.py` — read-only `inventory`, `verify`, and later `migrate` command surface.
- `src/yeoman_migration_toolkit/schema.py` — typed source-object, store, cutoff, and disposition records.
- `src/yeoman_migration_toolkit/inventory.py` — filesystem/SQLite inventory without content disclosure.
- `src/yeoman_migration_toolkit/manifest.py` — canonical JSON serialization, digesting, and detached signature envelope.
- `tests/test_inventory.py` and `tests/test_manifest.py` — generic source coverage and tamper tests.

### Target worktree

- `pyproject.toml` and `uv.lock` — minimal target workspace and locked inputs.
- `packages/shared/pyproject.toml` — dependency-light contract package.
- `packages/shared/yeoman_shared/contracts/identifiers.py` — opaque identifier primitives.
- `packages/shared/yeoman_shared/contracts/generations.py` — ordered generation/digest primitives.
- `packages/shared/yeoman_shared/contracts/manifests.py` — public build and encrypted-activation envelope schemas.
- `packages/gateway/pyproject.toml` — canonical Gateway package with only the fenced bootstrap.
- `packages/gateway/yeoman_gateway/app/main.py` — process entrypoint that exposes local liveness and refuses processing.
- `packages/gateway/yeoman_gateway/app/preflight.py` — strict release/build-generation preflight.
- `release/build-manifest.toml` — exhaustive public artifact allowlist.
- `release/forbidden-residuals.toml` — exact namespace, entrypoint, dependency, unit, config-key, and document bans.
- `release/activation-manifest.schema.json` — non-secret schema for encrypted activation payloads.
- `scripts/release/build_manifest.py` — deterministic manifest generator.
- `scripts/release/verify_artifact.py` — clean-wheel allowlist and residual verifier.
- `scripts/program/write_handoff.py` — protected receipt commitment plus safe handoff/reference writer.
- `artifacts/program/contracts/accepted-contracts.json` — release-safe cumulative interface/schema/ownership/digest registry.
- `tests/architecture/test_import_ownership.py` — AST/import boundary checks.
- `tests/architecture/test_release_manifest.py` — exhaustive artifact allowlist checks.
- `tests/architecture/test_forbidden_residuals.py` — repository and built-artifact stale scans.
- `tests/app/test_fenced_bootstrap.py` — proves the skeleton cannot process or emit effects.

## Task 1: Freeze the source authority and create isolated worktrees

**Files:**

- Create: signed source tag and two `c/` branches through Git metadata only.
- Create: `~/Documents/yeoman-rework`
- Create: `~/Documents/yeoman-migration-toolkit`
- Verify: `/home/dm/Documents/yeoman/AGENTS.md`

**Interfaces:**

- Consumes: current live build identity, current Git HEAD, and every dirty source path.
- Produces: one owner-accepted preservation commit/tag from which both worktrees can be reproduced.

- [ ] **Step 1: Prove worktree and dirty-state facts**

Run:

```bash
cd /home/dm/Documents/yeoman
git status --short
git branch --show-current
git rev-parse HEAD
git worktree list --porcelain
```

Expected: every dirty path is visible. Record a disposition for each path; no uncommitted path may be omitted implicitly.

- [ ] **Step 2: Create the signed preservation point**

Commit only owner-accepted source changes, verify a clean baseline, then create an annotated tag whose message names the deployed build digest:

```bash
git status --short
git tag -s yeoman-preservation-2026-08-16 -m "Yeoman pre-rework preservation baseline"
git tag -v yeoman-preservation-2026-08-16
git show --stat yeoman-preservation-2026-08-16
```

Expected: `git status --short` is empty at the selected baseline, signature verification succeeds with the pinned offline program-signing public key, and the tag resolves to the exact accepted commit. If the owner excludes a dirty path, preserve its path and digest in protected migration evidence before reverting or leaving it outside the tag.

- [ ] **Step 3: Create the target and toolkit worktrees using the worktree skill**

Run the commands selected by `superpowers:using-git-worktrees`, with these final outcomes:

```bash
git worktree list --porcelain
git -C /home/dm/Documents/yeoman-rework branch --show-current
git -C /home/dm/Documents/yeoman-migration-toolkit branch --show-current
```

Expected: distinct clean worktrees on `c/yeoman-architecture-rework` and `c/yeoman-migration-toolkit`, both rooted at the preservation tag.

- [ ] **Step 4: Commit the worktree decision record**

Create no source document for machine-specific paths. Commit only branch metadata through the later task changes; preserve the selected tag/commit in the protected program receipt.

## Task 2: Build the standalone read-only inventory toolkit

**Files:**

- Create: toolkit files listed in the file map.
- Test: `tests/test_inventory.py`
- Test: `tests/test_manifest.py`

**Interfaces:**

- Consumes: a source root opened without write permissions.
- Produces: `SourceManifestV1` canonical JSON plus a detached signature envelope; it never produces target state.

- [ ] **Step 1: Write failing inventory tests**

Create tests that build a temporary SQLite database and file tree, then assert:

```python
manifest = inventory_root(source_root, external_roots=())
assert manifest.schema == "yeoman.source-manifest.v1"
assert manifest.stores[0].sqlite.user_version == 7
assert manifest.stores[0].sqlite.tables[0].create_sql_sha256 == expected_schema_digest
assert manifest.objects_by_class["unclassified"] == 2
assert manifest.write_attempts == 0
```

Add tamper tests asserting that changing one count, digest, cutoff, or classification invalidates `verify_manifest()`.

- [ ] **Step 2: Run tests and confirm the missing-module failure**

```bash
cd /home/dm/Documents/yeoman-migration-toolkit
uv run pytest tests/test_inventory.py tests/test_manifest.py -q
```

Expected: FAIL because `yeoman_migration_toolkit` and its interfaces do not exist.

- [ ] **Step 3: Implement exact inventory contracts**

Define immutable `SourceObject` records and these public call signatures:

```python
@dataclass(frozen=True, slots=True)
class SourceObject:
    source_id: str
    relative_path: str
    object_kind: Literal["sqlite", "file", "directory", "symlink", "external_reference"]
    byte_size: int
    sha256: str
    classification: Literal[
        "canonical", "noncanonical", "credential_auth", "projection", "unclassified"
    ]
```

- `inventory_root(source_root: Path, *, external_roots: Sequence[Path]) -> SourceManifestV1`
- `canonical_manifest_bytes(manifest: SourceManifestV1) -> bytes`
- `verify_manifest(data: bytes, signature: bytes, public_key: bytes) -> SourceManifestV1`

Open SQLite with URI `mode=ro&immutable=1` only after the source generation is closed; otherwise use `mode=ro` plus explicit WAL/SHM inventory. Reject symlink escape, device files, sockets, unreadable objects, changing stat tuples, and duplicate source IDs. Record them as inventory failures; do not follow or repair them. The bootstrap signing command reads one offline Ed25519 private key from an explicit owner-selected descriptor outside Git, emits only the detached signature/public-key fingerprint, and never copies the key into either worktree or runtime state.

- [ ] **Step 4: Run toolkit tests and static checks**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src/yeoman_migration_toolkit
```

Expected: all pass; the tests prove the inspected fixture remains byte-identical.

- [ ] **Step 5: Build and sign the bootstrap toolkit artifact**

```bash
uv build
```

Sign the canonical artifact manifest containing every `dist/` filename, size, and SHA-256 with the same pinned offline Ed25519 program key used for the preservation tag. Verify the detached signature from the pinned public key in a separate process. Store signature, artifact manifest, and public-key fingerprint in encrypted preservation evidence; commit only their safe digests.

- [ ] **Step 6: Commit the standalone toolkit**

```bash
git add pyproject.toml uv.lock src tests
git commit -m "feat(migration): add signed read-only source inventory"
```

## Task 3: Capture the preservation baseline without private Git data

**Files:**

- Create outside Git: protected source manifest, raw count/hash evidence, and first pre-rework backup generation.
- Modify outside Git: only the explicitly approved purge/cleanup fences.
- Verify: current runtime service/build and every state writer.

**Interfaces:**

- Consumes: toolkit from Task 2 and the old live state root.
- Produces: authenticated source/state baseline digest used by every rehearsal; private manifest stays in protected storage.

- [ ] **Step 1: Enumerate all writers and destructive paths**

Run from the preservation source:

```bash
rg -n "DELETE FROM|unlink\(|rmtree\(|remove\(|prune|cleanup|retention|soft_delete|delete.after|forget" packages scripts tests
rg -n "\.db|\.sqlite|\.jsonl|\.json|spool|outbox|media|blob|persona|policy|config" packages scripts
```

Expected: a reviewed list mapping each writer/purger to service, schedule, state target, and preservation disposition. Unknown targets block the snapshot.

- [ ] **Step 2: Fence destructive behavior**

Use the old runtime's existing supported configuration/service mechanism only after the owner signs the exact writer/purger list, target paths, old config/build digest, and preservation action with the offline program key. Prove from current process arguments, loaded config digest, timers, and fresh logs that retention purges, delete-after-transform paths, misleading soft delete, and arbitrary Overseer database cleanup cannot run. Store the authorization/operation receipt in encrypted preservation evidence. Do not edit installed copies.

- [ ] **Step 3: Close an authenticated inventory generation**

Run the toolkit against `~/.yeoman` and every external local root found in Step 1. Store the full result in protected migration evidence and export only its manifest digest to the program receipt.

Expected: every database/table/file/blob/JSON log/spool/persona/config/policy/workload source has one classification or an explicit `unclassified` blocker; counts, sizes, schemas, hashes, relationships, cutoffs, and pending/unknown effects are represented.

- [ ] **Step 4: Create and verify the pre-rework backup**

Use the current supported backup mechanism if it produces an authenticated recoverable generation; otherwise quiesce writers with owner approval and make a filesystem-consistent encrypted preservation copy to a validated local destination. Restore it into disposable storage and run database integrity plus file/hash reconciliation.

Expected: a verified restore and protected receipt. Failure blocks target deletion.

## Task 4: Replace the target branch with the fenced canonical skeleton

**Files:**

- Delete: all old executable packages, bridge sources/build output, tests, scripts, units, configs, and superseded docs from the target worktree.
- Preserve: `AGENTS.md`, repository governance, the normative architecture spec, and the active plan set.
- Create: target workspace/shared/Gateway files listed in the file map.
- Test: `tests/app/test_fenced_bootstrap.py`

**Interfaces:**

- Consumes: `ReleaseGeneration` and `BuildManifest` only.
- Produces: `yeoman` process that reports local liveness and `FENCED_NOT_ACTIVATED`; no business port exists yet.

- [ ] **Step 1: Write the failing fenced-bootstrap test**

```python
def test_unactivated_gateway_exposes_no_effect_capability(tmp_path: Path) -> None:
    result = preflight_release(tmp_path / "missing-activation.enc")
    assert result.state == PreflightState.FENCED_NOT_ACTIVATED
    assert result.open_capabilities == frozenset()
```

Run:

```bash
uv run pytest tests/app/test_fenced_bootstrap.py -q
```

Expected: FAIL because the target package does not exist.

- [ ] **Step 2: Remove legacy source from the target worktree**

Use `git rm` with explicit top-level targets after comparing them to the signed preservation baseline. Do not delete the live checkout, toolkit worktree, normative spec, active plans, `.gitignore`, `.github` governance required by the new build, or `AGENTS.md`.

Run:

```bash
git status --short
git diff --cached --stat
```

Expected: every deletion is confined to the target worktree and is recoverable from the preservation tag.

- [ ] **Step 3: Implement the minimal target workspace and contracts**

Create opaque identifier and generation value objects with strict parsing; create `PreflightResult(state, open_capabilities, release_generation, errors)`. The only entrypoint accepts `--state-root` and `--liveness-socket`; it rejects `~/.yeoman`, refuses missing/invalid activation, opens no network listener, and does not import provider, channel, tool, workload, memory, or delivery code.

- [ ] **Step 4: Lock dependencies and prove the skeleton**

```bash
uv lock
uv sync --frozen
uv run pytest tests/app/test_fenced_bootstrap.py -q
uv run ruff check .
uv run mypy packages/shared packages/gateway
```

Expected: all pass from the target worktree.

- [ ] **Step 5: Commit the legacy-free skeleton**

```bash
git add -A
git commit -m "refactor(architecture)!: replace runtime with fenced target skeleton"
```

The commit body must include `BREAKING CHANGE:` and state that the branch is non-live until the cutover milestone.

## Task 5: Enforce public build and private activation separation

**Files:**

- Create: `release/build-manifest.toml`
- Create: `release/forbidden-residuals.toml`
- Create: `release/activation-manifest.schema.json`
- Create: `scripts/release/build_manifest.py`
- Create: `scripts/release/verify_artifact.py`
- Test: `tests/architecture/test_release_manifest.py`
- Test: `tests/architecture/test_forbidden_residuals.py`
- Test: `tests/architecture/test_import_ownership.py`

**Interfaces:**

- Consumes: clean source tree and built wheels/sdist.
- Produces: deterministic `BuildManifestV1` and a pass/fail artifact verification report; it never reads activation plaintext.

- [ ] **Step 1: Write failing release-gate tests**

Tests must inject one unlisted file, one forbidden import, one old config key, one unexpected unit, and one fake credential-like activation value. Assert that each produces a stable error code:

```python
assert verify_artifact(wheel).codes == {"UNLISTED_FILE"}
assert scan_source(repo).codes == {"FORBIDDEN_IMPORT"}
assert validate_activation_schema(public_schema).secret_values_seen is False
```

- [ ] **Step 2: Run the failing tests**

```bash
uv run pytest tests/architecture -q
```

Expected: FAIL because the manifest tools do not exist.

- [ ] **Step 3: Implement exhaustive allowlist verification**

The build manifest must name file paths/digests, package names, dependencies, schemas/migrations, entrypoints, systemd units/templates, channel/provider/profile/tool/workload definition types, config keys, and current documents. Verification fails on additions as well as omissions. The forbidden file must ban legacy namespaces, `v2`/`next`/compat branches, old database runtime names, direct provider/network/credential owners, direct `bwrap`, arbitrary subprocess/systemd calls, persona evolution, Discord, Feishu, dormant HTTP APIs, remote backup, and migration-toolkit imports.

- [ ] **Step 4: Build from a clean clone and verify non-editably**

```bash
uv build --all-packages
uv run python scripts/release/build_manifest.py --dist dist --output dist/build-manifest.json
uv run python scripts/release/verify_artifact.py --manifest dist/build-manifest.json --dist dist
```

Expected: PASS; unpacked artifacts contain only allowlisted files and no private activation value.

- [ ] **Step 5: Commit release gates**

```bash
git add release scripts/release tests/architecture packages/shared/yeoman_shared/contracts/manifests.py
git commit -m "chore(release): enforce exhaustive target artifacts"
```

## Task 6: Add program receipts and close Milestone 01

**Files:**

- Create: `scripts/program/write_handoff.py`
- Test: `tests/program/test_write_handoff.py`
- Create in encrypted preservation evidence: canonical complete Milestone 01 operational receipt signed by the offline program-signing key; Milestone 02 imports it unchanged into target Evidence.
- Create in Git: `artifacts/program/handoffs/m01-handoff.json`
- Create in Git: `artifacts/program/contracts/accepted-contracts.json`
- Modify: orchestration state table only after both reviews return GO.

**Interfaces:**

- Consumes: sanitized verification results and protected manifest digests.
- Produces: encrypted/signed bootstrap receipt commitment, safe schema `yeoman.program-handoff-reference.v1`, and cumulative accepted-contract registry with `next_milestone=m02`.

- [ ] **Step 1: Write failing strict receipt-validation tests**

Reject missing review decisions, non-zero verification exit codes, non-empty blocking risks, malformed digests, private absolute paths, chat/account identifiers, and unknown keys. Assert canonical sorted-key serialization. The safe reference must include the protected receipt digest, source/build/toolkit digests, exported interface versions, schema/migration versions, definition digests, capability/fence types, verification digests, ownership, invalidation rules, and all unresolved risks.

- [ ] **Step 2: Run the failing writer test**

```bash
uv run pytest tests/program/test_write_handoff.py -q
```

Expected: FAIL because the strict handoff writer does not exist.

- [ ] **Step 3: Implement, verify, and commit the handoff writer**

Implement canonical schema validation and safe-reference/registry generation only. It accepts typed digest/review inputs, refuses private fields and caller file paths outside exact artifact targets, and cannot read protected content directly.

```bash
uv run pytest tests/program/test_write_handoff.py -q
git add scripts/program/write_handoff.py tests/program/test_write_handoff.py
git commit -m "feat(program): add strict milestone handoff writer"
```

- [ ] **Step 4: Run the complete Milestone 01 gate**

```bash
uv run pytest -q
uv run ruff check .
uv run mypy packages/shared packages/gateway scripts/release scripts/program
uv build --all-packages
uv run python scripts/release/verify_artifact.py --manifest dist/build-manifest.json --dist dist
git diff --check
git status --short
```

Expected: all commands pass from the exact committed writer/source tree; only generated handoff/registry/orchestration review updates remain uncommitted.

- [ ] **Step 5: Obtain independent architecture and security GO**

Send reviewers the preservation tag/commit, target commit range, toolkit artifact digest, private-evidence summary with no private content, build manifest, deletion diff stat, test outputs, and draft receipt. Any NO-GO blocks advancement.

- [ ] **Step 6: Commit the accepted handoff and advance orchestration**

```bash
git add artifacts/program/handoffs/m01-handoff.json artifacts/program/contracts/accepted-contracts.json docs/superpowers/plans/2026-08-16-yeoman-rework-orchestration.md
git commit -m "docs(architecture): accept rework release baseline"
```

Expected: orchestration state is `READY_FOR_MILESTONE_02`, and the next fresh task loads only the orchestrator, normative sections, this receipt, and Milestone 02.
