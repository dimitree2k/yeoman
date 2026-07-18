# WhatsApp Outbound Reply Archive Design

## Goal

Make delayed WhatsApp replies to Yeoman messages resolve to the correct quoted
thread. Outbound WhatsApp messages must use the same reply archive and the same
30-day retention as inbound messages.

## Scope

- Archive every successfully sent WhatsApp message in
  `data/inbound/reply_context.db`.
- Store the real WhatsApp message ID and original send timestamp returned by the
  bridge.
- Use the existing reply-archive retention and cleanup behavior for inbound and
  outbound rows alike.
- Make an explicit WhatsApp quote authoritative over newer ambient chat topics.
- Leave session JSONL history, long-term `memory.db`, media retention, and all
  non-WhatsApp channels unchanged.

## Non-goals

- No separate outbound database or table.
- No new retention scheduler or 48-hour special case.
- No permanent full-chat archive.
- No WhatsApp history-sync or quote-text search.
- No changes to memory extraction or session-history retention.

## Data Flow

### Sending

1. The gateway sends text or media through the WhatsApp bridge.
2. The bridge returns the sent message's real WhatsApp message ID and send
   timestamp in the successful command response.
3. The WhatsApp channel records the visible outbound content in the existing
   `InboundArchive` storage, using the returned ID and timestamp and identifying
   the speaker as Yeoman.
4. Failed sends are not archived.

The archive continues to use its existing `(channel, chat_id, message_id)`
primary key and 30-day retention. No direction column or separate lifecycle is
required for reply lookup.

### Receiving a Reply

1. The inbound reply supplies `replyToMessageId`, quoted author, and quote text.
2. `ReplyContextMiddleware` looks up the quoted message by
   `(channel, chat_id, replyToMessageId)`.
3. If found, the stored original timestamp anchors the context window around the
   quoted message, even when unrelated chat activity occurred hours later.
4. The current ambient window may be included as secondary context, but the
   quoted message determines the referent.
5. If the archive lookup misses, Yeoman uses the quote text supplied by
   WhatsApp without inventing an archive row or timestamp. Recent ambient
   messages must not replace the explicit quote.

## Removing the Faulty Fallback

The current bridge and pipeline seed an unseen quoted target into the archive
with the timestamp of the new reply. That makes unrelated messages appear to
precede the quoted target. This synthetic seeding will be removed.

An archive miss remains valid input because WhatsApp already supplies the quote
text. It simply has no historical context window.

## Prompt Precedence

When `reply_to_text` is present, the current-message context will state:

- `quoted_message` is the authoritative referent for the reply.
- `topic_window_before_reply` may explain the original thread.
- `recent_messages` are secondary and must not redirect the answer to a newer
  topic.
- If quote text alone is ambiguous and no archived anchor exists, ask a short
  clarification instead of guessing from ambient chat.

## Retention

Inbound and outbound reply-archive rows share the existing 30-day retention.
The existing startup purge and opportunistic hourly purge remain unchanged.
This design does not delete or alter:

- WhatsApp session JSONL files;
- long-term memories in `data/memory/memory.db`;
- media or document caches;
- contact data.

## Failure Handling

- A bridge send without a usable message ID is treated as delivered but cannot
  become a durable reply anchor; emit a warning and do not create a fabricated
  ID.
- A missing bridge timestamp falls back to the gateway's send-completion time.
- An archive write failure does not turn a successful WhatsApp send into a
  user-visible failure; log the failure so the reply will later use quote-only
  fallback.
- Duplicate archive writes remain idempotent through the existing primary key.

## Tests

Add regression coverage for:

1. Bridge text and media responses include the real message ID and timestamp.
2. A successful outbound send is stored in the existing reply archive.
3. A failed outbound send creates no archive row.
4. A reply arriving hours later resolves its original outbound anchor and
   receives the context window from that time, not the newest group topic.
5. An archive miss uses WhatsApp quote text without seeding a current-time
   target row.
6. Prompt construction makes an explicit quote authoritative over unrelated
   recent messages.
7. Existing inbound retention, session JSONL, and memory behavior remain
   unchanged.

## Acceptance Criteria

- Steffen's shape of reply (`"Hier"` quoting Yeoman's delivery-choice question)
  resolves to the quoted delivery question after hours of unrelated group
  chatter.
- Yeoman does not answer the newer Fable thread in that case.
- Both inbound and outbound reply anchors expire under the same existing
  30-day policy.
- No new database, cleanup service, or retention setting is introduced.
