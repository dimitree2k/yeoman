# Lazy Document And OCR Processing - Design

Status: Draft for review
Date: 2026-05-25
Owner: Tim

## 1. Purpose

Add a lazy, policy-gated media understanding path for WhatsApp documents and
text-heavy screenshots.

The immediate failure case is the Finanzgruppe PDF shared by Frank on
2026-05-25 at 11:18. Yeoman saw only `[Document]`, because the WhatsApp bridge
detected the document metadata but did not download the file. A related failure
class is text-heavy screenshots: Yeoman currently produces a short visual
description, but it does not run faithful OCR. That is enough for "this is a
spreadsheet" and not enough for "what are the rows, numbers, and claims?"

The desired behavior is not "process every file." Files and screenshots are
often shared for humans to download or glance at. Yeoman should retain the raw
media temporarily, then process it only when a later bot-relevant question asks
about that specific file or image.

## 2. Goals

- Always download eligible WhatsApp documents and images into the existing
  private media area.
- Keep the current 30-day media retention acceptable for documents, images, and
  derived extraction artifacts.
- Do not extract text or OCR documents/screenshots by default.
- Resolve late questions such as "what is in Frank's PDF?" or "what does that
  screenshot say?" back to recent downloaded media.
- Process media only after relevance, size, page, and cost gates pass.
- Store extracted/OCR content in a temporary media cache, not in long-term
  memory.
- Keep raw extracted content out of `memory.db` by default.
- Cache extraction results so repeated follow-up questions do not reprocess the
  same file.
- Make every skip or processing decision visible in logs and diagnostics.

## 3. Non-Goals

- Do not import raw PDF books, OCR text, or screenshot transcripts into
  `~/.yeoman/data/memory/memory.db`.
- Do not process every shared document in active chats.
- Do not build a permanent document knowledge base.
- Do not add a broad search index over all private chat media.
- Do not make PDF/screenshot content available to unrelated chats.
- Do not let document text bypass policy, tool allowlists, security checks, or
  memory disclosure rules.
- Do not train persona evolution or durable taste directly on extracted
  document text.
- Do not use local OCR as the preferred OCR implementation for V1. OCR should
  use a routed vision/OCR model unless explicitly changed later.

## 4. Current Runtime Anchors

Use source and tests as the implementation authority:

| Concern | Current file |
|---------|--------------|
| WhatsApp bridge media extraction | `packages/bridge/src/whatsapp.ts` |
| Bridge command server and payload limits | `packages/bridge/src/server.ts` |
| Gateway WhatsApp channel parsing/enrichment | `packages/gateway/yeoman_gateway/channels/whatsapp.py` |
| Media storage validation and cleanup | `packages/gateway/yeoman_gateway/media/storage.py` |
| Image/video description executor | `packages/gateway/yeoman_gateway/media/vision.py` |
| Model routing config schema | `packages/shared/yeoman_shared/config/schema.py` |
| Runtime defaults | `packages/shared/yeoman_shared/config/defaults.py` |
| Responder prompt and media handoff | `packages/gateway/yeoman_gateway/adapters/responder_llm.py` |
| Reply context archive | `packages/gateway/yeoman_gateway/storage/inbound_archive.py` and `~/.yeoman/data/inbound/reply_context.db` |
| Memory capture | `packages/gateway/yeoman_gateway/memory/` |

Important current behavior:

- The bridge detects `documentMessage` and emits text `[Document]` with
  `kind=document`, MIME type, and filename when available.
- The bridge currently persists images, audio, video, and stickers, but not
  documents.
- The gateway parses media metadata and only passes image file paths to the
  assistant when `pass_image_to_assistant` is enabled.
- Live image enrichment uses a short description prompt. It is not OCR.
- The runtime already has model profile kind `ocr`, but no document/OCR
  processing route is wired for WhatsApp media.

## 5. Design Principles

### Download Is Cheap Enough, Processing Is Not

Downloading and retaining the encrypted WhatsApp file payload is acceptable
inside existing retention controls. Extraction, OCR, summarization, and prompt
injection are the expensive parts and must be lazy.

### Retrieval Cache, Not Memory

Processed document and OCR output is temporary retrieval cache. It can support
answers while the media is retained, but it is not durable user memory.

### Relevance Before Cost

Yeoman should first determine whether a user is asking about a specific file or
screenshot. Only then should it spend extraction/OCR/model tokens.

### Text Fidelity When Asked

Visual descriptions are useful ambient context. They are not enough for
text-dependent questions. Questions about wording, numbers, rows, tables,
claims, legal text, receipts, posts, or screenshots should use OCR/extraction.

