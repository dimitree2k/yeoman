#!/usr/bin/env bash

set -u -o pipefail

YEOMAN_BIN="${YEOMAN_BIN:-yeoman}"
YEOMAN_HOME="${YEOMAN_HOME:-$HOME/.yeoman}"
CONFIG_PATH="${CONFIG_PATH:-$YEOMAN_HOME/config.json}"
POLICY_PATH="${POLICY_PATH:-$YEOMAN_HOME/policy.json}"
ENV_PATH="${ENV_PATH:-$YEOMAN_HOME/.env}"
WORKSPACE_PATH="${WORKSPACE_PATH:-$YEOMAN_HOME/workspace}"

RED="$(printf '\033[0;31m')"
YELLOW="$(printf '\033[1;33m')"
GREEN="$(printf '\033[0;32m')"
DIM="$(printf '\033[2m')"
RESET="$(printf '\033[0m')"

CATEGORIES=(memory cron config workspace gateway security system)

declare -a ISSUE_IDS=()
declare -a ISSUE_CATEGORIES=()
declare -a ISSUE_SEVERITIES=()
declare -a ISSUE_TITLES=()
declare -a ISSUE_DETAILS=()
declare -a ISSUE_FIXES=()
declare -A CATEGORY_SCORE=()
declare -A CATEGORY_COUNT=()

for category in "${CATEGORIES[@]}"; do
    CATEGORY_SCORE["$category"]=0
    CATEGORY_COUNT["$category"]=0
done

severity_score() {
    case "$1" in
        WARNING) echo 1 ;;
        CRITICAL) echo 2 ;;
        *) echo 0 ;;
    esac
}

category_label() {
    case "$1" in
        memory) echo "Memory" ;;
        cron) echo "Cron" ;;
        config) echo "Config" ;;
        workspace) echo "Workspace" ;;
        gateway) echo "Gateway" ;;
        security) echo "Security" ;;
        system) echo "System" ;;
        *) echo "$1" ;;
    esac
}

section() {
    printf "\n%s%s%s\n" "$DIM" "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')" "$RESET"
}

line_ok() {
    printf "  [%sOK%s] %s\n" "$GREEN" "$RESET" "$1"
}

line_skip() {
    printf "  [%sskip%s] %s\n" "$DIM" "$RESET" "$1"
}

add_issue() {
    local category="$1"
    local severity="$2"
    local issue_id="$3"
    local title="$4"
    local detail="$5"
    local fix="$6"
    local score

    ISSUE_CATEGORIES+=("$category")
    ISSUE_SEVERITIES+=("$severity")
    ISSUE_IDS+=("$issue_id")
    ISSUE_TITLES+=("$title")
    ISSUE_DETAILS+=("$detail")
    ISSUE_FIXES+=("$fix")

    CATEGORY_COUNT["$category"]=$((CATEGORY_COUNT["$category"] + 1))
    score="$(severity_score "$severity")"
    if [ "$score" -gt "${CATEGORY_SCORE[$category]}" ]; then
        CATEGORY_SCORE["$category"]="$score"
    fi

    if [ "$severity" = "CRITICAL" ]; then
        printf "  [%sCRIT%s] %s (%s)\n" "$RED" "$RESET" "$title" "$issue_id"
    else
        printf "  [%sWARN%s] %s (%s)\n" "$YELLOW" "$RESET" "$title" "$issue_id"
    fi
}

category_status_text() {
    local category="$1"
    local score="${CATEGORY_SCORE[$category]}"
    local count="${CATEGORY_COUNT[$category]}"

    if [ "$score" -ge 2 ]; then
        printf "CRITICAL"
    elif [ "$count" -gt 0 ]; then
        printf "WARNING (%s issue" "$count"
        if [ "$count" -ne 1 ]; then
            printf "s"
        fi
        printf ")"
    else
        printf "OK"
    fi
}

extract_field() {
    local text="$1"
    local key="$2"
    printf '%s\n' "$text" | awk -F': ' -v k="$key" '$1 == k {print $2; exit}'
}

json_is_valid() {
    local path="$1"
    python3 - <<'PY' "$path" >/dev/null 2>&1
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
with path.open("r", encoding="utf-8") as fh:
    json.load(fh)
PY
}

