# Changelog

## v0.5.0 (2026-03-12)

### Features

- **Contacts CRM**: Full contacts subsystem with SQLite store, CRUD tool, identity resolution in pipeline, roster injection into LLM context, @Name mention resolution in outbound messages, and one-time memory backfill linking nodes to contact_id
- **Voice improvements**: Recording indicator (microphone icon) for WhatsApp voice messages, voice send result reporting to source chat, Fish Audio TTS provider with `/voice` command, `senderName` field support
- **Telemetry**: Langfuse tracing module with REST API client, agent loop instrumentation, lifecycle init/shutdown
- **Skills**: Bundle browser skill

### Fixes

- WhatsApp mention tokens: strip trailing punctuation for reliable matching
- Contacts: wire roster injection into responder, tag new memory nodes
- Doctor: detect CalDAV interpreter mismatch
- Tracing: wire lifecycle init/shutdown, add try/finally for spans

### Docs

- WhatsApp bridge health monitoring design spec
- Standardize CLI runtime entrypoints

### Build

- Update Python dependencies to latest versions

## v0.4.0

_Initial tagged release._
