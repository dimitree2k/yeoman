#!/usr/bin/env bash
# Daily headless log review (started by the Overseer ops-log-review runbook): reads the Overseer digest,
# lets Claude Code fix up to three issues as PRs, deploys if it merged something, and sends a Telegram summary.
set -euo pipefail

REPO="${YEOMAN_REPO:-$HOME/Documents/yeoman}"
REVIEW_DIR="${REVIEW_DIR:-$HOME/.yeoman/data/ops/log-review}"
WORKTREE="${WORKTREE:-$HOME/.cache/yeoman-log-review/worktree}"
AUTONOMY="$(cat "$REVIEW_DIR/autonomy" 2>/dev/null || echo pr-only)"
BUDGET_USD="${LOG_REVIEW_BUDGET_USD:-8}"
TODAY="$(date +%F)"
RUN_DIR="$REVIEW_DIR/runs/$TODAY-$(date +%H%M%S)"
REPORT="$RUN_DIR/report.md"
CANDIDATES="$RUN_DIR/merge-candidates.txt"
export PATH="$HOME/.local/bin:$PATH"

# Only the two Telegram values are read from ~/.yeoman/.env; nothing else from it
# reaches this process or the agent (an ANTHROPIC_API_KEY there would also
# override the Claude Code login).
env_value() { sed -n "s/^$1=//p" "$HOME/.yeoman/.env" 2>/dev/null | tail -1 | tr -d "\"'"; }

notify() {
  local TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_CHAT_ID
  TELEGRAM_BOT_TOKEN="$(env_value TELEGRAM_BOT_TOKEN)"
  TELEGRAM_OWNER_CHAT_ID="$(env_value TELEGRAM_OWNER_CHAT_ID)"
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OWNER_CHAT_ID:-}" ] || return 0
  curl --fail --silent --show-error --max-time 15 \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode chat_id="$TELEGRAM_OWNER_CHAT_ID" \
    --data-urlencode text="$1" >/dev/null || true
}

exec 9>"$HOME/.yeoman/run/log-review.lock"
flock -n 9 || { echo "log review already running"; exit 0; }

DIGEST="$(ls -1 "$REVIEW_DIR"/digests/"$TODAY"*.json 2>/dev/null | tail -1 || true)"
if [ -z "$DIGEST" ]; then
  notify "🔎 Log review skipped: no digest for $TODAY (is ops-log-digest running?)"
  exit 0
fi
mkdir -p "$RUN_DIR"
[ -f "$REVIEW_DIR/ledger.json" ] || echo '{}' > "$REVIEW_DIR/ledger.json"

# Fresh worktree on origin/main so the owner's checkout is never touched.
git -C "$REPO" fetch -q origin main
if [ -d "$WORKTREE" ]; then
  git -C "$WORKTREE" checkout -q --detach origin/main
  git -C "$WORKTREE" reset -q --hard origin/main
  git -C "$WORKTREE" clean -qfd
else
  mkdir -p "$(dirname "$WORKTREE")"
  git -C "$REPO" worktree add -q --detach "$WORKTREE" origin/main
fi
(cd "$WORKTREE" && uv sync -q)

PROMPT="$(sed -e "s|\$DIGEST|$DIGEST|g" -e "s|\$REVIEW_DIR|$REVIEW_DIR|g" \
  -e "s|\$AUTONOMY|$AUTONOMY|g" -e "s|\$REPORT|$REPORT|g" -e "s|\$CANDIDATES|$CANDIDATES|g" "$REPO/ops/log-review/prompt.md")"
BEFORE_MAIN="$(git -C "$REPO" rev-parse origin/main)"
START_ISO="$(date -u +%FT%TZ)"

set +e
(cd "$WORKTREE" && timeout 5400 claude -p "$PROMPT" \
    --permission-mode dontAsk \
    --max-budget-usd "$BUDGET_USD" \
    --add-dir "$REVIEW_DIR" \
    --allowedTools "Read" "Grep" "Glob" "Edit" "Write" \
      "Bash(git:*)" "Bash(gh pr:*)" "Bash(uv run:*)" "Bash(uv sync:*)" "Bash(ulimit:*)" \
      "Bash(timeout:*)" "Bash(yeoman history read:*)" "Bash(ls:*)" "Bash(cat:*)" \
      "Bash(jq:*)" "Bash(rg:*)" "Bash(sqlite3 -readonly:*)" \
    --disallowedTools "Read(~/.yeoman/.env)" "Read(~/.yeoman/secrets/**)" \
      "Read(~/.yeoman/policy.json)" "Read(**/.env)" "Bash(yeoman deploy:*)" \
      "Bash(git push --force:*)" "Bash(gh pr merge:*)" "Bash(gh pr ready:*)" \
    --output-format json > "$RUN_DIR/claude.json" 2> "$RUN_DIR/claude.err")
STATUS=$?
set -e

