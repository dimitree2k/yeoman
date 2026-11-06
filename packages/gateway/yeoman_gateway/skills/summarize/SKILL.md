---
name: summarize
description: "ALWAYS use for YouTube, Instagram, TikTok and other video links. YouTube → youtube_transcript(url=...); Instagram reels/posts, TikTok, Shorts and other short clips → video_understand(url=...). Never web_fetch or web_search a video URL. For articles/files use web_fetch."
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

## Instagram, TikTok and other short videos

When a message contains an Instagram reel/post (`instagram.com/reel/…`, `/p/…`, `/tv/…`),
a TikTok link, a YouTube Short or another short video link and you need to know what it is
about, call `video_understand(url="<url>")`. It returns the post's caption, a description of
what the video shows and a transcript of what is said. If it returns an error (e.g. the
post needs a login), say so honestly — do not guess from the URL.

## Articles, podcasts, PDFs

Use `web_fetch(url=...)` for non-YouTube URLs.

## Triggers

- Any `youtube.com` or `youtu.be` URL in the message (even bare, even without a request)
- "watch this", "check this out", "what's this about?", "summarize", "transcribe"
- Any question where a video URL appears

**Never say "I can't access YouTube."**
