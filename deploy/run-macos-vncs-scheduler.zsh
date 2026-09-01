#!/bin/zsh

# VNCS 排程 host 啟動器：由 com.partsouq.vncs-scheduler LaunchAgent 呼叫。
# VNCS 同步使用 Playwright（headless Chromium）與 repo 內 TWCA 憑證，
# 不使用 CloakBrowser；因此直接使用 repo .venv，不走 release staging。

set -euo pipefail
umask 077

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
RELEASE_DIR=${PROJECT_ROOT:h}
HOST_STATE_DIR="$HOME/Library/Application Support/partsouq-vncs-scheduler"
ENV_FILE="$RELEASE_DIR/vncs-scheduler.env"
RUNTIME_LOG_DIR="$HOST_STATE_DIR/logs"
PSQ_SCHEDULER_STATE_DIR="$HOST_STATE_DIR/scheduler"
RUNTIME_PYTHON=${PARTSOUQ_RUNTIME_PYTHON:-$PROJECT_ROOT/.venv/bin/python}

if [[ "${LAUNCHD_JOB:-}" != "1" ]]; then
  print -u2 "VNCS scheduler must be started by the com.partsouq.vncs-scheduler LaunchAgent"
  exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
  print -u2 "missing VNCS scheduler env file: $ENV_FILE"
  exit 2
fi
if [[ ! -x "$RUNTIME_PYTHON" ]]; then
  print -u2 "missing runtime Python: $RUNTIME_PYTHON"
  exit 2
fi

set -a
source "$ENV_FILE"
set +a

export PARTSOUQ_HOME="$PROJECT_ROOT"
export PSQ_SCHEDULER_STATE_DIR
mkdir -p "$PSQ_SCHEDULER_STATE_DIR" "$RUNTIME_LOG_DIR"
chmod 700 "$HOST_STATE_DIR" "$PSQ_SCHEDULER_STATE_DIR" "$RUNTIME_LOG_DIR"

if ! "$RUNTIME_PYTHON" -c 'import playwright' >/dev/null 2>&1; then
  print -u2 "runtime Python is missing the playwright package: $RUNTIME_PYTHON"
  exit 2
fi

exec "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" --job vncs --daemon