SUMMARY="$(head -c 3000 "$REPORT" 2>/dev/null || echo "No report written (exit $STATUS).")"
# Second layer: the fixer only proposes candidates; headless dsh reviews each diff
# and only an APPROVE (plus green CI, size and path limits) is merged.
review_candidate() {
  local pr="$1" diff files lines verdict
  files="$(cd "$REPO" && gh pr view "$pr" --json files -q '.files[].path')" || return 1
  if grep -qE '(^|/)(policy|knowledge|history|migrations?)(/|\.py$)|prompts?/|personas?/|\.yeoman/' <<<"$files"; then
    echo "#$pr: touches a protected path"; return 1
  fi
  lines="$(cd "$REPO" && gh pr view "$pr" --json additions,deletions -q '.additions + .deletions')"
  [ "${lines:-999}" -le 80 ] || { echo "#$pr: $lines changed lines"; return 1; }
  (cd "$REPO" && timeout 900 gh pr checks "$pr" --watch --interval 20 >/dev/null) \
    || { echo "#$pr: CI not green"; return 1; }
  diff="$(cd "$REPO" && gh pr view "$pr" --json title,body -q '.title + "\n\n" + .body'; \
    cd "$REPO" && gh pr diff "$pr")"
  verdict="$(cd "$WORKTREE" && git checkout -q --detach "origin/log-review/${2}" 2>/dev/null; \
    { cat "$REPO/ops/log-review/review-prompt.md"; printf '\n--- PULL REQUEST ---\n%s\n' "$diff"; } \
    | timeout 900 dsh headless - 2>> "$RUN_DIR/dsh.err")"
  printf '#%s dsh review:\n%s\n\n' "$pr" "$verdict" >> "$RUN_DIR/dsh-reviews.md"
  [ "$(printf '%s\n' "$verdict" | grep -E '^VERDICT: ' | tail -1)" = "VERDICT: APPROVE" ] \
    || { echo "#$pr: dsh blocked"; return 1; }
  (cd "$REPO" && gh pr ready "$pr" >/dev/null && gh pr merge "$pr" --squash --delete-branch) \
    && echo "#$pr: approved by dsh and merged" \
    && jq --arg pr "/pull/$pr" '(.[] | select((.pr // "") | endswith($pr))) |= (.state = "merged")' \
      "$REVIEW_DIR/ledger.json" > "$REVIEW_DIR/ledger.json.tmp" \
    && mv "$REVIEW_DIR/ledger.json.tmp" "$REVIEW_DIR/ledger.json"
}

GATE=""
if [ "$AUTONOMY" = "auto-merge" ] && [ -s "$CANDIDATES" ]; then
  git -C "$REPO" fetch -q origin
  for pr in $(grep -oE '[0-9]+' "$CANDIDATES" | sort -u); do
    branch="$(cd "$REPO" && gh pr view "$pr" --json headRefName,state -q 'select(.state == "OPEN") | .headRefName')"
    case "$branch" in log-review/*) ;; *) GATE="$GATE"$'\n'"#$pr: not an open log-review PR"; continue ;; esac
    GATE="$GATE"$'\n'"$(review_candidate "$pr" "${branch#log-review/}" 2>&1 | tail -1)"
  done
  git -C "$WORKTREE" checkout -q --detach origin/main || true
  SUMMARY="$SUMMARY"$'\n\n'"Second review (dsh):$GATE"
fi

# Only the review's own squash merges are deployed-and-maybe-reverted; other
# threads' merges that landed meanwhile are never touched.
MERGED="$(cd "$REPO" && gh pr list --state merged --limit 20 \
  --search "head:log-review/ merged:>=$START_ISO" --json mergeCommit,headRefName \
  -q '.[] | select(.headRefName | startswith("log-review/revert-") | not) | .mergeCommit.oid' || true)"

if [ "$AUTONOMY" = "auto-merge" ] && [ -n "$MERGED" ]; then
  # shellcheck disable=SC2086
  "$REPO/ops/log-review/deploy-and-check.sh" $MERGED >> "$RUN_DIR/deploy.log" 2>&1
  case $? in
    0) SUMMARY="$SUMMARY"$'\n\n'"Deployed; smoke check passed." ;;
    2) SUMMARY="$SUMMARY"$'\n\n'"⚠️ Deploy failed before the smoke check; the merged fix is not live. See $RUN_DIR/deploy.log" ;;
    1) SUMMARY="$SUMMARY"$'\n\n'"⚠️ Smoke check failed; the review's merges were reverted and the revert deployed. See $RUN_DIR/deploy.log" ;;
    *) SUMMARY="$SUMMARY"$'\n\n'"🔴 Smoke check failed and the revert did not complete. Needs a look: $RUN_DIR/deploy.log" ;;
  esac
fi

notify "🔎 Log review $TODAY ($AUTONOMY)"$'\n'"$SUMMARY"
exit "$STATUS"
