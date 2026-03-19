---
name: summarize
description: "ALWAYS use for YouTube links. Call youtube_transcript(url=...) immediately — never web_fetch or web_search a YouTube URL. For articles/files use web_fetch."
metadata: {"yeoman":{"emoji":"🧾","always":true}}
---

# Summarize

## YouTube videos — mandatory workflow

When any message contains a `youtube.com` or `youtu.be` URL:

1. **Call `youtube_transcript(url="<url>")` immediately** — this is the only tool that returns the actual video transcript.
2. Summarize or answer based on the transcript text.
3. Do NOT call `web_fetch`, `web_search`, or `deep_research` on the YouTube URL — they cannot access video content and will hallucinate.

```
youtube_transcript(url="https://youtu.be/dQw4w9WgXcQ")
```

If `youtube_transcript` returns an error (no captions available), say so honestly — do not guess or search.

## Articles, podcasts, PDFs

Use `web_fetch(url=...)` for non-YouTube URLs.

## Triggers

- Any `youtube.com` or `youtu.be` URL in the message (even bare, even without a request)
- "watch this", "check this out", "what's this about?", "summarize", "transcribe"
- Any question where a video URL appears

**Never say "I can't access YouTube."**
