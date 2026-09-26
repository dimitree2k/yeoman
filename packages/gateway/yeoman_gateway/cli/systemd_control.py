# packages/gateway/yeoman_gateway/cli/systemd_control.py
"""Thin ``systemctl --user`` wrapper for deploy-time service restarts.

The deploy path must speak to whatever actually supervises the service. On a
systemd install that is the user manager: its units never write ``run/*.pid``, so a
PID-file check cannot see them, and starting a CLI daemon next to an active unit
would leave two processes competing for the same port.

This mirrors the primitive the overseer already uses for its ``restart_service``
action (``systemctl --user restart <unit>`` followed by an ``is-active`` check), so
deploy and supervisor share one mechanism instead of two.
"""

from __future__ import annotations

import shutil
import subprocess
import time

#: How long a restarted unit gets to report itself active before the restart counts
#: as failed. Matches the overseer's post-restart grace period.
RESTART_SETTLE_SECONDS = 2.0


class Systemctl:
    """Query and restart local user units. Overridable for tests."""

    def is_available(self) -> bool:
        """Whether a user systemd is present at all.

        False means "no systemd here" - a source checkout without units - and the
        caller should fall back to the process-managed path. An *inactive* unit on a
        host that does have systemd is a different answer: the unit exists and must
        not be competed with.
        """
        if shutil.which("systemctl") is None:
            return False
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return proc.returncode == 0

    def is_active(self, unit: str) -> bool:
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return proc.returncode == 0

    def is_enabled(self, unit: str) -> bool:
        """Whether the unit is meant to be running. False = not installed/disabled."""
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-enabled", "--quiet", unit],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return proc.returncode == 0

    def stop(self, unit: str) -> bool:
        """Stop the unit. Idempotent: an already-inactive unit counts as stopped."""
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "stop", unit],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        return proc.returncode == 0

    def restart(self, unit: str) -> bool:
        """Restart the unit and confirm it came back active."""
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "restart", unit],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        if proc.returncode != 0:
            return False
        time.sleep(RESTART_SETTLE_SECONDS)
        return self.is_active(unit)
