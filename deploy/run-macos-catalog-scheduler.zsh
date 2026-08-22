#!/bin/zsh

set -eu
umask 077

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  print -u2 "missing $PROJECT_ROOT/.env"
  exit 2
fi

source "$PROJECT_ROOT/.env"

if [[ "${PARTSOUQ_LAUNCHD_JOB:-}" != "1" ]]; then
  print -u2 "formal catalog scheduler must be started by the Aqua LaunchAgent"
  exit 2
fi
if [[ -n "${CODEX_SANDBOX:-}" || -n "${SSH_CONNECTION:-}" || -n "${CI:-}" ]]; then
  print -u2 "refusing to start headed Chromium outside an interactive macOS Aqua session"
  exit 2
fi
if [[ "${PARTSOUQ_DB_NAME:-}" != "partsouq_catalog" ]]; then
  print -u2 "formal catalog scheduler requires PARTSOUQ_DB_NAME=partsouq_catalog"
  exit 2
fi

HOST_STATE_DIR="$HOME/Library/Application Support/partsouq-catalog"
export PSQ_CLOAK_STATE_DIR="$HOST_STATE_DIR/cloak"
export PSQ_SCHEDULER_STATE_DIR="$HOST_STATE_DIR/scheduler"
mkdir -p "$PSQ_CLOAK_STATE_DIR" "$PSQ_SCHEDULER_STATE_DIR"
chmod 700 "$HOST_STATE_DIR" "$PSQ_CLOAK_STATE_DIR" "$PSQ_SCHEDULER_STATE_DIR"

export PARTSOUQ_HOME="$PROJECT_ROOT"
export PSQ_CLOAK_PYTHON="${PSQ_CLOAK_PYTHON:-$HOME/.venvs/partsouq-cloak/bin/python}"
export PSQ_CLOAK_LAUNCHER=""
export PSQ_LIMIT_PARTS=0
export PSQ_BOUNDED_PARTS=10000
export PSQ_START_BRAND=""
export PSQ_LIMIT_BRANDS=0
export PSQ_LIMIT_MODELS=0
export PSQ_LIMIT_VEHICLES=0
export PSQ_LIMIT_GROUPS=0

