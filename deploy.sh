#!/usr/bin/env bash
set -euo pipefail

if [[ "${HYADEX_SERVER_INSTANCE:-default}" != "default" ]]; then
  echo "Direct deployment does not support named instances. Use instances.sh update NAME or a signed in-app update." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_HOST="${HYADEX_REMOTE_HOST:-${ZENITHDOCK_REMOTE_HOST:-${1:-}}}"
REMOTE_APP_DIR="${HYADEX_REMOTE_APP_DIR:-${ZENITHDOCK_REMOTE_APP_DIR:-.local/share/hyadex-server/current}}"
REMOTE_SERVER_PATH="$REMOTE_APP_DIR/agent_server.py"
REMOTE_SERVER_DIR="$(dirname "$REMOTE_SERVER_PATH")"
REMOTE_PYTHON="$REMOTE_APP_DIR/.venv/bin/python"
SERVICE_NAME="${HYADEX_SERVER_SERVICE:-${ZENITHDOCK_AGENT_SERVICE:-hyadex-server.service}}"
HEALTH_ATTEMPTS="${HYADEX_HEALTH_ATTEMPTS:-${ZENITHDOCK_HEALTH_ATTEMPTS:-45}}"
HEALTH_TOKEN="${HYADEX_AGENT_TOKEN:-${ZENITHDOCK_AGENT_TOKEN:-}}"
RUNTIME_FILES=(
  "$SCRIPT_DIR/execution_activation.py"
  "$SCRIPT_DIR/execution_legacy_runner.py"
  "$SCRIPT_DIR/execution_preparation.py"
  "$SCRIPT_DIR/update_preparation.py"
  "$SCRIPT_DIR/update_handoff.py"
  "$SCRIPT_DIR/update_recovery.py"
  "$SCRIPT_DIR/execution_update_status.py"
  "$SCRIPT_DIR/execution_uninstall.py"
  "$SCRIPT_DIR/execution_recovery.py"
  "$SCRIPT_DIR/execution_recovery_status.py"
  "$SCRIPT_DIR/execution_http.py"
  "$SCRIPT_DIR/execution_durability.py"
  "$SCRIPT_DIR/execution_control.py"
  "$SCRIPT_DIR/execution_install.py"
  "$SCRIPT_DIR/execution_maintenance.py"
  "$SCRIPT_DIR/execution_manage.py"
  "$SCRIPT_DIR/execution_ownership.py"
  "$SCRIPT_DIR/execution_service.py"
  "$SCRIPT_DIR/execution_transport.py"
  "$SCRIPT_DIR/server_instances.py"
  "$SCRIPT_DIR/agent_server.py"
  "$SCRIPT_DIR/workspace_git.py"
  "$SCRIPT_DIR/local_session_ownership.py"
  "$SCRIPT_DIR/cursor_history.py"
  "$SCRIPT_DIR/hyadex_jobs.py"
  "$SCRIPT_DIR/hyadex_chats.py"
  "$SCRIPT_DIR/hyadex/store/mailbox.py"
  "$SCRIPT_DIR/hyadex_emergency.py"
  "$SCRIPT_DIR/hyadex_notify.py"
  "$SCRIPT_DIR/hyadex_publish.py"
  "$SCRIPT_DIR/provider_commands.py"
  "$SCRIPT_DIR/provider_usage.py"
  "$SCRIPT_DIR/claude_sdk_client.py"
  "$SCRIPT_DIR/claude_model_catalog.py"
  "$SCRIPT_DIR/claude_goals.py"
  "$SCRIPT_DIR/claude_background_reconciliation.py"
  "$SCRIPT_DIR/codex_app_server.py"
  "$SCRIPT_DIR/codex_auth.py"
  "$SCRIPT_DIR/codex_provider.py"
  "$SCRIPT_DIR/provider_connections.py" "$SCRIPT_DIR/cursor_api_key.py"
  "$SCRIPT_DIR/side_questions.py"
  "$SCRIPT_DIR/hyadex/__init__.py"
  "$SCRIPT_DIR/hyadex/app.py"
  "$SCRIPT_DIR/hyadex/store/__init__.py"
  "$SCRIPT_DIR/hyadex/store/cross_chat.py"
  "$SCRIPT_DIR/hyadex/jobs/__init__.py"
  "$SCRIPT_DIR/hyadex/jobs/schedules.py"
  "$SCRIPT_DIR/hyadex/jobs/context.py"
  "$SCRIPT_DIR/hyadex/groups.py"
  "$SCRIPT_DIR/hyadex/autopilot.py"
  "$SCRIPT_DIR/hyadex/routes/__init__.py"
  "$SCRIPT_DIR/hyadex/routes/groups.py"
  "$SCRIPT_DIR/hyadex/routes/autopilot.py"
  "$SCRIPT_DIR/title_generation.py"
  "$SCRIPT_DIR/codex_side_question.py"
  "$SCRIPT_DIR/claude_side_question.py"
  "$SCRIPT_DIR/cursor_agent_client.py"
  "$SCRIPT_DIR/cursor_provider_mcp.py"
  "$SCRIPT_DIR/opencode_agent_client.py"
  "$SCRIPT_DIR/gemini_acp_client.py"
  "$SCRIPT_DIR/gemini_history.py"
  "$SCRIPT_DIR/gemini_content.py"
  "$SCRIPT_DIR/gemini_import.py"
  "$SCRIPT_DIR/gemini_runtime.py"
  "$SCRIPT_DIR/gemini_runner.py"
  "$SCRIPT_DIR/gemini_permissions.py"
  "$SCRIPT_DIR/gemini_api.py"
  "$SCRIPT_DIR/cursor_process_guard.py"
  "$SCRIPT_DIR/hyadex/store/transcripts.py"
  "$SCRIPT_DIR/claude_history_repair.py"
  "$SCRIPT_DIR/claude_history_provenance.py"
  "$SCRIPT_DIR/codex_history_repair.py"
  "$SCRIPT_DIR/public_chat_shares.py"
  "$SCRIPT_DIR/public_chat_transcript.py"
  "$SCRIPT_DIR/public_chat_share_routes.py"
  "$SCRIPT_DIR/interactive_chat_shares.py"
  "$SCRIPT_DIR/interactive_chat_share_routes.py"
  "$SCRIPT_DIR/interactive_chat_share_web.py"
  "$SCRIPT_DIR/interactive_chat_projection.py"
  "$SCRIPT_DIR/interactive_chat_runtime.py"
  "$SCRIPT_DIR/interactive_chat_native.py"
  "$SCRIPT_DIR/shared_chat_videos.py"
  "$SCRIPT_DIR/shared_chat_analytics_web.py"
  "$SCRIPT_DIR/shared_chat_video_stream.py"
  "$SCRIPT_DIR/interactive_chat_controls.py"
  "$SCRIPT_DIR/update_runner.py"
  "$SCRIPT_DIR/VERSION"
  "$SCRIPT_DIR/release-public-key.pem"
  "$SCRIPT_DIR/LICENSE"
  "$SCRIPT_DIR/NOTICE"
)

