"""history.db schema: the four tables of conversation history (spec, Layer 2)."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE contacts (
  contact_id   TEXT PRIMARY KEY,
  kind         TEXT NOT NULL CHECK (kind IN ('person', 'channel')),
  role         TEXT CHECK (role IN ('owner', 'assistant')),
  display_name TEXT,
  status       TEXT NOT NULL CHECK (status IN ('confirmed', 'provisional')),
  merged_into  TEXT REFERENCES contacts(contact_id),
  source_refs  TEXT NOT NULL CHECK (json_valid(source_refs))
);
CREATE TABLE identifier_history (
  id            INTEGER PRIMARY KEY,
  contact_id    TEXT NOT NULL REFERENCES contacts(contact_id),
  channel       TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('lid', 'pn_jid', 'newsletter', 'numeric', 'push_name')),
  value         TEXT NOT NULL,
  strength      TEXT NOT NULL CHECK (strength IN ('strong', 'weak')),
  evidence      TEXT NOT NULL CHECK (evidence IN
                  ('native_pair', 'observed', 'numeric_match', 'owner_attested', 'knowledge_binding')),
  first_seen_ms INTEGER,
  last_seen_ms  INTEGER,
  ended_ms      INTEGER,
  source_refs   TEXT NOT NULL CHECK (json_valid(source_refs)),
  UNIQUE (channel, kind, value, contact_id)
);
CREATE INDEX identifier_history_value ON identifier_history(channel, kind, value);
CREATE TABLE messages (
  message_id         TEXT PRIMARY KEY,
  channel            TEXT NOT NULL,
  chat_id            TEXT NOT NULL,
  native_message_id  TEXT,
  sender_contact_id  TEXT REFERENCES contacts(contact_id),
  sender_identifier  TEXT,
  sender_basis       TEXT NOT NULL CHECK (sender_basis IN
                       ('native_identifier', 'numeric_match', 'push_name', 'derived_claim',
                        'owner_attested', 'unknown')),
  direction          TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  sent_ms            INTEGER,
  time_certainty     TEXT NOT NULL CHECK (time_certainty IN
                       ('native', 'provider_timestamp', 'capture_time_approx', 'unknown')),
  text               TEXT,
  media_json         TEXT CHECK (media_json IS NULL OR json_valid(media_json)),
  reply_to_native_id TEXT,
  mentions_json      TEXT CHECK (mentions_json IS NULL OR json_valid(mentions_json)),
  provenance         TEXT NOT NULL CHECK (provenance IN
                       ('native', 'recovered_text', 'verbatim_unverified', 'derived_only')),
  source_refs        TEXT NOT NULL CHECK (json_valid(source_refs))
);
CREATE INDEX messages_chat_time ON messages(channel, chat_id, sent_ms);
CREATE INDEX messages_native ON messages(channel, chat_id, native_message_id);
CREATE INDEX messages_sender ON messages(sender_contact_id);
CREATE TABLE message_events (
  event_id          TEXT PRIMARY KEY,
  kind              TEXT NOT NULL CHECK (kind IN
                      ('reaction', 'edit', 'delete', 'member_add', 'member_remove', 'member_promote',
                       'member_demote', 'member_snapshot', 'group_subject', 'group_description')),
  channel           TEXT NOT NULL,
  chat_id           TEXT NOT NULL,
  target_message_id TEXT REFERENCES messages(message_id),
  target_native_id  TEXT,
  actor_contact_id  TEXT REFERENCES contacts(contact_id),
  actor_identifier  TEXT,
  actor_basis       TEXT NOT NULL CHECK (actor_basis IN
                      ('native_identifier', 'numeric_match', 'push_name', 'derived_claim',
                       'owner_attested', 'unknown', 'reaction_echo')),
  occurred_ms       INTEGER,
  time_certainty    TEXT NOT NULL CHECK (time_certainty IN
                      ('native', 'provider_timestamp', 'capture_time_approx', 'unknown')),
  payload_json      TEXT NOT NULL CHECK (json_valid(payload_json)),
  provenance        TEXT NOT NULL CHECK (provenance IN
                      ('native', 'recovered_text', 'verbatim_unverified', 'derived_only')),
  source_refs       TEXT NOT NULL CHECK (json_valid(source_refs))
);
CREATE INDEX message_events_target ON message_events(target_message_id, kind);
CREATE INDEX message_events_chat_time ON message_events(channel, chat_id, occurred_ms);
CREATE TABLE projector_state (
  file              TEXT PRIMARY KEY,
  lines             INTEGER NOT NULL,
  sha256            TEXT NOT NULL,
  projector_version INTEGER NOT NULL DEFAULT 1
);
CREATE VIEW messages_current AS
SELECT m.*,
  COALESCE(
    (SELECT json_extract(e.payload_json, '$.text') FROM message_events e
      WHERE e.target_message_id = m.message_id AND e.kind = 'edit'
      ORDER BY e.occurred_ms DESC, e.event_id DESC LIMIT 1),
    m.text) AS current_text,
  EXISTS (SELECT 1 FROM message_events e
           WHERE e.target_message_id = m.message_id AND e.kind = 'delete') AS deleted,
  (SELECT json_group_array(json_object('actor', r.actor_contact_id,
                                       'emoji', json_extract(r.payload_json, '$.emoji')))
     FROM message_events r
    WHERE r.target_message_id = m.message_id AND r.kind = 'reaction'
      AND json_extract(r.payload_json, '$.current') = 1) AS reactions
FROM messages m;
"""


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