typeset -a RUNTIME_ENV
RUNTIME_ENV=(
  "HOME=$HOME"
  "PATH=$PATH"
  "TMPDIR=${TMPDIR:-/tmp}"
  "LANG=${LANG:-en_US.UTF-8}"
  "USER=${USER:-}"
  "LOGNAME=${LOGNAME:-}"
  "SHELL=${SHELL:-/bin/zsh}"
  "PARTSOUQ_LAUNCHD_JOB=1"
  "LAUNCHD_JOB=1"
  "PARTSOUQ_HOME=$PARTSOUQ_HOME"
  "PARTSOUQ_DB_HOST=${PARTSOUQ_DB_HOST:-127.0.0.1}"
  "PARTSOUQ_DB_PORT=${PARTSOUQ_DB_PORT:-3308}"
  "PARTSOUQ_DB_NAME=$PARTSOUQ_DB_NAME"
  "PARTSOUQ_DB_USER=${PARTSOUQ_DB_USER:-partsouq}"
  "PARTSOUQ_DB_PASSWORD=${PARTSOUQ_DB_PASSWORD:-}"
  "PSQ_CLOAK_STATE_DIR=$PSQ_CLOAK_STATE_DIR"
  "PSQ_SCHEDULER_STATE_DIR=$PSQ_SCHEDULER_STATE_DIR"
  "PSQ_CLOAK_PYTHON=$PSQ_CLOAK_PYTHON"
  "PSQ_CLOAK_LAUNCHER=$PSQ_CLOAK_LAUNCHER"
  "PSQ_LIMIT_PARTS=$PSQ_LIMIT_PARTS"
  "PSQ_BOUNDED_PARTS=$PSQ_BOUNDED_PARTS"
  "PSQ_START_BRAND=$PSQ_START_BRAND"
  "PSQ_LIMIT_BRANDS=$PSQ_LIMIT_BRANDS"
  "PSQ_LIMIT_MODELS=$PSQ_LIMIT_MODELS"
  "PSQ_LIMIT_VEHICLES=$PSQ_LIMIT_VEHICLES"
  "PSQ_LIMIT_GROUPS=$PSQ_LIMIT_GROUPS"
  "PSQ_WORKERS=${PSQ_WORKERS:-4}"
  "PSQ_REQUEST_RATE=${PSQ_REQUEST_RATE:-0.5}"
  "PSQ_REQUEST_BURST=${PSQ_REQUEST_BURST:-4}"
  "PSQ_MIN_DELAY=${PSQ_MIN_DELAY:-2.0}"
  "PSQ_MAX_DELAY=${PSQ_MAX_DELAY:-5.0}"
  "PSQ_MAX_RUN_DAYS=${PSQ_MAX_RUN_DAYS:-25}"
  "PSQ_MIN_BRANDS=${PSQ_MIN_BRANDS:-18}"
  "PSQ_EVIDENCE_MAX_BODY_BYTES=${PSQ_EVIDENCE_MAX_BODY_BYTES:-8388608}"
  "PSQ_EVIDENCE_MAX_RUN_BYTES=${PSQ_EVIDENCE_MAX_RUN_BYTES:-1073741824}"
  "PSQ_EVIDENCE_MAX_ARTIFACTS=${PSQ_EVIDENCE_MAX_ARTIFACTS:-50000}"
  "PSQ_ROW_COUNT_SHRINK_RATIO=${PSQ_ROW_COUNT_SHRINK_RATIO:-0.5}"
  "PSQ_BLOCK_BREATHER=${PSQ_BLOCK_BREATHER:-45}"
)
[[ -n "${PSQ_CLOAK_USER_AGENT:-}" ]] && RUNTIME_ENV+=("PSQ_CLOAK_USER_AGENT=$PSQ_CLOAK_USER_AGENT")
[[ -n "${SSL_CERT_DIR:-}" ]] && RUNTIME_ENV+=("SSL_CERT_DIR=$SSL_CERT_DIR")
[[ -n "${SSL_CERT_FILE:-}" ]] && RUNTIME_ENV+=("SSL_CERT_FILE=$SSL_CERT_FILE")
[[ -n "${HTTP_PROXY:-}" ]] && RUNTIME_ENV+=("HTTP_PROXY=$HTTP_PROXY")
[[ -n "${HTTPS_PROXY:-}" ]] && RUNTIME_ENV+=("HTTPS_PROXY=$HTTPS_PROXY")
[[ -n "${ALL_PROXY:-}" ]] && RUNTIME_ENV+=("ALL_PROXY=$ALL_PROXY")
[[ -n "${NO_PROXY:-}" ]] && RUNTIME_ENV+=("NO_PROXY=$NO_PROXY")
[[ -n "${http_proxy:-}" ]] && RUNTIME_ENV+=("http_proxy=$http_proxy")
[[ -n "${https_proxy:-}" ]] && RUNTIME_ENV+=("https_proxy=$https_proxy")
[[ -n "${all_proxy:-}" ]] && RUNTIME_ENV+=("all_proxy=$all_proxy")
[[ -n "${no_proxy:-}" ]] && RUNTIME_ENV+=("no_proxy=$no_proxy")

if [[ ! -x "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" ]]; then
  print -u2 "missing project scheduler: $PROJECT_ROOT/.venv/bin/partsouq-scheduler"
  exit 2
fi
if [[ ! -x "$PROJECT_ROOT/.venv/bin/partsouq-catalog-migrate" ]]; then
  print -u2 "missing migration checker: $PROJECT_ROOT/.venv/bin/partsouq-catalog-migrate"
  exit 2
fi
if [[ ! -x "$PSQ_CLOAK_PYTHON" ]]; then
  print -u2 "missing host CloakBrowser Python: $PSQ_CLOAK_PYTHON"
  exit 2
fi
if ! /usr/bin/env -i \
  "HOME=$HOME" \
  "PATH=$PATH" \
  "TMPDIR=${TMPDIR:-/tmp}" \
  "LANG=${LANG:-en_US.UTF-8}" \
  "USER=${USER:-}" \
  "LOGNAME=${LOGNAME:-}" \
  "SHELL=${SHELL:-/bin/zsh}" \
  "$PSQ_CLOAK_PYTHON" -c 'import cloakbrowser'; then
  print -u2 "host CloakBrowser package is unavailable"
  exit 2
fi

/usr/bin/env -i "${RUNTIME_ENV[@]}" "$PROJECT_ROOT/.venv/bin/partsouq-catalog-migrate" check

exec /usr/bin/env -i "${RUNTIME_ENV[@]}" "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" \
  --job catalog \
  --daemon \
  --interval-seconds "${SCHEDULER_CATALOG_INTERVAL_SECONDS:-2592000}"