load_resolved_config_vars() {
    python3 - <<'PY' "$CONFIG_PATH" "$ENV_PATH"
import json
import os
import shlex
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
data = json.loads(config_path.read_text(encoding="utf-8"))

env_file = {}
if env_path.exists():
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        env_file[key] = value

def env_or_file(name: str) -> str:
    return os.environ.get(name, "") or env_file.get(name, "")

def pick(mapping, *names, default=None):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name in mapping:
            return mapping[name]
    return default

channels = data.get("channels", {}) if isinstance(data.get("channels"), dict) else {}
telegram = channels.get("telegram", {}) if isinstance(channels.get("telegram"), dict) else {}
whatsapp = channels.get("whatsapp", {}) if isinstance(channels.get("whatsapp"), dict) else {}
discord = channels.get("discord", {}) if isinstance(channels.get("discord"), dict) else {}
feishu = channels.get("feishu", {}) if isinstance(channels.get("feishu"), dict) else {}
memory = data.get("memory", {}) if isinstance(data.get("memory"), dict) else {}
memory_embedding = memory.get("embedding", {}) if isinstance(memory.get("embedding"), dict) else {}
memory_wal = memory.get("wal", {}) if isinstance(memory.get("wal"), dict) else {}
gateway = data.get("gateway", {}) if isinstance(data.get("gateway"), dict) else {}
tools = data.get("tools", {}) if isinstance(data.get("tools"), dict) else {}
tools_exec = tools.get("exec", {}) if isinstance(tools.get("exec"), dict) else {}
tools_exec_isolation = (
    tools_exec.get("isolation", {}) if isinstance(tools_exec.get("isolation"), dict) else {}
)
security = data.get("security", {}) if isinstance(data.get("security"), dict) else {}
runtime = data.get("runtime", {}) if isinstance(data.get("runtime"), dict) else {}
runtime_whatsapp = (
    runtime.get("whatsapp_bridge", {})
    if isinstance(runtime.get("whatsapp_bridge"), dict)
    else {}
)
providers = data.get("providers", {}) if isinstance(data.get("providers"), dict) else {}
agents = data.get("agents", {}) if isinstance(data.get("agents"), dict) else {}
agent_defaults = (
    agents.get("defaults", {}) if isinstance(agents.get("defaults"), dict) else {}
)

provider_env_keys = [
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "ZAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "GROQ_API_KEY",
]

provider_config_keys = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "zhipu": "ZAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY",
}

provider_count = 0
seen_provider_envs = set()
for provider_name, env_name in provider_config_keys.items():
    payload = providers.get(provider_name, {}) if isinstance(providers.get(provider_name), dict) else {}
    config_has_key = bool(str(pick(payload, "apiKey", "api_key", default="")).strip())
    env_has_key = bool(env_or_file(env_name))
    if config_has_key or env_has_key:
        provider_count += 1
        seen_provider_envs.add(env_name)

for env_name in provider_env_keys:
    if env_name in seen_provider_envs:
        continue
    if env_or_file(env_name):
        provider_count += 1

telegram_token_present = bool(str(telegram.get("token", "")).strip() or env_or_file("TELEGRAM_BOT_TOKEN"))
whatsapp_token_present = bool(
    str(pick(whatsapp, "bridgeToken", "bridge_token", default="")).strip()
    or str(pick(runtime_whatsapp, "token", default="")).strip()
    or env_or_file("WHATSAPP_BRIDGE_TOKEN")
)

values = {
    "default_model": str(agent_defaults.get("model", "anthropic/claude-opus-4-5")),
    "active_model_has_key": str(provider_count > 0).lower(),
    "provider_count": str(provider_count),
    "memory_enabled": str(bool(pick(memory, "enabled", default=True))).lower(),
    "memory_embedding_enabled": str(bool(pick(memory_embedding, "enabled", default=True))).lower(),
    "memory_wal_enabled": str(bool(pick(memory_wal, "enabled", default=True))).lower(),
    "memory_db_path": str(Path(str(pick(memory, "dbPath", "db_path", default="~/.yeoman/data/memory/memory.db"))).expanduser()),
    "telegram_enabled": str(bool(pick(telegram, "enabled", default=False))).lower(),
    "telegram_token_present": str(telegram_token_present).lower(),
    "whatsapp_enabled": str(bool(pick(whatsapp, "enabled", default=False))).lower(),
    "whatsapp_bridge_token_present": str(whatsapp_token_present).lower(),
    "whatsapp_auth_dir": str(Path(str(pick(whatsapp, "authDir", "auth_dir", default="~/.yeoman/secrets/whatsapp-auth"))).expanduser()),
    "bridge_host": str(pick(whatsapp, "bridgeHost", "bridge_host", default="127.0.0.1") or "127.0.0.1"),
    "bridge_port": str(pick(whatsapp, "bridgePort", "bridge_port", default=3001) or 3001),
    "gateway_host": str(pick(gateway, "host", default="0.0.0.0") or "0.0.0.0"),
    "gateway_port": str(pick(gateway, "port", default=18790) or 18790),
    "caldav_enabled": str(bool(env_or_file("ICLOUD_CALDAV_USERNAME") and env_or_file("ICLOUD_CALDAV_APP_PASSWORD"))).lower(),
    "exec_isolation_enabled": str(bool(pick(tools_exec_isolation, "enabled", default=True))).lower(),
    "restrict_to_workspace": str(bool(pick(tools, "restrictToWorkspace", "restrict_to_workspace", default=False))).lower(),
    "security_strict_profile": str(bool(pick(security, "strictProfile", "strict_profile", default=True))).lower(),
    "any_channel_enabled": str(bool(
        pick(telegram, "enabled", default=False)
        or pick(whatsapp, "enabled", default=False)
        or pick(discord, "enabled", default=False)
        or pick(feishu, "enabled", default=False)
    )).lower(),
}

