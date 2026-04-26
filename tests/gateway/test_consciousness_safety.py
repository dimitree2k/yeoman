"""Safety properties for proactive consciousness tool boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.agent import ConsciousnessAgent
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.tools import ConsciousnessTools
from yeoman_gateway.core.models import SecurityDecision, SecurityResult
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import Config, ConsciousnessConfig


class _FakeSecurity:
    def check_output(
        self,
        text: str,
        context: dict[str, object] | None = None,
    ) -> SecurityResult:
        del text, context
        return SecurityResult(
            stage="output",
            decision=SecurityDecision(action="allow", reason="fake_allow"),
        )


def _config(*, daily_cap: int) -> Config:
    return Config(
        consciousness=ConsciousnessConfig.model_validate(
            {
                "enabled": True,
                "ownerDmDefaultEnabled": True,
                "defaultDailyCap": daily_cap,
                "maxSpeakupLengthChars": 200,
            }
        )
    )


def _policy() -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "owner@s.whatsapp.net": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                                "preview": "none",
                            }
                        }
                    }
                }
            },
        }
    )


def _operation_scripts() -> list[list[str]]:
    alphabet = [
        "propose_valid",
        "propose_invalid_chat",
        "propose_low_confidence",
        "commit_latest",
        "commit_first",
        "commit_fake",
        "agent_run",
        "concurrent_commits",
    ]
    scripts: list[list[str]] = []
    state = 17
    for _ in range(48):
        script: list[str] = []
        for _ in range(8):
            state = (state * 1103515245 + 12345) % (2**31)
            script.append(alphabet[state % len(alphabet)])
        scripts.append(script)
    scripts.extend(
        [
            ["propose_valid", "commit_latest"] * 6,
            ["propose_valid"] * 6 + ["concurrent_commits"] * 3,
            ["agent_run"] * 6,
            ["propose_valid", "commit_first", "commit_first", "commit_latest"] * 3,
        ]
    )
    return scripts


@pytest.mark.asyncio
@pytest.mark.parametrize("daily_cap", [0, 1, 2])
async def test_adversarial_tool_sequences_cannot_exceed_daily_cap(
    tmp_path: Path,
    daily_cap: int,
) -> None:
    for script_index, script in enumerate(_operation_scripts()):
        bus = MessageBus()
        log = SpeakupLog(tmp_path / f"speakups-{daily_cap}-{script_index}.db")
        tools = ConsciousnessTools(
            config=_config(daily_cap=daily_cap),
            policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
            bus=bus,
            log=log,
            inbound_archive=InboundArchive(tmp_path / f"inbound-{daily_cap}-{script_index}.db"),
            memory=None,
            security=_FakeSecurity(),
            now=lambda: datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
        )
        proposals: list[str] = []
        agent = ConsciousnessAgent(
            tools=tools,
            planner=lambda prompt: json.dumps(
                {
                    "chat_id": "owner@s.whatsapp.net",
                    "message": "adversarial proposal",
                    "action_type": "observation",
                    "confidence": 0.95,
                }
            ),
        )

        for op in script:
            if op == "propose_valid":
                result = await tools.propose_speakup(
                    chat_id="owner@s.whatsapp.net",
                    message="valid proposal",
                    action_type="observation",
                    confidence=0.95,
                )
                if result.get("status") == "proposed":
                    proposals.append(str(result["proposal_id"]))
            elif op == "propose_invalid_chat":
                await tools.propose_speakup(
                    chat_id="group@g.us",
                    message="invalid chat",
                    action_type="observation",
                    confidence=0.95,
                )
            elif op == "propose_low_confidence":
                await tools.propose_speakup(
                    chat_id="owner@s.whatsapp.net",
                    message="low confidence",
                    action_type="observation",
                    confidence=0.1,
                )
            elif op == "commit_latest":
                await tools.commit_speakup(proposals[-1] if proposals else "missing")
            elif op == "commit_first":
                await tools.commit_speakup(proposals[0] if proposals else "missing")
            elif op == "commit_fake":
                await tools.commit_speakup("fake-proposal-id")
            elif op == "agent_run":
                await agent.run_once(trigger="adversarial")
            elif op == "concurrent_commits":
                first = await tools.propose_speakup(
                    chat_id="owner@s.whatsapp.net",
                    message="concurrent one",
                    action_type="observation",
                    confidence=0.95,
                )
                second = await tools.propose_speakup(
                    chat_id="owner@s.whatsapp.net",
                    message="concurrent two",
                    action_type="observation",
                    confidence=0.95,
                )
                ids = [
                    str(result["proposal_id"])
                    for result in (first, second)
                    if result.get("status") == "proposed"
                ]
                await asyncio.gather(*(tools.commit_speakup(proposal_id) for proposal_id in ids))

            sent_today = await log.count_sent_today(
                channel="whatsapp",
                chat_id="owner@s.whatsapp.net",
                now=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            )
            assert sent_today <= daily_cap
            assert bus.outbound_size <= daily_cap