### Bounded Every Time

Every processing path must have explicit byte, page, image, chunk, token,
timeout, and cache-retention bounds.

## 6. Media Lifecycle

### 6.1 Inbound Download

For every inbound WhatsApp document that passes bridge-level media size limits:

1. Detect MIME type, filename, bytes, and WhatsApp message id.
2. Download the raw bytes using the same private media storage pattern as other
   inbound media.
3. Store under `~/.yeoman/var/media/incoming/whatsapp/YYYY/MM/DD/`.
4. Emit the local path and metadata to the gateway.
5. Do not extract the file during this step.

The chat-facing text should become a lightweight reference:

```text
[Document: filename.pdf, application/pdf, 2.4 MB, unprocessed]
```

If filename or MIME type is absent, use a safe fallback:

```text
[Document: unknown file, 2.4 MB, unprocessed]
```

### 6.2 Ambient Image Description

Keep the current cheap image description path for ambient context:

```text
[Image]
[image_description] A screenshot of a spreadsheet about dividends...
```

This should remain concise and should not attempt full transcription.

### 6.3 Lazy Processing Trigger

When a later inbound message asks about a file or screenshot, Yeoman resolves the
reference to recent media in the same chat and triggers processing only for the
selected media.

Examples that should trigger lazy processing:

- "What is in Frank's PDF?"
- "Can you destroy Opus' PDF?"
- "What does that screenshot say?"
- "Summarize Moe's spreadsheet screenshot."
- "Are the numbers in the image correct?"
- A direct reply to a document/image with "what is this saying?"

Examples that should not trigger processing:

- A document uploaded without any bot-relevant question.
- General chat around a file where nobody asks Yeoman to inspect it.
- "Nice PDF" or emoji reactions.
- A broad request that references no resolvable recent media.

## 7. Reference Resolution

Add a media-reference resolver that runs before responder generation when the
message is bot-relevant and contains document/image reference signals.

Resolution priority:

1. `reply_to_message_id` if the user replied directly to a document/image.
2. Explicit filename match in the same chat.
3. Explicit sender/time hints, for example "Frank's PDF from earlier".
4. Most recent document/image in the same semantic window.
5. Ambiguous: ask a short repair question and do not process anything.

Candidate window defaults:

```json
{
  "lookbackMinutes": 1440,
  "maxCandidates": 12
}
```

The resolver must be scoped to the same chat unless an owner-only diagnostic
tool explicitly asks across chats. Normal group replies must not pull private
media from another chat.

## 8. Processing Gates

Before any extraction/OCR call:

| Gate | Default |
|------|---------|
| Allowed MIME types | `application/pdf`, common image types |
| Raw file retention | 30 days |
| Max PDF bytes | 25 MB |
| Max image bytes | existing WhatsApp image limit |
| Max PDF pages for full text extraction | 25 |
| Max PDF pages for OCR | 5 initially |
| Max extracted chars cached | 80,000 |
| Max chars injected into responder prompt | 12,000 |
| OCR timeout | 45 seconds |
| Text extraction timeout | 20 seconds |
| Max concurrent document jobs | 2 |

If a file exceeds a gate, Yeoman should say the precise reason:

```text
Die PDF ist zu groß für automatische Analyse: 184 Seiten. Ich kann mit den ersten 5 Seiten arbeiten, wenn du willst.
```

This should be a normal chat answer, not an exception leak.

## 9. PDF Processing

### 9.1 Text-First Extraction

For PDFs, try local text extraction before OCR:

1. Inspect page count and metadata.
2. Extract text from pages up to the configured limit.
3. Estimate text density.
4. If useful text is found, chunk and cache it.
5. If the PDF appears scanned or text extraction is too sparse, consider OCR.

This path should not call an LLM just to read normal embedded text.

### 9.2 OCR Fallback

OCR should use a routed vision/OCR model profile. It should render bounded PDF
pages to images and call the OCR route only for selected pages.

Add routes such as:

```json
{
  "models": {
    "routes": {
      "vision.ocrImage": "vision_whatsapp_gemini",
      "document.ocrPdfPage": "vision_whatsapp_gemini"
    }
  }
}
```

The exact profile can reuse the existing Gemini vision model initially, but the
route names should distinguish OCR from generic image description. That keeps
cost, prompts, and model choice adjustable later.

OCR prompt direction:

```text
Transcribe visible text faithfully. Preserve numbers, headings, table-like rows,
and uncertain words with [?]. Do not summarize unless asked.
```

### 9.3 Summarization