for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
}

load_raw_config_secret_vars() {
    python3 - <<'PY' "$CONFIG_PATH"
import json
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
secret_fields = []

providers = data.get("providers", {})
if isinstance(providers, dict):
    for name, payload in providers.items():
        if isinstance(payload, dict) and (
            str(payload.get("apiKey", "")).strip() or str(payload.get("api_key", "")).strip()
        ):
            secret_fields.append(f"providers.{name}.apiKey")

channels = data.get("channels", {})
if isinstance(channels, dict):
    telegram = channels.get("telegram", {})
    if isinstance(telegram, dict) and str(telegram.get("token", "")).strip():
        secret_fields.append("channels.telegram.token")

    whatsapp = channels.get("whatsapp", {})
    if isinstance(whatsapp, dict) and (
        str(whatsapp.get("bridgeToken", "")).strip()
        or str(whatsapp.get("bridge_token", "")).strip()
    ):
        secret_fields.append("channels.whatsapp.bridgeToken")

runtime = data.get("runtime", {})
if isinstance(runtime, dict):
    wa_runtime = runtime.get("whatsappBridge", runtime.get("whatsapp_bridge", {}))
    if isinstance(wa_runtime, dict) and str(wa_runtime.get("token", "")).strip():
        secret_fields.append("runtime.whatsappBridge.token")

values = {
    "raw_secret_count": str(len(secret_fields)),
    "raw_secret_fields": ",".join(secret_fields),
}

for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
}

load_cron_vars() {
    python3 - <<'PY' "$1"
import json
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
jobs = data.get("jobs", [])

disabled = 0
failed = 0
for job in jobs:
    if not job.get("enabled", True):
        disabled += 1
    state = job.get("state") or {}
    last_status = str(state.get("lastStatus") or "").strip().lower()
    last_error = str(state.get("lastError") or "").strip()
    if last_error or last_status in {"error", "failed"}:
        failed += 1

values = {
    "cron_jobs_total": str(len(jobs)),
    "cron_jobs_disabled": str(disabled),
    "cron_jobs_failed": str(failed),
}

for key, value in values.items():
    print(f"{key}={shlex.quote(str(value))}")
PY
}

compare_version_ge() {
    python3 - <<'PY' "$1" "$2"
import sys

current = tuple(int(part) for part in sys.argv[1].split("."))
minimum = tuple(int(part) for part in sys.argv[2].split("."))
print("true" if current >= minimum else "false")
PY
}

detect_yeoman_python() {
    local yeoman_path
    local shebang
    local candidate

    yeoman_path="$(command -v "$YEOMAN_BIN" 2>/dev/null || true)"
    if [ -z "$yeoman_path" ] || [ ! -f "$yeoman_path" ]; then
        return 1
    fi

    IFS= read -r shebang < "$yeoman_path" || true
    case "$shebang" in
        '#!'*python*)
            candidate="${shebang#\#!}"
            candidate="${candidate%% *}"
            if [ -n "$candidate" ] && [ -x "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
            ;;
    esac

    return 1
}

python_missing_modules() {
    local python_bin="$1"
    shift

    "$python_bin" - <<'PY' "$@"
import importlib.util
import sys

missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
print(",".join(missing))
PY
}

printf "YEOMAN DIAGNOSTIC REPORT - %s\n" "$(date '+%Y-%m-%d %H:%M:%S')"

if ! command -v "$YEOMAN_BIN" >/dev/null 2>&1; then
    printf "\n[%sCRIT%s] yeoman not found in PATH\n" "$RED" "$RESET"
    exit 1
fi

status_output="$("$YEOMAN_BIN" status 2>&1)"
status_rc=$?

config_valid="false"
policy_valid="false"
resolved_config_loaded="false"

if [ -f "$CONFIG_PATH" ] && json_is_valid "$CONFIG_PATH"; then
    config_valid="true"
fi

if [ -f "$POLICY_PATH" ] && json_is_valid "$POLICY_PATH"; then
    policy_valid="true"
fi

if [ "$config_valid" = "true" ]; then
    if resolved_vars="$(load_resolved_config_vars 2>/dev/null)"; then
        eval "$resolved_vars"
        resolved_config_loaded="true"
    fi
fi

