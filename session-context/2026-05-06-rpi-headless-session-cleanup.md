# RPi Headless And Stale Session Cleanup

Date: 2026-05-06
Host: `moltypython`

## Current Intent

This Raspberry Pi is a headless Yeoman host. It should run SSH/Tailscale and the
Yeoman runtime, not desktop, printer, Bluetooth audio, or stale interactive agent
sessions.

Stale agent-session cleanup is now automated by the overseer starter runbook
`ops-stale-agent-session-cleanup`, scheduled daily at 04:00. It kills only
`mosh-server` roots older than 3600 seconds that contain a `codex` or `claude`
descendant.

Important: cron runbooks must not catch up on overseer startup. On 2026-05-06,
the cleanup runbook fired immediately after an overseer restart and killed the
active operator session because that session was older than the 1-hour age guard.
The trigger evaluator now initializes cron baselines on first tick and waits for
the next scheduled time.

Keep:

- `ssh.service`
- `tailscaled.service`
- `yeoman gateway` process
- `yeoman-overseer.service`
- WhatsApp bridge process from `~/.yeoman/var/cache/bridge`
- The current active operator `mosh-server -> bash -> codex` chain

Do not keep:

- Old `mosh-server -> bash -> claude` chains
- Old `mosh-server -> bash -> codex` chains
- `lightdm` / graphical desktop stack
- CUPS / printer discovery
- Bluetooth audio / PipeWire / WirePlumber
- Avahi mDNS when Tailscale/SSH is the access path

## Inspect Sessions

Use the real host process table, not Codex's sandboxed PID namespace:

```bash
ps -eo pid,ppid,user,stat,lstart,etime,pcpu,pmem,comm,args --sort=start_time
loginctl list-sessions
w
```

Focused view:

```bash
ps -eo pid,ppid,user,stat,lstart,etime,pcpu,pmem,comm,args --forest | rg 'mosh-server|claude|codex|yeoman|node dist/index.js'
```

Before killing anything, identify the current operator chain. In the active
Codex shell it should look like:

```text
mosh-server -> bash -> codex
```

Keep that chain. Kill only older sibling `mosh-server` roots.

## Kill Stale Agent Sessions

Automatic path:

```bash
yeoman overseer trigger ops-stale-agent-session-cleanup
```

Manual path:

Terminate stale `mosh-server` roots, not random child PIDs. This lets the shell
and agent child receive the session teardown together.

```bash
kill -TERM <old-mosh-pid>...
sleep 2
ps -eo pid,ppid,user,stat,lstart,etime,pcpu,pmem,comm,args --forest | rg 'mosh-server|claude|codex'
```

If a stale chain survives and is clearly not the current operator session:

```bash
kill -KILL <old-mosh-pid>...
```

## Headless Service Baseline

Set future boots to headless mode:

```bash
sudo -n systemctl set-default multi-user.target
```

Disable and stop printer, display, Bluetooth, and mDNS services:

```bash
sudo -n systemctl disable --now lightdm.service cups-browsed.service cups.service cups.socket cups.path bluetooth.service avahi-daemon.service avahi-daemon.socket
```

Disable and stop user audio/media services for `dm`:

```bash
systemctl --user disable --now pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket wireplumber.service filter-chain.service mpris-proxy.service
sudo -n systemctl --global disable pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket wireplumber.service filter-chain.service mpris-proxy.service
```

Disable leftover desktop/keyring activation:

```bash
systemctl --user disable gnome-keyring-daemon.service gnome-keyring-daemon.socket xdg-desktop-portal-rewrite-launchers.service
sudo -n systemctl --global disable gnome-keyring-daemon.service gnome-keyring-daemon.socket xdg-desktop-portal-rewrite-launchers.service
systemctl --user stop xdg-desktop-portal.service xdg-desktop-portal-wlr.service xdg-desktop-portal-gtk.service xdg-document-portal.service xdg-permission-store.service gvfs-daemon.service gvfs-udisks2-volume-monitor.service gvfs-mtp-volume-monitor.service gvfs-goa-volume-monitor.service gvfs-afc-volume-monitor.service gvfs-gphoto2-volume-monitor.service gnome-keyring-daemon.service
```

## Verify

```bash
systemctl get-default
systemctl status lightdm.service cups.service cups-browsed.service bluetooth.service avahi-daemon.service --no-pager
systemctl --user list-unit-files --type=service --type=socket | rg 'pipewire|wireplumber|mpris|filter-chain|gnome-keyring|xdg-desktop'
yeoman gateway status
yeoman overseer status
ps -eo pid,ppid,user,stat,lstart,etime,pcpu,pmem,comm,args --sort=start_time
```

Expected:

- Default target is `multi-user.target`.
- `lightdm`, CUPS, Bluetooth, and Avahi are inactive and disabled.
- PipeWire, WirePlumber, MPRIS, keyring, and desktop launcher user units are disabled.
- Only the current `mosh-server -> bash -> codex` agent chain remains.
- Gateway, overseer, and bridge are still running.
