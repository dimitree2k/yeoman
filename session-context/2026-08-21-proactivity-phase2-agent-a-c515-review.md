# Agent A review: Proactivity Phase 2 provisional head c5157f82

Date: 2026-08-21
Reviewed commit: `c5157f82c719027853d7a74f1a63086fc5add96d`
Reviewed tree: `c5c889f693e09320f6d28d060db9a1d1ee50d981`
Checkout note: the worktree was at a newer `fe5f103030afe53898ca393ad2e4ec4f219af966`; review used `git show` against the exact requested commit and did not mutate or checkout it.

## Decision

NO-GO for advancing this implementation toward activation or accepting the
Task 1 state as production-ready. The lane is genuinely build-only and
non-activated at this exact head: the commit contains Gateway proposal/
activation state and tests, but no proactivity workload package, definition,
service entrypoint, operator, activation manifest, unit, or live activation
receipt. That isolation must remain explicit until the findings below are
fixed and later gates are independently reviewed.

## Positive evidence

- Static AST parsing passed for all 17 changed Python files.
- Proposal, budget, cooldown, dedup, approval, outcome, and activation tables
  are Workloads-prefixed and repository writes use `ModuleOwner.WORKLOADS`.
- Proposal digests bind domain, destination/audience/relationship, content
  reference, source manifest, generations, instance, purpose, and action class.
- Budget and dedup checks occur inside one transaction; expiry and terminal
  outcome states are persisted.
- Activation validates generation/stage ordering and narrows disable state.

## Findings

### Critical

1. Activation CAS is not atomic. `proactivity_repository.py:267-291` reads
   `actual = self.activation()` before opening the write transaction, then
   unconditionally deletes and inserts `current`. Two concurrent callers can
   both observe the same generation and the later writer can overwrite the
   first candidate. Implement the compare-and-swap predicate inside the same
   transaction (conditional update/insert or a locked re-read), and add a
   concurrent-writer test.

2. The migration path is not wired into runtime startup. The commit adds
   `register_builtin_migrations()` in `storage/migrations.py:107-112`, but
   `git grep` finds no production caller; only the test calls it. Meanwhile
   `ProactivityRepository.ensure_schema()` directly executes all DDL
   (`proactivity_repository.py:55-59`), bypassing the migration version ledger.
   Make the normal startup path register/apply the migration and make the
   repository refuse or delegate direct schema creation; prove upgrade and
   rollback atomically.

3. Action-bound approval expiry is not enforced at outcome time.
   `approve_proposal()` (`proactivity_repository.py:160-190`) permits an
   approval expiry later than the proposal expiry and stores it without a
   bound generation/expiry check. `record_outcome()` (`:193-235`) never reads
   the approval expiry and accepts a delivered/unknown outcome regardless of
   whether the approval is expired or `now` is outside the proposal window.
   Bind approval expiry to proposal expiry and revalidate approval, current
   generations, and time immediately before any effect/outcome commit.

### Important

4. `record_outcome()` does not validate `now` or enforce a complete state
   transition matrix. It can record a terminal outcome with a timestamp before
   creation or after expiry, and can turn an otherwise inconsistent state into
   a terminal result. Add bounded timestamp checks and explicit allowed
   transitions, including already-sent/probably-sent handling.

5. Owner authorization is represented only as an arbitrary 64-hex
   `owner_action_digest`. The repository's private `_allow_narrowing` switch
   (`proactivity_repository.py:255-278`) lets any caller request narrowing,
   while no action receipt, actor, scope, nonce, or expiry is verified at this
   head. This is acceptable only while the lane remains unactivated; the next
   operator/activation task must make owner step-up and disable authority an
   action-bound local boundary, not a repository flag.

6. The migration test named “atomic” only applies a successful migration
   (`tests/proactivity/test_migration.py:10-23`). It injects no failing second
   statement, rollback assertion, duplicate registration, or direct-worker SQL
   rejection. Add fault-injection and ownership tests before treating the
   migration contract as proven.

### Minor

7. Domain isolation is represented in proposal/budget/cooldown keys and is
   covered for two synthetic domains, but tests do not cover same-person,
   different-group identity collisions or owner-private aggregation. Those
   belong in the Task 2 workload compartment tests before any observation.

## Authorization boundary

This review authorizes no merge, registry advancement, install, activation,
runtime access, channel/model credential access, delivery, or live observation.
Only further offline implementation and tests in the provisional Phase 2
worktree are within scope. After correction, rebase on the accepted Core
stabilization commit, rerun the complete affected gate from a clean tree, and
obtain fresh architecture/security reviews before any activation decision.