if [[ -z "$REMOTE_HOST" ]]; then
  cat >&2 <<'USAGE'
Usage:
  HYADEX_REMOTE_HOST=<ssh-host> ./deploy.sh
  ./deploy.sh <ssh-host>

Optional:
  HYADEX_REMOTE_APP_DIR=<remote-app-dir>
  HYADEX_SERVER_SERVICE=<systemd-user-service>
  HYADEX_AGENT_TOKEN=<health-check-token>
  HYADEX_HEALTH_ATTEMPTS=<startup-health-attempts>

Direct in-place deployment is refused when the target hosts Team Hub or has a
paired Teamspace client. Use the signed managed update so its versioned
rollback and Teamspace continuity checks remain in force.
USAGE
  exit 2
fi

if [[ -n "$HEALTH_TOKEN" ]] && [[ ! "$HEALTH_TOKEN" =~ ^[A-Za-z0-9_-]{32,}$ ]]; then
  echo "HYADEX_AGENT_TOKEN is invalid." >&2
  exit 2
fi
if [[ -z "$HEALTH_TOKEN" ]]; then
  echo "HYADEX_AGENT_TOKEN is required to prove exact post-deploy version." >&2
  echo "  Use the signed managed update if authenticated in-place health is unavailable." >&2
  exit 2
fi

