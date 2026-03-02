---
name: summarize
description: “Use whenever the user shares a YouTube link, video URL, article, podcast, or file. Fetches transcript or summary via the summarize CLI. Never say \”I can't access YouTube\” — use this skill instead.”
homepage: https://summarize.sh
metadata: {"yeoman":{"emoji":"🧾","requires":{"bins":["summarize"]},"install":[{"id":"brew","kind":"brew","formula":"steipete/tap/summarize","bins":["summarize"],"label":"Install summarize (brew)"}]}}
---

# Summarize

Fast CLI to summarize URLs, local files, and YouTube links.

## When to use

Use this skill immediately when any of the following apply:

**Explicit requests:**
- “summarize this URL/article/video”
- “transcribe this YouTube/video”
- “what’s this link/video about?”
- “use summarize.sh”

**YouTube or video URL present in the message** (even without an explicit request):
- Message contains a `youtube.com` or `youtu.be` URL
- “watch this”, “check this out”, “have you seen this”
- “what does [person] say in this video?”
- “is this worth watching?”
- Any question where a video or article URL appears in the message

**Never respond with “I can’t access YouTube” or “I can’t watch videos.”**
Use `summarize --youtube auto` instead.

## Quick start

```bash
summarize "https://example.com" --model google/gemini-3-flash-preview
summarize "/path/to/file.pdf" --model google/gemini-3-flash-preview
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto
```

## YouTube: summary vs transcript

Best-effort transcript (URLs only):

```bash
summarize "https://youtu.be/dQw4w9WgXcQ" --youtube auto --extract-only
```

If the user asked for a transcript but it’s huge, return a tight summary first, then ask which section/time range to expand.

## Model + keys

Set the API key for your chosen provider:
- OpenAI: `OPENAI_API_KEY`
- Anthropic: `ANTHROPIC_API_KEY`
- xAI: `XAI_API_KEY`
- Google: `GEMINI_API_KEY` (aliases: `GOOGLE_GENERATIVE_AI_API_KEY`, `GOOGLE_API_KEY`)

Default model is `google/gemini-3-flash-preview` if none is set.

## Useful flags

- `--length short|medium|long|xl|xxl|<chars>`
- `--max-output-tokens <count>`
- `--extract-only` (URLs only)
- `--json` (machine readable)
- `--firecrawl auto|off|always` (fallback extraction)
- `--youtube auto` (Apify fallback if `APIFY_API_TOKEN` set)

## Config

Optional config file: `~/.summarize/config.json`

```json
{ "model": "openai/gpt-5.2" }
```

Optional services:
- `FIRECRAWL_API_KEY` for blocked sites
- `APIFY_API_TOKEN` for YouTube fallback
