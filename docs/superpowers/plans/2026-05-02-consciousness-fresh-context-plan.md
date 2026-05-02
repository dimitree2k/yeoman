# Consciousness Fresh Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent proactive consciousness speakups from reacting to days-old messages during active chats, while preserving existing burst, lull, memory, archive, approval, policy, and daily-cap behavior.

**Architecture:** Keep the existing two-mode design: `BurstObserver` fires during active chat activity, and `LullObserver` fires after recent activity goes quiet. Add trigger-aware freshness rails inside `ConsciousnessTools`, because it is the hard boundary between the model and outbound messages. `burst` uses `burst_window_minutes`; `lull`, `cron`, and manual proactive runs use `lull_activity_window_minutes` as a conservative recent-context window. The `n=20` limit remains only a maximum count inside the fresh time window, never permission to pull messages from the last seven days as current topic.

**Tech Stack:** Python, pytest, existing Yeoman gateway consciousness modules, `InboundArchive`, `ConsciousnessTools`, `ConsciousnessAgent`.

---

## Approach Review

This does not overwrite existing features:

- Full chat history remains archived in `~/.yeoman/data/inbound/reply_context.db`.
- Memory search and learned chat taste remain available to the planner.
- `BurstObserver` still fires on ambient activity and still ignores direct bot interactions.
- `LullObserver` still fires after silence and can still start a standalone topic.
- Approval preview, quiet hours, security checks, allowed actions, and daily caps stay unchanged.
- Normal reactive responder behavior is untouched.
- No new config keys are added.

The only behavior change is in proactive consciousness context selection and quote anchoring:

- `burst` prompt windows only include messages inside the configured burst window.
- `lull`, `cron`, and manual proactive prompt windows only include messages inside the configured lull activity window.
- `n=20` remains a cap after freshness filtering, so sparse chats cannot drag days-old messages into the current-topic prompt.
- Any proactive trigger rejects stale `reply_to_message_id` anchors instead of sending a quote-reply to an old message.

This preserves the intended product rule:

- Active conversation: react only to the current topic.
- Longer silence: optionally bring up a standalone thought, without making it look like a reply to a days-old message.
- Old messages remain available through archive and memory paths, but they are no longer presented as the live chat window.

## File Structure

- Modify: `packages/gateway/yeoman_gateway/consciousness/tools.py`
  - Owns hard freshness rails for chat windows and `reply_to_message_id` validation.
- Modify: `packages/gateway/yeoman_gateway/consciousness/agent.py`
  - Adds trigger-aware prompt guidance without changing model/provider wiring.
- Modify: `tests/gateway/test_consciousness_phase4.py`
  - Adds burst regressions for fresh chat windows and stale reply anchors.
- Modify: `tests/gateway/test_consciousness_lull.py`
  - Adds lull regression for stale quote anchors after silence.
- Modify: `tests/gateway/test_consciousness_phase1.py`
  - Adds a generic proactive regression proving cron/manual prompt context no longer uses the old seven-day chat window.

Do not edit `packages/gateway/yeoman_gateway/adapters/responder_llm.py` for this fix. The observed bug is in proactive consciousness, not normal reactive replies.

Do not revert unrelated dirty worktree changes. This repository currently has existing uncommitted edits in consciousness, responder, policy, config, and tests. Work with the current files and keep this patch narrowly scoped.

---

### Task 1: Add Failing Burst Freshness Tests

**Files:**
- Modify: `tests/gateway/test_consciousness_phase4.py`

- [ ] **Step 1: Add a failing test that burst prompt context excludes old messages**

Add this test near the other burst tick tests in `tests/gateway/test_consciousness_phase4.py`:

