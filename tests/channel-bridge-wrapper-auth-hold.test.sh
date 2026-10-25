#!/usr/bin/env bash
# channel-bridge-wrapper.sh: a rejected credential is held, not retried, and crash
# notices are rate limited.
#
#   exit-78 child  -> one auth-failed notice, no relaunch while held, no restart notice
#   deliberate-restart stamp newer than the hold -> exactly one retry
#   wrapper relaunched while held -> no second notice, no "restarted" alert
#   exit-1 child   -> 3 restart notices, then one "restarting repeatedly", then quiet
#
# Run: bash tests/channel-bridge-wrapper-auth-hold.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
# shellcheck source=../scripts/python-binary.sh
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the wrapper auth-hold test")" || exit 1

check() {  # $1=description $2=actual $3=expected
  if [ "$2" = "$3" ]; then echo "  ok   $1 ($2)"; else echo "  FAIL $1: got $2, want $3"; fail=1; fi
}

setup() {  # $1 = exit code the stub bridge returns; prints the fixture dir
  local d; d=$(mktemp -d)
  mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; *) echo "$d/none.env" ;; esac
EOS
  chmod +x "$d/scripts/sutando-config.sh"
  printf 'import sys\nopen("%s/launches", "a").write("x\\n")\nsys.exit(%s)\n' "$d" "$1" > "$d/src/discord-bridge.py"
  cp "$REPO/src/launchd/channel-bridge-wrapper.sh" "$d/src/launchd/"
  cp "$REPO/src/result_publish.py" "$d/src/"
  printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"; chmod +x "$d/shims/osascript"
  echo "$d"
}

start_wrapper() {  # $1 = fixture dir; prints the wrapper pid
  ( cd "$1" && PATH="$1/shims:$PATH" DISCORD_BOT_TOKEN=t SUTANDO_CHANNEL_BRIDGE_PYTHON="$PY" \
      SUTANDO_CHANNEL_BRIDGE_RESTART_DELAY=1 SUTANDO_BRIDGE_ALERT_COOLDOWN_S=0 \
      SUTANDO_BRIDGE_AUTH_HOLD_POLL_S=1 \
      exec bash src/launchd/channel-bridge-wrapper.sh discord >> "$1/o.log" 2>&1 ) &
  echo $!
}

stop_wrapper() { kill -TERM "$1" 2>/dev/null; wait "$1" 2>/dev/null; }
launches() { [ -f "$1/launches" ] && wc -l < "$1/launches" | tr -d ' ' || echo 0; }
notices() { find "$1/ws/results" -name "proactive-discord-bridge-$2-*.txt" | wc -l | tr -d ' '; }

echo "auth rejection (exit 78):"
d=$(setup 78)
p=$(start_wrapper "$d"); sleep 4
check "child launched once" "$(launches "$d")" 1
check "one auth-failed notice" "$(notices "$d" auth-failed)" 1
check "no restart notice" "$(notices "$d" restarted)" 0
check "notice names the vault key" \
  "$(grep -l 'DISCORD_BOT_TOKEN' "$d"/ws/results/proactive-discord-bridge-auth-failed-*.txt | wc -l | tr -d ' ')" 1
check "log names the fix" "$(grep -c 'held: credential rejected (DISCORD_BOT_TOKEN); vault set' "$d/o.log")" 1
echo $(( $(date +%s) + 1 )) > "$d/ws/state/channel-bridge-supervisor/deliberate-restart"
sleep 4
check "deliberate restart -> exactly one retry" "$(launches "$d")" 2
check "the retry's rejection notifies once more" "$(notices "$d" auth-failed)" 2
sleep 3
check "no further retry while held" "$(launches "$d")" 2
stop_wrapper "$p"
p=$(start_wrapper "$d"); sleep 3
check "relaunched wrapper tries once" "$(launches "$d")" 3
check "relaunch while held: no new notice" "$(notices "$d" auth-failed)" 2
check "relaunch while held: no restart notice" "$(notices "$d" restarted)" 0
stop_wrapper "$p"; rm -rf "$d"

echo "genuine crash loop (exit 1):"
d=$(setup 1)
p=$(start_wrapper "$d"); sleep 10; stop_wrapper "$p"
n=$(launches "$d")
if [ "$n" -ge 6 ]; then echo "  ok   child relaunched repeatedly ($n)"; else echo "  FAIL only $n launches"; fail=1; fi
check "three restart notices" "$(notices "$d" restarted)" 3
check "one 'restarting repeatedly' notice" "$(notices "$d" restarting-repeatedly)" 1
check "it points at the log" \
  "$(cat "$d"/ws/results/proactive-discord-bridge-restarting-repeatedly-*.txt | grep -c 'see logs/discord-bridge.log')" 1
check "no auth-failed notice" "$(notices "$d" auth-failed)" 0
rm -rf "$d"

[ "$fail" -eq 0 ] && echo "PASS"
exit $fail
