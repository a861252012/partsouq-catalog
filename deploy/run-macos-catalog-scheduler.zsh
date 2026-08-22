#!/bin/zsh

set -eu

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}

if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  print -u2 "missing $PROJECT_ROOT/.env"
  exit 2
fi

set -a
source "$PROJECT_ROOT/.env"
set +a

export PARTSOUQ_HOME="$PROJECT_ROOT"
export PSQ_CLOAK_PYTHON="${PSQ_CLOAK_PYTHON:-$HOME/.venvs/partsouq-cloak/bin/python}"
export PSQ_CLOAK_LAUNCHER=""
export PSQ_LIMIT_PARTS=0
export PSQ_BOUNDED_PARTS="${PSQ_BOUNDED_PARTS:-10000}"

if [[ ! -x "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" ]]; then
  print -u2 "missing project scheduler: $PROJECT_ROOT/.venv/bin/partsouq-scheduler"
  exit 2
fi
if [[ ! -x "$PSQ_CLOAK_PYTHON" ]]; then
  print -u2 "missing host CloakBrowser Python: $PSQ_CLOAK_PYTHON"
  exit 2
fi
if ! "$PSQ_CLOAK_PYTHON" -c 'import cloakbrowser'; then
  print -u2 "host CloakBrowser package is unavailable"
  exit 2
fi

exec "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" \
  --job catalog \
  --daemon \
  --interval-seconds "${SCHEDULER_CATALOG_INTERVAL_SECONDS:-2592000}"
