## Policy (`~/.nanobot/policy.json`)

`policy.json` is the **access/reply/tool/persona** policy layer for chat channels (currently **Telegram** and **WhatsApp**).

It answers, per chat:
- **Who can talk** (message acceptance)
- **When to reply** (response gating)
- **Which tools are allowed** (tool ACL)
- **Which persona file** is used
- **Voice behavior** (wake phrases + voice replies)

Policy is **hot-reloaded** by default while the gateway is running.

Important: policy is only applied to channels that are **enabled** in `~/.nanobot/config.json`. If a channel is disabled, the gateway treats messages as `policy_not_applied` (accept + reply, tools allowed).

### Location

```bash
nanobot policy path
```

Default location: `~/.nanobot/policy.json`

### Merge Precedence

For a given message, policy is resolved in this order:

`defaults -> channels.<channel>.default -> channels.<channel>.chats.<chat_id>`

### Full Schema (all options)

```json
{
  "version": 2,
  "runtime": {
    "reloadOnChange": true,
    "reloadCheckIntervalSeconds": 1.0,
    "featureFlags": {},
    "adminCommandRateLimitPerMinute": 30,
    "adminRequireConfirmForRisky": false
  },
  "owners": {
    "telegram": ["453897507"],
    "whatsapp": ["+491757070305", "34596062240904@lid"]
  },
  "defaults": {
    "whoCanTalk": { "mode": "everyone", "senders": [] },
    "whenToReply": { "mode": "all", "senders": [] },
    "blockedSenders": { "senders": [] },
    "allowedTools": {
      "mode": "allowlist",
      "tools": ["list_dir", "read_file", "web_search", "web_fetch"],
      "deny": []
    },
    "personaFile": null,
    "voice": {
      "input": { "wakePhrases": [] },
      "output": {
        "mode": "text",
        "ttsRoute": "tts.speak",
        "voice": "alloy",
        "format": "opus",
        "maxSentences": 2,
        "maxChars": 150
      }
    }
  },
  "channels": {
    "telegram": {
      "default": {
        "whoCanTalk": { "mode": "everyone", "senders": [] },
        "whenToReply": { "mode": "mention_only", "senders": [] },
        "allowedTools": { "mode": "all", "tools": [], "deny": ["exec", "spawn"] },
        "personaFile": null
      },
      "chats": {
        "-1001234567890": {
          "whoCanTalk": { "mode": "allowlist", "senders": ["453897507", "@my_username"] },
          "whenToReply": { "mode": "allowed_senders", "senders": ["453897507"] },
          "blockedSenders": { "senders": ["+491234567890"] },
          "allowedTools": { "mode": "allowlist", "tools": ["read_file", "web_fetch"], "deny": ["exec", "spawn"] },
          "personaFile": "memory/personas/professional.md",
          "comment": "My private admin group"
        }
      }
    },
    "whatsapp": {
      "default": {
        "whenToReply": { "mode": "mention_only", "senders": [] },
        "comment": "Default for WA"
      },
      "chats": {
        "120363407040317023@g.us": {
          "whenToReply": { "mode": "mention_only", "senders": [] },
          "comment": "Family chat"
        }
      }
    }
  }
}
```

Notes:
- The file is parsed **strictly**: unknown keys cause validation to fail.
- If hot-reload fails, the gateway keeps the **previous** working policy and logs an error.
- `reloadCheckIntervalSeconds` has a minimum of `0.1`.
- `featureFlags` is reserved for optional future rule families.
- `adminCommandRateLimitPerMinute` limits owner-DM deterministic policy command throughput.
- `adminRequireConfirmForRisky` requires `--confirm` for risky commands (for example `rollback`).
- `comment` is optional everywhere a chat override exists; it is ignored by the policy engine and meant for humans.
- Policy is written as UTF-8 JSON. Older versions may have escaped emoji sequences like `\\ud83d...`; both forms load fine, but re-saving with current versions keeps the readable characters.

### Modes

#### `whoCanTalk.mode`
- `everyone`: accept messages from anyone in the chat.
- `allowlist`: accept only if sender matches `senders`.
- `owner_only`: accept only if sender matches `owners.<channel>`.

#### `whenToReply.mode`
- `all`: reply to every accepted message.
- `off`: never reply (useful for “listen only”).
- `mention_only`:
  - DMs: reply (mention not required)
  - Groups: reply only if the message mentions the bot or replies to the bot
- `allowed_senders`: reply only if sender matches `senders`.
- `owner_only`: reply only if sender matches `owners.<channel>`.

#### `allowedTools.mode`
- `all`: allow all registered tools, minus anything in `deny`.
- `allowlist`: allow only tools listed in `tools`, minus anything in `deny`.

Tool safety notes:
- If `exec` is denied, `spawn` is automatically denied as well (guardrail).

#### `blockedSenders`
- `blockedSenders.senders`: deny-list evaluated before `whoCanTalk`.
- Useful for \"everyone except X\" group behavior without complex allowlist rewrites.

