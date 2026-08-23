#!/bin/zsh

set -euo pipefail
umask 077

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
RELEASE_DIR=${PROJECT_ROOT:h}
RUNTIME_CONFIG="$RELEASE_DIR/scheduler.env"

if [[ "${PARTSOUQ_LAUNCHD_JOB:-}" != "1" ]]; then
  print -u2 "formal catalog scheduler must be started by the Aqua LaunchAgent"
  exit 2
fi
if [[ -n "${CODEX_SANDBOX:-}" || -n "${SSH_CONNECTION:-}" || -n "${CI:-}" ]]; then
  print -u2 "refusing to start headed Chromium outside an interactive macOS Aqua session"
  exit 2
fi
reject_forbidden_runtime_env() {
  typeset -a FORBIDDEN_ENV
  FORBIDDEN_ENV=(
    CLOAKBROWSER_API_KEY
    CLOAKBROWSER_BINARY_PATH
    CLOAKBROWSER_DOWNLOAD_URL
    CLOAKBROWSER_LICENSE_KEY
    CLOAKBROWSER_SKIP_CHECKSUM
    CLOAKBROWSER_TOKEN
  )
  for NAME in "${FORBIDDEN_ENV[@]}"; do
    if (( ${+parameters[$NAME]} )) && [[ -n "${(P)NAME}" ]]; then
      print -u2 "refusing forbidden CloakBrowser setting: $NAME"
      exit 2
    fi
  done

  typeset -a FORBIDDEN_NETWORK_ENV
  FORBIDDEN_NETWORK_ENV=(
    HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
    SSL_CERT_DIR SSL_CERT_FILE
  )
  for NAME in "${FORBIDDEN_NETWORK_ENV[@]}"; do
    if (( ${+parameters[$NAME]} )) && [[ -n "${(P)NAME}" ]]; then
      print -u2 "refusing proxy or custom TLS setting in the formal scheduler: $NAME"
      exit 2
    fi
  done
}

reject_forbidden_runtime_env

HOST_STATE_DIR="$HOME/Library/Application Support/partsouq-catalog"
export PSQ_CLOAK_STATE_DIR="$HOST_STATE_DIR/cloak"
export PSQ_SCHEDULER_STATE_DIR="$HOST_STATE_DIR/scheduler"
export PSQ_RUNTIME_LOG_DIR="$HOST_STATE_DIR/logs/runtime"
export CLOAKBROWSER_CACHE_DIR="$HOST_STATE_DIR/cloak/free-browser-cache"
export CLOAKBROWSER_AUTO_UPDATE=false
READY_MARKER="$PSQ_SCHEDULER_STATE_DIR/launch-ready-${RELEASE_DIR:t}"
reject_existing_symlink_ancestors() {
  local PRIVATE_PATH CURRENT_PATH
  for PRIVATE_PATH in "$@"; do
    CURRENT_PATH=$PRIVATE_PATH
    while [[ "$CURRENT_PATH" != "/" ]]; do
      if [[ -L "$CURRENT_PATH" ]]; then
        print -u2 "refusing symlink in private runtime state path: $CURRENT_PATH"
        return 2
      fi
      CURRENT_PATH=${CURRENT_PATH:h}
    done
  done
}

reject_existing_symlink_ancestors \
    "$HOST_STATE_DIR" \
    "$PSQ_CLOAK_STATE_DIR" \
    "$PSQ_SCHEDULER_STATE_DIR" \
    "$HOST_STATE_DIR/quarantine" \
    "$HOST_STATE_DIR/releases" \
    "$HOST_STATE_DIR/logs" \
    "$PSQ_RUNTIME_LOG_DIR" \
    "$CLOAKBROWSER_CACHE_DIR"
mkdir -p \
  "$PSQ_CLOAK_STATE_DIR" \
  "$PSQ_SCHEDULER_STATE_DIR" \
  "$PSQ_RUNTIME_LOG_DIR" \
  "$CLOAKBROWSER_CACHE_DIR"
chmod 700 "$HOST_STATE_DIR" "$PSQ_CLOAK_STATE_DIR" "$PSQ_SCHEDULER_STATE_DIR" \
  "$PSQ_RUNTIME_LOG_DIR" "$CLOAKBROWSER_CACHE_DIR"
if [[ -e "$CLOAKBROWSER_CACHE_DIR/license.key" ]]; then
  print -u2 "refusing CloakBrowser free cache containing license.key"
  exit 2
