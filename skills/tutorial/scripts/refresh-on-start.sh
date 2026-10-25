#!/bin/bash
# The engine-start pass: rebuild every room's Getting started page whose feature list is now newer.
# Detached into its own session, so the launcher that starts it can exit or be torn down first.
set -u
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)" || exit 0
WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || exit 0
[ -n "$PY" ] && [ -n "$WS" ] && mkdir -p "$WS/logs" || exit 0
"$PY" -c 'import os, sys; os.setsid(); os.execv(sys.argv[1], sys.argv[1:])' \
  "$PY" "$REPO/skills/tutorial/scripts/getting_started.py" publish --all-rooms --if-newer \
  >> "$WS/logs/tutorial-refresh.log" 2>&1 < /dev/null &
exit 0