section "memory"
memory_output="$("$YEOMAN_BIN" memory status 2>&1)"
memory_rc=$?
if [ "$memory_rc" -ne 0 ]; then
    add_issue "memory" "CRITICAL" "MEM-001" "yeoman memory status failed" \
        "$(printf '%s' "$memory_output" | tail -n 1)" \
        "Run 'yeoman memory status' directly and inspect the traceback."
else
    line_ok "yeoman memory status completed"

    mem_enabled="$(extract_field "$memory_output" "enabled")"
    mem_wal_enabled="$(extract_field "$memory_output" "wal_enabled")"
    mem_db_path="$(extract_field "$memory_output" "db_path")"
    mem_total_active="$(extract_field "$memory_output" "total_active")"
    mem_wal_files="$(extract_field "$memory_output" "wal_files")"

    if [ "$mem_enabled" = "True" ]; then
        line_ok "memory.enabled is true"
    else
        add_issue "memory" "CRITICAL" "MEM-002" "memory is disabled" \
            "yeoman reports enabled: $mem_enabled" \
            "Set memory.enabled=true in config.json and restart the gateway."
    fi

    if [ "$resolved_config_loaded" = "true" ]; then
        if [ "$memory_embedding_enabled" = "true" ]; then
            line_ok "memory.embedding.enabled is true"
        else
            add_issue "memory" "WARNING" "MEM-003" "memory embeddings are disabled" \
                "Semantic recall routes are disabled." \
                "Set memory.embedding.enabled=true in config.json if semantic recall is required."
        fi

        if [ "$memory_wal_enabled" = "true" ]; then
            line_ok "memory.wal.enabled is true"
        else
            add_issue "memory" "WARNING" "MEM-004" "session-state WAL is disabled" \
                "Session-state files will not be persisted." \
                "Set memory.wal.enabled=true in config.json."
        fi
    fi

    if [ -n "$mem_db_path" ] && [ -f "$mem_db_path" ]; then
        line_ok "memory DB exists at $mem_db_path"
        db_kb="$(du -k "$mem_db_path" | awk '{print $1}')"
        if [ "$db_kb" -gt 512000 ]; then
            add_issue "memory" "WARNING" "MEM-007" "memory DB is larger than 500 MB" \
                "Current size: $(du -h "$mem_db_path" | awk '{print $1}')" \
                "Back up the DB, then run sqlite3 \"$mem_db_path\" 'VACUUM;'."
        else
            line_ok "memory DB size is within threshold"
        fi
    else
        add_issue "memory" "CRITICAL" "MEM-001" "memory DB path is missing" \
            "Expected DB path: ${mem_db_path:-unknown}" \
            "Verify memory.db_path and restart the gateway."
    fi

    if [ "${mem_total_active:-0}" -gt 0 ] 2>/dev/null; then
        line_ok "memory has active entries (${mem_total_active})"
    else
        add_issue "memory" "WARNING" "MEM-005" "memory has zero active entries" \
            "total_active=${mem_total_active:-0}" \
            "Check capture settings and test manual memory insertion."
    fi

    if [ "${mem_wal_files:-0}" -gt 0 ] 2>/dev/null; then
        line_ok "session-state files exist (${mem_wal_files})"
    elif [ "$resolved_config_loaded" = "true" ] && [ "$memory_wal_enabled" = "true" ]; then
        add_issue "memory" "WARNING" "MEM-004" "session-state WAL files are missing" \
            "wal_enabled is true but wal_files=${mem_wal_files:-0}" \
            "Verify workspace/memory/session-state and memory.wal.state_dir."
    fi

    if command -v sqlite3 >/dev/null 2>&1 && [ -n "${mem_db_path:-}" ] && [ -f "${mem_db_path:-}" ]; then
        journal_mode="$(sqlite3 "$mem_db_path" "PRAGMA journal_mode;" 2>/dev/null || true)"
        if [ "$journal_mode" = "wal" ]; then
            line_ok "SQLite journal mode is WAL"
        elif [ -n "$journal_mode" ]; then
            add_issue "memory" "WARNING" "MEM-006" "SQLite journal mode is not WAL" \
                "journal_mode=$journal_mode" \
                "Run sqlite3 \"$mem_db_path\" 'PRAGMA journal_mode=WAL;'."
        fi
    fi
fi

section "cron"
cron_output="$("$YEOMAN_BIN" cron list 2>&1)"
cron_rc=$?
if [ "$cron_rc" -ne 0 ]; then
    add_issue "cron" "WARNING" "CRON-001" "yeoman cron list failed" \
        "$(printf '%s' "$cron_output" | tail -n 1)" \
        "Run 'yeoman cron list' directly and inspect ~/.yeoman/data/cron/jobs.json."
else
    line_ok "yeoman cron list completed"
fi