fi
typeset -a PRO_ARTIFACTS
PRO_ARTIFACTS=(
  "$CLOAKBROWSER_CACHE_DIR/license.key"(N)
  "$CLOAKBROWSER_CACHE_DIR/.license_cache"(N)
  "$CLOAKBROWSER_CACHE_DIR/.last_pro_version_check"(N)
  "$CLOAKBROWSER_CACHE_DIR/.last_pro_update_check"(N)
  "$CLOAKBROWSER_CACHE_DIR"/latest_pro_version_*(N)
  "$CLOAKBROWSER_CACHE_DIR"/*[pP][rR][oO]*(N/)
)
if (( ${#PRO_ARTIFACTS} )); then
  print -u2 "refusing CloakBrowser Pro artifacts in the free-only cache"
  exit 2
fi
if [[ ! -f "$RELEASE_DIR/.trusted-free-runtime-v1" ]]; then
  print -u2 "missing trusted free-runtime marker"
  exit 2
fi

validate_release_permissions() {
  CURRENT_UID=$(/usr/bin/id -u)
  INVALID_OWNER=$(/usr/bin/find "$RELEASE_DIR" ! -uid "$CURRENT_UID" -print -quit)
  INVALID_MODE=$(/usr/bin/find "$RELEASE_DIR" ! -type l -perm +077 -print -quit)
  if [[ -n "$INVALID_OWNER" || -n "$INVALID_MODE" ]]; then
    print -u2 "staged runtime ownership or permissions are not owner-only"
    return 2
  fi
  for PRIVATE_FILE in scheduler.env source.sha256 runtime.sha256 cloak-browser.sha256; do
    if [[ ! -f "$RELEASE_DIR/$PRIVATE_FILE" \
        || "$(/usr/bin/stat -f '%Lp' "$RELEASE_DIR/$PRIVATE_FILE")" != "600" ]]; then
      print -u2 "staged runtime manifest/config must be owner-only 0600: $PRIVATE_FILE"
      return 2
    fi
  done
}

validate_release_permissions

for MANIFEST in source.sha256 runtime.sha256 cloak-browser.sha256; do
  if [[ ! -f "$RELEASE_DIR/$MANIFEST" ]]; then
    print -u2 "missing staged runtime integrity manifest: $MANIFEST"
    exit 2
  fi
done
SOURCE_CHECK=$(/usr/bin/mktemp "$PSQ_SCHEDULER_STATE_DIR/source-check.XXXXXX")
RUNTIME_CHECK=$(/usr/bin/mktemp "$PSQ_SCHEDULER_STATE_DIR/runtime-check.XXXXXX")
trap '/bin/rm -f "$SOURCE_CHECK" "$RUNTIME_CHECK"' EXIT HUP INT TERM
(
  cd "$PROJECT_ROOT"
  /usr/bin/find . -type f ! -path './.venv/*' -print0 \
    | /usr/bin/xargs -0 /usr/bin/shasum -a 256 \
    | LC_ALL=C /usr/bin/sort
) > "$SOURCE_CHECK"

(
  cd "$RELEASE_DIR"
  CURRENT_UID=$(/usr/bin/id -u)
  CURRENT_GROUP_IDS=" $(/usr/bin/id -G) "
  {
    print -rn -- \
      $'scheduler.env\0source.sha256\0cloak-browser.sha256\0.install-complete\0.trusted-free-runtime-v1\0'
    /usr/bin/find app/.venv cloak-venv \( -type f -o -type l \) -print0
  } | while IFS= read -r -d '' FILE; do
    if [[ -L "$FILE" ]]; then
      RESOLVED_FILE=${FILE:A}
      if [[ ! -f "$RESOLVED_FILE" ]]; then
        print -u2 "runtime symlink target is missing or not a regular file: $FILE"
        exit 2
      fi
      TARGET_UID=$(/usr/bin/stat -f '%u' "$RESOLVED_FILE")
      TARGET_GID=$(/usr/bin/stat -f '%g' "$RESOLVED_FILE")
      TARGET_MODE=$(/usr/bin/stat -f '%Lp' "$RESOLVED_FILE")
      GROUP_WRITABLE_BY_CURRENT_USER=0
      if (( (8#$TARGET_MODE & 8#20) != 0 )) \
          && [[ "$CURRENT_GROUP_IDS" == *" $TARGET_GID "* ]]; then
        GROUP_WRITABLE_BY_CURRENT_USER=1
      fi
      if [[ "$TARGET_UID" != "0" && "$TARGET_UID" != "$CURRENT_UID" ]] \
          || (( (8#$TARGET_MODE & 8#2) != 0 )) \
          || (( GROUP_WRITABLE_BY_CURRENT_USER != 0 )); then
        print -u2 "runtime symlink target has unsafe owner or writable mode: $FILE"
        exit 2
      fi
      TARGET_SHA=$(/usr/bin/shasum -a 256 "$RESOLVED_FILE")
      print -r -- \
        $'L\t'"${TARGET_SHA%% *}"$'\t'"$FILE"$'\t'"$(/usr/bin/readlink "$FILE")"
    else
      FILE_SHA=$(/usr/bin/shasum -a 256 "$FILE")
      print -r -- $'F\t'"${FILE_SHA%% *}"$'\t'"$FILE"
    fi
  done | LC_ALL=C /usr/bin/sort
) > "$RUNTIME_CHECK"
EXPECTED_LINE=$(/usr/bin/head -n 1 "$RELEASE_DIR/cloak-browser.sha256")
EXPECTED_CLOAK_SHA256=${EXPECTED_LINE%% *}
EXPECTED_CLOAK_BINARY=${EXPECTED_LINE#*  }
EXPECTED_CLOAK_BINARY=${EXPECTED_CLOAK_BINARY:A}
EXPECTED_CLOAK_RELATIVE=${EXPECTED_CLOAK_BINARY#"$CLOAKBROWSER_CACHE_DIR"/}
EXPECTED_CLOAK_VERSION=${EXPECTED_CLOAK_RELATIVE%%/*}
if [[ ! "$EXPECTED_CLOAK_SHA256" =~ '^[0-9a-f]{64}$' \
    || "$EXPECTED_CLOAK_BINARY" != "$CLOAKBROWSER_CACHE_DIR"/* \
    || "$EXPECTED_CLOAK_VERSION" != chromium-* \
    || "$EXPECTED_CLOAK_VERSION" == *[pP][rR][oO]* \
    || ! -x "$EXPECTED_CLOAK_BINARY" ]]; then
  print -u2 "invalid staged CloakBrowser free binary manifest"
  exit 2
fi
if ! /usr/bin/cmp -s "$SOURCE_CHECK" "$RELEASE_DIR/source.sha256" \
    || ! /usr/bin/cmp -s "$RUNTIME_CHECK" "$RELEASE_DIR/runtime.sha256" \
    || ! /usr/bin/shasum -a 256 -c "$RELEASE_DIR/cloak-browser.sha256" >/dev/null; then
  print -u2 "staged runtime integrity check failed"
  exit 2
fi
/bin/rm -f "$SOURCE_CHECK"
/bin/rm -f "$RUNTIME_CHECK"
trap - EXIT HUP INT TERM
if [[ ! -f "$RUNTIME_CONFIG" ]]; then
  print -u2 "missing staged scheduler config: $RUNTIME_CONFIG"
  exit 2
fi
source "$RUNTIME_CONFIG"
reject_forbidden_runtime_env
if [[ "${PARTSOUQ_DB_NAME:-}" != "partsouq_catalog" ]]; then
  print -u2 "formal catalog scheduler requires PARTSOUQ_DB_NAME=partsouq_catalog"
  exit 2
fi
export CLOAKBROWSER_BINARY_PATH="$EXPECTED_CLOAK_BINARY"

export PARTSOUQ_HOME="$PROJECT_ROOT"
export PSQ_CLOAK_PYTHON="$RELEASE_DIR/cloak-venv/bin/python"
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
  "PARTSOUQ_APPLY_MIGRATIONS_ON_START=1"
  "PYTHONDONTWRITEBYTECODE=1"
  "PARTSOUQ_HOME=$PARTSOUQ_HOME"
  "PARTSOUQ_DB_HOST=${PARTSOUQ_DB_HOST:-127.0.0.1}"
  "PARTSOUQ_DB_PORT=${PARTSOUQ_DB_PORT:-3308}"
  "PARTSOUQ_DB_NAME=$PARTSOUQ_DB_NAME"
  "PARTSOUQ_DB_USER=${PARTSOUQ_DB_USER:-partsouq}"
  "PARTSOUQ_DB_PASSWORD=${PARTSOUQ_DB_PASSWORD:-}"
  "PSQ_CLOAK_STATE_DIR=$PSQ_CLOAK_STATE_DIR"
  "PSQ_RUNTIME_LOG_DIR=$PSQ_RUNTIME_LOG_DIR"
  "CLOAKBROWSER_CACHE_DIR=$CLOAKBROWSER_CACHE_DIR"
  "CLOAKBROWSER_AUTO_UPDATE=$CLOAKBROWSER_AUTO_UPDATE"
  "CLOAKBROWSER_BINARY_PATH=$CLOAKBROWSER_BINARY_PATH"
  "PSQ_CLOAK_EXPECTED_SHA256=$EXPECTED_CLOAK_SHA256"
  "PSQ_SCHEDULER_STATE_DIR=$PSQ_SCHEDULER_STATE_DIR"
  "PARTSOUQ_SCHEDULER_READY_MARKER=$READY_MARKER"
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
if [[ ! -x "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" ]]; then
  print -u2 "missing staged scheduler: $PROJECT_ROOT/.venv/bin/partsouq-scheduler"
  exit 2
fi
if [[ ! -x "$PSQ_CLOAK_PYTHON" ]]; then
  print -u2 "missing staged CloakBrowser Python: $PSQ_CLOAK_PYTHON"
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
  "PYTHONDONTWRITEBYTECODE=1" \
  "CLOAKBROWSER_CACHE_DIR=$CLOAKBROWSER_CACHE_DIR" \
  "CLOAKBROWSER_AUTO_UPDATE=$CLOAKBROWSER_AUTO_UPDATE" \
  "CLOAKBROWSER_BINARY_PATH=$CLOAKBROWSER_BINARY_PATH" \
  "$PSQ_CLOAK_PYTHON" -c 'import cloakbrowser'; then
  print -u2 "staged CloakBrowser package is unavailable"
  exit 2
fi

exec /usr/bin/env -i "${RUNTIME_ENV[@]}" "$PROJECT_ROOT/.venv/bin/partsouq-scheduler" \
  --job catalog \
  --daemon \
  --interval-seconds "${SCHEDULER_CATALOG_INTERVAL_SECONDS:-2592000}"