#### `personaFile`
- `null`: no persona override.
- String path: relative to your workspace (usually `~/.nanobot/workspace`), e.g. `memory/personas/poe-brash.md`.

#### `voice`

Voice settings are **per chat** and are evaluated alongside `whenToReply`.

**`voice.input.wakePhrases`**
- Used only for **WhatsApp groups** under `whenToReply.mode=mention_only`.
- A group **voice note** can trigger a reply if its transcript contains any wake phrase.
- Wake phrase matching is deterministic:
  - lowercase
  - non-alphanumeric → spaces
  - collapse spaces
  - match as whole-token substring (so `"nano"` does not match `"nanobot"` unless you list both)
- Wake phrases do **not** make the bot respond to normal text messages in mention-only groups (only voice notes).

**`voice.output.mode`**
- `text`: always send text replies (default).
- `in_kind`: if the inbound message is a voice note, reply with a voice note; otherwise reply with text.
- `always`: always reply with a voice note.
- `off`: disable voice output (text replies only).

**`voice.output` other fields**
- `ttsRoute`: model route for TTS (default `tts.speak`, supports `whatsapp.tts.speak` overrides).
- `voice`: provider-specific voice selector (default `alloy` for OpenAI; use a **voice ID** for ElevenLabs, or set `providers.elevenlabs.voiceId` as default).
- `format`: for WhatsApp voice notes, use `opus`.
- `maxSentences` / `maxChars`: strict guardrails before TTS; if TTS fails or audio is too large, nanobot falls back to text.

### Sender Matching (identity normalization)

Nanobot normalizes sender IDs so you can write policy entries in multiple common forms.

**Telegram**
- Numeric user IDs (e.g. `"453897507"`) work best.
- Usernames may also match (e.g. `"@my_username"` / `"my_username"`).

**WhatsApp**
- Phone numbers with or without `+` (e.g. `"+491757070305"` or `"491757070305"`).
- LIDs (WhatsApp “linked device” identifiers), often seen as `"...@lid"`.
- Full JIDs (e.g. `"491722704433:10@s.whatsapp.net"`), which normalize to the base number as well.

### Tool Names (for `allowedTools`)

These are the current top-level tools that can appear in policy:
- `read_file`, `write_file`, `edit_file`, `list_dir`
- `exec`
- `web_search`, `web_fetch`
- `message`
- `spawn`
- `cron`

### Examples

#### 1) “Silent” group (accept messages, never reply)

```json
{
  "channels": {
    "telegram": {
      "chats": {
        "-1001234567890": {
          "whoCanTalk": { "mode": "everyone", "senders": [] },
          "whenToReply": { "mode": "off", "senders": [] }
        }
      }
    }
  }
}
```

#### 2) WhatsApp group: reply only on mention/reply

```json
{
  "channels": {
    "whatsapp": {
      "default": { "whenToReply": { "mode": "mention_only", "senders": [] } }
    }
  }
}
```

#### 3) Owner-only “admin” chat with read-only tools

```json
{
  "defaults": {
    "allowedTools": { "mode": "all", "tools": [], "deny": ["exec", "spawn", "write_file", "edit_file"] }
  },
  "channels": {
    "telegram": {
      "chats": {
        "-1001234567890": {
          "whoCanTalk": { "mode": "owner_only", "senders": [] },
          "whenToReply": { "mode": "owner_only", "senders": [] },
          "allowedTools": { "mode": "allowlist", "tools": ["read_file", "web_fetch"], "deny": [] }
        }
      }
    }
  }
}
```

#### 4) Respond only to a sender allowlist (but let anyone “talk”)

This can be useful when you want the bot to ignore most people without rejecting the chat entirely:

```json
{
  "channels": {
    "telegram": {
      "chats": {
        "-1001234567890": {
          "whoCanTalk": { "mode": "everyone", "senders": [] },
          "whenToReply": { "mode": "allowed_senders", "senders": ["453897507"] }
        }
      }
    }
  }
}
```

### Debugging

To see the merged policy + decision that the gateway would apply:

```bash
nanobot policy explain \
  --channel telegram \
  --chat -1001234567890 \
  --sender "453897507|@my_username" \
  --group \
  --mentioned
```

### Deterministic Policy Commands (DM + CLI)

- Owner DM syntax is slash-first: `/policy ...`.
- Canonical CLI entrypoint uses the same shared parser/backend:

```bash
nanobot policy cmd "/policy list-groups"
nanobot policy cmd "/policy set-when 120363407040317023@g.us mention_only --dry-run"
nanobot policy cmd "/policy rollback <change_id> --confirm"
```

- Non-slash text (`policy ...`) is not deterministic command routing.
- Use `/policy history` to inspect recent audited mutations.

### Annotating WhatsApp Group Names

If your `channels.whatsapp.chats` contains cryptic `*@g.us` IDs, you can auto-fill human-readable names into `comment`
using the running WhatsApp bridge:

```bash
nanobot policy annotate-whatsapp-comments
```

Use `--overwrite` to replace existing comments, or `--dry-run` to preview changes.
