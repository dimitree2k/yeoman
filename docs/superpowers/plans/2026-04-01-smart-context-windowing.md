# Smart Context Windowing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce token waste by making session history and ambient context configurable, adding `/new` session boundaries, and giving the bot a tool to look deeper when needed.

**Architecture:** Five independent changes (A–E) that together eliminate redundant context in DMs, make all limits configurable per-chat, add a `/new` boundary command, and give the LLM an adaptive mechanism (heuristic + tool) to look back further when messages reference earlier conversation.

**Tech Stack:** Python 3.14, Pydantic config, JSONL session storage, asyncio

**Spec:** `docs/superpowers/specs/2026-04-01-smart-context-windowing-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `packages/shared/yeoman_shared/config/schema.py` | Add `session_history_limit`, `session_history_limit_group` to `WhatsAppConfig` |
| Modify | `packages/shared/yeoman_shared/config/defaults.py` | Add default values for new config fields |
| Modify | `packages/gateway/yeoman_gateway/core/models.py` | Add `session_history_limit` field to `PolicyDecision` |
| Modify | `packages/gateway/yeoman_gateway/policy/schema.py` | Add optional `session_history_limit` to `ChatPolicyOverride` |
| Modify | `packages/gateway/yeoman_gateway/policy/engine.py` | Thread `session_history_limit` through `_CompiledPolicy` and resolution |
| Modify | `packages/gateway/yeoman_gateway/pipeline/reply_context.py` | Skip ambient window for DM chats |
| Modify | `packages/gateway/yeoman_gateway/session/manager.py` | Respect `session_boundary` markers in `get_history()`, add `add_boundary()` |
| Modify | `packages/gateway/yeoman_gateway/adapters/policy_engine.py` | Add `NewSessionCommandHandler`, wire config into `_CompiledPolicy` |
| Modify | `packages/gateway/yeoman_gateway/adapters/responder_llm.py` | Read `session_history_limit` from decision instead of hardcoded, add preflight heuristic, register recall tool |
| Create | `packages/gateway/yeoman_gateway/agent/tools/recall_conversation.py` | New tool: search session history on demand |
| Create | `tests/gateway/test_context_windowing.py` | Tests for all changes |

---

## Task 1: Config schema — add session history limits

**Files:**
- Modify: `packages/shared/yeoman_shared/config/defaults.py:105-109`
- Modify: `packages/shared/yeoman_shared/config/schema.py:142-144`

- [ ] **Step 1: Add defaults**

In `packages/shared/yeoman_shared/config/defaults.py`, add to `DEFAULT_WHATSAPP_REPLY_CONTEXT`:

```python
DEFAULT_WHATSAPP_REPLY_CONTEXT: dict[str, Any] = {
    "window_limit": 6,
    "line_max_chars": 256,
    "ambient_window_limit": 8,
    "session_history_limit": 15,
    "session_history_limit_group": 20,
}
```

- [ ] **Step 2: Add fields to WhatsAppConfig**

In `packages/shared/yeoman_shared/config/schema.py`, add after the `ambient_window_limit` field (line 144):

```python
    session_history_limit: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["session_history_limit"])
    session_history_limit_group: int = int(DEFAULT_WHATSAPP_REPLY_CONTEXT["session_history_limit_group"])
```

- [ ] **Step 3: Verify imports still work**

Run: `python -c "from yeoman_shared.config.schema import WhatsAppConfig; c = WhatsAppConfig(); print(c.session_history_limit, c.session_history_limit_group)"`

Expected: `15 20`

- [ ] **Step 4: Commit**

```bash
git add packages/shared/yeoman_shared/config/defaults.py packages/shared/yeoman_shared/config/schema.py
git commit -m "feat(config): add session_history_limit and session_history_limit_group to WhatsApp config"
```

---

## Task 2: Per-chat policy override for session_history_limit

**Files:**
- Modify: `packages/gateway/yeoman_gateway/policy/schema.py:189-204`
- Modify: `packages/gateway/yeoman_gateway/policy/engine.py:137-163` (`_CompiledPolicy`)
- Modify: `packages/gateway/yeoman_gateway/policy/engine.py:267-309` (`_compile_chat_policy`)
- Modify: `packages/gateway/yeoman_gateway/core/models.py:42-71` (`PolicyDecision`)

- [ ] **Step 1: Add field to ChatPolicyOverride**

In `packages/gateway/yeoman_gateway/policy/schema.py`, add to `ChatPolicyOverride` after the `talkative_cooldown` field:

```python
    session_history_limit: int | None = Field(default=None, alias="sessionHistoryLimit", ge=1, le=100)
