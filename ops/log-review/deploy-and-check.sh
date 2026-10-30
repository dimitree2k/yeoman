#!/usr/bin/env bash
# Deploy origin/main from the log-review worktree and smoke-check the Gateway.
# Usage: deploy-and-check.sh <squash-merge sha>...   (the review's own merges only)
# Exit 0: deployed and healthy. 2: the deploy itself failed (nothing reverted).
# 1: smoke check failed; those commits were reverted on main and the revert deployed.
# 3: smoke check failed and the revert did not complete.
set -uo pipefail

REPO="${YEOMAN_REPO:-$HOME/Documents/yeoman}"
WORKTREE="${WORKTREE:-$HOME/.cache/yeoman-log-review/worktree}"
LOG="$HOME/.yeoman/var/logs/gateway.log"
export PATH="$HOME/.local/bin:$PATH"
[ "$#" -gt 0 ] || { echo "no commits given"; exit 2; }

deploy_at() {
  git -C "$WORKTREE" checkout -q --detach "$1" || return 1
  # A fresh worktree has no Bridge dependencies; the deploy builds the Bridge.
  if [ ! -x "$WORKTREE/packages/bridge/node_modules/.bin/tsc" ]; then
    npm ci --prefix "$WORKTREE/packages/bridge" --no-audit --no-fund || return 1
  fi
  (cd "$WORKTREE" && YEOMAN_SOURCE_DIR="$WORKTREE" yeoman deploy)
}

smoke() {
  local start_line="$1" deadline=$((SECONDS + 180)) fresh
  while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 10
    systemctl --user is-active --quiet yeoman-gateway.service || continue
    systemctl --user is-active --quiet yeoman-bridge.service || continue
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
if ! deploy_at "$AFTER"; then
  echo "deploy failed; nothing reverted"; exit 2
fi
smoke "$START_LINE" && exit 0

echo "reverting the review's commits: $*"
BRANCH="log-review/revert-$(date +%Y%m%d-%H%M%S)"
git -C "$WORKTREE" checkout -q -b "$BRANCH" "$AFTER" || exit 3
for sha in "$@"; do  # newest first: gh lists newest merges first
  git -C "$WORKTREE" revert --no-edit "$sha" || { git -C "$WORKTREE" revert --abort; exit 3; }
done
START_LINE=$(( $(wc -l < "$LOG") + 1 ))
deploy_at "$BRANCH" && smoke "$START_LINE" || echo "revert deploy is unhealthy too"
git -C "$WORKTREE" push -q -u origin "$BRANCH" || exit 3
(cd "$WORKTREE" && gh pr create --head "$BRANCH" \
  --title "Revert log-review fixes that failed the deploy smoke check" \
  --body "The automatic log review merged $*; the deploy smoke check failed, so those commits are reverted and the revert was deployed." \
  && gh pr merge "$BRANCH" --squash --delete-branch) || exit 3
exit 1
