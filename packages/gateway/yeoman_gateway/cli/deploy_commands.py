# packages/gateway/yeoman_gateway/cli/deploy_commands.py
"""Deploy pipeline CLI command."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from yeoman_gateway.cli.core import app, console
from yeoman_gateway.deploy import bridge_is_stale, find_source_repo, hash_bridge_sources

if TYPE_CHECKING:
    from yeoman_gateway.cli.systemd_control import Systemctl


@app.command()
def deploy(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without executing"),
) -> None:
    """Build, install, and restart yeoman from source."""
    repo = find_source_repo()
    if repo is None:
        console.print(
            "[red]Cannot find yeoman source repo.[/red]\n"
            "Set YEOMAN_SOURCE_DIR or ensure ~/Documents/yeoman/ exists "
            "with a workspace pyproject.toml.\n"
            "If the tool env is broken, run bin/deploy from the source repo."
        )
        raise typer.Exit(1)

    console.print(f"Source: {repo}")

    bridge_dir = repo / "packages" / "bridge"
    bridge_src = bridge_dir / "src"
    bridge_dist = bridge_dir / "dist"

    # Pre-flight: check tools
    uv = shutil.which("uv")
    if not uv:
        console.print("[red]uv not found on PATH.[/red]")
        raise typer.Exit(1)

    npm = shutil.which("npm")
    stale = bridge_is_stale(bridge_src, bridge_dist)

    # Warn about stale legacy tool env
    legacy_tool = Path.home() / ".local" / "share" / "uv" / "tools" / "yeoman"
    if legacy_tool.exists():
        console.print(
            "[yellow]Warning:[/yellow] stale legacy tool env at "
            f"{legacy_tool}\n  Remove with: uv tool uninstall yeoman"
        )

    # Step 1: Build bridge
    if stale:
        if npm:
            console.print("Building bridge (npm run build)...")
            if not dry_run:
                result = subprocess.run(
                    ["npm", "run", "build"],
                    cwd=bridge_dir,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    console.print(f"[red]Bridge build failed:[/red]\n{result.stderr[:800]}")
                    raise typer.Exit(1)
                new_hash = hash_bridge_sources(bridge_src)
                (bridge_dist / ".build-hash").write_text(new_hash)
                console.print(f"  bridge: built (hash {new_hash[:12]})")
            else:
                console.print("  bridge: [yellow]would build[/yellow] (stale)")
        else:
            console.print(
                "[red]Bridge is stale but npm/tsc not found.[/red]\n"
                f"Run 'npm run build' in {bridge_dir} manually."
            )
            raise typer.Exit(1)
    else:
        hash_file = bridge_dist / ".build-hash"
        stored = hash_file.read_text().strip()[:12] if hash_file.exists() else "?"
        console.print(f"  bridge: current (hash {stored})")

    # Step 2: refresh the bridge runtime the systemd unit starts from.
    # Must happen before any restart: the unit loads var/cache/bridge directly and
    # never calls ensure_runtime() itself.
    if not dry_run:
        try:
            from yeoman_gateway.channels.whatsapp_runtime import WhatsAppRuntimeManager

            runtime_dir = WhatsAppRuntimeManager().ensure_runtime()
            console.print(f"  bridge runtime: {runtime_dir}")
        except Exception as exc:  # noqa: BLE001 - deploy continues, restart follows
            console.print(
                f"  [yellow]bridge runtime refresh failed:[/yellow] {exc}\n"
                "  The gateway refreshes it on start, so the bridge restart below "
                "must wait for the gateway."
            )

    # Step 3: uv sync
    console.print("Syncing dev venv (uv sync)...")
    if not dry_run:
        result = subprocess.run([uv, "sync"], cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]uv sync failed:[/red]\n{result.stderr[:800]}")
            raise typer.Exit(1)
        console.print("  venv: synced")
    else:
        console.print("  venv: [yellow]would sync[/yellow]")

    # Step 4: Stop overseer before reinstall (binary gets replaced)
    _overseer_was_running = False
    if not dry_run:
        _overseer_was_running = _stop_overseer_for_reinstall()

    # Step 5: uv tool install
    console.print("Reinstalling tool env (uv tool install)...")
    if not dry_run:
        result = subprocess.run(
            [uv, "tool", "install", "--reinstall", "--editable", "packages/gateway[overseer]"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]uv tool install failed:[/red]\n{result.stderr[:800]}")
            raise typer.Exit(1)
        console.print("  tool env: reinstalled")
    else:
        console.print("  tool env: [yellow]would reinstall[/yellow]")

    # Step 6: Restart running services
    if not dry_run:
        _restart_running_services(_overseer_was_running)
    else:
        _report_running_services()

    # Step 7: Post-deploy verification
    if not dry_run:
        _verify_deploy(bridge_src, bridge_dist)

    console.print("\n[bold green]yeoman deploy — ok[/bold green]")


def _stop_overseer_for_reinstall(*, systemctl: "Systemctl | None" = None) -> bool:
    """Take the overseer down before ``uv tool install`` replaces its files.

    Returns True when a restart is owed. A systemd-managed install stops the unit so
    systemd brings it back; a process-managed one is stopped here and started again by
    :func:`_restart_running_services`. Either way the restart goes through the same
    supervisor that owns the process - a CLI-started overseer next to an active unit
    would leave systemd blind to a running service.
    """
    from yeoman_gateway.cli.systemd_control import Systemctl

    control = systemctl if systemctl is not None else Systemctl()
    if control.is_available() and control.is_active("yeoman-overseer.service"):
        if not control.stop("yeoman-overseer.service"):
            console.print("[yellow]Warning:[/yellow] could not stop yeoman-overseer.service")
        return True

    yeoman_bin = shutil.which("yeoman")
    if yeoman_bin is None:
        return False
    result = subprocess.run([yeoman_bin, "overseer", "stop"], capture_output=True, text=True)
    return result.returncode == 0


#: Services a deploy restarts, in restart order. The order is load-bearing twice
#: over: the gateway refreshes the bridge runtime on its way up, so it goes first,
#: and the overseer was stopped before the tool env was replaced, so it comes last.
_DEPLOY_SERVICES = (
    ("gateway", "yeoman-gateway.service", "gateway.pid"),
    ("bridge", "yeoman-bridge.service", "whatsapp-bridge.pid"),
    ("overseer", "yeoman-overseer.service", "overseer.pid"),
)


def _restart_running_services(
    overseer_was_running: bool = False, *, systemctl: "Systemctl | None" = None
) -> None:
    """Restart every service that is running, through whatever supervises it.

    A systemd-managed service is restarted through ``systemctl --user``, the same
    primitive the overseer uses for its own repairs. Only when there is no systemd at
    all does this fall back to the process-managed path.
    """
    from yeoman_shared.utils.helpers import get_run_path
    from yeoman_shared.utils.process import pid_alive, read_pid_file

    from yeoman_gateway.cli.systemd_control import Systemctl

    control = systemctl if systemctl is not None else Systemctl()
    run_dir = get_run_path()
    yeoman_bin = shutil.which("yeoman")
    use_units = control.is_available()

    for name, unit, pid_file in _DEPLOY_SERVICES:
        pid = read_pid_file(run_dir / pid_file)
        # Process-managed state. The overseer was stopped on purpose before the tool
        # env was replaced, so that restart is still owed.
        is_running = bool(pid and pid_alive(pid))
        stopped_before_reinstall = name == "overseer" and overseer_was_running

        if use_units:
            # Restart what systemd had live, plus anything this deploy took down on
            # purpose. A unit that is enabled but down was meant to run, so the deploy
            # raises it rather than leaving a service stopped behind an "ok".
            if control.is_active(unit) or stopped_before_reinstall:
                _reload_unit(control, name, unit, verb="Restarting")
                continue
            if control.is_enabled(unit):
                _reload_unit(control, name, unit, verb="Starting")
                continue
            console.print(f"  {name}: unit not enabled (skipped)")

        # No systemd, or nothing of this service is running: process-managed path.
        if not (is_running or stopped_before_reinstall):
            console.print(f"  {name}: not running (skipped)")
            continue

        if yeoman_bin is None:
            console.print(f"  {name}: [yellow]yeoman not on PATH — skipped[/yellow]")
            continue
        if name == "overseer":
            _start_overseer_process(yeoman_bin)
            continue

        restart_cmd = (
            [yeoman_bin, "gateway", "restart"]
            if name == "gateway"
            else [yeoman_bin, "channels", "bridge", "restart"]
        )
        console.print(f"  Restarting {name}...")
        result = subprocess.run(restart_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            console.print(f"  {name}: restarted")
        else:
            console.print(f"  {name}: [red]restart failed[/red] — {result.stderr[:200]}")


def _reload_unit(control: "Systemctl", name: str, unit: str, *, verb: str) -> None:
    """Bring one unit back through systemd and report what actually happened."""
    console.print(f"  {verb} {name} ({unit})...")
    if not control.restart(unit):
        console.print(
            f"  {name}: [red]systemctl failed[/red] — {unit} is not active afterwards"
        )
    else:
        console.print(f"  {name}: {'restarted' if verb == 'Restarting' else 'started'}")


def _start_overseer_process(yeoman_bin: str) -> None:
    """Process-managed fallback: the CLI starts the overseer without systemd."""
    subprocess.run([yeoman_bin, "overseer", "stop"], capture_output=True, check=False)
    time.sleep(0.5)
    proc = subprocess.Popen(
        [yeoman_bin, "overseer", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(1.0)
    if proc.poll() is not None:
        console.print("  overseer: [red]restart failed[/red] (exited immediately)")
    else:
        console.print(f"  overseer: restarted (PID {proc.pid})")


def _report_running_services(*, systemctl: "Systemctl | None" = None) -> None:
    """Dry-run: report which services a real deploy would restart."""
    from yeoman_shared.utils.helpers import get_run_path
    from yeoman_shared.utils.process import pid_alive, read_pid_file

    from yeoman_gateway.cli.systemd_control import Systemctl

    control = systemctl if systemctl is not None else Systemctl()
    run_dir = get_run_path()
    use_units = control.is_available()
    for name, unit, pid_file in [
        ("gateway", "yeoman-gateway.service", "gateway.pid"),
        ("bridge", "yeoman-bridge.service", "whatsapp-bridge.pid"),
        ("overseer", "yeoman-overseer.service", "overseer.pid"),
    ]:
        if use_units and control.is_active(unit):
            console.print(f"  {name}: [yellow]would restart[/yellow] ({unit})")
            continue
        pid = read_pid_file(run_dir / pid_file)
        if pid and pid_alive(pid):
            console.print(f"  {name}: [yellow]would restart[/yellow] (PID {pid})")
        else:
            console.print(f"  {name}: not running (would skip)")


def _verify_deploy(bridge_src: Path, bridge_dist: Path) -> None:
    """Post-deploy verification."""
    hash_file = bridge_dist / ".build-hash"
    if hash_file.exists():
        stored = hash_file.read_text().strip()
        current = hash_bridge_sources(bridge_src)
        if stored != current:
            console.print("[red]Warning:[/red] bridge .build-hash does not match source after deploy")

    tool_python = (
        Path.home() / ".local" / "share" / "uv" / "tools" / "yeoman-gateway" / "bin" / "python3"
    )
    if tool_python.exists():
        result = subprocess.run(
            [str(tool_python), "-c", "import openai; import croniter"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            console.print(
                "[red]Warning:[/red] overseer dependencies not importable in tool env.\n"
                "  Try: uv tool install --reinstall --editable 'packages/gateway[overseer]'"
            )
