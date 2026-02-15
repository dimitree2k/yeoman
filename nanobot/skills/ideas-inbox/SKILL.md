---
name: ideas-inbox
description: Capture, retrieve, review, and prioritize idea notes from chat and memory. Use when the user wants an "idea inbox" workflow (quick capture while moving, evening triage, backlog promotion), especially with voice-transcribed messages from Telegram/WhatsApp.
---

# Ideas Inbox

Use this skill to run a lightweight GTD-like pipeline:
1. Capture idea quickly.
2. Retrieve today's or recent idea candidates.
3. Analyze and cluster duplicates/themes.
4. Promote selected items into explicit backlog records.

## Capture Protocol

Default approach:
- Only classify as idea/backlog when the first spoken word signals intent (for example: `Idea`, `Idee`, `Ideia`, `Backlog`, `Todo`).
- Regular research/chat messages stay unclassified.
- For WhatsApp capture messages, Nano acknowledges by emoji reaction (`💡` for idea, `📌` for backlog) and skips normal Q&A reply.

Optional explicit markers:
- Idea capture marker: `[IDEA]`
- Backlog marker: `[BACKLOG]`
- Voice-friendly prefixes also work: `Idea ...`, `Idea: ...`, `Idea. ...`, `Backlog ...`

When capturing from chat, keep one item per message in this shape:
```text
[IDEA] <short title> :: <one sentence detail>
```

When promoting an accepted idea:
```text
[BACKLOG] <short title> :: impact=<high|med|low> effort=<high|med|low> next=<first action>
```

Use `nanobot memory add` for durable backlog promotion:
```bash
nanobot memory add \
  --kind decision \
  --scope chat \
  --channel <telegram|whatsapp> \
  --chat-id <chat_id> \
  --text "[BACKLOG] Improve onboarding :: impact=high effort=med next=write checklist"
```

## Retrieval Workflow

Use the helper script for deterministic listing and status split:
```bash
python3 nanobot/skills/ideas-inbox/scripts/ideas_report.py \
  --channel <telegram|whatsapp> \
  --chat-id <chat_id> \
  --days 1
```

For quick fallback search:
```bash
nanobot memory search --query "[IDEA]" --channel <channel> --chat-id <chat_id> --scope chat
nanobot memory search --query "[BACKLOG]" --channel <channel> --chat-id <chat_id> --scope chat
```

Optional dedicated-inbox mode:
```bash
python3 nanobot/skills/ideas-inbox/scripts/ideas_report.py --mode inbox
```
Use this only if you intentionally want every unmarked message to count as an idea.

## Evening Review Workflow

1. List recent inbox ideas with `ideas_report.py`.
2. Remove duplicates by title/theme.
3. Score each item by impact and effort.
4. Promote only top items to `[BACKLOG]` via `nanobot memory add`.
5. Keep the rest as `[IDEA]` for later review.

If the user asks for analysis, summarize:
- Top 3 themes
- Top 3 high impact / low effort candidates
- Risks or unknowns for each promoted item

## Reference

For command cookbook and triage rubric, read `references/workflow.md`.