```python
@pytest.mark.asyncio
async def test_burst_prompt_window_excludes_messages_outside_burst_window(
    tmp_path: Path,
) -> None:
    cfg = _config(burstWindowMinutes=10)
    now = datetime(2026, 5, 2, 7, 2, tzinfo=UTC)
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=None,
        now=lambda: now,
    )
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="old-options",
        participant="timo@s.whatsapp.net",
        sender_id="timo@s.whatsapp.net",
        text="31 Win Streak options trader from days ago",
        timestamp=int((now - timedelta(hours=40)).timestamp()),
        sender_name="Timo",
    )
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="fresh-magic",
        participant="robin@s.whatsapp.net",
        sender_id="robin@s.whatsapp.net",
        text="Current Magic card topic",
        timestamp=int((now - timedelta(minutes=2)).timestamp()),
        sender_name="Robin",
    )
    captured: dict[str, object] = {}

    async def planner(prompt: str) -> dict[str, object]:
        captured.update(json.loads(prompt))
        return {"silence": True, "reason": "test"}

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    result = await agent.run_once(
        trigger="burst",
        target_channel="whatsapp",
        target_chat_id="group@g.us",
    )

    assert result["status"] == "silent_pass"
    messages = captured["chat_window"]["messages"]
    assert [message["message_id"] for message in messages] == ["fresh-magic"]
    assert "old-options" not in json.dumps(captured)
```

- [ ] **Step 2: Add a failing test that burst rejects stale quote anchors**

Add this test near `test_burst_prompt_window_excludes_messages_outside_burst_window`:

```python
@pytest.mark.asyncio
async def test_burst_rejects_stale_reply_to_message_id(tmp_path: Path) -> None:
    cfg = _config(burstWindowMinutes=10)
    now = datetime(2026, 5, 2, 7, 2, tzinfo=UTC)
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=None,
        now=lambda: now,
    )
    tools.begin_run(trigger="burst")
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="old-options",
        participant="timo@s.whatsapp.net",
        sender_id="timo@s.whatsapp.net",
        text="31 Win Streak options trader from days ago",
        timestamp=int((now - timedelta(hours=40)).timestamp()),
        sender_name="Timo",
    )

    result = await tools.propose_speakup(
        channel="whatsapp",
        chat_id="group@g.us",
        message="Options-Streaks mit 100% Winrate sind selten.",
        action_type="observation",
        confidence=0.95,
        reply_to_message_id="old-options",
    )

    assert result == {"status": "rejected", "reason": "stale_reply_to_message"}
```

- [ ] **Step 3: Run burst tests and verify the new tests fail**

Run:

```bash
uv run pytest tests/gateway/test_consciousness_phase4.py -q
```

Expected:

- `test_burst_prompt_window_excludes_messages_outside_burst_window` fails because the old archived options message is still present in the prompt.
- `test_burst_rejects_stale_reply_to_message_id` fails because stale archived anchors are currently accepted.

---

### Task 2: Add Failing Lull Stale Anchor Test

**Files:**
- Modify: `tests/gateway/test_consciousness_lull.py`

- [ ] **Step 1: Add imports needed by the new lull tool-boundary test**

At the top of `tests/gateway/test_consciousness_lull.py`, keep existing imports and add these if they are missing:

```python
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.tools import ConsciousnessTools
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
```

- [ ] **Step 2: Add minimal local fakes for `ConsciousnessTools`**

Add these helpers below `_config()` if the file does not already have equivalent helpers:

```python
class _FakeSecurity:
    def check_output(self, text: str, context: dict[str, object] | None = None) -> object:
        del text, context

        class _Decision:
            action = "allow"
            reason = "fake_allow"

        class _Result:
            decision = _Decision()
            sanitized_text = None

        return _Result()


def _policy(*, group_enabled: bool = True, daily_cap: int = 1) -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "group@g.us": {
                            "spontaneity": {
                                "enabled": group_enabled,
                                "profile": "balanced",
                                "dailyCap": daily_cap,
                                "preview": "owner_dm",
                            }
                        }
                    }
                }
            },
        }
    )
```

- [ ] **Step 3: Add a failing lull stale quote-anchor test**

Add this test near the existing lull observer tests:

