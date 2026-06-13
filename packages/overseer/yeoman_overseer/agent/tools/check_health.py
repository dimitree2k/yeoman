"""check_health tool — delegates to trigger/checks.py."""
from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        from yeoman_overseer.trigger.checks import _CHECK_REGISTRY, run_check

        check_name = args["check"]
        check_kwargs = {k: v for k, v in args.items() if k != "check"}
        check_fn = _CHECK_REGISTRY.get(check_name)
        if check_fn is not None:
            accepted = set(inspect.signature(check_fn).parameters)
            check_kwargs = {k: v for k, v in check_kwargs.items() if k in accepted}
        result = run_check(check_name, **check_kwargs)
        return (
            f"[check_health] {check_name}({args['target']}): "
            f"value={result.value} detail={result.detail}"
        )
    except ValueError as exc:
        return f"[check_health] ERROR: {exc}"
    except Exception as exc:
        return f"[check_health] ERROR: {exc}"
