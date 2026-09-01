#!/bin/zsh

set -eu

if (( $# != 0 )); then
  print -u2 "usage: $0"
  exit 2
fi
if [[ "$(/usr/bin/uname -s)" != "Darwin" ]]; then
  print -u2 "the VNCS LaunchAgent controller only supports macOS"
  exit 2
fi

LABEL=com.partsouq.vncs-scheduler
LAUNCHCTL_BIN=${PARTSOUQ_LAUNCHCTL_BIN:-/bin/launchctl}
SERVICE_TARGET="gui/$(/usr/bin/id -u)/$LABEL"
AGENT_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
HOST_STATE_DIR="$HOME/Library/Application Support/partsouq-vncs-scheduler"

if "$LAUNCHCTL_BIN" print "$SERVICE_TARGET" >/dev/null 2>&1; then
  "$LAUNCHCTL_BIN" bootout "$SERVICE_TARGET"
  print "LaunchAgent disabled: $SERVICE_TARGET"
else
  print "LaunchAgent is already disabled: $SERVICE_TARGET"
fi

print "Configuration retained: $AGENT_PATH"
print "State and logs retained: $HOST_STATE_DIR"