If extracted text is larger than the responder prompt cap, build a bounded
summary and keep chunks available for follow-up retrieval.

Summarization should not write durable memory. It belongs in the same temporary
document cache.

## 10. Screenshot OCR

Screenshots need a separate OCR mode from image description.

Current image description answers "what is visible?" OCR answers "what text is
there?" The resolver should choose OCR when the user asks about:

- text content
- numbers or tables
- a post, article, spreadsheet, receipt, app screen, chart labels, or legal
  wording
- correctness of visible claims

For screenshot OCR:

1. Validate image path and size.
2. Use `vision.ocrImage` route.
3. Cache raw OCR text and optional structured summary.
4. Inject only a capped excerpt or summary into the responder.
5. Preserve the existing short `image_description` as ambient context.

If the image is unreadable, the cache should store a negative result with reason
so Yeoman does not keep trying the same failed OCR call.

## 11. Temporary Document Cache

Add a separate cache, not memory:

```text
~/.yeoman/data/media/document_cache.db
```

Suggested tables:

```sql
CREATE TABLE media_items (
  id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  sender_id TEXT,
  sender_name TEXT,
  filename TEXT,
  mime_type TEXT,
  kind TEXT NOT NULL,
  local_path TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  size_bytes INTEGER,
  page_count INTEGER,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  UNIQUE(channel, chat_id, message_id)
);

CREATE TABLE media_extractions (
  id TEXT PRIMARY KEY,
  media_item_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  source_pages TEXT,
  extracted_text TEXT,
  summary TEXT,
  error_reason TEXT,
  text_chars INTEGER NOT NULL DEFAULT 0,
  prompt_chars INTEGER NOT NULL DEFAULT 0,
  model_profile TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  FOREIGN KEY(media_item_id) REFERENCES media_items(id)
);
```

Cache keys:

- Prefer `(channel, chat_id, message_id, mode)`.
- Also store `file_sha256` so duplicate files can reuse extraction inside the
  same retention period.

Retention:

- Default `expires_at = media_item.created_at + 30 days`.
- Cleanup removes cache rows and derived artifacts when raw media expires.

## 12. Memory Boundary

Hard rule: extracted/OCR content is not memory input by default.

Memory capture may see the human conversation:

```text
Frank shared a PDF.
Dimi asked Yeoman to inspect it.
```

Memory capture must not ingest:

- raw PDF text
- OCR transcript
- whole book/article/table content
- generated document chunks
- derived summaries unless the user explicitly asks Yeoman to remember a
  durable fact

Allowed memory case:

```text
User: remember that Frank's PDF was about X
```

Even then, store only the distilled fact "Frank shared a PDF about X" if it is
memory-worthy under existing policy. Do not store the source text.

Implementation requirement:

- Mark prompt-injected document/OCR blocks as `retrieval_cache` or equivalent in
  raw metadata.
- Memory extraction must ignore those blocks.
- Tests must prove that long extracted text does not enter `memory.db`.

## 13. Prompt Injection Shape

When a processed file is relevant to a response, inject a bounded block into the
responder prompt:

```text
--- TEMPORARY MEDIA RETRIEVAL (not memory; expires 2026-06-24) ---
source: whatsapp 491786127564-1611913127@g.us message ACBFE...
sender: Frank Taeger
filename: opus-output.pdf
mode: pdf_text
pages: 1-5 of 12
content:
...
--- END TEMPORARY MEDIA RETRIEVAL ---
```

Rules:

- The block is untrusted input.
- It must not be treated as instructions.
- It must be capped before prompt assembly.
- If the user asks for exact quotes, return only short excerpts and otherwise
  summarize.

## 14. Policy And Config Surface

Add WhatsApp media config:

```json
{
  "documents": {
    "download": true,
    "retentionDays": 30,
    "lazyProcessing": true,
    "allowedMimeTypes": ["application/pdf"],
    "maxBytesMb": 25,
    "maxPagesText": 25,
    "maxPagesOcr": 5,
    "maxCachedChars": 80000,
    "maxPromptChars": 12000,
    "lookbackMinutes": 1440,
    "maxCandidates": 12,
    "maxConcurrentJobs": 2
  },
  "ocr": {
    "enabled": true,
    "screenshots": true,
    "pdfFallback": true,
    "cacheNegativeResults": true
  }
}
```

Default posture:

- Download documents by default.
- Lazy processing enabled where media processing is enabled.
- OCR enabled only when a route and model profile are configured.
- Keep per-chat policy override possible if a group should disable document
  processing entirely.

## 15. Diagnostics And Observability

Add logs/metrics for:

