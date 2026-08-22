#!/bin/zsh

set -euo pipefail
umask 077

usage() {
  print -u2 "usage: $0 [--no-start]"
}

NO_START=0
if (( $# > 1 )); then
  usage
  exit 2
fi
if (( $# == 1 )); then
  if [[ "$1" != "--no-start" ]]; then
    usage
    exit 2
  fi
  NO_START=1
fi

if [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
  print -u2 "the catalog LaunchAgent installer only supports macOS"
  exit 2
fi
READY_STABLE_SECONDS=3
READY_TIMEOUT=${PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS:-60}
if (( ! NO_START )) \
    && [[ "$READY_TIMEOUT" != <-> || "$READY_TIMEOUT" -lt "$READY_STABLE_SECONDS" ]]; then
  print -u2 "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS must be an integer of at least 3"
  exit 2
fi

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
LABEL=com.partsouq.catalog-scheduler
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
AGENT_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
HOST_STATE_DIR="$HOME/Library/Application Support/partsouq-catalog"
RELEASES_DIR="$HOST_STATE_DIR/releases"
LOG_DIR="$HOST_STATE_DIR/logs"
STDOUT_PATH="$LOG_DIR/catalog-scheduler.stdout.log"
STDERR_PATH="$LOG_DIR/catalog-scheduler.stderr.log"
RELOAD_MARKER="$HOST_STATE_DIR/launch-agent-needs-reload"
ACTIVE_AGENT_PATH="$HOST_STATE_DIR/active-launch-agent.plist"
INSTALL_LOCK_PATH="$HOST_STATE_DIR/install.lock"
LAUNCHCTL_BIN=${PARTSOUQ_LAUNCHCTL_BIN:-/bin/launchctl}
GIT_BIN=${PARTSOUQ_GIT_BIN:-/usr/bin/git}
UV_BIN=${PARTSOUQ_UV_BIN:-${commands[uv]:-}}
RUNTIME_PYTHON=${PARTSOUQ_RUNTIME_PYTHON:-$PROJECT_ROOT/.venv/bin/python}
SERVICE_TARGET="gui/$(/usr/bin/id -u)/$LABEL"
DOMAIN_TARGET="gui/$(/usr/bin/id -u)"

if [[ ! -f "$TEMPLATE" ]]; then
  print -u2 "missing LaunchAgent template: $TEMPLATE"
  exit 2
fi
if [[ ! -x "$PROJECT_ROOT/deploy/run-macos-catalog-scheduler.zsh" ]]; then
  print -u2 "missing executable host scheduler launcher"
  exit 2
fi
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  print -u2 "missing $PROJECT_ROOT/.env"
  exit 2
fi
if [[ ! -x "$GIT_BIN" ]]; then
  print -u2 "missing git executable: $GIT_BIN"
  exit 2
fi
if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  print -u2 "missing uv executable"
  exit 2
fi
if [[ ! -x "$RUNTIME_PYTHON" ]]; then
  print -u2 "missing runtime Python: $RUNTIME_PYTHON"
  exit 2
fi

TRACKED_CHANGES=$("$GIT_BIN" -C "$PROJECT_ROOT" status --porcelain --untracked-files=no)
if [[ -n "$TRACKED_CHANGES" ]]; then
  print -u2 "refusing to install an uncommitted tracked worktree"
  exit 2
fi
COMMIT_SHA=$("$GIT_BIN" -C "$PROJECT_ROOT" rev-parse --verify HEAD)
if [[ ! "$COMMIT_SHA" =~ '^[0-9a-f]{40}$' ]]; then
  print -u2 "could not resolve the committed runtime revision"
  exit 2
fi

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
  HTTP_PROXY
  HTTPS_PROXY
  ALL_PROXY
  http_proxy
  https_proxy
  all_proxy
  SSL_CERT_DIR
  SSL_CERT_FILE
)
for NAME in "${FORBIDDEN_NETWORK_ENV[@]}"; do
  if (( ${+parameters[$NAME]} )) && [[ -n "${(P)NAME}" ]]; then
    print -u2 "refusing proxy or custom TLS setting in the formal scheduler: $NAME"
    exit 2
  fi
done

typeset -a CONFIG_ENV
CONFIG_ENV=(
  PARTSOUQ_DB_HOST
  PARTSOUQ_DB_PORT
  PARTSOUQ_DB_NAME
  PARTSOUQ_DB_USER
  PARTSOUQ_DB_PASSWORD
  PSQ_CLOAK_USER_AGENT
  PSQ_WORKERS
  PSQ_REQUEST_RATE
  PSQ_REQUEST_BURST
  PSQ_MIN_DELAY
  PSQ_MAX_DELAY
  PSQ_MAX_RUN_DAYS
  PSQ_MIN_BRANDS
  PSQ_EVIDENCE_MAX_BODY_BYTES
  PSQ_EVIDENCE_MAX_RUN_BYTES
  PSQ_EVIDENCE_MAX_ARTIFACTS
  PSQ_ROW_COUNT_SHRINK_RATIO
  PSQ_BLOCK_BREATHER
  SCHEDULER_CATALOG_INTERVAL_SECONDS
)
typeset -a CONFIG_INPUT_ENV
CONFIG_INPUT_ENV=(
  "HOME=$HOME"
  "PATH=$PATH"
  "TMPDIR=${TMPDIR:-/tmp}"
  "LANG=${LANG:-en_US.UTF-8}"
)
for NAME in "${CONFIG_ENV[@]}"; do
  if (( ${+parameters[$NAME]} )); then
    CONFIG_INPUT_ENV+=("$NAME=${(P)NAME}")
  fi
done
CONFIG_EXPORTS=$(/usr/bin/env -i "${CONFIG_INPUT_ENV[@]}" /bin/zsh -c '
  set -euo pipefail
  ENV_FILE=$1
  shift
  source "$ENV_FILE" >/dev/null
  if [[ "${PARTSOUQ_DB_NAME:-}" != "partsouq_catalog" ]]; then
    print -u2 "formal catalog scheduler requires PARTSOUQ_DB_NAME=partsouq_catalog"
    exit 2
  fi
  for NAME in \
      CLOAKBROWSER_API_KEY CLOAKBROWSER_BINARY_PATH CLOAKBROWSER_DOWNLOAD_URL \
      CLOAKBROWSER_LICENSE_KEY CLOAKBROWSER_SKIP_CHECKSUM CLOAKBROWSER_TOKEN; do
    if (( ${+parameters[$NAME]} )) && [[ -n "${(P)NAME}" ]]; then
      print -u2 "refusing forbidden CloakBrowser setting: $NAME"
      exit 2
    fi
  done
  for NAME in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy \
      SSL_CERT_DIR SSL_CERT_FILE; do
    if (( ${+parameters[$NAME]} )) && [[ -n "${(P)NAME}" ]]; then
      print -u2 "refusing proxy or custom TLS setting in the formal scheduler: $NAME"
      exit 2
    fi
  done
  print -r -- "# Generated by install-macos-catalog-scheduler.zsh; owner-only."
  for NAME in "$@"; do
    if (( ${+parameters[$NAME]} )); then
      VALUE=${(P)NAME}
      print -r -- "export $NAME=${(q)VALUE}"
    fi
  done
' partsouq-env "$PROJECT_ROOT/.env" "${CONFIG_ENV[@]}")

CLOAK_CACHE_DIR="$HOST_STATE_DIR/cloak/free-browser-cache"
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
    "$LAUNCH_AGENTS_DIR" \
    "$AGENT_PATH" \
    "$HOST_STATE_DIR" \
    "$HOST_STATE_DIR/cloak" \
    "$HOST_STATE_DIR/scheduler" \
    "$HOST_STATE_DIR/quarantine" \
    "$RELEASES_DIR" \
    "$LOG_DIR" \
    "$STDOUT_PATH" \
    "$STDERR_PATH" \
    "$CLOAK_CACHE_DIR" \
    "$RELOAD_MARKER"
/bin/mkdir -p \
  "$LAUNCH_AGENTS_DIR" \
  "$HOST_STATE_DIR" \
  "$HOST_STATE_DIR/cloak" \
  "$HOST_STATE_DIR/scheduler" \
  "$HOST_STATE_DIR/quarantine" \
  "$RELEASES_DIR" \
  "$LOG_DIR"
/bin/chmod 700 \
  "$HOST_STATE_DIR" \
  "$HOST_STATE_DIR/cloak" \
  "$HOST_STATE_DIR/scheduler" \
  "$HOST_STATE_DIR/quarantine" \
  "$RELEASES_DIR" \
  "$LOG_DIR"
/bin/mkdir -p "$CLOAK_CACHE_DIR"
/bin/chmod 700 "$CLOAK_CACHE_DIR"

assert_free_only_cache() {
  FREE_CACHE=$1
  if [[ -e "$FREE_CACHE/license.key" ]]; then
    print -u2 "refusing CloakBrowser free cache containing license.key"
    return 2
  fi
  typeset -a PRO_ARTIFACTS
  PRO_ARTIFACTS=(
    "$FREE_CACHE/.license_cache"(N)
    "$FREE_CACHE/.last_pro_version_check"(N)
    "$FREE_CACHE/.last_pro_update_check"(N)
    "$FREE_CACHE"/latest_pro_version_*(N)
    "$FREE_CACHE"/*[pP][rR][oO]*(N/)
  )
  if (( ${#PRO_ARTIFACTS} )); then
    print -u2 "refusing CloakBrowser Pro artifacts in the free-only cache"
    return 2
  fi
}

assert_free_only_cache "$CLOAK_CACHE_DIR"

if ! /usr/bin/shlock -f "$INSTALL_LOCK_PATH" -p $$; then
  print -u2 "another catalog scheduler installation is already running"
  exit 75
fi
LOCK_HELD=1

CREATED_RELEASE=""
ROLLBACK_NEEDED=0
HAD_PREVIOUS=0
WAS_LOADED=0
PREVIOUS_AGENT=""
cleanup() {
  if [[ -n "${CREATED_RELEASE:-}" \
      && -d "$CREATED_RELEASE" \
      && ! -f "$CREATED_RELEASE/.install-complete" ]]; then
    /bin/rm -rf "$CREATED_RELEASE"
  fi
  if [[ -n "${WORK_DIR:-}" && -d "$WORK_DIR" ]]; then
    /bin/rm -rf "$WORK_DIR"
  fi
  if (( ${LOCK_HELD:-0} )); then
    /bin/rm -f "$INSTALL_LOCK_PATH"
    LOCK_HELD=0
  fi
}

rollback_launch_agent() {
  if (( ! ROLLBACK_NEEDED )); then
    return
  fi
  ROLLBACK_NEEDED=0
  "$LAUNCHCTL_BIN" bootout "$SERVICE_TARGET" >/dev/null 2>&1 || true
  if (( HAD_PREVIOUS )); then
    ROLLBACK_AGENT=$(/usr/bin/mktemp "$LAUNCH_AGENTS_DIR/.$LABEL.rollback.XXXXXX")
    /bin/cp "$PREVIOUS_AGENT" "$ROLLBACK_AGENT"
    /bin/chmod 600 "$ROLLBACK_AGENT"
    /bin/mv -f "$ROLLBACK_AGENT" "$AGENT_PATH"
    if (( WAS_LOADED )) \
        && ! "$LAUNCHCTL_BIN" bootstrap "$DOMAIN_TARGET" "$AGENT_PATH"; then
      print -u2 "failed to restart the previous LaunchAgent during rollback"
    fi
  else
    /bin/rm -f "$AGENT_PATH"
  fi
}

finish() {
  STATUS=$1
  trap - EXIT HUP INT TERM
  if (( STATUS != 0 )); then
    rollback_launch_agent
  fi
  cleanup
  exit "$STATUS"
}
trap 'finish $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

WORK_DIR=$(/usr/bin/mktemp -d "$HOST_STATE_DIR/.install.XXXXXX")
PREVIOUS_AGENT="$WORK_DIR/previous.plist"

TEMP_CONFIG="$WORK_DIR/scheduler.env"
print -r -- "$CONFIG_EXPORTS" > "$TEMP_CONFIG"
/bin/chmod 600 "$TEMP_CONFIG"

write_source_manifest() {
  MANIFEST_APP=$1
  MANIFEST_OUTPUT=$2
  (
    cd "$MANIFEST_APP"
    /usr/bin/find . -type f ! -path './.venv/*' -print0 \
      | /usr/bin/xargs -0 /usr/bin/shasum -a 256 \
      | LC_ALL=C /usr/bin/sort
  ) > "$MANIFEST_OUTPUT"
}

write_runtime_manifest() {
  MANIFEST_RELEASE=$1
  MANIFEST_OUTPUT=$2
  (
    cd "$MANIFEST_RELEASE"
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
  ) > "$MANIFEST_OUTPUT"
}

validate_release_permissions() {
  PERMISSION_RELEASE=$1
  CURRENT_UID=$(/usr/bin/id -u)
  INVALID_OWNER=$(/usr/bin/find "$PERMISSION_RELEASE" ! -uid "$CURRENT_UID" -print -quit)
  INVALID_MODE=$(/usr/bin/find "$PERMISSION_RELEASE" ! -type l -perm +077 -print -quit)
  if [[ -n "$INVALID_OWNER" || -n "$INVALID_MODE" ]]; then
    print -u2 "staged runtime ownership or permissions are not owner-only"
    return 2
  fi
  for PRIVATE_FILE in scheduler.env source.sha256 runtime.sha256 cloak-browser.sha256; do
    if [[ ! -f "$PERMISSION_RELEASE/$PRIVATE_FILE" \
        || "$(/usr/bin/stat -f '%Lp' "$PERMISSION_RELEASE/$PRIVATE_FILE")" != "600" ]]; then
      print -u2 "staged runtime manifest/config must be owner-only 0600: $PRIVATE_FILE"
      return 2
    fi
  done
}

verify_or_repair_free_browser() {
  BROWSER_RELEASE=$1
  BROWSER_MANIFEST="$BROWSER_RELEASE/cloak-browser.sha256"
  EXPECTED_LINE=$(/usr/bin/head -n 1 "$BROWSER_MANIFEST")
  EXPECTED_SHA=${EXPECTED_LINE%% *}
  EXPECTED_BINARY=${EXPECTED_LINE#*  }
  EXPECTED_BINARY=${EXPECTED_BINARY:A}
  if [[ ! "$EXPECTED_SHA" =~ '^[0-9a-f]{64}$' \
      || "$EXPECTED_BINARY" != "$CLOAK_CACHE_DIR"/* ]]; then
    print -u2 "invalid CloakBrowser free binary manifest"
    exit 2
  fi
  RELATIVE_BINARY=${EXPECTED_BINARY#"$CLOAK_CACHE_DIR"/}
  VERSION_NAME=${RELATIVE_BINARY%%/*}
  if [[ "$VERSION_NAME" != chromium-* \
      || "$VERSION_NAME" == *[pP][rR][oO]* ]]; then
    print -u2 "refusing to quarantine a non-free CloakBrowser cache target"
    exit 2
  fi
  if [[ -x "$EXPECTED_BINARY" ]] \
      && /usr/bin/shasum -a 256 -c "$BROWSER_MANIFEST" >/dev/null 2>&1; then
    return
  fi

  if [[ -e "$CLOAK_CACHE_DIR/$VERSION_NAME" && ! -d "$CLOAK_CACHE_DIR/$VERSION_NAME" ]]; then
    print -u2 "refusing to replace a non-directory CloakBrowser cache target"
    exit 2
  fi
  if [[ -d "$CLOAK_CACHE_DIR/$VERSION_NAME" ]]; then
    QUARANTINE_DIR=$(/usr/bin/mktemp -d \
      "$HOST_STATE_DIR/quarantine/cloak-free-corrupt-$(/bin/date -u +%Y%m%dT%H%M%SZ).XXXXXX")
    /bin/chmod 700 "$QUARANTINE_DIR"
    /bin/mv "$CLOAK_CACHE_DIR/$VERSION_NAME" "$QUARANTINE_DIR/"
    print -u2 "quarantined corrupt free CloakBrowser cache: $QUARANTINE_DIR/$VERSION_NAME"
  fi

  REPAIRED_BINARY=$(/usr/bin/env -i \
    "HOME=$HOME" \
    "PATH=$PATH" \
    "TMPDIR=${TMPDIR:-/tmp}" \
    "LANG=${LANG:-en_US.UTF-8}" \
    "PYTHONDONTWRITEBYTECODE=1" \
    "CLOAKBROWSER_CACHE_DIR=$CLOAK_CACHE_DIR" \
    "CLOAKBROWSER_AUTO_UPDATE=false" \
    "$BROWSER_RELEASE/cloak-venv/bin/python" -c \
    'from cloakbrowser.download import ensure_binary; print(ensure_binary())')
  if ! assert_free_only_cache "$CLOAK_CACHE_DIR"; then
    exit 2
  fi
  if [[ "${REPAIRED_BINARY:A}" != "$EXPECTED_BINARY" || ! -x "$EXPECTED_BINARY" ]] \
      || ! /usr/bin/shasum -a 256 -c "$BROWSER_MANIFEST" >/dev/null 2>&1; then
    print -u2 "re-downloaded free CloakBrowser binary failed integrity verification"
    exit 2
  fi
}

RUNTIME_PROJECT_ROOT=""
for CANDIDATE in "$RELEASES_DIR/$COMMIT_SHA-"*(N/); do
  if [[ ! -d "$CANDIDATE/app/.venv" \
      || ! -d "$CANDIDATE/cloak-venv" \
      || ! -f "$CANDIDATE/.install-complete" \
      || ! -f "$CANDIDATE/.trusted-free-runtime-v1" \
      || ! -f "$CANDIDATE/source.sha256" \
      || ! -f "$CANDIDATE/runtime.sha256" \
      || ! -f "$CANDIDATE/cloak-browser.sha256" \
      || ! -x "$CANDIDATE/app/deploy/run-macos-catalog-scheduler.zsh" \
      || ! -x "$CANDIDATE/app/.venv/bin/partsouq-scheduler" \
      || ! -x "$CANDIDATE/app/.venv/bin/partsouq-catalog-migrate" \
      || ! -x "$CANDIDATE/cloak-venv/bin/python" ]]; then
    continue
  fi
  if ! validate_release_permissions "$CANDIDATE"; then
    continue
  fi
  CANDIDATE_SOURCE_MANIFEST="$WORK_DIR/candidate-source.sha256"
  CANDIDATE_RUNTIME_MANIFEST="$WORK_DIR/candidate-runtime.sha256"
  write_source_manifest "$CANDIDATE/app" "$CANDIDATE_SOURCE_MANIFEST"
  write_runtime_manifest "$CANDIDATE" "$CANDIDATE_RUNTIME_MANIFEST"
  if /usr/bin/grep -Fxq "$COMMIT_SHA" "$CANDIDATE/.install-complete" \
      && /usr/bin/grep -Fxq "$COMMIT_SHA" "$CANDIDATE/.trusted-free-runtime-v1" \
      && /usr/bin/cmp -s "$TEMP_CONFIG" "$CANDIDATE/scheduler.env" \
      && /usr/bin/cmp -s "$CANDIDATE_SOURCE_MANIFEST" "$CANDIDATE/source.sha256" \
      && /usr/bin/cmp -s "$CANDIDATE_RUNTIME_MANIFEST" "$CANDIDATE/runtime.sha256"; then
    verify_or_repair_free_browser "$CANDIDATE"
    RUNTIME_PROJECT_ROOT="$CANDIDATE/app"
    break
  fi
done

if [[ -z "$RUNTIME_PROJECT_ROOT" ]]; then
  RELEASE_PREFIX="$RELEASES_DIR/$COMMIT_SHA-$(/bin/date -u +%Y%m%dT%H%M%SZ)"
  CREATED_RELEASE=$(/usr/bin/mktemp -d "$RELEASE_PREFIX.XXXXXX")
  FINAL_RELEASE="$CREATED_RELEASE"
  FINAL_APP="$FINAL_RELEASE/app"
  SOURCE_ARCHIVE="$WORK_DIR/source.tar"
  /bin/mkdir -p "$FINAL_APP"
  /bin/chmod 700 "$FINAL_RELEASE" "$FINAL_APP"

  "$GIT_BIN" -C "$PROJECT_ROOT" archive --format=tar --output="$SOURCE_ARCHIVE" \
    "$COMMIT_SHA" -- \
    pyproject.toml uv.lock README.md src db migrations deploy
  /usr/bin/tar -xf "$SOURCE_ARCHIVE" -C "$FINAL_APP"
  if [[ -e "$FINAL_APP/.git" || -e "$FINAL_APP/.env" ]]; then
    print -u2 "committed runtime unexpectedly contains repository identity or local secrets"
    exit 2
  fi
  /bin/cp "$TEMP_CONFIG" "$FINAL_RELEASE/scheduler.env"
  /bin/chmod 600 "$FINAL_RELEASE/scheduler.env"

  typeset -a SAFE_INSTALL_ENV
  SAFE_INSTALL_ENV=(
    "HOME=$HOME"
    "PATH=$PATH"
    "TMPDIR=${TMPDIR:-/tmp}"
    "LANG=${LANG:-en_US.UTF-8}"
    "PYTHONDONTWRITEBYTECODE=1"
  )

  BASE_PYTHON=$(/usr/bin/env -i "${SAFE_INSTALL_ENV[@]}" \
    "$RUNTIME_PYTHON" -c 'import sys; print(sys._base_executable)')
  if [[ -z "$BASE_PYTHON" || ! -x "$BASE_PYTHON" ]]; then
    print -u2 "could not resolve the runtime base Python"
    exit 2
  fi
  case "${BASE_PYTHON:A}" in
    "$PROJECT_ROOT"/*)
      print -u2 "runtime base Python must not live inside the repository"
      exit 2
      ;;
  esac

  /usr/bin/env -i "${SAFE_INSTALL_ENV[@]}" "$UV_BIN" sync \
    --locked \
    --no-dev \
    --no-editable \
    --python "$BASE_PYTHON" \
    --project "$FINAL_APP"
  /usr/bin/env -i "${SAFE_INSTALL_ENV[@]}" "$UV_BIN" venv \
    --python "$BASE_PYTHON" "$FINAL_RELEASE/cloak-venv"
  /usr/bin/env -i "${SAFE_INSTALL_ENV[@]}" "$UV_BIN" pip install \
    --python "$FINAL_RELEASE/cloak-venv/bin/python" \
    --require-hashes \
    -r "$FINAL_APP/deploy/requirements-cloakbrowser.txt"

  /usr/bin/env -i \
    "HOME=$HOME" \
    "PATH=$PATH" \
    "TMPDIR=${TMPDIR:-/tmp}" \
    "LANG=${LANG:-en_US.UTF-8}" \
    "PYTHONDONTWRITEBYTECODE=1" \
    "$FINAL_RELEASE/cloak-venv/bin/python" -c 'import cloakbrowser'

  FRESH_CLOAK_CACHE="$WORK_DIR/fresh-free-browser-cache"
  /bin/mkdir -p "$FRESH_CLOAK_CACHE"
  /bin/chmod 700 "$FRESH_CLOAK_CACHE"
  FRESH_CLOAK_BINARY=$(/usr/bin/env -i \
    "HOME=$HOME" \
    "PATH=$PATH" \
    "TMPDIR=${TMPDIR:-/tmp}" \
    "LANG=${LANG:-en_US.UTF-8}" \
    "PYTHONDONTWRITEBYTECODE=1" \
    "CLOAKBROWSER_CACHE_DIR=$FRESH_CLOAK_CACHE" \
    "CLOAKBROWSER_AUTO_UPDATE=false" \
    "$FINAL_RELEASE/cloak-venv/bin/python" -c \
    'from cloakbrowser.download import ensure_binary; print(ensure_binary())')
  FRESH_CLOAK_BINARY=${FRESH_CLOAK_BINARY:A}
  if ! assert_free_only_cache "$FRESH_CLOAK_CACHE"; then
    exit 2
  fi
  CLOAK_RELATIVE_BINARY=${FRESH_CLOAK_BINARY#"$FRESH_CLOAK_CACHE"/}
  CLOAK_VERSION_NAME=${CLOAK_RELATIVE_BINARY%%/*}
  if [[ "$FRESH_CLOAK_BINARY" != "$FRESH_CLOAK_CACHE"/* \
      || "$CLOAK_VERSION_NAME" != chromium-* \
      || "$CLOAK_VERSION_NAME" == *[pP][rR][oO]* \
      || ! -x "$FRESH_CLOAK_BINARY" ]]; then
    print -u2 "CloakBrowser install did not return an executable free-cache binary"
    exit 2
  fi

  typeset -a UNTRUSTED_CACHE_TARGETS
  UNTRUSTED_CACHE_TARGETS=(
    "$CLOAK_CACHE_DIR/$CLOAK_VERSION_NAME"(N)
    "$CLOAK_CACHE_DIR/latest_version"(N)
    "$CLOAK_CACHE_DIR"/latest_version_*(N)
  )
  if (( ${#UNTRUSTED_CACHE_TARGETS} )); then
    FRESH_QUARANTINE=$(/usr/bin/mktemp -d \
      "$HOST_STATE_DIR/quarantine/cloak-free-replaced-$(/bin/date -u +%Y%m%dT%H%M%SZ).XXXXXX")
    /bin/chmod 700 "$FRESH_QUARANTINE"
    for CACHE_TARGET in "${UNTRUSTED_CACHE_TARGETS[@]}"; do
      /bin/mv "$CACHE_TARGET" "$FRESH_QUARANTINE/"
    done
  fi
  /bin/mv "$FRESH_CLOAK_CACHE/$CLOAK_VERSION_NAME" "$CLOAK_CACHE_DIR/"
  CLOAK_BINARY="$CLOAK_CACHE_DIR/$CLOAK_RELATIVE_BINARY"
  if [[ ! -x "$CLOAK_BINARY" ]]; then
    print -u2 "trusted CloakBrowser binary was not published to the free cache"
    exit 2
  fi
  /usr/bin/shasum -a 256 "$CLOAK_BINARY" > "$FINAL_RELEASE/cloak-browser.sha256"
  print -r -- "$COMMIT_SHA" > "$FINAL_RELEASE/.trusted-free-runtime-v1"
  /usr/bin/find "$FINAL_APP/.venv" "$FINAL_RELEASE/cloak-venv" \
    -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
  /usr/bin/find "$FINAL_APP/.venv" "$FINAL_RELEASE/cloak-venv" \
    -type d -name '__pycache__' -empty -delete
  write_source_manifest "$FINAL_APP" "$FINAL_RELEASE/source.sha256"
  print -r -- "$COMMIT_SHA" > "$FINAL_RELEASE/.install-complete"
  /bin/chmod 600 \
    "$FINAL_RELEASE/.install-complete" \
    "$FINAL_RELEASE/.trusted-free-runtime-v1" \
    "$FINAL_RELEASE/source.sha256" \
    "$FINAL_RELEASE/cloak-browser.sha256"
  /bin/chmod -R go-rwx "$FINAL_RELEASE"
  write_runtime_manifest "$FINAL_RELEASE" "$FINAL_RELEASE/runtime.sha256"
  /bin/chmod 600 "$FINAL_RELEASE/runtime.sha256"
  validate_release_permissions "$FINAL_RELEASE"
  RUNTIME_PROJECT_ROOT="$FINAL_APP"
fi
TEMP_AGENT="$WORK_DIR/$LABEL.plist"
/bin/cp "$TEMPLATE" "$TEMP_AGENT"
/usr/bin/plutil -remove ProgramArguments.0 "$TEMP_AGENT"
/usr/bin/plutil -insert ProgramArguments.0 \
  -string "$RUNTIME_PROJECT_ROOT/deploy/run-macos-catalog-scheduler.zsh" "$TEMP_AGENT"
/usr/bin/plutil -replace WorkingDirectory -string "$RUNTIME_PROJECT_ROOT" "$TEMP_AGENT"
/usr/bin/plutil -replace StandardOutPath -string "$STDOUT_PATH" "$TEMP_AGENT"
/usr/bin/plutil -replace StandardErrorPath -string "$STDERR_PATH" "$TEMP_AGENT"

if /usr/bin/grep -Eq '__[A-Z0-9_]+' "$TEMP_AGENT"; then
  print -u2 "rendered LaunchAgent still contains an unresolved placeholder"
  exit 2
fi
/usr/bin/plutil -lint "$TEMP_AGENT" >/dev/null
/bin/chmod 600 "$TEMP_AGENT"

INSTALL_CHANGED=1
if [[ -f "$AGENT_PATH" ]] && /usr/bin/cmp -s "$TEMP_AGENT" "$AGENT_PATH"; then
  INSTALL_CHANGED=0
fi

plist_program() {
  /usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$1" 2>/dev/null || true
}

read_launchctl_service() {
  CURRENTLY_LOADED=0
  LOADED_HEALTHY=0
  LOADED_PROGRAM=""
  LOADED_STATE=""
  LOADED_PID=""
  if ! LAUNCHCTL_OUTPUT=$("$LAUNCHCTL_BIN" print "$SERVICE_TARGET" 2>/dev/null); then
    return 1
  fi
  CURRENTLY_LOADED=1
  LOADED_PROGRAM=$(print -r -- "$LAUNCHCTL_OUTPUT" \
    | /usr/bin/sed -n 's/^[[:space:]]*program = //p' | /usr/bin/head -n 1)
  LOADED_STATE=$(print -r -- "$LAUNCHCTL_OUTPUT" \
    | /usr/bin/sed -n 's/^[[:space:]]*state = //p' | /usr/bin/head -n 1)
  LOADED_PID=$(print -r -- "$LAUNCHCTL_OUTPUT" \
    | /usr/bin/sed -n 's/^[[:space:]]*pid = //p' | /usr/bin/head -n 1)
  if [[ "$LOADED_STATE" == "running" && "$LOADED_PID" == <-> ]] \
      && /bin/kill -0 "$LOADED_PID" 2>/dev/null; then
    LOADED_HEALTHY=1
  fi
  return 0
}

read_launchctl_service || true
DISK_PROGRAM=""
ACTIVE_PROGRAM=""
[[ -f "$AGENT_PATH" ]] && DISK_PROGRAM=$(plist_program "$AGENT_PATH")
[[ -f "$ACTIVE_AGENT_PATH" ]] && ACTIVE_PROGRAM=$(plist_program "$ACTIVE_AGENT_PATH")
DESIRED_PROGRAM=$(plist_program "$TEMP_AGENT")
if (( CURRENTLY_LOADED )) && [[ -z "$LOADED_PROGRAM" ]]; then
  print -u2 "loaded LaunchAgent did not report its program; refusing an unsafe switch"
  exit 2
fi

if (( NO_START )); then
  if (( CURRENTLY_LOADED )) \
      && [[ -f "$AGENT_PATH" ]] \
      && { [[ -z "$LOADED_PROGRAM" ]] || [[ "$DISK_PROGRAM" == "$LOADED_PROGRAM" ]]; }; then
    ACTIVE_TEMP=$(/usr/bin/mktemp "$HOST_STATE_DIR/.active-launch-agent.XXXXXX")
    /bin/cp "$AGENT_PATH" "$ACTIVE_TEMP"
    /bin/chmod 600 "$ACTIVE_TEMP"
    /bin/mv -f "$ACTIVE_TEMP" "$ACTIVE_AGENT_PATH"
  fi
  if (( INSTALL_CHANGED )); then
    /bin/mv -f "$TEMP_AGENT" "$AGENT_PATH"
    /bin/chmod 600 "$AGENT_PATH"
    /usr/bin/touch "$RELOAD_MARKER"
    /bin/chmod 600 "$RELOAD_MARKER"
  fi
  print "LaunchAgent installed without starting: $AGENT_PATH"
  print "Runtime: $RUNTIME_PROJECT_ROOT"
  exit 0
fi

ACTIVE_RELEASE=${RUNTIME_PROJECT_ROOT:h}
READY_MARKER="$HOST_STATE_DIR/scheduler/launch-ready-${ACTIVE_RELEASE:t}"
if (( LOADED_HEALTHY )); then
  MARKER_PID=""
  [[ -s "$READY_MARKER" ]] && MARKER_PID=$(/bin/cat "$READY_MARKER")
  if [[ "$MARKER_PID" != "$LOADED_PID" ]]; then
    LOADED_HEALTHY=0
  fi
fi

RELOAD_REQUIRED=$INSTALL_CHANGED
if [[ -f "$RELOAD_MARKER" ]]; then
  RELOAD_REQUIRED=1
fi
if (( CURRENTLY_LOADED )) \
    && { (( ! LOADED_HEALTHY )) || [[ "$LOADED_PROGRAM" != "$DESIRED_PROGRAM" ]]; }; then
  RELOAD_REQUIRED=1
fi
WAS_LOADED=$CURRENTLY_LOADED
if (( WAS_LOADED )); then
  if (( ! RELOAD_REQUIRED )); then
    print "LaunchAgent is already loaded and unchanged: $SERVICE_TARGET"
    exit 0
  fi
  if [[ -f "$AGENT_PATH" && "$DISK_PROGRAM" == "$LOADED_PROGRAM" ]]; then
    /bin/cp "$AGENT_PATH" "$PREVIOUS_AGENT"
    HAD_PREVIOUS=1
  elif [[ -f "$ACTIVE_AGENT_PATH" && "$ACTIVE_PROGRAM" == "$LOADED_PROGRAM" ]]; then
    /bin/cp "$ACTIVE_AGENT_PATH" "$PREVIOUS_AGENT"
    HAD_PREVIOUS=1
  else
    print -u2 "loaded LaunchAgent program does not match a recoverable plist; refusing switch"
    exit 2
  fi
elif [[ -f "$AGENT_PATH" ]]; then
  /bin/cp "$AGENT_PATH" "$PREVIOUS_AGENT"
  HAD_PREVIOUS=1
fi

ROLLBACK_NEEDED=1
if (( WAS_LOADED )); then
  "$LAUNCHCTL_BIN" bootout "$SERVICE_TARGET"
fi

if (( INSTALL_CHANGED )); then
  /bin/mv -f "$TEMP_AGENT" "$AGENT_PATH"
fi
/bin/chmod 600 "$AGENT_PATH"

/bin/rm -f "$READY_MARKER"
CHILD_READY=0
READY_PID=""
READY_STREAK=0
if "$LAUNCHCTL_BIN" bootstrap "$DOMAIN_TARGET" "$AGENT_PATH"; then
  for (( SECOND = 0; SECOND < READY_TIMEOUT; SECOND++ )); do
    if read_launchctl_service \
        && (( LOADED_HEALTHY )) \
        && [[ "$LOADED_PROGRAM" == "$DESIRED_PROGRAM" \
        && -s "$READY_MARKER" ]]; then
      CANDIDATE_PID=$(/bin/cat "$READY_MARKER")
      if [[ "$CANDIDATE_PID" == "$LOADED_PID" ]]; then
        if [[ "$CANDIDATE_PID" == "$READY_PID" ]]; then
          (( READY_STREAK += 1 ))
        else
          READY_PID=$CANDIDATE_PID
          READY_STREAK=1
        fi
        if (( READY_STREAK >= READY_STABLE_SECONDS )); then
          CHILD_READY=1
          break
        fi
      else
        READY_PID=""
        READY_STREAK=0
      fi
    else
      READY_PID=""
      READY_STREAK=0
    fi
    /bin/sleep 1
  done
fi

if (( ! CHILD_READY )); then
  print -u2 "LaunchAgent child readiness failed; previous plist was restored"
  exit 1
fi

ACTIVE_TEMP=$(/usr/bin/mktemp "$HOST_STATE_DIR/.active-launch-agent.XXXXXX")
/bin/cp "$AGENT_PATH" "$ACTIVE_TEMP"
/bin/chmod 600 "$ACTIVE_TEMP"
/bin/mv -f "$ACTIVE_TEMP" "$ACTIVE_AGENT_PATH"
ROLLBACK_NEEDED=0
/bin/rm -f "$RELOAD_MARKER"
print "LaunchAgent installed and started: $SERVICE_TARGET"
print "Runtime: $RUNTIME_PROJECT_ROOT"
print "Logs: $LOG_DIR"
