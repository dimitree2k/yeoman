# packages/gateway/yeoman_gateway/cli/deploy_commands.py
"""Deploy pipeline CLI command."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from yeoman_gateway.cli.core import app, console
from yeoman_gateway.deploy import bridge_is_stale, find_source_repo, hash_bridge_sources


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

    # Step 2: uv sync
    console.print("Syncing dev venv (uv sync)...")
    if not dry_run:
        result = subprocess.run([uv, "sync"], cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            console.print(f"[red]uv sync failed:[/red]\n{result.stderr[:800]}")
            raise typer.Exit(1)
        console.print("  venv: synced")
    else:
        console.print("  venv: [yellow]would sync[/yellow]")

    # Step 3: Stop overseer before reinstall (binary gets replaced)
    _overseer_was_running = False
    if not dry_run:
        unit_active = subprocess.run(
            ["systemctl", "--user", "is-active", "yeoman-overseer.service"],
            capture_output=True, text=True,
        )
        if unit_active.returncode == 0:
            _overseer_was_running = True
            subprocess.run(
                ["systemctl", "--user", "stop", "yeoman-overseer.service"],
                capture_output=True,
            )

    # Step 4: uv tool install
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

    # Step 4: Restart running services
    if not dry_run:
        _restart_running_services(_overseer_was_running)
    else:
        _report_running_services()

    # Step 5: Post-deploy verification
    if not dry_run:
        _verify_deploy(bridge_src, bridge_dist)

    console.print("\n[bold green]yeoman deploy — ok[/bold green]")


def _restart_running_services(overseer_was_running: bool = False) -> None:
    """Restart services that are currently running."""
    from yeoman_shared.utils.helpers import get_run_path
    from yeoman_shared.utils.process import pid_alive, read_pid_file

    run_dir = get_run_path()
    yeoman_bin = shutil.which("yeoman")
    if not yeoman_bin:
        console.print("  [yellow]yeoman not on PATH — skipping service restarts[/yellow]")
        return

    services = [
        ("gateway", "gateway.pid", [yeoman_bin, "gateway", "restart"]),
        ("bridge", "whatsapp-bridge.pid", [yeoman_bin, "channels", "bridge", "restart"]),
        ("overseer", "overseer.pid", None),
    ]

    for name, pid_file, restart_cmd in services:
        pid = read_pid_file(run_dir / pid_file)
        is_running = pid and pid_alive(pid)
        # For overseer, also check if it was stopped pre-reinstall
        if not is_running and name == "overseer":
            is_running = overseer_was_running
        if not is_running:
            console.print(f"  {name}: not running (skipped)")
            continue

        console.print(f"  Restarting {name}...")
        if name == "overseer":
            # Prefer systemctl if the unit is enabled, otherwise fall back to CLI
            unit_check = subprocess.run(
                ["systemctl", "--user", "is-enabled", "yeoman-overseer.service"],
                capture_output=True, text=True,
            )
            if unit_check.returncode == 0:
                result = subprocess.run(
                    ["systemctl", "--user", "restart", "yeoman-overseer.service"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    # Read PID from systemd
                    show = subprocess.run(
                        ["systemctl", "--user", "show", "-p", "MainPID",
                         "yeoman-overseer.service"],
                        capture_output=True, text=True,
                    )
                    svc_pid = show.stdout.strip().split("=")[-1] if show.returncode == 0 else "?"
                    console.print(f"  {name}: restarted (PID {svc_pid})")
                else:
                    console.print(
                        f"  {name}: [red]systemctl restart failed[/red]"
                        f" — {result.stderr[:200]}"
                    )
            else:
                subprocess.run(
                    [yeoman_bin, "overseer", "stop"], capture_output=True, check=False,
                )
                import time
                time.sleep(0.5)
                log_path = get_run_path().parent / "logs" / "overseer.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a") as log_file:
                    proc = subprocess.Popen(
                        [yeoman_bin, "overseer", "start"],
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                time.sleep(1.0)
                if proc.poll() is not None:
                    console.print(
                        f"  {name}: [red]restart failed[/red] (exited immediately)"
                    )
                else:
                    console.print(f"  {name}: restarted (PID {proc.pid})")
        else:
            result = subprocess.run(restart_cmd, capture_output=True, text=True)
            if result.returncode == 0:
                console.print(f"  {name}: restarted")
            else:
                console.print(f"  {name}: [red]restart failed[/red] — {result.stderr[:200]}")


def _report_running_services() -> None:
    """Dry-run: report which services would be restarted."""
    from yeoman_shared.utils.helpers import get_run_path
    from yeoman_shared.utils.process import pid_alive, read_pid_file

    run_dir = get_run_path()
    for name, pid_file in [
        ("gateway", "gateway.pid"),
        ("bridge", "whatsapp-bridge.pid"),
        ("overseer", "overseer.pid"),
    ]:
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