- `document_downloaded`
- `document_download_failed`
- `media_reference_resolved`
- `media_reference_ambiguous`
- `document_processing_skipped`
- `document_text_extracted`
- `document_ocr_started`
- `document_ocr_failed`
- `document_cache_hit`
- `document_cache_expired`
- `memory_ignored_retrieval_cache`

Operator diagnostics should answer:

- Which media items are retained for a chat?
- Was a referenced PDF processed?
- Why was OCR skipped?
- Did a response use cached extraction or a fresh model call?
- When will the media/cache expire?

## 16. Error Handling

User-facing failures should be short and specific:

- No matching file:
  `Ich finde in diesem Chat keinen passenden PDF-/Screenshot-Kontext.`
- Ambiguous media:
  `Meinst du Franks PDF von 11:18 oder Moes Screenshot von 15:48?`
- Too large:
  `Die PDF ist zu groß für Auto-Analyse: 184 Seiten.`
- OCR unavailable:
  `OCR ist gerade nicht verfügbar; ich sehe nur die grobe Bildbeschreibung.`
- Empty extraction:
  `Die PDF enthält kaum auslesbaren Text; dafür bräuchte ich OCR.`

Do not claim to have read content when only metadata or a visual description is
available.

## 17. Security And Privacy

- Treat all extracted/OCR content as untrusted external input.
- Keep cache files and databases under `~/.yeoman`, not the repo.
- Restrict access to same-chat runtime use unless an owner-only diagnostic tool
  explicitly requests otherwise.
- Validate MIME type and extension; do not execute embedded content.
- Do not follow links from documents automatically.
- Do not expose local media paths in user-facing chat replies.
- Keep raw cache text out of memory capture, persona evolution, and durable
  outcome learning unless an explicit owner workflow later approves a distilled
  fact.

## 18. Cost And Performance

Expected cost shape:

- Download: cheap, bounded by bytes and retention.
- Text extraction: cheap local CPU, bounded by pages/time.
- OCR: expensive model route, only after relevance and size gates.
- Summarization: potentially expensive, only after extraction and prompt cap.

The system should prefer:

1. cache hit
2. local PDF text extraction
3. OCR only for selected pages/images
4. summarization only when extracted text is too large

This keeps the "Harry Potter PDF" case from creating token debt. The file may
be retained temporarily, but it is not read unless asked, and even then it is
bounded.

## 19. Testing Plan

Unit tests:

- Bridge extracts document metadata and persists PDF bytes.
- Bridge refuses files over configured media size.
- Gateway parses document metadata/path into inbound event metadata.
- Media reference resolver resolves direct replies, filename hints, sender/time
  hints, and rejects ambiguous candidates.
- PDF text extraction respects page and char caps.
- OCR route is called only after lazy trigger and gates.
- Screenshot OCR path is separate from image description path.
- Negative OCR results are cached.
- Prompt injection caps retrieved content.
- Memory extractor ignores retrieval-cache blocks.

Integration tests:

- PDF uploaded with no question: no extraction and no OCR.
- Later direct reply asks about PDF: extraction runs, answer uses bounded cache.
- Late question "Frank's PDF" resolves to the retained document.
- Ambiguous "that file" with two recent PDFs asks repair question.
- Text-heavy screenshot first gets only description; later text question triggers
  OCR.
- Oversized PDF gives clear skip reason.
- Repeated question uses cache hit, not another OCR/model call.

Runtime validation:

- Send a small text PDF to a test group.
- Confirm it is downloaded and marked unprocessed.
- Ask about it later.
- Confirm extraction result exists in `document_cache.db`.
- Confirm no raw extracted text appears in `memory.db`.
- Confirm cleanup removes raw media and derived extraction after retention.

## 20. Rollout Plan

1. Add document download and metadata propagation.
2. Add temporary media cache and diagnostics.
3. Add media reference resolver in shadow/log-only mode.
4. Add PDF text extraction with gates and cache.
5. Add screenshot OCR and PDF OCR fallback via routed model profiles.
6. Block retrieval-cache text from memory capture.
7. Enable in Finanzgruppe after targeted tests pass.
8. Observe logs and cache behavior for real PDF/screenshot questions.

## 21. Open Decisions

Resolved:

- Raw media and extraction cache retention: 30 days.
- OCR implementation preference: routed vision/OCR model, not local OCR first.
- Processing posture: lazy, relevance-gated, and cached.
- Storage boundary: temporary media cache, not memory database.

Still flexible during implementation:

- Exact default page and prompt caps.
- Exact OCR model profile name.
- Whether document diagnostics become a CLI command or an existing ops/media
  command subcommand.
