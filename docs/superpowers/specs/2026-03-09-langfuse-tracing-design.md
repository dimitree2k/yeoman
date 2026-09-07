# Langfuse Tracing for Yeoman

## Goal

Add nested trace visibility into yeoman's agent loop: LLM calls, tool
executions, token usage, and latency — viewable in Langfuse Cloud.

## Decisions

- **Scope:** Agent loop only (LLM calls + tool calls per message). No
  middleware pipeline tracing.
- **Approach:** Custom `@observe()` decorators + manual spans for loop
  iterations. Not the LiteLLM built-in callback (which produces flat traces).
- **Activation:** Auto-enable when `LANGFUSE_SECRET_KEY` is present in env.
  No config toggle.
- **Privacy:** Full input/output in traces. The user controls their own
  Langfuse instance.

## Trace Structure

```
trace: generate (channel=whatsapp, chat_id=..., model=claude-sonnet-4-5)
  ├─ span: iteration-1
  │   ├─ generation: llm (model, prompt_tokens=8900, completion_tokens=137)
  │   ├─ span: tool/web_search (args, result, 0.3s)
  │   └─ span: tool/write_file (args, result, 0.01s)
  ├─ span: iteration-2
  │   └─ generation: llm (prompt_tokens=8956, completion_tokens=27)
  └─ metadata: {total_iterations: 2, total_tokens: 17920}
```

## Files Changed

| File | Change |
|------|--------|
| `pyproject.toml` | Add `langfuse>=3.0.0` dependency |
| `yeoman/telemetry/tracing.py` | **New.** Thin wrapper: init client from env, export helpers, no-op when keys absent |
| `yeoman/adapters/responder_llm.py` | Instrument `_generate()` (root trace), `_chat_loop()` (iteration spans), `_execute_tool()` (tool spans) |
| `yeoman/providers/litellm_provider.py` | Instrument `chat()` with generation span (token usage, model) |

## Instrumentation Points

1. **`_generate()`** — Create root trace. Tags: channel, chat_id, model,
   session_key.
2. **`_chat_loop()` loop body** — One span per iteration.
3. **`provider.chat()`** — Generation span. Captures model, prompt_tokens,
   completion_tokens, total_tokens from `LLMResponse.usage`.
4. **`_execute_tool()`** — Span per tool call. Captures tool name, arguments
   (truncated), result (truncated).

## No-op Pattern

```python
if os.environ.get("LANGFUSE_SECRET_KEY"):
    _langfuse = Langfuse()
else:
    _langfuse = None
```

All helpers return immediately when `_langfuse is None`.

## Dependency

`langfuse>=3.0.0` added to main dependencies in `pyproject.toml`. The SDK
batches traces in memory and flushes async over HTTPS — no blocking, no extra
LLM tokens.
