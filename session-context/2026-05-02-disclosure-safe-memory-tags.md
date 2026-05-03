# 2026-05-02 Disclosure-Safe Memory Tags

## Summary

Implemented lightweight disclosure metadata for long-term memory and a
pre-generation disclosure gate. The first cheap-model backfill was too broad, so
the live DB was retagged with a narrower deterministic policy.

## Current Policy

Outside-world topics stay `normal` / `speakable`, even when they include war,
politics, public deaths, public illness, scandals, offensive jokes,
provocations, Epstein, finance, trading, work, AI, or news.

Only personal-context material should be restricted:

- group member, user, known contact, or close-relative death/funeral/grief
- severe or chronic illness in that personal context
- self-harm or suicide wording in that personal context
- direct personal medication/dosage context may be `sensitive`

The deterministic classifier is in
`packages/gateway/yeoman_gateway/memory/disclosure.py` as
`classify_disclosure_for_content()`.

## Runtime State

Live memory DB path:

```text
/home/dm/.yeoman/data/memory/memory.db
```

After retagging all active memories across all workspace IDs:

```text
total active: 2227
tagged:       2227
missing:      0

normal/speakable:        2223
taboo/never_initiate:       4
sensitive/context_only:     0
private/owner_only:         0
```

Remaining taboo rows are personal-context cases: bereavement/funeral with a
mother, self-harm wording, chronic illness, and a death/medical outcome tied to
someone's relationship.

Latest backup before the final deterministic retag:

```text
/home/dm/.yeoman/data/memory/memory.disclosure-backfill.20260502-105222.db
```

Earlier backups from model/deterministic passes:

```text
/home/dm/.yeoman/data/memory/memory.disclosure-backfill.20260502-100000.db
/home/dm/.yeoman/data/memory/memory.disclosure-backfill.20260502-101651.db
/home/dm/.yeoman/data/memory/memory.disclosure-backfill.20260502-104851.db
/home/dm/.yeoman/data/memory/memory.disclosure-backfill.20260502-105045.db
```

## Commands Added

Cheap-model backfill:

```bash
yeoman memory disclosure-backfill --profile gptNano --all-workspaces --apply
```

No-model deterministic retag:

```bash
yeoman memory disclosure-retag-narrow --all-workspaces --apply
```

Manual metadata controls:

```bash
yeoman memory add --topics funeral,family --sensitivity taboo --disclosure never_initiate
yeoman memory tag <entry-id> --topics family --sensitivity normal --disclosure speakable
```

## Future Behavior

Automatic capture now writes disclosure metadata at memory-write time using the
narrow deterministic rule. Future memories should not require a model pass just
to avoid the earlier broad `taboo` labeling.

## Verification

Focused checks passed:

```text
uv run pytest tests/gateway/test_memory_disclosure.py tests/gateway/test_memory_forget.py tests/gateway/test_memory_person_profile.py tests/test_memory_cli.py tests/test_responder_memory_recall.py -q
47 passed

uv run ruff check ...
All checks passed

git diff --check
passed
```

Gateway was restarted after Python runtime changes and reported:

```text
Gateway running on port 18790 (pid 1177427)
```

## Related Docs

- `docs/superpowers/specs/2026-05-02-disclosure-safe-memory-tags-design.md`
- `docs/superpowers/plans/2026-05-02-disclosure-safe-memory-tags.md`
- `docs/superpowers/specs/2026-05-02-topic-graph-memory-architecture.md`
