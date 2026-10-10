"""Coordinator fix witnesses: synthetic proc, systemd runner, DBs and fake time only."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.gateway.test_history_cutover import PAUSE_CANONICAL, procedure, record
from tests.gateway.test_history_cutover_host import (
    Runner,
    host_module,
    inventory,
    payload,
    raw_status,
)


def seen_chats_file():
    from yeoman_shared.utils.helpers import get_operational_store_path

    return get_operational_store_path("seen_chats")


class Clock:
    now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize("relevant,denied_cmdline", [(False, False), (True, False), (False, True)])
def test_proc_denied_exe_uses_exact_owner_argv(tmp_path, monkeypatch, relevant, denied_cmdline):
    proc = tmp_path / "proc"
    entry = proc / "71"
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(
        b"/usr/bin/python\0"
        + (b"/synthetic/gateway" if relevant else b"/synthetic/unrelated")
        + b"\0"
    )
    resolve, read = Path.resolve, Path.read_bytes

    def denied_exe(path, *args, **kwargs):
        if path == entry / "exe":
            raise PermissionError()
        return resolve(path, *args, **kwargs)

    def cmdline(path):
        if denied_cmdline and path == entry / "cmdline":
            raise PermissionError()
        return read(path)

    monkeypatch.setattr(Path, "resolve", denied_exe)
    monkeypatch.setattr(Path, "read_bytes", cmdline)
    h = host_module().live_host_controls(
        inventory=inventory(), runner=Runner(), proc_root=proc, clock=Clock()
    )
    if denied_cmdline:
        with pytest.raises(ValueError, match="writer_cmdline_unobservable"):
            h("verify-quiescent", payload(tmp_path))
    else:
        assert h("verify-quiescent", payload(tmp_path))["writers_absent"] is (not relevant)


class GuardRunner(Runner):
    def __init__(self, root):
        super().__init__()
        self.root = root
        self.hide = False

    def __call__(self, argv):
        if argv[0] == "busctl":
            marker = next(self.root.glob(".yeoman-cutover-*.hold"), None)
            data = (
                []
                if self.hide or marker is None
                else [["ConditionPathExists", False, True, str(marker), 0]]
            )
            return CompletedProcess(argv, 0, json.dumps(dict(type="a(sbbsi)", data=data)), "")
        result = super().__call__(argv)
        if argv[:3] == ["systemctl", "--user", "show"]:
            unit = argv[3]
            guards = list((self.root / (unit + ".d")).glob("*cutover*.conf"))
            result.stdout += "\nLoadState=loaded\nNeedDaemonReload=no\nDropInPaths=" + (
                "" if self.hide else " ".join(map(str, guards))
            )
        return result


def guard_case(tmp_path):
    root = tmp_path / "systemd/user"
    root.mkdir(parents=True)
    pin = tmp_path / "prior-pin"
    pin.write_bytes(b"synthetic prior source")
    inv = inventory() | dict(
        systemd_runtime_dir=str(root),
        owner_uid=os.getuid(),
        pause_path=str(tmp_path / "pauses.json"),
        prior_yeoman="/synthetic/prior-yeoman",
        prior_source_dir="/synthetic/prior",
        prior_pinned_files={str(pin): __import__("hashlib").sha256(pin.read_bytes()).hexdigest()},
    )
    p = payload(tmp_path)
    p["record"]["digest"] = "a" * 64
    runner = GuardRunner(root)
    h = host_module().live_host_controls(
        inventory=inv, runner=runner, clock=Clock(), proc_root=tmp_path / "proc"
    )
    (tmp_path / "proc").mkdir()
    return h, p, runner, root


def test_guard_loaded_condition_hash_release_and_restore(tmp_path):
    h, p, runner, root = guard_case(tmp_path)
    assert h("suppress-restarts", p)["suppressed"]
    paths = list(root.glob("*/*.conf"))
    assert len(paths) == 7 and not any(a[2] == "mask" for a in runner.calls if a[0] == "systemctl")
    marker = next(root.glob("*.hold")) if list(root.glob("*.hold")) else next(root.glob(".*.hold"))
    assert p["record"]["digest"] in marker.read_text()
    runner.hide = True
    assert not h("verify-deploy-suppression", p)["ok"]
    runner.hide = False
    assert h("start-bridge", p)["ok"]
    assert not list((root / "yeoman-bridge.service.d").glob("*.conf"))
    assert len(list(root.glob("*/*.conf"))) == 6
    # Restore reinstates only this attempt's released guard, preserving others.
    p["operation"] = "restore"
    runner(["systemctl", "--user", "stop", "yeoman-bridge.service"])
    assert h("suppress-restarts", p)["ok"]
    assert len(list(root.glob("*/*.conf"))) == 7
    guard = next((root / "yeoman-bridge.service.d").glob("*.conf"))
    guard.write_text("foreign")
    with pytest.raises(ValueError, match="guard_drift"):
        h("start-bridge", p)


def test_guard_refuses_preexisting_paths_before_write(tmp_path):
    h, p, runner, root = guard_case(tmp_path)
    marker = root / f".yeoman-cutover-{p['record']['digest']}.hold"
    marker.write_text("foreign")
    with pytest.raises(ValueError, match="guard_path_exists"):
        h("suppress-restarts", p)
    assert marker.read_text() == "foreign" and not list(root.glob("*/*.conf"))


def frozen_case(tmp_path):
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES

    h = host_module()
    m = procedure()
    _, home, value = record(tmp_path)
    knowledge = home / "identity.db"
    with sqlite3.connect(knowledge) as db:
        for table in (*FROZEN_IDENTITY_TABLES, "knowledge_statements", "knowledge_jobs"):
            db.execute(f'CREATE TABLE "{table}"(id TEXT)')
    frozen = home / "retired.db"
    with sqlite3.connect(frozen) as db:
        db.execute("CREATE TABLE retained(value TEXT)")
        db.execute("INSERT INTO retained VALUES ('pre-record')")
    value["inventory"].update(
        knowledge_db=str(knowledge), frozen_files=[str(frozen), str(knowledge)]
    )
    value["inventory"]["frozen_watermarks"] = {str(frozen): h._hash(frozen)}
    value["inventory"]["members"].extend(
        [
            dict(path="identity.db", kind="sqlite", restore=True),
            dict(path="retired.db", kind="sqlite", restore=True),
        ]
    )
    value["digest"] = m.record_digest(value)
    with sqlite3.connect(frozen) as db:
        db.execute("UPDATE retained SET value='writer-off'")
    acquired = m.acquire_cutover_snapshot(
        home=home, output=Path(value["output"]), inventory=value["inventory"]
    )
    p = dict(
        record=value,
        receipts=[
            dict(
                action="verify-quiescent",
                record_digest=value["digest"],
                receipt=dict(ok=True, writers_absent=True, bridge_stopped=True),
            )
        ],
        acquisition=acquired,
    )
    Path(value["receipts"]).mkdir()
    (Path(value["receipts"]) / "cutover-01.json").write_text(json.dumps(p["receipts"][0]))
    return h, p, frozen, knowledge


def test_quiescent_baseline_ignores_pre_record_change_and_detects_later_change(tmp_path):
    h, p, frozen, _ = frozen_case(tmp_path)
    baseline = h._capture_frozen_baseline(p)
    p["receipts"].append(
        dict(
            action="acquire",
            record_digest=p["record"]["digest"],
            frozen_baseline_digest=baseline["baseline_digest"],
            receipt=p["acquisition"],
        )
    )
    (Path(p["record"]["receipts"]) / "cutover-02.json").write_text(json.dumps(p["receipts"][-1]))
    assert h._verify_frozen_baseline(p)["ok"]
    frozen.write_bytes(b"after-writer-off")
    with pytest.raises(ValueError, match="frozen_watermark_changed"):
        h._verify_frozen_baseline(p)


def test_identity_baseline_allows_statement_job_growth_only(tmp_path):
    h, p, _, knowledge = frozen_case(tmp_path)
    baseline = h._capture_frozen_baseline(p)
    p["receipts"].append(
        dict(
            action="acquire",
            record_digest=p["record"]["digest"],
            frozen_baseline_digest=baseline["baseline_digest"],
            receipt=p["acquisition"],
        )
    )
    (Path(p["record"]["receipts"]) / "cutover-02.json").write_text(json.dumps(p["receipts"][-1]))
    with sqlite3.connect(knowledge) as db:
        db.execute("INSERT INTO knowledge_statements VALUES ('synthetic')")
        db.execute("INSERT INTO knowledge_jobs VALUES ('synthetic')")
    assert h._verify_frozen_baseline(p)["ok"]
    with sqlite3.connect(knowledge) as db:
        db.execute("INSERT INTO contacts VALUES ('synthetic')")
    with pytest.raises(ValueError, match="frozen_identity_changed"):
        h._verify_frozen_baseline(p)


def test_baseline_requires_this_attempt_quiescence_and_digest(tmp_path):
    h, p, _, _ = frozen_case(tmp_path)
    p["receipts"][0]["record_digest"] = "foreign"
    with pytest.raises(ValueError, match="quiescent_baseline_required"):
        h._capture_frozen_baseline(p)


@pytest.mark.parametrize("action", ["health", "drain-durable-tails", "all-committed-barrier"])
def test_readiness_waits_for_delayed_ready_within_one_phase(tmp_path, monkeypatch, action):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

    h = host_module()
    clock = Clock()
    monkeypatch.setattr(
        h, "_boundary", lambda _: dict(generation=3, sources=[], all_committed=True, reopened=True)
    )
    monkeypatch.setattr(h, "_capture_ready", lambda _: clock.now >= 65)
    p = payload(tmp_path)
    p["record"].update(python="/synthetic/python", readiness_timeout_seconds=90)

    def bridge():
        return dict(
            whatsapp=dict(connected=clock.now >= 60),
            protocolVersion=PROTOCOL_VERSION,
            persistenceFailure=False,
            outbox=dict(pending=0),
            queue=dict(inflight=0),
        )

    def runner(argv):
        return CompletedProcess(argv, 0, json.dumps(raw_status()), "")

    c = h.live_host_controls(
        inventory=inventory(),
        runner=runner,
        clock=clock,
        bridge_probe=bridge,
        ipc=lambda _: dict(
            status="ok", health=dict(status="ready", generation=3, lag_lines=0, lag_bytes=0)
        ),
    )
    assert c(action, p)["ok"] and clock.now >= 60
    assert clock.now <= 90


@pytest.mark.parametrize("defect", ["timeout", "protocol", "persistence", "invalid"])
def test_readiness_timeout_and_fatal_evidence_do_not_retry_cutover(tmp_path, defect):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

    h = host_module()
    clock = Clock()
    p = payload(tmp_path)
    p["record"].update(python="/synthetic/python", readiness_timeout_seconds=60)
    status = dict(
        whatsapp=dict(connected=False),
        protocolVersion=PROTOCOL_VERSION,
        persistenceFailure=False,
        outbox=dict(pending=0),
        queue=dict(inflight=0),
    )
    if defect == "protocol":
        status["protocolVersion"] = -1
    if defect == "persistence":
        status["persistenceFailure"] = True
    if defect == "invalid":
        status["outbox"]["pending"] = True
    c = h.live_host_controls(
        inventory=inventory(),
        runner=lambda a: CompletedProcess(a, 0, json.dumps(raw_status()), ""),
        clock=clock,
        bridge_probe=lambda: status,
    )
    code = {
        "timeout": "readiness_timeout",
        "protocol": "readiness_protocol_mismatch",
        "persistence": "readiness_persistence_failure",
        "invalid": "invalid_readiness_evidence",
    }[defect]
    with pytest.raises(ValueError, match=code):
        c("health", p)
    assert clock.now == (60 if defect == "timeout" else 0)


def test_restore_origin_checks_prior_root(tmp_path, monkeypatch):
    h = host_module()
    p = payload(tmp_path)
    p["operation"] = "restore"
    p["record"]["python"] = "/synthetic/python"
    inv = inventory() | dict(
        source_dir=str(tmp_path / "candidate"),
        prior_source_dir=str(tmp_path / "prior"),
        tool_python="/synthetic/tool",
    )
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        return CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    str(tmp_path / "prior/packages" / name / "__init__.py")
                    for name in ("gateway", "shared", "overseer")
                ]
            ),
            "",
        )

    monkeypatch.setattr(h.subprocess, "run", run)
    assert h.live_host_controls(inventory=inv, clock=Clock())("verify-import-origins", p)["ok"]
    assert (
        seen[0]["cwd"] == inv["prior_source_dir"]
        and seen[0]["env"]["YEOMAN_SOURCE_DIR"] == inv["prior_source_dir"]
    )


@pytest.mark.parametrize("timeout", [True, 0, 1201, "360"])
def test_readiness_configuration_refused_before_any_phase(tmp_path, timeout):
    from scripts.history_cutover import validate_readiness_timeout
    from scripts.history_cutover_inputs import build_cutover_record

    with pytest.raises(ValueError, match="invalid_readiness_timeout"):
        validate_readiness_timeout(dict(readiness_timeout_seconds=timeout))
    with pytest.raises(ValueError, match="invalid_readiness_timeout"):
        build_cutover_record(
            inventory=tmp_path / "missing",
            layout={},
            mode="live",
            window=(0, 1),
            expected_gateway_jobs=0,
            readiness_timeout_seconds=timeout,
        )


def test_readiness_timeout_refences_started_writers_without_retry(tmp_path):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

    from tests.gateway.test_history_cutover import Controls

    m = procedure()
    _, home, value = record(tmp_path)
    value["readiness_timeout_seconds"] = 60
    value["python"] = "/synthetic/python"
    clock = Clock()
    live = host_module().live_host_controls(
        inventory=inventory(),
        clock=clock,
        runner=lambda a: CompletedProcess(a, 0, json.dumps(raw_status()), ""),
        bridge_probe=lambda: dict(
            whatsapp=dict(connected=False),
            protocolVersion=PROTOCOL_VERSION,
            persistenceFailure=False,
            outbox=dict(pending=0),
            queue=dict(inflight=0),
        ),
    )
    data = Controls(m)

    def controls(action, p):
        return live(action, p) if action == "health" else data(action, p)

    controls.mode = "rehearsal"
    with m.injected_controls(controls):
        result = m._run(
            value, home, ["fence-effects", "start-gateway", "health"], record_dir=tmp_path
        )
    journal = json.loads(Path(result["receipt"]).read_bytes())
    assert not result["ok"] and journal["error_code"] == "readiness_timeout"
    assert journal["refence"]["ok"] and journal["fenced"]
    assert data.calls.count("start-gateway") == 1 and data.calls.count("fence-effects") == 2


def test_writer_off_baseline_is_checked_before_release(tmp_path):
    from tests.gateway.test_history_cutover import Controls

    h, p, frozen, _ = frozen_case(tmp_path)
    # Start a fresh attempt, leaving the first fixture acquisition unused.
    value = p["record"]
    value["output"] = str(tmp_path / "fresh-acquisition")
    value["receipts"] = str(tmp_path / "fresh-receipts")
    value["digest"] = procedure().record_digest(value)
    m = procedure()
    data = Controls(m)

    def controls(action, p):
        if action == "capture-frozen-baseline":
            return h._capture_frozen_baseline(p)
        if action == "frozen-watermarks":
            return h._verify_frozen_baseline(p)
        if action == "synthetic-mutation":
            frozen.write_bytes(b"post-acquisition")
        return data(action, p)

    controls.mode = "rehearsal"
    with m.injected_controls(controls):
        result = m._run(
            value,
            Path(value["home"]),
            ["fence-effects", "verify-quiescent", "acquire", "synthetic-mutation", "release-fence"],
            record_dir=tmp_path,
        )
    journal = json.loads(Path(result["receipt"]).read_bytes())
    assert not result["ok"] and journal["failed_phase"] == "release-fence"
    assert journal["error_code"] == "frozen_watermark_changed" and "release-fence" not in data.calls


def test_guards_restore_after_whole_attempt_release_and_prior_deploy(tmp_path):
    h, p, runner, root = guard_case(tmp_path)
    h("suppress-restarts", p)
    for action in (
        "start-bridge",
        "start-gateway",
        "resume-vetted-manual-routes",
        "start-overseer",
        "start-timers",
    ):
        assert h(action, p)["ok"]
    assert not list(root.glob("*/*.conf")) and not list(root.glob(".yeoman-cutover-*.hold"))
    for unit in inventory()["units"]:
        runner(["systemctl", "--user", "stop", unit["name"]])
    for unit in ("watch.timer", "manual.service"):
        runner(["systemctl", "--user", "stop", unit])
    p["operation"] = "restore"
    assert h("suppress-restarts", p)["ok"]
    assert len(list(root.glob("*/*.conf"))) == 7
    assert h("restore-software-install-config-units", p)["ok"]
    assert ["/synthetic/prior-yeoman", "deploy"] in runner.calls
    assert not any(a[0] == "systemctl" and a[2] == "start" for a in runner.calls[-15:])


def test_guard_refuses_foreign_dropin_without_creating_marker(tmp_path):
    h, p, _, root = guard_case(tmp_path)
    path = root / "yeoman-gateway.service.d" / f"zz-yeoman-cutover-{p['record']['digest']}.conf"
    path.parent.mkdir()
    path.write_text("owner content")
    with pytest.raises(ValueError, match="guard_path_exists"):
        h("suppress-restarts", p)
    assert path.read_text() == "owner content" and not list(root.glob(".yeoman-cutover-*.hold"))


def test_readiness_never_accepts_sample_returned_after_deadline(tmp_path):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

    h = host_module()
    clock = Clock()
    p = payload(tmp_path)
    p["record"].update(python="/synthetic/python", readiness_timeout_seconds=60)

    def bridge():
        clock.now += 61
        return dict(
            whatsapp=dict(connected=True),
            protocolVersion=PROTOCOL_VERSION,
            persistenceFailure=False,
            outbox=dict(pending=0),
            queue=dict(inflight=0),
        )

    control = h.live_host_controls(
        inventory=inventory(),
        clock=clock,
        bridge_probe=bridge,
        runner=lambda a: CompletedProcess(a, 0, json.dumps(raw_status()), ""),
    )
    with pytest.raises(ValueError, match="readiness_timeout"):
        control("health", p)


def test_live_guard_record_fields_validate_before_phases(tmp_path):
    from scripts.history_cutover import validate_host_inventory

    _, _, value = record(tmp_path)
    validate_host_inventory(value["inventory"], mode="live")
    value["inventory"]["systemd_runtime_dir"] = "/synthetic/root with space"
    with pytest.raises(ValueError, match="invalid_host_inventory"):
        validate_host_inventory(value["inventory"], mode="live")
    value["inventory"]["systemd_runtime_dir"] = str(tmp_path / "systemd/user")
    value["inventory"]["owner_uid"] = os.getuid() + 1
    with pytest.raises(ValueError, match="writer_owner_uid_mismatch"):
        validate_host_inventory(value["inventory"], mode="live")


def test_ipc_read_deadline_covers_fragmented_response(tmp_path, monkeypatch):
    from types import SimpleNamespace

    h = host_module()
    clock = Clock()

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, path):
            pass

        def sendall(self, data):
            pass

        def recv(self, size):
            clock.now += 31
            return b'{"status":"ok"' if clock.now == 31 else b"}\n"

        def makefile(self, *args):
            class Reader:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def readline(self, size):
                    clock.now += 61
                    return b'{"status":"ok"}\n'

            return Reader()

    monkeypatch.setattr(h.socket, "socket", lambda *a: Socket())
    monkeypatch.setattr(h, "time", SimpleNamespace(monotonic=clock.monotonic))
    with pytest.raises(TimeoutError):
        h.gateway_socket_client({}, socket_path=tmp_path / "synthetic.sock", timeout_seconds=60)


def test_bridge_probe_deadline_includes_connection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from yeoman_gateway.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman_shared.config import loader

    h = host_module()
    monkeypatch.setattr(
        loader,
        "load_config",
        lambda **kw: SimpleNamespace(
            channels=SimpleNamespace(whatsapp=SimpleNamespace(bridge_token="synthetic"))
        ),
    )

    async def health(self, timeout):
        return {}

    monkeypatch.setattr(WhatsAppRuntimeManager, "_health_check_async", health)
    seen = []

    async def bounded(awaitable, timeout):
        seen.append(timeout)
        awaitable.close()
        raise TimeoutError()

    monkeypatch.setattr(h.asyncio, "wait_for", bounded)
    with pytest.raises(TimeoutError):
        h._bridge_probe(config_path=tmp_path / "synthetic.json", timeout_seconds=3)
    assert seen == [3]


def test_refence_reinstates_attempt_guards_and_preserves_prior_pauses(tmp_path):
    h, p, runner, root = guard_case(tmp_path)
    (tmp_path / "pauses.json").write_text(PAUSE_CANONICAL)
    h("suppress-restarts", p)
    h("start-bridge", p)
    h("start-gateway", p)
    fenced = h("fence-effects", p)
    assert fenced["ok"]
    assert len(list(root.glob("*/*.conf"))) == 7
    assert (tmp_path / "pauses.json").read_text() == PAUSE_CANONICAL
    assert fenced["pause_baseline_sha256"] == hashlib.sha256(PAUSE_CANONICAL.encode()).hexdigest()
    assert fenced["pause_global_until_ms"] == -1 and fenced["pause_chat_keys"] == []


@pytest.mark.parametrize("binary", ["node", "nodejs", "python3", "bash"])
def test_live_inventory_refuses_generic_writer_binary(tmp_path, binary):
    from scripts.history_cutover import validate_host_inventory

    _, _, value = record(tmp_path)
    next(unit for unit in value["inventory"]["units"]
         if unit["name"] == "yeoman-bridge.service")["executable"] = f"/usr/bin/{binary}"
    with pytest.raises(ValueError, match="specific_writer_entrypoint_required"):
        validate_host_inventory(value["inventory"], mode="live")


def test_frozen_baseline_requires_durable_acquisition_phase(tmp_path):
    h, p, _, _ = frozen_case(tmp_path)
    baseline = h._capture_frozen_baseline(p)
    acquired = dict(action="acquire", record_digest=p["record"]["digest"],
                    frozen_baseline_digest=baseline["baseline_digest"], receipt=p["acquisition"])
    p["receipts"].append(acquired)
    with pytest.raises(ValueError, match="frozen_baseline_pin_mismatch"):
        h._verify_frozen_baseline(p)
    path = Path(p["record"]["receipts"]) / "cutover-02.json"
    path.write_text(json.dumps(acquired))
    assert h._verify_frozen_baseline(p)["ok"]
    path.write_text(json.dumps(acquired | dict(frozen_baseline_digest="foreign")))
    with pytest.raises(ValueError, match="frozen_baseline_pin_mismatch"):
        h._verify_frozen_baseline(p)


@pytest.mark.asyncio
async def test_existing_pause_fences_first_contact_notification():
    """B1 witness: the owner's global pause defers the first-contact alert.

    The deferred chat stays unseen in memory and on disk, so the next unpaused
    invocation announces it exactly once instead of losing the alert.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from yeoman_gateway.core.intents import SendOutboundIntent
    from yeoman_gateway.core.models import InboundEvent, PolicyDecision
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
    from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware
    from yeoman_gateway.pipeline.policy import PolicyMiddleware

    decision = PolicyDecision(accept_message=True, should_respond=False,
                              allowed_tools=frozenset(), reason="paused_global")
    paused = {"value": True}
    notify = NewChatNotifyMiddleware(
        owner_alert_resolver=lambda _: ["synthetic-owner@s.whatsapp.net"],
        global_pause=lambda *_: "paused_global" if paused["value"] else None,
    )
    pipeline = Pipeline([
        PolicyMiddleware(policy=SimpleNamespace(evaluate=lambda _: decision)),
        AccessControlMiddleware(),
        notify,
        NoReplyFilterMiddleware(),
    ])
    event = InboundEvent(channel="whatsapp", chat_id="synthetic-new@g.us",
                         sender_id="synthetic-sender", content="synthetic",
                         timestamp=datetime(2026, 10, 10, tzinfo=UTC))
    before = seen_chats_file().read_bytes() if seen_chats_file().exists() else None
    intents = await pipeline.run(event)
    assert not any(isinstance(intent, SendOutboundIntent) for intent in intents)
    assert notify._notified == set()
    # Neither the in-memory set nor the persisted store advanced for this chat.
    after = seen_chats_file().read_bytes() if seen_chats_file().exists() else None
    assert after == before
    assert b"synthetic-new@g.us" not in (after or b"")

    paused["value"] = False
    intents = await pipeline.run(event)
    assert len([i for i in intents if isinstance(i, SendOutboundIntent)]) == 1
    assert b"synthetic-new@g.us" in seen_chats_file().read_bytes()
