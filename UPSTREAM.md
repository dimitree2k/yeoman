# Upstream Relationship

This repository is maintained as an independent fork of:

- Upstream project: https://github.com/HKUDS/nanobot
- Fork line: `nanobot-stack`
- License model: MIT (preserved)

## Why This Fork Exists

`nanobot-stack` prioritizes a professional, policy-first runtime track and operational stability for standalone maintenance.

## Compatibility Policy

- Python package import path remains `nanobot.*` for now.
- CLI compatibility is preserved:
  - `nanobot` (legacy-compatible)
  - `nanobot-stack` (preferred branding)
- Config and policy paths stay under `~/.nanobot` unless explicitly changed in a future major version.

## Upstream Sync Policy

- Security fixes from upstream should be reviewed regularly and cherry-picked when relevant.
- Large feature backports are optional and evaluated by operational fit, not parity.
- Breaking behavior from upstream is not auto-adopted.

## Contributor Guidance

- Prefer changes that keep command and config backward compatibility.
- When introducing divergence from upstream behavior, document it in:
  - `README.md` for user-facing behavior
  - `SECURITY.md` for security-impacting behavior
  - this file for long-term maintenance policy