refuse_remote_execution_layout() {
  local quoted_runtime=""
  printf -v quoted_runtime '%q' "$REMOTE_SERVER_DIR"
  ssh "$REMOTE_HOST" "bash -s -- $quoted_runtime" <<'EXECUTION_LAYOUT_CHECK'
set -euo pipefail
check_ancestors() {
  local candidate="$1" marker=""
  while :; do
    for marker in "$candidate/execution-layout.json" "$candidate/.execution-transaction"; do
      if [[ -e "$marker" || -L "$marker" ]]; then
        echo "deploy.sh does not support the separate gateway/execution layout or its pending transaction." >&2
        echo "Use the execution-aware managed lifecycle; the pinned worker has not been changed." >&2
        return 1
      fi
    done
    [[ "$candidate" != "/" ]] || break
    candidate="$(dirname -- "$candidate")"
  done
}
runtime="$1"
[[ "$runtime" == /* ]] || runtime="$PWD/$runtime"
check_ancestors "$runtime"
# Check both the current-link namespace and its physical release ancestors.
# A caller may name releases/V directly instead of the managed current link.
while [[ ! -d "$runtime" && "$runtime" != "/" ]]; do
  runtime="$(dirname -- "$runtime")"
done
runtime="$(cd -P -- "$runtime" && pwd -P)"
check_ancestors "$runtime"
EXECUTION_LAYOUT_CHECK
}

refuse_remote_execution_layout || exit 1

EXPECTED_SERVER_IDENTITY="$(
  ssh "$REMOTE_HOST" \
    "curl -fsS --connect-timeout 2 --max-time 5 --max-filesize 1048576 -H 'Authorization: Bearer ${HEALTH_TOKEN}' http://127.0.0.1:7870/api/health" | python3 -c '
import json, re, sys
health = json.load(sys.stdin)
identity = health.get("server_identity")
if health.get("ok") is not True or not isinstance(identity, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", identity) is None:
    raise SystemExit(1)
print(identity)
'
)" || { echo "Cannot verify the target server identity." >&2; exit 1; }

echo "Deploying HyadexServer runtime to $REMOTE_HOST:$REMOTE_SERVER_DIR"
refuse_remote_execution_layout || exit 1
ssh "$REMOTE_HOST" "mkdir -p '$REMOTE_SERVER_DIR'"
# Preserve package paths without opening one SSH connection per file.
runtime_parents=()
for runtime_file in "${RUNTIME_FILES[@]}"; do
  relative_file="${runtime_file#"$SCRIPT_DIR/"}"
  relative_parent="$(dirname -- "$relative_file")"
  parent_seen=false
  for existing_parent in "${runtime_parents[@]+"${runtime_parents[@]}"}"; do
    [[ "$existing_parent" != "$relative_parent" ]] || parent_seen=true
  done
  if [[ "$parent_seen" == false ]]; then
    runtime_parents+=("$relative_parent")
  fi
done
for relative_parent in "${runtime_parents[@]}"; do
  parent_files=()
  for runtime_file in "${RUNTIME_FILES[@]}"; do
    relative_file="${runtime_file#"$SCRIPT_DIR/"}"
    if [[ "$(dirname -- "$relative_file")" == "$relative_parent" ]]; then
      parent_files+=("$runtime_file")
    fi
  done
  printf -v remote_parent '%q' "$REMOTE_SERVER_DIR/$relative_parent"
  ssh "$REMOTE_HOST" "mkdir -p -- $remote_parent"
  scp "${parent_files[@]}" "$REMOTE_HOST:$REMOTE_SERVER_DIR/$relative_parent/"
done
echo "Checking server runtime dependencies"
ssh "$REMOTE_HOST" "
  if ! '$REMOTE_PYTHON' -c 'from importlib.metadata import version; import claude_agent_sdk, croniter, cryptography, dateutil, tzdata; sdk_version = version(\"claude-agent-sdk\"); raise SystemExit(0 if sdk_version == \"0.2.130\" else f\"expected claude-agent-sdk 0.2.130, got {sdk_version}\")' >/dev/null 2>&1; then
    if [[ -x \"\$HOME/.local/bin/uv\" ]]; then
      \"\$HOME/.local/bin/uv\" pip install --python '$REMOTE_PYTHON' \
        --no-binary claude-agent-sdk \
        'claude-agent-sdk==0.2.130' 'croniter>=6,<7' 'cryptography>=44,<47' 'python-dateutil>=2.9,<3' 'tzdata>=2025.2'
    else
      '$REMOTE_PYTHON' -m pip install \
        --no-binary claude-agent-sdk \
        'claude-agent-sdk==0.2.130' 'croniter>=6,<7' 'cryptography>=44,<47' 'python-dateutil>=2.9,<3' 'tzdata>=2025.2'
    fi
  fi
  PYTHONPATH='$REMOTE_SERVER_DIR' '$REMOTE_PYTHON' -c 'from importlib.metadata import version; import hyadex.jobs, hyadex.jobs.schedules, hyadex.jobs.context; import hyadex.store.cross_chat; import hyadex.app, hyadex.store, hyadex.store.mailbox, hyadex.store.transcripts; import hyadex, hyadex.groups, hyadex.autopilot, hyadex.routes, hyadex.routes.groups, hyadex.routes.autopilot; import codex_auth, codex_provider, provider_connections, side_questions, codex_side_question, claude_side_question, claude_agent_sdk, cursor_agent_client, cursor_provider_mcp, cursor_process_guard, provider_commands, opencode_agent_client, gemini_acp_client, gemini_history, gemini_content, gemini_import, gemini_runtime, gemini_runner, gemini_permissions, gemini_api; sdk_version = version(\"claude-agent-sdk\"); raise SystemExit(0 if sdk_version == \"0.2.130\" else f\"expected claude-agent-sdk 0.2.130, got {sdk_version}\")'
"

echo "Compiling server on $REMOTE_HOST"
ssh "$REMOTE_HOST" "PYTHONPATH='$REMOTE_SERVER_DIR' '$REMOTE_PYTHON' -c 'import workspace_git, claude_model_catalog'"
ssh "$REMOTE_HOST" "chmod 755 '$REMOTE_SERVER_DIR/hyadex_jobs.py' '$REMOTE_SERVER_DIR/hyadex_chats.py' '$REMOTE_SERVER_DIR/hyadex_emergency.py' '$REMOTE_SERVER_DIR/hyadex_notify.py' '$REMOTE_SERVER_DIR/hyadex_publish.py' && '$REMOTE_PYTHON' -m py_compile '$REMOTE_SERVER_PATH' '$REMOTE_SERVER_DIR/hyadex/__init__.py' '$REMOTE_SERVER_DIR/hyadex/app.py' '$REMOTE_SERVER_DIR/hyadex/store/__init__.py' '$REMOTE_SERVER_DIR/hyadex/store/cross_chat.py' '$REMOTE_SERVER_DIR/hyadex/jobs/__init__.py' '$REMOTE_SERVER_DIR/hyadex/jobs/schedules.py' '$REMOTE_SERVER_DIR/hyadex/jobs/context.py' '$REMOTE_SERVER_DIR/hyadex/groups.py' '$REMOTE_SERVER_DIR/hyadex/autopilot.py' '$REMOTE_SERVER_DIR/hyadex/routes/__init__.py' '$REMOTE_SERVER_DIR/hyadex/routes/groups.py' '$REMOTE_SERVER_DIR/hyadex/routes/autopilot.py' '$REMOTE_SERVER_DIR/execution_durability.py' '$REMOTE_SERVER_DIR/execution_http.py' '$REMOTE_SERVER_DIR/execution_recovery_status.py' '$REMOTE_SERVER_DIR/execution_recovery.py' '$REMOTE_SERVER_DIR/execution_uninstall.py' '$REMOTE_SERVER_DIR/update_recovery.py' '$REMOTE_SERVER_DIR/execution_activation.py' '$REMOTE_SERVER_DIR/execution_legacy_runner.py' '$REMOTE_SERVER_DIR/execution_preparation.py' '$REMOTE_SERVER_DIR/execution_update_status.py' '$REMOTE_SERVER_DIR/update_handoff.py' '$REMOTE_SERVER_DIR/update_preparation.py' '$REMOTE_SERVER_DIR/execution_control.py' '$REMOTE_SERVER_DIR/execution_install.py' '$REMOTE_SERVER_DIR/execution_maintenance.py' '$REMOTE_SERVER_DIR/execution_manage.py' '$REMOTE_SERVER_DIR/execution_ownership.py' '$REMOTE_SERVER_DIR/execution_service.py' '$REMOTE_SERVER_DIR/execution_transport.py' '$REMOTE_SERVER_DIR/workspace_git.py' '$REMOTE_SERVER_DIR/hyadex_jobs.py' '$REMOTE_SERVER_DIR/hyadex_chats.py' '$REMOTE_SERVER_DIR/hyadex/store/mailbox.py' '$REMOTE_SERVER_DIR/provider_commands.py' '$REMOTE_SERVER_DIR/provider_usage.py' '$REMOTE_SERVER_DIR/hyadex_emergency.py' '$REMOTE_SERVER_DIR/hyadex_notify.py' '$REMOTE_SERVER_DIR/hyadex_publish.py' '$REMOTE_SERVER_DIR/claude_sdk_client.py' '$REMOTE_SERVER_DIR/claude_model_catalog.py' '$REMOTE_SERVER_DIR/claude_goals.py' '$REMOTE_SERVER_DIR/claude_background_reconciliation.py' '$REMOTE_SERVER_DIR/codex_app_server.py' '$REMOTE_SERVER_DIR/codex_auth.py' '$REMOTE_SERVER_DIR/codex_provider.py' '$REMOTE_SERVER_DIR/provider_connections.py' '$REMOTE_SERVER_DIR/cursor_api_key.py' '$REMOTE_SERVER_DIR/side_questions.py' '$REMOTE_SERVER_DIR/title_generation.py' '$REMOTE_SERVER_DIR/codex_side_question.py' '$REMOTE_SERVER_DIR/claude_side_question.py' '$REMOTE_SERVER_DIR/cursor_agent_client.py' '$REMOTE_SERVER_DIR/cursor_provider_mcp.py' '$REMOTE_SERVER_DIR/opencode_agent_client.py' '$REMOTE_SERVER_DIR/gemini_acp_client.py' '$REMOTE_SERVER_DIR/gemini_history.py' '$REMOTE_SERVER_DIR/gemini_content.py' '$REMOTE_SERVER_DIR/gemini_import.py' '$REMOTE_SERVER_DIR/gemini_runtime.py' '$REMOTE_SERVER_DIR/gemini_runner.py' '$REMOTE_SERVER_DIR/gemini_permissions.py' '$REMOTE_SERVER_DIR/gemini_api.py' '$REMOTE_SERVER_DIR/cursor_process_guard.py' '$REMOTE_SERVER_DIR/hyadex/store/transcripts.py' '$REMOTE_SERVER_DIR/claude_history_repair.py' '$REMOTE_SERVER_DIR/claude_history_provenance.py' '$REMOTE_SERVER_DIR/codex_history_repair.py' '$REMOTE_SERVER_DIR/public_chat_shares.py' '$REMOTE_SERVER_DIR/public_chat_transcript.py' '$REMOTE_SERVER_DIR/public_chat_share_routes.py' '$REMOTE_SERVER_DIR/interactive_chat_shares.py' '$REMOTE_SERVER_DIR/interactive_chat_share_routes.py' '$REMOTE_SERVER_DIR/interactive_chat_share_web.py' '$REMOTE_SERVER_DIR/interactive_chat_projection.py' '$REMOTE_SERVER_DIR/interactive_chat_runtime.py' '$REMOTE_SERVER_DIR/interactive_chat_native.py' '$REMOTE_SERVER_DIR/shared_chat_videos.py' '$REMOTE_SERVER_DIR/shared_chat_analytics_web.py' '$REMOTE_SERVER_DIR/shared_chat_video_stream.py' '$REMOTE_SERVER_DIR/interactive_chat_controls.py' '$REMOTE_SERVER_DIR/update_runner.py' '$REMOTE_SERVER_DIR/server_instances.py' '$REMOTE_SERVER_DIR/local_session_ownership.py' '$REMOTE_SERVER_DIR/cursor_history.py'"

echo "Restarting $SERVICE_NAME"
ssh "$REMOTE_HOST" "systemctl --user restart '$SERVICE_NAME'"

echo "Checking health"
HEALTH_OK=0
EXPECTED_VERSION="$(tr -d '[:space:]' < "$SCRIPT_DIR/VERSION")"
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
  if REMOTE_HEALTH="$(
    ssh "$REMOTE_HOST" \
      "curl -fsS --connect-timeout 2 --max-time 5 --max-filesize 1048576 -H 'Authorization: Bearer ${HEALTH_TOKEN}' http://127.0.0.1:7870/api/health"
  )"; then
    if ((${#REMOTE_HEALTH} <= 1048576)) && \
      printf '%s' "$REMOTE_HEALTH" | python3 -c '
import json
import re
import sys

expected_version = sys.argv[1]
expected_server_identity = sys.argv[2]
try:
    health = json.load(sys.stdin)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
if not isinstance(health, dict) or health.get("ok") is not True:
    raise SystemExit(1)
if health.get("server_version") != expected_version:
    raise SystemExit(1)
server_identity = health.get("server_identity")
if not isinstance(server_identity, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{8,240}", server_identity) is None:
    raise SystemExit(1)
if server_identity != expected_server_identity:
    raise SystemExit(1)
' "$EXPECTED_VERSION" "$EXPECTED_SERVER_IDENTITY"; then
      printf '%s\n' "$REMOTE_HEALTH"
      HEALTH_OK=1
      break
    fi
  fi
  sleep 1
done
if [[ "$HEALTH_OK" != "1" ]]; then
  echo "Authenticated health did not prove exact version $EXPECTED_VERSION and the stable server identity." >&2
  exit 1
fi
echo
