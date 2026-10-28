#!/usr/bin/env bash
# Deploy origin/main from the log-review worktree and smoke-check the Gateway.
# On failure: redeploy the previous main and merge a revert of what the review merged.
# Usage: deploy-and-check.sh <main sha before the review>
set -euo pipefail

BEFORE="$1"
REPO="${YEOMAN_REPO:-$HOME/Documents/yeoman}"
WORKTREE="${WORKTREE:-$HOME/.cache/yeoman-log-review/worktree}"
LOG="$HOME/.yeoman/var/logs/gateway.log"
export PATH="$HOME/.local/bin:$PATH"

deploy_at() {
  git -C "$WORKTREE" checkout -q --detach "$1"
  (cd "$WORKTREE" && YEOMAN_SOURCE_DIR="$WORKTREE" yeoman deploy)
}

smoke() {
  local start_line="$1" deadline=$((SECONDS + 180))
  while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 10
    systemctl --user is-active --quiet yeoman-gateway.service || continue
    systemctl --user is-active --quiet yeoman-bridge.service || continue
    local fresh
    fresh="$(tail -n +"$start_line" "$LOG" 2>/dev/null || true)"
    if grep -qE 'WhatsApp channel fatal error|ERR_SUBSCRIBE_REPLAY|Traceback' <<<"$fresh"; then
      echo "smoke: fatal channel error or traceback after deploy"; return 1
    fi
    if grep -q 'Connected to WhatsApp bridge' <<<"$fresh"; then
      echo "smoke: gateway and bridge active, WhatsApp bridge connected"; return 0
    fi
  done
  echo "smoke: no WhatsApp bridge connection within 180 s"; return 1
}

git -C "$REPO" fetch -q origin main
AFTER="$(git -C "$REPO" rev-parse origin/main)"
START_LINE=$(( $(wc -l < "$LOG") + 1 ))
if deploy_at "$AFTER" && smoke "$START_LINE"; then
  exit 0
fi

echo "rolling back to $BEFORE"
deploy_at "$BEFORE" || echo "rollback deploy failed"
BRANCH="log-review/revert-$(date +%Y%m%d-%H%M%S)"
git -C "$WORKTREE" checkout -q -b "$BRANCH" "$AFTER"
git -C "$WORKTREE" revert --no-edit "$BEFORE..$AFTER"
git -C "$WORKTREE" push -q -u origin "$BRANCH"
(cd "$WORKTREE" && gh pr create --title "Revert log-review fixes that failed the deploy smoke check" \
  --body "The automatic log review merged ${BEFORE:0:8}..${AFTER:0:8}; the deploy smoke check failed, so the previous main was redeployed. This reverts those commits." \
  && gh pr merge --squash --delete-branch) || echo "revert PR failed; main still contains the bad change"
exit 1
