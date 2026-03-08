#!/usr/bin/env bash
# start.sh — Launch pinchtab under bubblewrap

set -euo pipefail

PINCHTAB_BIN="$HOME/.yeoman/pinchtab/bin/pinchtab"
DATA_DIR="$HOME/.yeoman/pinchtab/data"
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN_FILE="$SKILL_DIR/state/bridge_token.txt"
PORT="${BRIDGE_PORT:-9867}"

if [[ ! -x "$PINCHTAB_BIN" ]]; then
    echo "pinchtab binary not found at $PINCHTAB_BIN" >&2
    exit 1
fi

mkdir -p "$DATA_DIR"

BRIDGE_TOKEN=""
if [[ -f "$TOKEN_FILE" ]]; then
    BRIDGE_TOKEN="$(cat "$TOKEN_FILE")"
fi

BWRAP_ARGS=(
    --ro-bind /usr /usr
    --symlink usr/bin /bin
    --symlink usr/sbin /sbin
    --symlink usr/lib /lib
)

for d in /lib32 /lib64 /libx32; do
    if [[ -e "$d" ]]; then
        if [[ -L "$d" ]]; then
            BWRAP_ARGS+=(--symlink "$(readlink "$d")" "$d")
        else
            BWRAP_ARGS+=(--ro-bind "$d" "$d")
        fi
    fi
done

BWRAP_ARGS+=(
    --ro-bind-try /etc/fonts /etc/fonts
    --ro-bind-try /etc/ssl /etc/ssl
    --ro-bind-try /etc/ca-certificates /etc/ca-certificates
    --ro-bind /etc/resolv.conf /etc/resolv.conf
    --ro-bind /etc/localtime /etc/localtime
    --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf
    --ro-bind /etc/passwd /etc/passwd
    --ro-bind /etc/group /etc/group
    --ro-bind-try /etc/hostname /etc/hostname
    --ro-bind-try /etc/chromium.d /etc/chromium.d
    --dev-bind /dev /dev
    --proc /proc
    --ro-bind /sys /sys
    --tmpfs /tmp
    --tmpfs /run
    --ro-bind "$PINCHTAB_BIN" "$PINCHTAB_BIN"
    --bind "$DATA_DIR" "$DATA_DIR"
    --setenv HOME "$DATA_DIR"
    --setenv TMPDIR /tmp
    --setenv BRIDGE_PORT "$PORT"
    --setenv BRIDGE_BIND "127.0.0.1"
    --setenv CHROME_BINARY /usr/bin/chromium
    --share-net
    --setenv CHROMIUM_FLAGS "--disable-setuid-sandbox"
    --new-session
)

[[ -n "$BRIDGE_TOKEN" ]] && BWRAP_ARGS+=(--setenv BRIDGE_TOKEN "$BRIDGE_TOKEN")
[[ "${BRIDGE_HEADLESS:-}" == "false" ]] && BWRAP_ARGS+=(--setenv BRIDGE_HEADLESS false)

echo "Starting pinchtab on 127.0.0.1:${PORT} (bwrap sandbox)..." >&2
exec bwrap "${BWRAP_ARGS[@]}" -- "$PINCHTAB_BIN"