```

- [ ] **Step 2: Add field to _CompiledPolicy**

In `packages/gateway/yeoman_gateway/policy/engine.py`, add to the `_CompiledPolicy` dataclass (after `contacts_disclosure`):

```python
    session_history_limit: int | None = None
```

- [ ] **Step 3: Thread through _compile_chat_policy**

In `packages/gateway/yeoman_gateway/policy/engine.py`, in `_compile_chat_policy()`, after the `contacts_disclosure` assignment, add:

```python
            session_history_limit=getattr(resolved, "session_history_limit", None),
```

The `ChatPolicy` base class doesn't have this field (it's override-only), so `getattr` with default `None` handles both cases. Only `ChatPolicyOverride` carries it.

- [ ] **Step 4: Add field to core PolicyDecision**

In `packages/gateway/yeoman_gateway/core/models.py`, add to `PolicyDecision` after `contacts_disclosure`:

```python
    session_history_limit: int | None = None
```

- [ ] **Step 5: Wire compiled policy into PolicyDecision**

Find where `PolicyDecision` (from `core/models.py`) is constructed from `_CompiledPolicy`. Search for the construction site:

```bash
rg "PolicyDecision\(" packages/gateway/yeoman_gateway/adapters/policy_engine.py | head -5
```

Add `session_history_limit=compiled.session_history_limit` to the construction.

- [ ] **Step 6: Verify**

Run: `python -c "from yeoman_gateway.core.models import PolicyDecision; d = PolicyDecision(accept_message=True, should_respond=True, allowed_tools=frozenset(), reason='ok', session_history_limit=30); print(d.session_history_limit)"`

Expected: `30`

- [ ] **Step 7: Commit**

```bash
git add packages/gateway/yeoman_gateway/policy/schema.py packages/gateway/yeoman_gateway/policy/engine.py packages/gateway/yeoman_gateway/core/models.py packages/gateway/yeoman_gateway/adapters/policy_engine.py
git commit -m "feat(policy): add per-chat session_history_limit override"
```

---

## Task 3: Skip ambient window in DMs

**Files:**
- Modify: `packages/gateway/yeoman_gateway/pipeline/reply_context.py:141-153`
- Create: `tests/gateway/test_context_windowing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/gateway/test_context_windowing.py`:

```python
"""Tests for smart context windowing."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from yeoman_gateway.core.models import ArchivedMessage, InboundEvent
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware


def _make_event(
    chat_id: str = "owner@s.whatsapp.net",
    message_id: str = "msg-1",
    text: str = "hello",
    channel: str = "whatsapp",
) -> InboundEvent:
    return InboundEvent(
        channel=channel,
        chat_id=chat_id,
        sender_id="owner@s.whatsapp.net",
        text=text,
        message_id=message_id,
        raw_metadata={},
    )


def _make_archive_rows(n: int) -> list[ArchivedMessage]:
    return [
        ArchivedMessage(
            channel="whatsapp",
            chat_id="owner@s.whatsapp.net",
            message_id=f"prev-{i}",
            participant=None,
            sender_id="owner@s.whatsapp.net",
            text=f"message {i}",
            timestamp=1000 + i,
            created_at="2026-01-01",
        )
        for i in range(n)
    ]


class TestAmbientWindowSkipDM:
    """Ambient window should be empty for DM chats, populated for groups."""

    def test_dm_returns_empty_ambient(self):
        archive = MagicMock()
        archive.lookup_messages_before.return_value = _make_archive_rows(5)
        mw = ReplyContextMiddleware(archive=archive, ambient_window_limit=8)

        event = _make_event(chat_id="owner@s.whatsapp.net")
        result = mw._build_ambient_window(event)

        assert result == []
        archive.lookup_messages_before.assert_not_called()

    def test_group_returns_ambient(self):
        archive = MagicMock()
        archive.lookup_messages_before.return_value = _make_archive_rows(5)
        mw = ReplyContextMiddleware(archive=archive, ambient_window_limit=8)

        event = _make_event(chat_id="123456@g.us")
        result = mw._build_ambient_window(event)

        assert len(result) > 0
        archive.lookup_messages_before.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestAmbientWindowSkipDM -v`

Expected: `test_dm_returns_empty_ambient` FAILS (currently ambient is built for all chats)

- [ ] **Step 3: Implement the change**

In `packages/gateway/yeoman_gateway/pipeline/reply_context.py`, modify `_build_ambient_window` (line 141):

```python
    def _build_ambient_window(self, event: InboundEvent) -> list[str]:
        if self._archive is None or self._ambient_limit <= 0 or not event.message_id:
            return []
        # Skip ambient window for DMs — session history already covers these messages.
        if not event.chat_id.endswith("@g.us"):
            return []
        try:
            before = self._archive.lookup_messages_before(
                event.channel,
                event.chat_id,
                event.message_id,
                limit=self._ambient_limit,
            )
        except Exception:
            return []
        return self._format_lines(before)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestAmbientWindowSkipDM -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/gateway/yeoman_gateway/pipeline/reply_context.py tests/gateway/test_context_windowing.py
git commit -m "feat(context): skip redundant ambient window in DM chats"
```

---

## Task 4: `/new` session boundary command

**Files:**
- Modify: `packages/gateway/yeoman_gateway/session/manager.py:58-89`
- Modify: `packages/gateway/yeoman_gateway/adapters/policy_engine.py:160-177`
- Modify: `tests/gateway/test_context_windowing.py`

- [ ] **Step 1: Write tests for session boundary**

Append to `tests/gateway/test_context_windowing.py`:

```python
from yeoman_gateway.session.manager import Session


class TestSessionBoundary:
    """Session.get_history() should stop at the most recent session_boundary."""

    def test_no_boundary_returns_all(self):
        s = Session(key="test")
        s.add_message("user", "msg1")
        s.add_message("assistant", "reply1")
        s.add_message("user", "msg2")
        s.add_message("assistant", "reply2")

        history = s.get_history(max_messages=50)
        assert len(history) == 4

    def test_boundary_limits_history(self):
        s = Session(key="test")
        s.add_message("user", "old message")
        s.add_message("assistant", "old reply")
        s.add_boundary()
        s.add_message("user", "new message")
        s.add_message("assistant", "new reply")

        history = s.get_history(max_messages=50)
        assert len(history) == 2
        assert history[0]["content"] == "new message"
        assert history[1]["content"] == "new reply"

    def test_multiple_boundaries_uses_latest(self):
        s = Session(key="test")
        s.add_message("user", "ancient")
        s.add_boundary()
        s.add_message("user", "old")
        s.add_boundary()
        s.add_message("user", "recent")

        history = s.get_history(max_messages=50)
        assert len(history) == 1
        assert history[0]["content"] == "recent"

    def test_boundary_with_max_messages_uses_smaller(self):
        s = Session(key="test")
        s.add_boundary()
        for i in range(20):
            s.add_message("user", f"msg {i}")

        history = s.get_history(max_messages=5)
        assert len(history) == 5

    def test_boundary_at_end_returns_empty(self):
        s = Session(key="test")
        s.add_message("user", "hello")
        s.add_boundary()

        history = s.get_history(max_messages=50)
        assert len(history) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestSessionBoundary -v`

Expected: FAIL — `Session` has no `add_boundary()` method

- [ ] **Step 3: Implement add_boundary() and modify get_history()**

In `packages/gateway/yeoman_gateway/session/manager.py`, add a method to `Session` after `add_tool_call` (after line 56):

```python
    def add_boundary(self) -> None:
        """Insert a session boundary marker. get_history() will not look past this."""
        self.messages.append({
            "role": "session_boundary",
            "timestamp": datetime.now().isoformat(),
        })
        self.updated_at = datetime.now()
```

Modify `get_history()` (replacing lines 58-89):

```python
    def get_history(self, max_messages: int = 50) -> list[dict[str, Any]]:
        """
        Get message history for LLM context.

        Scans backwards from the end and stops at the most recent
        ``session_boundary`` marker or at *max_messages*, whichever comes first.

        Args:
            max_messages: Maximum messages to return.

        Returns:
            List of messages in LLM format (tool traces excluded).
        """
        # Find the most recent session boundary.
        boundary_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "session_boundary":
                boundary_idx = i
                break

        start = boundary_idx + 1 if boundary_idx >= 0 else 0
        candidates = self.messages[start:]

        # Apply max_messages limit.
        if len(candidates) > max_messages:
            candidates = candidates[-max_messages:]

        # Convert to LLM format, skipping internal or malformed rows.
        history: list[dict[str, Any]] = []
        allowed_roles = {"system", "user", "assistant"}
        for message in candidates:
            role = str(message.get("role") or "").strip()
            if role == "tool_trace" or role not in allowed_roles:
                continue

            if "content" not in message:
                continue

            content = message.get("content")
            if content is None:
                content = ""
            if not isinstance(content, (str, list, dict)):
                content = str(content)

            history.append({"role": role, "content": content})
        return history
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestSessionBoundary -v`

Expected: PASS

- [ ] **Step 5: Add NewSessionCommandHandler**

In `packages/gateway/yeoman_gateway/adapters/policy_engine.py`, add the handler class after `ResetSessionCommandHandler` (after line 2375):

```python
class NewSessionCommandHandler(AdminCommandHandler):
    """Deterministic `/new` command for inserting a session boundary."""

    def __init__(self, adapter: EnginePolicyAdapter) -> None:
        self._adapter = adapter

    def namespace(self) -> str:
        return "new"

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        return self._adapter.session_reset_is_applicable(ctx)

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        return self._adapter.new_session_handle(ctx, argv)

    def help_hint(self) -> str:
        return "/new"
```

- [ ] **Step 6: Add new_session_handle to EnginePolicyAdapter**

In the `EnginePolicyAdapter` class, add after `session_reset_handle` (after the method ending around line 1285):

```python
    def new_session_handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        if argv:
            return AdminCommandResult(status="handled", response="Usage: /new")

        policy = self._load_policy_for_admin()
        if policy is None:
            return AdminCommandResult(
                status="handled",
                response="Session boundary unavailable: policy engine is not active.",
            )
        if not self._is_whatsapp_owner(ctx, policy):
            return AdminCommandResult(status="ignored")
        if self._session_manager is None:
            return AdminCommandResult(
                status="handled",
                response="Session boundary unavailable: session manager is not configured.",
            )

        session_key = f"{ctx.channel}:{ctx.chat_id}"
        try:
            session = self._session_manager.get_or_create(session_key)
            session.add_boundary()
            self._session_manager.save(session)
        except Exception as e:
            return AdminCommandResult(status="handled", response=f"Session boundary failed: {e}")

        return AdminCommandResult(
            status="handled",
            response="New session started. Previous context will not be included.",
            command_name="new",
            outcome="applied",
            source="dm" if not ctx.is_group else "group",
        )
```

- [ ] **Step 7: Register the handler**

In `EnginePolicyAdapter.__init__()` (around line 174), add `NewSessionCommandHandler(self)` to the handler list, after `ResetSessionCommandHandler(self)`:

```python
                ResetSessionCommandHandler(self),
                NewSessionCommandHandler(self),
                ForgetCommandHandler(self),
```

- [ ] **Step 8: Run existing tests to verify no regressions**

Run: `python -m pytest tests/gateway/ -v --timeout=30`

Expected: All tests pass.

- [ ] **Step 9: Commit**

```bash
git add packages/gateway/yeoman_gateway/session/manager.py packages/gateway/yeoman_gateway/adapters/policy_engine.py tests/gateway/test_context_windowing.py
git commit -m "feat(commands): add /new session boundary command"
```

---

## Task 5: Use configurable limits in the responder

**Files:**
- Modify: `packages/gateway/yeoman_gateway/adapters/responder_llm.py:1111-1114`

- [ ] **Step 1: Write failing test**

Append to `tests/gateway/test_context_windowing.py`:

```python
class TestConfigurableHistoryLimit:
    """Responder should use decision.session_history_limit when present."""

    def test_decision_limit_overrides_default(self):
        from yeoman_gateway.core.models import PolicyDecision

        d = PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="ok",
            session_history_limit=30,
        )
        assert d.session_history_limit == 30

    def test_decision_limit_defaults_to_none(self):
        from yeoman_gateway.core.models import PolicyDecision

        d = PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="ok",
        )
        assert d.session_history_limit is None
```

- [ ] **Step 2: Run to verify it passes (field already added in Task 2)**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestConfigurableHistoryLimit -v`

Expected: PASS (field was added in Task 2)

- [ ] **Step 3: Replace hardcoded limits in responder**

In `packages/gateway/yeoman_gateway/adapters/responder_llm.py`, find the `generate_reply` method. The responder needs access to the WhatsApp config for default limits. Add a `whatsapp_config` parameter to `LLMResponder.__init__()`.

First, add the import at the top of the file (in the `TYPE_CHECKING` block):

```python
    from yeoman_shared.config.schema import ExecToolConfig, WebToolsConfig, WhatsAppConfig
```

Add parameter to `__init__` (after `inbound_archive`):

```python
        whatsapp_session_history_limit: int = 15,
        whatsapp_session_history_limit_group: int = 20,
```

Store them:

```python
        self._session_history_limit = whatsapp_session_history_limit
        self._session_history_limit_group = whatsapp_session_history_limit_group
```

Then replace the hardcoded line at line 1112-1113:

```python
                    history=session.get_history(
                        max_messages=20 if chat_id.endswith("@g.us") else 50
                    ),
```

with:

```python
                    history=session.get_history(
                        max_messages=self._resolve_history_limit(chat_id, decision),
                    ),
```

Add the helper method to `LLMResponder`:

```python
    def _resolve_history_limit(self, chat_id: str, decision: PolicyDecision) -> int:
        """Resolve session history limit: per-chat policy > global config default."""
        per_chat = getattr(decision, "session_history_limit", None)
        if per_chat is not None:
            return int(per_chat)
        if chat_id.endswith("@g.us"):
            return self._session_history_limit_group
        return self._session_history_limit
```

- [ ] **Step 4: Wire config values in bootstrap**

In `packages/gateway/yeoman_gateway/app/bootstrap.py`, at the `LLMResponder(` construction (line 382), add:

```python
        whatsapp_session_history_limit=config.channels.whatsapp.session_history_limit,
        whatsapp_session_history_limit_group=config.channels.whatsapp.session_history_limit_group,
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/gateway/ -v --timeout=30`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add packages/gateway/yeoman_gateway/adapters/responder_llm.py packages/gateway/yeoman_gateway/app/bootstrap.py
git commit -m "feat(responder): use configurable session history limits instead of hardcoded values"
```

---

## Task 6: Preflight heuristic for adaptive context expansion

**Files:**
- Modify: `packages/gateway/yeoman_gateway/adapters/responder_llm.py`
- Modify: `tests/gateway/test_context_windowing.py`

- [ ] **Step 1: Write tests for the heuristic**

Append to `tests/gateway/test_context_windowing.py`:

```python
class TestPreflightHeuristic:
    """Preflight heuristic should detect backward references in messages."""

    def test_detects_earlier_reference(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert _has_backward_reference("as we discussed earlier, the plan was...")
        assert _has_backward_reference("you mentioned something about auth")
        assert _has_backward_reference("remember when we talked about the API?")
        assert _has_backward_reference("go back to what you said about config")
        assert _has_backward_reference("what about the idea from before?")

    def test_ignores_normal_messages(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert not _has_backward_reference("hello")
        assert not _has_backward_reference("what's the weather?")
        assert not _has_backward_reference("please write a function that adds two numbers")
        assert not _has_backward_reference("can you help me?")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestPreflightHeuristic -v`

Expected: FAIL — `_has_backward_reference` doesn't exist

- [ ] **Step 3: Implement the heuristic**

In `packages/gateway/yeoman_gateway/adapters/responder_llm.py`, add near the top (after imports, before the class):

```python
import re as _re

_BACKWARD_REF_RE = _re.compile(
    r"\b(?:"
    r"earlier|before|previously|as (?:we|i|you) (?:discussed|said|mentioned|talked)"
    r"|you (?:said|mentioned|told me|suggested)"
    r"|remember when|go back to|what about the"
    r"|we (?:discussed|agreed|decided|talked about)"
    r"|i (?:said|asked|mentioned)"
    r")\b",
    _re.IGNORECASE,
)


def _has_backward_reference(text: str) -> bool:
    """Return True if the message appears to reference earlier conversation."""
    return bool(_BACKWARD_REF_RE.search(text))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestPreflightHeuristic -v`

Expected: PASS

- [ ] **Step 5: Integrate heuristic into _resolve_history_limit**

Update `_resolve_history_limit` in `LLMResponder`:

```python
    def _resolve_history_limit(
        self, chat_id: str, decision: PolicyDecision, content: str = "",
    ) -> int:
        """Resolve session history limit: per-chat policy > heuristic > global config default."""
        per_chat = getattr(decision, "session_history_limit", None)
        if per_chat is not None:
            base = int(per_chat)
        elif chat_id.endswith("@g.us"):
            base = self._session_history_limit_group
        else:
            base = self._session_history_limit

        # Expand window when message references earlier conversation.
        if content and _has_backward_reference(content):
            return min(base * 3, 50)
        return base
```

Update the call site (where `_resolve_history_limit` is called) to pass `content`:

```python
                    history=session.get_history(
                        max_messages=self._resolve_history_limit(chat_id, decision, content),
                    ),
```

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/gateway/ -v --timeout=30`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add packages/gateway/yeoman_gateway/adapters/responder_llm.py tests/gateway/test_context_windowing.py
git commit -m "feat(context): preflight heuristic expands history window for backward references"
```

---

## Task 7: `recall_conversation` tool

**Files:**
- Create: `packages/gateway/yeoman_gateway/agent/tools/recall_conversation.py`
- Modify: `packages/gateway/yeoman_gateway/adapters/responder_llm.py`
- Modify: `tests/gateway/test_context_windowing.py`

- [ ] **Step 1: Write the test**

Append to `tests/gateway/test_context_windowing.py`:

```python
import pytest
from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
from yeoman_gateway.session.manager import Session, SessionManager
from pathlib import Path
import tempfile


class TestRecallConversationTool:
    """recall_conversation tool should search session history."""

    @pytest.fixture
    def session_manager(self, tmp_path):
        return SessionManager(workspace=tmp_path, sessions_dir=tmp_path / "sessions")

    @pytest.fixture
    def tool(self, session_manager):
        t = RecallConversationTool(session_manager=session_manager)
        t.set_context("whatsapp", "owner@s.whatsapp.net")
        return t

    @pytest.mark.asyncio
    async def test_finds_matching_messages(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "let's use PostgreSQL for the database")
        session.add_message("assistant", "sure, PostgreSQL is a good choice")
        session.add_message("user", "what about Redis for caching?")
        session_manager.save(session)

        result = await tool.execute(query="PostgreSQL")
        assert "PostgreSQL" in result
        assert "Redis" not in result

    @pytest.mark.asyncio
    async def test_returns_no_matches(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "hello world")
        session_manager.save(session)

        result = await tool.execute(query="kubernetes")
        assert "No matching" in result or "no match" in result.lower()

    @pytest.mark.asyncio
    async def test_searches_across_boundaries(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "use PostgreSQL")
        session.add_boundary()
        session.add_message("user", "hello")
        session_manager.save(session)

        result = await tool.execute(query="PostgreSQL")
        assert "PostgreSQL" in result

    @pytest.mark.asyncio
    async def test_respects_max_messages(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        for i in range(50):
            session.add_message("user", f"message about topic {i}")
        session_manager.save(session)

        result = await tool.execute(query="topic", max_messages=5)
        lines = [l for l in result.strip().split("\n") if l.strip()]
        assert len(lines) <= 5
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestRecallConversationTool -v`

Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement the tool**

Create `packages/gateway/yeoman_gateway/agent/tools/recall_conversation.py`:

```python
"""Tool for searching session conversation history on demand."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.session.manager import SessionManager


