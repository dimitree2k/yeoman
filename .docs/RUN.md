# Run nanobot (Step by Step)

This guide shows the fastest way to run this repository locally.

## 1. Prerequisites

- Python `3.14+`
- Node.js `18+` (only needed for WhatsApp bridge)
- `uv` (recommended) or `pip`

## 2. Clone and install

```bash
git clone https://github.com/dimitree2k/nanobot.git
cd nanobot
uv sync
```

If you prefer `pip`:

```bash
pip install -e .
```

## 3. Initialize config and workspace

```bash
nanobot onboard
```

This creates:

- `~/.nanobot/config.json`
- `~/.nanobot/workspace`

## 4. Add API key

Edit `~/.nanobot/config.json`:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  }
}
```

## 5. Quick test (single message)

```bash
nanobot agent -m "What is 2+2?"
```

## 6. Interactive CLI mode

```bash
nanobot agent
```

## 7. Run gateway (chat channels)

```bash
nanobot gateway
```

Check status anytime:

```bash
nanobot status
nanobot channels status
```

## 8. Optional: WhatsApp bridge

Terminal 1:

```bash
nanobot channels login
```

Terminal 2:

```bash
nanobot gateway
```

## 9. Optional: Run with Docker

```bash
docker build -t nanobot .
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot onboard
docker run -v ~/.nanobot:/root/.nanobot --rm nanobot agent -m "Hello!"
docker run -v ~/.nanobot:/root/.nanobot -p 18790:18790 nanobot gateway
```

## 10. Optional: Linux exec isolation

If you enable `tools.exec.isolation.enabled`, create this file first:

`~/.config/nanobot/mount-allowlist.json`

```json
{
  "allowedRoots": ["~/.nanobot/workspace"],
  "blockedHostPatterns": [".ssh", ".aws", ".env", "id_rsa", "id_ed25519"]
}
```