cron_jobs_file="$YEOMAN_HOME/data/cron/jobs.json"
if [ -f "$cron_jobs_file" ]; then
    cron_vars="$(load_cron_vars "$cron_jobs_file" 2>/dev/null || true)"
    if [ -n "$cron_vars" ]; then
        eval "$cron_vars"
        line_ok "cron store parsed (${cron_jobs_total} jobs)"
        if [ "${cron_jobs_disabled:-0}" -gt 0 ] 2>/dev/null; then
            add_issue "cron" "WARNING" "CRON-002" "disabled cron jobs found" \
                "${cron_jobs_disabled} job(s) are disabled." \
                "Review 'yeoman cron list' and re-enable recurring jobs intentionally."
        fi
        if [ "${cron_jobs_failed:-0}" -gt 0 ] 2>/dev/null; then
            add_issue "cron" "WARNING" "CRON-003" "cron jobs show failure state" \
                "${cron_jobs_failed} job(s) have lastError or failed lastStatus." \
                "Inspect ~/.yeoman/data/cron/jobs.json and rerun affected jobs."
        fi
    fi
else
    line_skip "no cron jobs store found"
fi

section "config"
if [ "$status_rc" -eq 0 ]; then
    line_ok "yeoman status completed"
else
    add_issue "config" "CRITICAL" "CFG-001" "yeoman status failed" \
        "$(printf '%s' "$status_output" | tail -n 1)" \
        "Run 'yeoman status' directly and inspect config loading errors."
fi

if [ "$config_valid" = "true" ]; then
    line_ok "config.json is valid JSON"
else
    add_issue "config" "CRITICAL" "CFG-001" "config.json is invalid or missing" \
        "$CONFIG_PATH could not be parsed as JSON." \
        "Repair config.json syntax and rerun yeoman status."
fi

if [ "$policy_valid" = "true" ]; then
    line_ok "policy.json is valid JSON"
else
    add_issue "config" "CRITICAL" "CFG-002" "policy.json is invalid or missing" \
        "$POLICY_PATH could not be parsed as JSON." \
        "Repair policy.json syntax and rerun yeoman status."
fi

if [ "$resolved_config_loaded" = "true" ]; then
    if [ "${provider_count:-0}" -gt 0 ] 2>/dev/null; then
        line_ok "provider credentials resolve (${provider_count} provider(s))"
    else
        add_issue "config" "CRITICAL" "CFG-003" "no provider credentials resolve" \
            "No provider API keys were resolved from config or environment." \
            "Add a provider key in config.json, .env, or process environment."
    fi

    if [ "${active_model_has_key:-false}" = "true" ]; then
        line_ok "active default model has a resolved credential"
    else
        add_issue "config" "CRITICAL" "CFG-003" "active default model has no resolved credential" \
            "default model: ${default_model:-unknown}" \
            "Add credentials for the active model provider or change the default model."
    fi

    if [ "${telegram_enabled:-false}" = "true" ]; then
        if [ "${telegram_token_present:-false}" = "true" ]; then
            line_ok "Telegram token resolved"
        else
            add_issue "config" "CRITICAL" "CFG-004" "Telegram is enabled but no token resolves" \
                "channels.telegram.enabled=true but token is empty after config/env resolution." \
                "Set TELEGRAM_BOT_TOKEN or channels.telegram.token."
        fi
    else
        line_skip "Telegram disabled"
    fi

    if [ "${whatsapp_enabled:-false}" = "true" ]; then
        if [ "${whatsapp_bridge_token_present:-false}" = "true" ]; then
            line_ok "WhatsApp bridge token resolved"
        else
            add_issue "config" "WARNING" "CFG-005" "WhatsApp is enabled but no bridge token resolves" \
                "The bridge token is currently empty after config/env resolution." \
                "Run 'yeoman channels bridge rotate-token' or start the bridge so yeoman can generate one."
        fi
    else
        line_skip "WhatsApp disabled"
    fi
fi

if [ "$config_valid" = "true" ]; then
    raw_secret_vars="$(load_raw_config_secret_vars 2>/dev/null || true)"
    if [ -n "$raw_secret_vars" ]; then
        eval "$raw_secret_vars"
        if [ "${raw_secret_count:-0}" -gt 0 ] 2>/dev/null; then
            add_issue "config" "WARNING" "CFG-005" "config.json stores secrets directly" \
                "Fields: ${raw_secret_fields}" \
                "Move secrets to .env or process env; 'yeoman config migrate-to-env' can help."
        else
            line_ok "config.json does not store direct secret fields"
        fi
    fi
fi

section "workspace"
for required_file in SOUL.md USER.md AGENTS.md; do
    if [ -f "$WORKSPACE_PATH/$required_file" ]; then
        line_ok "$required_file exists"
    else
        add_issue "workspace" "WARNING" "WORK-001" "$required_file is missing" \
            "Expected at $WORKSPACE_PATH/$required_file" \
            "Recreate the missing workspace file."
    fi
done

