"""Direct agent interaction command."""

from __future__ import annotations

import asyncio

import typer

from nanobot import __logo__

from .core import app, console, make_memory_service, make_provider


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
) -> None:
    """Interact with the agent directly."""
    from nanobot.adapters.responder_llm import LLMResponder
    from nanobot.agent.tools.file_access import build_file_access_resolver
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config
    from nanobot.policy.loader import load_policy
    from nanobot.security import NoopSecurity, SecurityEngine
    from nanobot.telemetry import InMemoryTelemetry

    config = load_config()
    memory_service = make_memory_service(config)
    telemetry = InMemoryTelemetry()

    bus = MessageBus(
        inbound_maxsize=config.bus.inbound_maxsize,
        outbound_maxsize=config.bus.outbound_maxsize,
    )
    provider = make_provider(config)
    security = SecurityEngine(config.security) if config.security.enabled else NoopSecurity()
    restrict_to_workspace = bool(config.tools.restrict_to_workspace)
    exec_config = config.tools.exec.model_copy(deep=True)
    if config.security.strict_profile:
        restrict_to_workspace = True
        exec_config.isolation.enabled = True
        exec_config.isolation.fail_closed = True
        exec_config.allow_host_execution = False

    policy = load_policy()
    file_access_resolver = build_file_access_resolver(
        workspace=config.workspace_path,
        policy=policy,
    )

    responder = LLMResponder(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        exec_config=exec_config,
        restrict_to_workspace=restrict_to_workspace,
        memory_service=memory_service,
        telemetry=telemetry,
        security=security,
        file_access_resolver=file_access_resolver,
    )

    if message:

        async def run_once() -> None:
            response = await responder.process_direct(message, session_key=session_id)
            console.print(f"\n{__logo__} {response}")

        try:
            asyncio.run(run_once())
        finally:
            responder.close()
            memory_service.close()
    else:
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

        async def run_interactive() -> None:
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    response = await responder.process_direct(user_input, session_key=session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        try:
            asyncio.run(run_interactive())
        finally:
            responder.close()
            memory_service.close()
