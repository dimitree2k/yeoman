# Consciousness Spec Review

Date: 2026-04-25
Context: Review of the first consciousness-layer spec before implementation.

## Findings

- The product direction is worth pursuing, but the original spec had three
  codebase integration bugs: per-chat policy was placed in shared config,
  outbound speakup approval was routed through inbound workflow approval, and
  burst triggering assumed an inbound observer API that does not exist.
- The original draft was preserved as
  `docs/superpowers/specs/2026-04-25-consciousness-layer-design-superseded.md`.
- The active spec is now
  `docs/superpowers/specs/2026-04-25-consciousness-layer-design.md`.
- Phase 0 now explicitly covers policy integration, global config, an
  `InboundObservedEvent`, and dedicated speakup approval primitives.

## Follow-up

- Implement Phase 0 before any proactive bot behavior is enabled.
- Keep owner-DM-only helpful cron speakups as Phase 1.
- Do not add groups, burst triggers, outcome enrichment, or taste distillation
  until the earlier phases pass tests and prove useful in practice.