if [ -d "$WORKSPACE_PATH/skills" ]; then
    skill_count="$(find "$WORKSPACE_PATH/skills" -name "SKILL.md" -type f 2>/dev/null | wc -l | tr -d ' ')"
    line_ok "workspace skills directory present (${skill_count} skill(s))"
else
    add_issue "workspace" "WARNING" "WORK-001" "workspace skills directory is missing" \
        "Expected at $WORKSPACE_PATH/skills" \
        "Create the skills directory and reinstall missing workspace skills."
fi

persona_count=0
if [ -d "$WORKSPACE_PATH/personas" ]; then
    persona_count="$(find "$WORKSPACE_PATH/personas" -maxdepth 1 -name '*.md' -type f 2>/dev/null | wc -l | tr -d ' ')"
fi
if [ "$persona_count" -gt 0 ] 2>/dev/null; then
    line_ok "persona files present (${persona_count})"
else
    add_issue "workspace" "WARNING" "WORK-002" "no persona files found" \
        "No *.md persona files found under $WORKSPACE_PATH/personas" \
        "Add persona files if you rely on persona switching."
fi

section "gateway"
gateway_output="$("$YEOMAN_BIN" gateway status 2>&1)"
gateway_rc=$?
if [ "$gateway_rc" -eq 0 ] && printf '%s' "$gateway_output" | grep -q "Gateway running"; then
    line_ok "gateway is running"
else
    if [ "${resolved_config_loaded:-false}" = "true" ] && [ "${any_channel_enabled:-false}" = "true" ]; then
        add_issue "gateway" "CRITICAL" "GATE-001" "gateway is not running while channels are enabled" \
            "$(printf '%s' "$gateway_output" | tail -n 1)" \
            "Run 'yeoman gateway start --daemon' or 'yeoman gateway'."
    else
        add_issue "gateway" "WARNING" "GATE-001" "gateway is not running" \
            "$(printf '%s' "$gateway_output" | tail -n 1)" \
            "Start the gateway if channel runtime is expected."
    fi
fi

channels_output="$("$YEOMAN_BIN" channels status 2>&1)"
channels_rc=$?
if [ "$channels_rc" -eq 0 ]; then
    line_ok "yeoman channels status completed"
else
    add_issue "gateway" "WARNING" "GATE-001" "yeoman channels status failed" \
        "$(printf '%s' "$channels_output" | tail -n 1)" \
        "Run 'yeoman channels status' directly."
fi

if [ "${resolved_config_loaded:-false}" = "true" ] && [ "${whatsapp_enabled:-false}" = "true" ]; then
    bridge_output="$("$YEOMAN_BIN" channels bridge status 2>&1)"
    bridge_rc=$?
    if [ "$bridge_rc" -eq 0 ] && printf '%s' "$bridge_output" | grep -q "Bridge running"; then
        line_ok "WhatsApp bridge is running"
    else
        add_issue "gateway" "CRITICAL" "GATE-002" "WhatsApp bridge is not running" \
            "$(printf '%s' "$bridge_output" | tail -n 1)" \
            "Run 'yeoman channels bridge restart'."
    fi

    auth_dir="${whatsapp_auth_dir:-$YEOMAN_HOME/secrets/whatsapp-auth}"
    auth_count=0
    if [ -d "$auth_dir" ]; then
        auth_count="$(find "$auth_dir" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
    fi
    if [ "$auth_count" -gt 0 ] 2>/dev/null; then
        line_ok "WhatsApp auth files present (${auth_count})"
    else
        add_issue "gateway" "CRITICAL" "GATE-003" "WhatsApp auth state is missing" \
            "No auth files found under $auth_dir" \
            "Run 'yeoman channels login'."
    fi
else
    line_skip "WhatsApp bridge checks skipped because WhatsApp is disabled"
fi

gateway_log="$YEOMAN_HOME/var/logs/gateway.log"
if [ -f "$gateway_log" ]; then
    gateway_errors="$(tail -n 200 "$gateway_log" | grep -Eic 'error|traceback|exception' || true)"
    if [ "${gateway_errors:-0}" -gt 20 ] 2>/dev/null; then
        add_issue "gateway" "WARNING" "GATE-004" "gateway log shows frequent recent errors" \
            "${gateway_errors} error-like lines in the last 200 log lines." \
            "Inspect $gateway_log."
    else
        line_ok "gateway log error rate is within threshold"
    fi
fi

bridge_log="$YEOMAN_HOME/var/logs/whatsapp-bridge.log"
if [ -f "$bridge_log" ] && [ "${resolved_config_loaded:-false}" = "true" ] && [ "${whatsapp_enabled:-false}" = "true" ]; then
    bridge_errors="$(tail -n 200 "$bridge_log" | grep -Eic 'error|exception|ERR' || true)"
    if [ "${bridge_errors:-0}" -gt 20 ] 2>/dev/null; then
        add_issue "gateway" "WARNING" "GATE-005" "bridge log shows frequent recent errors" \
            "${bridge_errors} error-like lines in the last 200 log lines." \
            "Inspect $bridge_log."
    else
        line_ok "bridge log error rate is within threshold"
    fi