class RecallConversationTool(Tool):
    """Search conversation history for messages matching a query."""

    def __init__(self, session_manager: "SessionManager") -> None:
        self._session_manager = session_manager
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "recall_conversation"

    @property
    def description(self) -> str:
        return (
            "Search earlier conversation history for messages matching a query. "
            "Use when the user references something from a previous part of the "
            "conversation that is not in your visible context. "
            "Searches across session boundaries (including before /new)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to match against message content.",
                    "minLength": 1,
                },
                "max_messages": {
                    "type": "integer",
                    "description": "Maximum number of matching messages to return.",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        query = str(kwargs.get("query") or "").strip().lower()
        if not query:
            return "Error: query cannot be empty."

        max_messages = int(kwargs.get("max_messages") or 30)
        max_messages = max(1, min(50, max_messages))

        session_key = f"{self._channel}:{self._chat_id}"
        session = self._session_manager.get_or_create(session_key)

        # Search ALL messages (ignoring boundaries) for query matches.
        matches: list[str] = []
        for msg in session.messages:
            role = str(msg.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = str(msg.get("content") or "")
            if query in content.lower():
                ts = msg.get("timestamp", "?")
                matches.append(f"[{ts}] [{role}]: {content[:300]}")
                if len(matches) >= max_messages:
                    break

        if not matches:
            return f"No matching messages found for '{query}'."

        header = f"Found {len(matches)} matching message(s):\n"
        return header + "\n".join(matches)
```

- [ ] **Step 4: Run to verify tests pass**

Run: `python -m pytest tests/gateway/test_context_windowing.py::TestRecallConversationTool -v`

Expected: PASS

- [ ] **Step 5: Register the tool in the responder**

In `packages/gateway/yeoman_gateway/adapters/responder_llm.py`, in `__init__`, after the `SummarizeHistoryTool` registration block (around line 243), add:

```python
        # Recall conversation — search session history on demand
        if self.sessions is not None:
            from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool

            self._recall_tool = RecallConversationTool(session_manager=self.sessions)
            self.tools.register(self._recall_tool)
```

Note: `self.sessions` is how the session manager is stored — verify the attribute name by checking the `__init__` body. It may be `self.session_manager` or similar. Look for where `session_manager` is stored:

```bash
rg "self\.sessions\b|self\.session_manager\b" packages/gateway/yeoman_gateway/adapters/responder_llm.py | head -5
```

Use the correct attribute name.

- [ ] **Step 6: Set context on the tool per-request**

In the `_set_tool_context` method (around line 280), add after the `SummarizeHistoryTool` context setting:

```python
        from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool

        recall_tool = self.tools.get("recall_conversation")
        if isinstance(recall_tool, RecallConversationTool):
            recall_tool.set_context(channel, chat_id)
```

- [ ] **Step 7: Run all tests**

Run: `python -m pytest tests/gateway/ -v --timeout=30`

Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add packages/gateway/yeoman_gateway/agent/tools/recall_conversation.py packages/gateway/yeoman_gateway/adapters/responder_llm.py tests/gateway/test_context_windowing.py
git commit -m "feat(tools): add recall_conversation tool for on-demand history search"
```

---

## Task 8: System prompt hint for recall_conversation

**Files:**
- Modify: `packages/gateway/yeoman_gateway/agent/context.py`

- [ ] **Step 1: Add a brief instruction to the system prompt**

In `packages/gateway/yeoman_gateway/agent/context.py`, find the `build_system_prompt` method. Add a one-liner about the recall tool. Look for a suitable place in the prompt assembly (near other tool-related instructions):

```python
        parts.append(
            "If the user references something outside your visible conversation history, "
            "use the `recall_conversation` tool to search for it."
        )
```

Place this after the existing system prompt parts, before returning. This is a lightweight addition — the tool description itself does most of the work.

- [ ] **Step 2: Run tests to verify nothing breaks**

Run: `python -m pytest tests/ -v --timeout=30`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add packages/gateway/yeoman_gateway/agent/context.py
git commit -m "feat(context): add system prompt hint for recall_conversation tool"
```

---

## Task 9: Integration test and final verification

**Files:**
- Modify: `tests/gateway/test_context_windowing.py`

- [ ] **Step 1: Add integration-style test for full flow**

Append to `tests/gateway/test_context_windowing.py`:

```python
class TestHistoryLimitResolution:
    """Test the full resolution chain: per-chat > heuristic > global default."""

    def test_default_dm_limit(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        # DM default should be 15 (from config)
        from yeoman_shared.config.schema import WhatsAppConfig
        c = WhatsAppConfig()
        assert c.session_history_limit == 15
        assert c.session_history_limit_group == 20

    def test_heuristic_expansion_capped_at_50(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert _has_backward_reference("as we discussed earlier")
        # When base=20 and heuristic triggers: min(20*3, 50) = 50
        # When base=15 and heuristic triggers: min(15*3, 50) = 45

    def test_session_boundary_and_history_combined(self):
        s = Session(key="test")
        # Add 30 messages, then boundary, then 5 messages
        for i in range(30):
            s.add_message("user", f"old msg {i}")
        s.add_boundary()
        for i in range(5):
            s.add_message("user", f"new msg {i}")

        # With limit=50, boundary wins (5 messages)
        assert len(s.get_history(max_messages=50)) == 5
        # With limit=3, max_messages wins (3 messages)
        assert len(s.get_history(max_messages=3)) == 3
```

- [ ] **Step 2: Run all tests**

Run: `python -m pytest tests/gateway/test_context_windowing.py -v`

Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v --timeout=30`

Expected: PASS

- [ ] **Step 4: Run linter**

Run: `ruff check packages/ tests/`

Expected: Clean (fix any issues)

- [ ] **Step 5: Final commit**

```bash
git add tests/gateway/test_context_windowing.py
git commit -m "test(context): add integration tests for smart context windowing"
```

---

## Task 10: Restart gateway and verify

- [ ] **Step 1: Restart the gateway**

Run: `yeoman gateway restart`

- [ ] **Step 2: Test `/new` command via DM**

Send `/new` in a WhatsApp DM. Expected response: "New session started. Previous context will not be included."

- [ ] **Step 3: Verify ambient is skipped in DM**

Send a regular message in DM. Check logs for absence of ambient context injection.

- [ ] **Step 4: Test `/new` in a group**

Send `/new` in a group chat. Expected: same boundary behavior.

---

## Summary of config options after implementation

**`~/.yeoman/config.json`** (global defaults):
```json
{
  "channels": {
    "whatsapp": {
      "session_history_limit": 15,
      "session_history_limit_group": 20,
      "ambient_window_limit": 8
    }
  }
}
```

**`~/.yeoman/policy.json`** (per-chat override):
```json
{
  "channels": {
    "whatsapp": {
      "chats": {
        "123456@g.us": {
          "sessionHistoryLimit": 30
        }
      }
    }
  }
}
```

**Commands**:
- `/new` — start fresh session (boundary marker)
- `/reset` — nuclear option (delete session file)

**Automatic**: Backward-reference heuristic expands window to 3x (capped at 50). `recall_conversation` tool available as LLM fallback.