```python
@pytest.mark.asyncio
async def test_lull_rejects_reply_to_outside_activity_window(tmp_path: Path) -> None:
    cfg = _config(lullActivityWindowMinutes=60)
    now = datetime(2026, 5, 2, 7, 30, tzinfo=UTC)
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=None,
        now=lambda: now,
    )
    tools.begin_run(trigger="lull")
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="yesterday-options",
        participant="timo@s.whatsapp.net",
        sender_id="timo@s.whatsapp.net",
        text="31 Win Streak options trader from yesterday",
        timestamp=int((now - timedelta(days=1)).timestamp()),
        sender_name="Timo",
    )

    result = await tools.propose_speakup(
        channel="whatsapp",
        chat_id="group@g.us",
        message="Random callback: streaks like that are statistically suspicious.",
        action_type="surface_memory",
        confidence=0.95,
        reply_to_message_id="yesterday-options",
    )

    assert result == {"status": "rejected", "reason": "stale_reply_to_message"}
```

- [ ] **Step 4: Run lull tests and verify the new test fails**

Run:

```bash
uv run pytest tests/gateway/test_consciousness_lull.py -q
```

Expected:

- Existing lull observer tests still pass.
- `test_lull_rejects_reply_to_outside_activity_window` fails because stale archived anchors are currently accepted.

---

### Task 3: Add Failing Cron Freshness Test

**Files:**
- Modify: `tests/gateway/test_consciousness_phase1.py`

- [ ] **Step 1: Add a failing test that cron prompt context does not use the old seven-day window**

Add this test near the other `ConsciousnessAgent` prompt tests in `tests/gateway/test_consciousness_phase1.py`:

```python
@pytest.mark.asyncio
async def test_cron_prompt_window_uses_recent_context_not_seven_day_backfill(
    tmp_path: Path,
) -> None:
    cfg = _config(lullActivityWindowMinutes=120)
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    tools = _tools(tmp_path, config=cfg)
    tools._now = lambda: now
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="old-market-thread",
        participant="timo@s.whatsapp.net",
        sender_id="timo@s.whatsapp.net",
        text="Old market thread from days ago",
        timestamp=int((now - timedelta(days=3)).timestamp()),
        sender_name="Timo",
    )
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="fresh-thread",
        participant="robin@s.whatsapp.net",
        sender_id="robin@s.whatsapp.net",
        text="Fresh topic from this hour",
        timestamp=int((now - timedelta(minutes=30)).timestamp()),
        sender_name="Robin",
    )
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    result = await agent.run_once(trigger="cron")

    assert result["status"] == "silent_pass"
    messages = captured["chat_window"]["messages"]
    assert [message["message_id"] for message in messages] == ["fresh-thread"]
    assert "old-market-thread" not in json.dumps(captured)
```

- [ ] **Step 2: Run the cron freshness test and verify it fails**

Run:

```bash
uv run pytest tests/gateway/test_consciousness_phase1.py::test_cron_prompt_window_uses_recent_context_not_seven_day_backfill -q
```

Expected:

- FAIL because `read_chat_window()` currently backfills from up to seven days for cron/manual proactive runs.

---

### Task 4: Implement Trigger-Aware Chat Windows

**Files:**
- Modify: `packages/gateway/yeoman_gateway/consciousness/tools.py`

- [ ] **Step 1: Add private freshness helpers**

In `ConsciousnessTools`, add this method near `begin_run()`:

```python
    def current_trigger(self) -> str:
        return self._trigger

    def _chat_window_since_for_trigger(self) -> datetime:
        now = self._now()
        if self._trigger == "burst":
            return now - timedelta(
                minutes=max(1, int(self.config.consciousness.burst_window_minutes))
            )
        return now - timedelta(
            minutes=max(1, int(self.config.consciousness.lull_activity_window_minutes))
        )
```

This intentionally removes the seven-day proactive prompt window. Full historical access remains in archive/memory, but `chat_window` means fresh recent conversation.

- [ ] **Step 2: Use the helper in `read_chat_window()`**

Replace this block in `read_chat_window()`:

```python
        now = self._now()
        since = now - timedelta(days=7)
```

with:

```python
        now = self._now()
        since = self._chat_window_since_for_trigger()
```