fi

section "security"
if [ "${resolved_config_loaded:-false}" = "true" ]; then
    case "${gateway_host:-}" in
        127.0.0.1|localhost)
            line_ok "gateway host is local-only (${gateway_host})"
            ;;
        0.0.0.0)
            add_issue "security" "WARNING" "SEC-001" "gateway listens on all interfaces" \
                "gateway.host=${gateway_host}" \
                "Set gateway.host=127.0.0.1 if external exposure is not intentional."
            ;;
        *)
            add_issue "security" "WARNING" "SEC-001" "gateway host is not local-only" \
                "gateway.host=${gateway_host}" \
                "Use localhost/127.0.0.1 unless remote exposure is intentional."
            ;;
    esac

    if [ "${whatsapp_enabled:-false}" = "true" ]; then
        case "${bridge_host:-}" in
            127.0.0.1|localhost)
                line_ok "WhatsApp bridge host is local-only (${bridge_host})"
                ;;
            *)
                add_issue "security" "WARNING" "SEC-002" "WhatsApp bridge host is not local-only" \
                    "channels.whatsapp.bridge_host=${bridge_host}" \
                    "Set bridge_host to localhost or 127.0.0.1 unless remote bridge access is intentional."
                ;;
        esac
    fi

    if [ "${exec_isolation_enabled:-false}" = "true" ]; then
        line_ok "exec isolation is enabled"
    else
        add_issue "security" "WARNING" "SEC-003" "exec isolation is disabled" \
            "tools.exec.isolation.enabled=false" \
            "Set tools.exec.isolation.enabled=true."
    fi

    if [ "${restrict_to_workspace:-false}" = "true" ] || [ "${security_strict_profile:-false}" = "true" ]; then
        line_ok "workspace restriction or strict profile is enabled"
    else
        add_issue "security" "WARNING" "SEC-004" "workspace restriction is relaxed" \
            "Neither tools.restrictToWorkspace nor security.strictProfile is enabled." \
            "Enable tools.restrictToWorkspace and/or security.strictProfile."
    fi
fi

if [ -f "$ENV_PATH" ]; then
    env_mode="$(stat -c '%a' "$ENV_PATH" 2>/dev/null || printf 'unknown')"
    case "$env_mode" in
        600|640)
            line_ok ".env permissions are acceptable ($env_mode)"
            ;;
        *)
            add_issue "security" "WARNING" "SEC-005" ".env permissions are too open" \
                "Current mode: $env_mode" \
                "Run 'chmod 600 $ENV_PATH'."
            ;;
    esac
else
    line_skip ".env file not present"
fi

secrets_dir="$YEOMAN_HOME/secrets"
if [ -d "$secrets_dir" ]; then
    secrets_mode="$(stat -c '%a' "$secrets_dir" 2>/dev/null || printf 'unknown')"
    case "$secrets_mode" in
        700)
            line_ok "secrets directory permissions are acceptable ($secrets_mode)"
            ;;
        *)
            add_issue "security" "WARNING" "SEC-006" "secrets directory permissions are too open" \
                "Current mode: $secrets_mode" \
                "Run 'chmod 700 $secrets_dir'."
            ;;
    esac
fi

workspace_secret_matches=0
if command -v rg >/dev/null 2>&1; then
    workspace_secret_matches="$(rg -l '\b(sk-[A-Za-z0-9]{12,}|xai-[A-Za-z0-9]{12,}|gsk_[A-Za-z0-9]{12,})\b' "$WORKSPACE_PATH" 2>/dev/null | wc -l | tr -d ' ')"
else
    workspace_secret_matches="$(grep -ERl '\b(sk-[A-Za-z0-9]{12,}|xai-[A-Za-z0-9]{12,}|gsk_[A-Za-z0-9]{12,})\b' "$WORKSPACE_PATH" 2>/dev/null | wc -l | tr -d ' ')"
fi
if [ "${workspace_secret_matches:-0}" -gt 0 ] 2>/dev/null; then
    add_issue "security" "CRITICAL" "SEC-007" "likely API keys found in workspace files" \
        "${workspace_secret_matches} file(s) matched API-key patterns." \
        "Remove the secret from workspace files and rotate the credential."
else
    line_ok "no likely API keys found in workspace files"
fi

section "system"
python_version="$(python3 --version 2>/dev/null | awk '{print $2}')"
if [ -n "$python_version" ]; then
    if [ "$(compare_version_ge "$python_version" "3.14")" = "true" ]; then
        line_ok "Python version is supported ($python_version)"
    else
        add_issue "system" "WARNING" "SYS-001" "Python version is below yeoman's packaging floor" \
            "Current python3 version: $python_version; expected >= 3.14." \
            "Install Python 3.14+ and reinstall yeoman into that interpreter."
    fi
