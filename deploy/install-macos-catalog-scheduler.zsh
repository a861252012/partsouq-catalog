#!/bin/zsh

set -eu
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

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
LABEL=com.partsouq.catalog-scheduler
TEMPLATE="$SCRIPT_DIR/$LABEL.plist.template"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
AGENT_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
HOST_STATE_DIR="$HOME/Library/Application Support/partsouq-catalog"
LOG_DIR="$HOST_STATE_DIR/logs"
STDOUT_PATH="$LOG_DIR/catalog-scheduler.stdout.log"
STDERR_PATH="$LOG_DIR/catalog-scheduler.stderr.log"
RELOAD_MARKER="$HOST_STATE_DIR/launch-agent-needs-reload"
LAUNCHCTL_BIN=${PARTSOUQ_LAUNCHCTL_BIN:-/bin/launchctl}
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

/bin/mkdir -p \
  "$LAUNCH_AGENTS_DIR" \
  "$HOST_STATE_DIR" \
  "$HOST_STATE_DIR/cloak" \
  "$HOST_STATE_DIR/scheduler" \
  "$LOG_DIR"
/bin/chmod 700 \
  "$HOST_STATE_DIR" \
  "$HOST_STATE_DIR/cloak" \
  "$HOST_STATE_DIR/scheduler" \
  "$LOG_DIR"

TEMP_AGENT=$(/usr/bin/mktemp "$AGENT_PATH.tmp.XXXXXX")
cleanup() {
  if [[ -n "${TEMP_AGENT:-}" && -e "$TEMP_AGENT" ]]; then
    /bin/rm -f "$TEMP_AGENT"
  fi
}
trap cleanup EXIT HUP INT TERM

/bin/cp "$TEMPLATE" "$TEMP_AGENT"
/usr/bin/plutil -remove ProgramArguments.0 "$TEMP_AGENT"
/usr/bin/plutil -insert ProgramArguments.0 \
  -string "$PROJECT_ROOT/deploy/run-macos-catalog-scheduler.zsh" "$TEMP_AGENT"
/usr/bin/plutil -replace WorkingDirectory -string "$PROJECT_ROOT" "$TEMP_AGENT"
/usr/bin/plutil -replace StandardOutPath -string "$STDOUT_PATH" "$TEMP_AGENT"
/usr/bin/plutil -replace StandardErrorPath -string "$STDERR_PATH" "$TEMP_AGENT"

if /usr/bin/grep -Eq '__[A-Z0-9_]+__' "$TEMP_AGENT"; then
  print -u2 "rendered LaunchAgent still contains an unresolved placeholder"
  exit 2
fi
/usr/bin/plutil -lint "$TEMP_AGENT" >/dev/null
/bin/chmod 600 "$TEMP_AGENT"

INSTALL_CHANGED=1
if [[ -f "$AGENT_PATH" ]] && /usr/bin/cmp -s "$TEMP_AGENT" "$AGENT_PATH"; then
  INSTALL_CHANGED=0
else
  /bin/mv -f "$TEMP_AGENT" "$AGENT_PATH"
  TEMP_AGENT=""
fi
/bin/chmod 600 "$AGENT_PATH"

if (( NO_START )); then
  if (( INSTALL_CHANGED )); then
    /usr/bin/touch "$RELOAD_MARKER"
    /bin/chmod 600 "$RELOAD_MARKER"
  fi
  print "LaunchAgent installed without starting: $AGENT_PATH"
  exit 0
fi

RELOAD_REQUIRED=$INSTALL_CHANGED
if [[ -f "$RELOAD_MARKER" ]]; then
  RELOAD_REQUIRED=1
fi
if "$LAUNCHCTL_BIN" print "$SERVICE_TARGET" >/dev/null 2>&1; then
  if (( ! RELOAD_REQUIRED )); then
    print "LaunchAgent is already loaded and unchanged: $SERVICE_TARGET"
    exit 0
  fi
  "$LAUNCHCTL_BIN" bootout "$SERVICE_TARGET"
fi

"$LAUNCHCTL_BIN" bootstrap "$DOMAIN_TARGET" "$AGENT_PATH"
/bin/rm -f "$RELOAD_MARKER"
print "LaunchAgent installed and started: $SERVICE_TARGET"
print "Logs: $LOG_DIR"
