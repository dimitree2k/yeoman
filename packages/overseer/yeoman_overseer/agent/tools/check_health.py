"""check_health tool — delegates to trigger/checks.py."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        from yeoman_overseer.trigger.checks import run_check

        result = run_check(args["check"], target=args["target"])
        return (
            f"[check_health] {args['check']}({args['target']}): "
            f"value={result.value} detail={result.detail}"
        )
    except ValueError as exc:
        return f"[check_health] ERROR: {exc}"
    except Exception as exc:
        return f"[check_health] ERROR: {exc}"
