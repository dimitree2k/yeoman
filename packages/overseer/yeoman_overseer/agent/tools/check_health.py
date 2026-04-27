"""check_health tool — delegates to trigger/checks.py."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        from yeoman_overseer.trigger.checks import run_check

        check_name = args["check"]
        check_kwargs = {k: v for k, v in args.items() if k != "check"}
        result = run_check(check_name, **check_kwargs)
        return (
            f"[check_health] {check_name}({args['target']}): "
            f"value={result.value} detail={result.detail}"
        )
    except ValueError as exc:
        return f"[check_health] ERROR: {exc}"
    except Exception as exc:
        return f"[check_health] ERROR: {exc}"