else
    add_issue "system" "CRITICAL" "SYS-001" "python3 is not available" \
        "python3 is missing from PATH." \
        "Install Python 3.14+."
fi

if [ "${resolved_config_loaded:-false}" = "true" ] && [ "${caldav_enabled:-false}" = "true" ]; then
    yeoman_python=""
    if yeoman_python="$(detect_yeoman_python 2>/dev/null)"; then
        yeoman_caldav_missing="$(python_missing_modules "$yeoman_python" caldav icalendar 2>/dev/null || true)"
        if [ -z "$yeoman_caldav_missing" ]; then
            line_ok "CalDAV dependencies are available in the yeoman runtime ($yeoman_python)"
        else
            add_issue "system" "CRITICAL" "SYS-005" "CalDAV dependencies are missing in the yeoman runtime" \
                "CalDAV credentials are configured, but $yeoman_python cannot import: $yeoman_caldav_missing" \
                "Install the missing packages into the interpreter used by 'yeoman', then restart the gateway."
        fi

        host_python="$(command -v python3 2>/dev/null || true)"
        if [ -n "$host_python" ] && [ "$host_python" != "$yeoman_python" ]; then
            host_caldav_missing="$(python_missing_modules "$host_python" caldav icalendar 2>/dev/null || true)"
            if [ -n "$host_caldav_missing" ]; then
                add_issue "system" "WARNING" "SYS-006" "host python3 lacks CalDAV dependencies used by source CLI commands" \
                    "python3 ($host_python) cannot import: $host_caldav_missing; yeoman uses $yeoman_python" \
                    "Use 'yeoman ...' or the yeoman venv interpreter, or install the missing packages into python3 before running 'python3 -m yeoman.cli.commands ...'."
            else
                line_ok "host python3 can also import CalDAV dependencies"
            fi
        fi
    else
        line_skip "CalDAV interpreter check skipped because the yeoman launcher interpreter could not be resolved"
    fi
else
    line_skip "CalDAV dependency check skipped because CalDAV credentials are not configured"
fi

if [ "${resolved_config_loaded:-false}" = "true" ] && [ "${whatsapp_enabled:-false}" = "true" ]; then
    node_version="$(node --version 2>/dev/null | tr -d 'v')"
    if [ -n "$node_version" ]; then
        if [ "$(compare_version_ge "$node_version" "18.0.0")" = "true" ]; then
            line_ok "Node.js version is supported for WhatsApp ($node_version)"
        else
            add_issue "system" "WARNING" "SYS-002" "Node.js version is below the WhatsApp floor" \
                "Current node version: $node_version; expected >= 18." \
                "Install Node.js 18+."
        fi
    else
        add_issue "system" "WARNING" "SYS-002" "node is not available" \
            "WhatsApp is enabled but node was not found in PATH." \
            "Install Node.js 18+."
    fi
else
    line_skip "Node.js check skipped because WhatsApp is disabled"
fi

free_kb="$(df -k "$YEOMAN_HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "$free_kb" ]; then
    if [ "$free_kb" -ge 1048576 ] 2>/dev/null; then
        line_ok "disk space is above 1 GB"
    else
        add_issue "system" "WARNING" "SYS-003" "free disk space is below 1 GB" \
            "Available: $((free_kb / 1024)) MB" \
            "Clear logs, media, or vacuum large SQLite databases."
    fi
fi

if [ "${resolved_config_loaded:-false}" = "true" ] && [ "${memory_enabled:-false}" = "true" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
        line_ok "sqlite3 CLI is available"
    else
        add_issue "system" "WARNING" "SYS-004" "sqlite3 CLI is missing while memory is enabled" \
            "Low-level DB diagnostics and maintenance are unavailable." \
            "Install sqlite3."
    fi
fi

printf "\nSUMMARY\n"
for category in "${CATEGORIES[@]}"; do
    printf "  %-9s %s\n" "$(category_label "$category"):" "$(category_status_text "$category")"
done

if [ "${#ISSUE_IDS[@]}" -gt 0 ]; then
    printf "\nPROBLEMS FOUND\n"
    for i in "${!ISSUE_IDS[@]}"; do
        idx=$((i + 1))
        printf "\n%d. [%s] %s - %s\n" \
            "$idx" "${ISSUE_SEVERITIES[$i]}" "${ISSUE_IDS[$i]}" "${ISSUE_TITLES[$i]}"
        printf "   %s\n" "${ISSUE_DETAILS[$i]}"
        printf "   Fix: %s\n" "${ISSUE_FIXES[$i]}"
    done
    exit 1
fi

printf "\nNo problems found.\n"
exit 0