Keep the existing `lookup_messages_in_range(...)` call unchanged.

- [ ] **Step 3: Run burst tests and verify the prompt window test passes**

Run:

```bash
uv run pytest \
  tests/gateway/test_consciousness_phase4.py::test_burst_prompt_window_excludes_messages_outside_burst_window \
  tests/gateway/test_consciousness_phase1.py::test_cron_prompt_window_uses_recent_context_not_seven_day_backfill \
  -q
```

Expected:

- PASS.

---

### Task 5: Implement Trigger-Aware Reply Anchor Freshness

**Files:**
- Modify: `packages/gateway/yeoman_gateway/consciousness/tools.py`

- [ ] **Step 1: Add timestamp parsing helpers**

In `ConsciousnessTools`, add these methods near `_chat_window_since_for_trigger()`:

```python
    @staticmethod
    def _message_observed_at(row: dict[str, object]) -> datetime | None:
        raw_timestamp = row.get("timestamp")
        if raw_timestamp is not None:
            try:
                return datetime.fromtimestamp(float(raw_timestamp), UTC)
            except (TypeError, ValueError, OSError):
                pass
        raw_created_at = str(row.get("created_at") or "").strip()
        if not raw_created_at:
            return None
        try:
            parsed = datetime.fromisoformat(raw_created_at)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _reply_to_is_fresh_for_trigger(self, row: dict[str, object]) -> bool:
        observed_at = self._message_observed_at(row)
        if observed_at is None:
            return False
        return observed_at >= self._chat_window_since_for_trigger()
```

- [ ] **Step 2: Reject stale anchors in `propose_speakup()`**

Find this block in `propose_speakup()`:

```python
        validated_reply_to: str | None = None
        if reply_to_message_id:
            candidate = str(reply_to_message_id).strip()
            if candidate:
                row = self.inbound_archive.lookup_message(
                    eligible.channel, eligible.chat_id, candidate
                )
                if row is not None:
                    validated_reply_to = candidate
```

Replace it with:

```python
        validated_reply_to: str | None = None
        if reply_to_message_id:
            candidate = str(reply_to_message_id).strip()
            if candidate:
                row = self.inbound_archive.lookup_message(
                    eligible.channel, eligible.chat_id, candidate
                )
                if row is not None:
                    if not self._reply_to_is_fresh_for_trigger(row):
                        return {
                            "status": "rejected",
                            "reason": "stale_reply_to_message",
                        }
                    validated_reply_to = candidate
```

This preserves existing behavior for fake or missing IDs: missing IDs are still stripped to `None`, matching `test_reply_to_message_id_validates_against_archive`. Existing-but-stale anchors are rejected for all proactive triggers, including `cron`, because quote-replying to a stale message makes the old thread look current.

- [ ] **Step 3: Run focused freshness tests**

Run:

```bash
uv run pytest \
  tests/gateway/test_consciousness_phase4.py::test_burst_rejects_stale_reply_to_message_id \
  tests/gateway/test_consciousness_lull.py::test_lull_rejects_reply_to_outside_activity_window \
  -q
```

Expected:

- PASS.

---

### Task 6: Add Trigger Guidance To The Planner Prompt

**Files:**
- Modify: `packages/gateway/yeoman_gateway/consciousness/agent.py`
- Modify: `tests/gateway/test_consciousness_phase4.py`

- [ ] **Step 1: Add a burst prompt assertion**

Extend `test_burst_prompt_window_excludes_messages_outside_burst_window()` with:

```python
    assert captured["trigger"] == "burst"
    assert any("current burst window" in rule for rule in captured["golden_rules"])
```

- [ ] **Step 2: Add trigger and trigger-specific rules to the prompt**

In `ConsciousnessAgent._build_prompt()`, add a small trigger-specific list before the final `return json.dumps(...)`:

