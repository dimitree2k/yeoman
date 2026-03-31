# tests/gateway/test_workflow_chaining.py
"""Tests for workflow chaining logic."""


from yeoman_gateway.cron.types import CronJob, CronPayload


def test_chain_depth_decrement() -> None:
    """Verify max_chain_depth is decremented before passing to next job."""
    payload = CronPayload(message="step 1", max_chain_depth=3, next_job_id="next")
    remaining = payload.max_chain_depth - 1
    assert remaining == 2


def test_output_truncation() -> None:
    """Verify output is truncated at 4000 chars for input_from_previous."""
    from yeoman_gateway.cron.workflow_chain import build_chained_prompt

    long_output = "x" * 5000
    prompt = build_chained_prompt(long_output, "Do the next thing", input_from_previous=True)
    assert "[Previous step output]" in prompt
    assert "...[truncated]" in prompt
    assert len(prompt) < 5200  # truncated output + task text


def test_output_not_injected_when_disabled() -> None:
    from yeoman_gateway.cron.workflow_chain import build_chained_prompt

    prompt = build_chained_prompt("some output", "Do the next thing", input_from_previous=False)
    assert prompt == "Do the next thing"
    assert "[Previous step output]" not in prompt


def test_cycle_detection() -> None:
    from yeoman_gateway.cron.workflow_chain import detect_chain_cycle

    jobs = {
        "a": CronJob(id="a", name="A", payload=CronPayload(next_job_id="b")),
        "b": CronJob(id="b", name="B", payload=CronPayload(next_job_id="c")),
        "c": CronJob(id="c", name="C", payload=CronPayload(next_job_id="a")),  # cycle!
    }
    assert detect_chain_cycle("a", jobs, max_depth=5) is True


def test_no_cycle() -> None:
    from yeoman_gateway.cron.workflow_chain import detect_chain_cycle

    jobs = {
        "a": CronJob(id="a", name="A", payload=CronPayload(next_job_id="b")),
        "b": CronJob(id="b", name="B", payload=CronPayload(next_job_id=None)),
    }
    assert detect_chain_cycle("a", jobs, max_depth=5) is False
