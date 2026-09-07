# Agent A M06 review — exact head f1383bec

Reviewed commit: `f1383bec6494ab6c4c407f1c7d239c82ce668902`
Tree: `ce317cc8d931282fa73710001235267b469c7fe7`
Parent: `91732481a3e7f84eff0a19654984bc2da5f5d679`
Commit: `fix(status): bind reviewer decisions to release`

The requested commit was reviewed by `git show`; the checkout itself was not
changed and no runtime, service, protected state, or live evidence was read.
The changed Python files parse successfully with the AST parser.

## Decision

NO-GO for accepting this M06 head as complete or permitting retirement/destructive
follow-on work. The change correctly carries the current release digest into
stabilization status and rejects an explicitly mismatched reviewer release
binding, but the new binding is not yet fail-closed against sentinel digests,
cross-paired reviewer receipts, or exact-expiry reuse.

## Findings

### Critical

1. The all-zero release sentinel is accepted as an exact release. Both
   `StabilizationInput.current_release_sha256` and
   `CoreStabilizationStatus.release_sha256` default to `"0" * 64`, while
   `_require_digest()` validates only hexadecimal shape. `RetirementRequest`
   then accepts reviewer bindings equal to that zero value. A caller can build
   a formally ready status and two reviewer bindings for a placeholder rather
   than the actual release. Reject the zero digest at evidence and retirement
   boundaries and require a non-placeholder release commitment.

### Important

2. Reviewer release bindings are supplied as a parallel tuple independent of
   `reviewer_go_digests`, principals, and roles (`stabilization.py:386-435`).
   The model checks that both strings equal `status.release_sha256`, but it
   does not prove that each reviewer GO digest cryptographically commits to
   that release or that the digest is paired with the declared principal/role.
   Use a canonical reviewer-decision commitment (or signed receipt object)
   whose digest includes release, principal, role, decision, and expiry; bind
   that object into the retirement action.

3. Expiry boundaries are still reusable at the exact deadline:
   `retire_legacy_bundle()` rejects only `expiry < request.now` for reviewer
   GOs and owner step-up, and status freshness rejects only `now > fresh_until`
   (`stabilization.py:688-699`). Use `now >= expiry` consistently and add
   exact-boundary tests.

4. The new test covers only one mismatched reviewer release. It does not cover
   the zero sentinel, reviewer digest/release cross-pairing, role-to-receipt
   pairing, exact expiry, or serialized/legacy status migration. Add those
   adversarial cases before accepting the gate correction.

### Minor

5. `release_sha256` was inserted into `CoreStabilizationStatus` before the
   existing defaulted evidence fields (`stabilization.py:290-305`). Any
   positional constructor outside the searched tree can now bind the former
   daily-snapshot argument to the release field. Prefer keyword-only
   construction or append the field after the established defaults, with a
   compatibility test.

## What is correct

The status evidence digest includes `release_sha256`; the evaluator propagates
`StabilizationInput.current_release_sha256`; retirement action hashing includes
the reviewer release tuple; and the explicit mismatched-release test fails
closed. The retirement operation remains an inert dry run (`executable=False`)
and this review authorizes no deletion, controller invocation, owner step-up,
service action, or runtime operation.

## Required next order

1. Make release commitments non-placeholder and receipt-bound per reviewer.
2. Correct expiry semantics and add adversarial/serialization tests.
3. Re-run the full M06 stabilization/status/retirement gate from this exact
   corrected commit, then obtain fresh independent architecture and security
   decisions bound to the resulting release digest.