```python
        trigger_rules: list[str] = []
        trigger = self._tools.current_trigger()
        if trigger == "burst":
            trigger_rules.append(
                "This is an active burst. React only to the current burst window. "
                "If there is no useful current-topic contribution, stay silent."
            )
        elif trigger == "lull":
            trigger_rules.append(
                "This is a lull after recent activity went quiet. You may start a "
                "standalone thought, callback, or fun fact, but do not pretend an old "
                "message is the current thread."
            )
```

Then add `"trigger": trigger` to the JSON payload and append `trigger_rules` to `golden_rules`:

```python
                "trigger": trigger,
                "golden_rules": [
                    *trigger_rules,
                    "Do NOT echo, paraphrase, or restate any message in chat_window. "
                    "If your draft shares a 4-word run with any existing message, rewrite "
                    "it from a different angle or stay silent.",
```

- [ ] **Step 3: Update fake tool classes if needed**

If any test fake used with `ConsciousnessAgent` does not expose `current_trigger()`, add this method to the fake:

```python
    def current_trigger(self) -> str:
        return getattr(self, "trigger", "cron")
```

Known likely file:

- `tests/gateway/test_consciousness_learned_taste.py`

- [ ] **Step 4: Run focused prompt tests**

Run:

```bash
uv run pytest \
  tests/gateway/test_consciousness_phase4.py::test_burst_prompt_window_excludes_messages_outside_burst_window \
  tests/gateway/test_consciousness_learned_taste.py \
  -q
```

Expected:

- PASS.

---

### Task 7: Regression Sweep And Runtime Handoff

**Files:**
- No new source files.

- [ ] **Step 1: Run focused consciousness tests**

Run:

```bash
uv run pytest \
  tests/gateway/test_consciousness_phase1.py \
  tests/gateway/test_consciousness_phase2.py \
  tests/gateway/test_consciousness_phase4.py \
  tests/gateway/test_consciousness_lull.py \
  tests/gateway/test_consciousness_learned_taste.py \
  -q
```

Expected:

- PASS.

- [ ] **Step 2: Run lint on touched files**

Run:

```bash
uv run ruff check \
  packages/gateway/yeoman_gateway/consciousness/agent.py \
  packages/gateway/yeoman_gateway/consciousness/tools.py \
  tests/gateway/test_consciousness_phase1.py \
  tests/gateway/test_consciousness_phase4.py \
  tests/gateway/test_consciousness_lull.py \
  tests/gateway/test_consciousness_learned_taste.py
```

Expected:

- PASS.

- [ ] **Step 3: Inspect git diff for scope creep**

Run:

```bash
git diff -- \
  packages/gateway/yeoman_gateway/consciousness/agent.py \
  packages/gateway/yeoman_gateway/consciousness/tools.py \
  tests/gateway/test_consciousness_phase1.py \
  tests/gateway/test_consciousness_phase4.py \
  tests/gateway/test_consciousness_lull.py \
  tests/gateway/test_consciousness_learned_taste.py
```

Expected:

- Diff is limited to trigger-aware chat windows, stale `reply_to_message_id` rejection, prompt guidance, and tests.
- No responder, policy, config schema, memory store, or bridge changes.

- [ ] **Step 4: Restart gateway after Python runtime changes**

Run:

```bash
yeoman gateway restart
yeoman gateway status
tail -n 60 /home/dm/.yeoman/var/logs/gateway.log
```

Expected:

- Gateway restarts cleanly.
- Logs show normal startup.
- No traceback from `ConsciousnessAgent`, `ConsciousnessTools`, `BurstObserver`, or `LullObserver`.

- [ ] **Step 5: Commit only the narrow fix if requested**

If committing, stage only the files changed for this fix:

```bash
git add \
  packages/gateway/yeoman_gateway/consciousness/agent.py \
  packages/gateway/yeoman_gateway/consciousness/tools.py \
  tests/gateway/test_consciousness_phase1.py \
  tests/gateway/test_consciousness_phase4.py \
  tests/gateway/test_consciousness_lull.py \
  tests/gateway/test_consciousness_learned_taste.py
git commit -m "fix(consciousness): prevent stale active-thread speakups"
```

Do not stage unrelated dirty files.
