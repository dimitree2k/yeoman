# Ideas Inbox Workflow

## Capture

Recommended for voice/multilingual capture:
- Start idea messages with an intent word (`Idea`, `Idee`, `Ideia`, etc.).
- Start backlog messages with an intent word (`Backlog`, `Todo`, etc.).
- Regular messages without those prefixes stay unclassified.
- WhatsApp acknowledgments are emoji-only: `💡` for idea, `📌` for backlog.

Optional markers (still supported):
- Inbox idea: `[IDEA]`
- Accepted backlog: `[BACKLOG]`
- Spoken shortcuts also classify correctly: `Idea ...`, `Idea: ...`, `Idea. ...`, `Backlog ...`

Recommended message format:
```text
[IDEA] <title> :: <detail>
```

Example:
```text
[IDEA] Voice shortcut for grocery list :: one tap voice note that converts to checklist
```

## Daily Review (10-20 min)

1. Pull all recent ideas:
```bash
python3 nanobot/skills/ideas-inbox/scripts/ideas_report.py --days 1
```
2. Cluster duplicates by title/theme.
3. Score each item:
- `impact`: high | med | low
- `effort`: high | med | low
- `urgency`: now | soon | later
4. Promote best items to `[BACKLOG]` entries with `nanobot memory add`.

## Promotion Template

```bash
nanobot memory add \
  --kind decision \
  --scope chat \
  --channel <channel> \
  --chat-id <chat_id> \
  --text "[BACKLOG] <title> :: impact=<high|med|low> effort=<high|med|low> urgency=<now|soon|later> next=<first action>"
```

## Retrieval Commands

All inbox ideas for a chat:
```bash
python3 nanobot/skills/ideas-inbox/scripts/ideas_report.py \
  --channel <telegram|whatsapp> \
  --chat-id <chat_id> \
  --status idea

Optional dedicated-inbox mode:
```bash
python3 nanobot/skills/ideas-inbox/scripts/ideas_report.py --mode inbox
```
Use only when every unmarked message should count as an idea.
```

All backlog entries for a chat:
```bash
python3 nanobot/skills/ideas-inbox/scripts/ideas_report.py \
  --channel <telegram|whatsapp> \
  --chat-id <chat_id> \
  --status backlog
```

Fallback FTS query:
```bash
nanobot memory search --query "[IDEA]" --channel <channel> --chat-id <chat_id> --scope chat
nanobot memory search --query "[BACKLOG]" --channel <channel> --chat-id <chat_id> --scope chat
```
