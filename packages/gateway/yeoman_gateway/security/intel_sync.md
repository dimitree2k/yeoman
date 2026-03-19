# Security Intel Sync Process

This project uses a curated internal security rule set (no runtime dependency on external guard libraries).

## Update Cadence

- Review external prompt-injection/security sources periodically (for example monthly).
- Import only high-signal, low-noise patterns relevant to yeoman workflows.
- Keep rule IDs stable and human-readable.

## Update Workflow

1. Propose new/changed patterns in `yeoman/security/rules.py`.
2. Add regression examples for both malicious and benign cases.
3. Run security regression tests and targeted integration tests.
4. Review false positives/false negatives before merge.

## Principles

- Prefer deterministic containment (policy/tool restrictions) over regex-only defenses.
- Keep runtime behavior predictable.
- Avoid large uncurated pattern dumps.
