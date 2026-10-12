#!/bin/bash
# --witness <name>: a throwaway core from THIS checkout on its own socket, session and workspace.
#
# --witness-stop <name> removes only what it made, only once its recorded processes are gone (else exit 1,
# state kept); every refusal exits 2 before a side effect.

WITNESS_DEFAULT_SOCKET="/tmp/sutando-tmux.sock"
WITNESS_DEFAULT_SESSION="sutando-core"

witness_refuse() {
  echo "start-cli witness: refusing — $*" >&2
  exit 2
}

# A stop that could not finish: everything recorded is kept so a rerun can.
witness_fail() {
  echo "start-cli witness: stop incomplete — $*; $WITNESS_ROOT is kept, rerun --witness-stop $WITNESS_NAME" >&2
  exit 1
}

witness_tmux_usable() { command -v tmux > /dev/null 2>&1 && tmux -V > /dev/null 2>&1; }

# Physical path of a directory that may not exist yet: its nearest existing ancestor
# is resolved, so /tmp and /private/tmp compare equal.
witness_physical() {
  local p="$1" tail=""
  while [ -n "$p" ] && [ ! -d "$p" ]; do
    tail="/${p##*/}$tail"
    p="${p%/*}"
  done
  [ -n "$p" ] || p="/"
  printf '%s%s\n' "$(cd "$p" && pwd -P)" "$tail"
}

# Sets WITNESS_NAME, _ROOT, _WS, _SOCKET, _SESSION, _CONFIG, _RECORD from <name>.
# The session ends in -core so no witness name is a prefix-match of another's.
witness_paths() {
  local name="$1" base sum
  case "$name" in
    "") witness_refuse "a witness needs a name: --witness <name>" ;;
  esac
  if ! printf '%s' "$name" | grep -Eq '^[a-z0-9][a-z0-9-]{0,19}$'; then
    witness_refuse "witness name '$name' must be 1-20 of [a-z0-9-], starting alphanumeric"
  fi
  base="$(witness_physical "${TMPDIR:-/tmp}")"
  base="${base%/}"
  WITNESS_NAME="$name"
  WITNESS_ROOT="$base/sutando-witness/$name"
  WITNESS_WS="$WITNESS_ROOT/workspace"
  WITNESS_SOCKET="$WITNESS_ROOT/tmux.sock"
  # The root's checksum in the name keeps a same-name witness under another TMPDIR from matching this one's claude.
  sum="$(printf '%s' "$WITNESS_ROOT" | cksum | awk '{print $1}')"
  case "$sum" in ''|*[!0-9]*) witness_refuse "could not checksum $WITNESS_ROOT" ;; esac
  WITNESS_SESSION="witness-$name-$sum-core"
  WITNESS_CONFIG="$REPO/sutando.config.local.json"
  WITNESS_RECORD="$WITNESS_ROOT/witness.env"
}

# Fails closed when a socket or session could be the production core's: the
# launcher defaults, or (when given) the caller's own ambient values.
witness_assert_not_production() {
  local sock="$1" sess="$2" ambient_sock="${3:-}" ambient_sess="${4:-}" phys
  [ -n "$sock" ] && [ -n "$sess" ] || witness_refuse "empty tmux socket or session"
  phys="$(witness_physical "$sock")"
  if [ "$phys" = "$(witness_physical "$WITNESS_DEFAULT_SOCKET")" ]; then
    witness_refuse "tmux socket $sock is the production default"
  fi
  if [ "$sess" = "$WITNESS_DEFAULT_SESSION" ]; then
    witness_refuse "tmux session $sess is the production default"
  fi
  if [ -n "$ambient_sock" ] && [ "$phys" = "$(witness_physical "$ambient_sock")" ]; then
    witness_refuse "tmux socket $sock is the caller's own SUTANDO_TMUX_SOCKET"
  fi
  if [ -n "$ambient_sess" ] && [ "$sess" = "$ambient_sess" ]; then
    witness_refuse "tmux session $sess is the caller's own SUTANDO_TMUX_SESSION"
  fi
  case "$sess" in
    witness-*-core) ;;
    *) witness_refuse "tmux session $sess is not a witness session" ;;
  esac
}

# A linked worktree's .git is a file naming <common>/worktrees/<id>; a main
# checkout (where a production core runs) has a .git directory.
witness_assert_linked_worktree() {
  local gitfile="$REPO/.git" line
  [ -f "$gitfile" ] || witness_refuse "$REPO is not a linked git worktree; run witness mode from a PR worktree"
  IFS= read -r line < "$gitfile" || line=""
  case "$line" in
    gitdir:*/worktrees/*) ;;
    *) witness_refuse "$REPO/.git does not name a linked worktree" ;;
  esac
}

# The workspace this checkout resolves to, through the same helper every service uses.
witness_resolved_workspace() {
  env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
    bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null
}

# Any .alive beaten within the last 2 minutes means a live core uses that workspace.
witness_workspace_has_live_core() {
  local cores="$1/state/cores" found
  [ -e "$cores" ] || return 1
  # A failed listing proves nothing, so it counts as a live core.
  found="$(find "$cores" -maxdepth 1 -name '*.alive' -mmin -2 2>/dev/null)" || return 0
  [ -n "$found" ]
}

# Fails unless a real sha256 came back: an empty hash must never prove ownership.
witness_sha() {
  local out
  out="$(shasum -a 256 "$1" 2>/dev/null)" || return 1
  out="${out%% *}"
  [[ "$out" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$out"
}

witness_record_get() { sed -n "s/^$1=//p" "$WITNESS_RECORD" 2>/dev/null | head -1; }

witness_record_set() {
  sed "/^$1=/d" "$WITNESS_RECORD" > "$WITNESS_RECORD.tmp" && printf '%s=%s\n' "$1" "$2" >> "$WITNESS_RECORD.tmp" \
    && mv -f "$WITNESS_RECORD.tmp" "$WITNESS_RECORD"
}

# <pid>:<start time>; the start time keeps a recycled pid from passing as the recorded process.
witness_proc_id() {
  local st
  [ -n "$1" ] || return 1
  st="$(LC_ALL=C TZ=UTC0 ps -p "$1" -o lstart= 2>/dev/null)" && [ -n "$st" ] && printf '%s:%s\n' "$1" "$st"
}

# alive, dead or unknown for a recorded <pid>:<start>. Only a readable process table
# that lists this shell is evidence; dead means the pid is absent or now another process.
witness_proc_state() {
  local pid="${1%%:*}" table now
  [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" != "$1" ] || { echo unknown; return; }
  table="$(ps -A -o pid= 2>/dev/null)" || { echo unknown; return; }
  awk -v p="$$" '$1 == p { f = 1 } END { exit !f }' <<< "$table" || { echo unknown; return; }
  awk -v p="$pid" '$1 == p { f = 1 } END { exit !f }' <<< "$table" || { echo dead; return; }
  now="$(witness_proc_id "$pid")" || { echo unknown; return; }
  if [ "$now" = "$1" ]; then echo alive; else echo dead; fi
}

witness_proc_live() { [ "$(witness_proc_state "$1")" = alive ]; }
witness_proc_gone() { [ "$(witness_proc_state "$1")" = dead ]; }

# Called by start-cli.sh after launch: the server, core pane, any claude under it, and watcher pane.
# Fails unless server, core and watcher are all recorded; stop needs every one to prove the teardown.
witness_record_launch() {
  local pid id core role panes
  pid="$(tmux -S "$TMUX_SOCKET" display-message -p -t "=$SESSION" '#{pid}' 2>/dev/null)" || return 1
  id="$(witness_proc_id "$pid")" && witness_record_set server "$id" || return 1
  panes="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$SESSION" -F '#{pane_pid}' 2>/dev/null)" || return 1
  core="$(witness_proc_id "${panes%%$'\n'*}")" && witness_record_set core "$core" || return 1
  for pid in $(claude_named_pids); do
    [ "$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')" = "${core%%:*}" ] || continue
    id="$(witness_proc_id "$pid")" && witness_record_set core_claude "$id" || return 1
  done
  panes="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$WATCHER_SESSION" -F '#{pane_pid}' 2>/dev/null)" || return 1
  id="$(witness_proc_id "${panes%%$'\n'*}")" && witness_record_set watcher "$id" || return 1
  for role in server core watcher; do [ -n "$(witness_record_get "$role")" ] || return 1; done
}

# A launch that failed, or could not record what it owns, kills its own server and exits nonzero.
witness_abort_launch() {
  tmux -S "$TMUX_SOCKET" kill-server 2>/dev/null \
    || echo "  ⚠ could not kill the witness tmux server on $TMUX_SOCKET" >&2
  echo "  ⚠ witness '$WITNESS' launch FAILED: it did not come up, or not every process it started was recorded." >&2
  echo "    Its record is incomplete, so --witness-stop will refuse. Check that nothing of it runs" >&2
  echo "    (tmux -S $TMUX_SOCKET ls; ps), then remove $WITNESS_ROOT and $WITNESS_CONFIG." >&2
  exit 1
}

# The main checkout's resolved CLAUDE_CONFIG_DIR: where the production core keeps its settings.
witness_production_ccd() {
  local line common
  IFS= read -r line < "$REPO/.git" 2>/dev/null || return 1
  common="${line#gitdir: }"
  common="${common%/worktrees/*}"
  [ "${common##*/}" = ".git" ] && [ -r "${common%/.git}/scripts/sutando-config.sh" ] || return 1
  env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
    bash "${common%/.git}/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null
}

# True when any given Claude settings.json sets skipDangerousModePermissionPrompt to true.
witness_settings_skip_bypass_prompt() {
  local py="$1"; shift
  "$py" - "$@" <<'PY'
import json, sys
for path in sys.argv[1:]:
    try:
        with open(path) as f:
            if json.load(f).get("skipDangerousModePermissionPrompt") is True:
                sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
}

witness_start() {
  local name="$1" ws0 ccd py sha login="" mirror_bypass="" prod_ccd wccd
  witness_paths "$name"
  witness_assert_not_production "$WITNESS_SOCKET" "$WITNESS_SESSION" \
    "${SUTANDO_TMUX_SOCKET:-}" "${SUTANDO_TMUX_SESSION:-}"
  witness_assert_linked_worktree
  [ -e "$WITNESS_ROOT" ] && witness_refuse "witness '$name' already exists at $WITNESS_ROOT; stop it first: --witness-stop $name"
  [ -e "$WITNESS_CONFIG" ] && witness_refuse "$WITNESS_CONFIG exists; witness mode writes its own and will not overwrite or share one"
  witness_tmux_usable || witness_refuse "tmux is missing or not runnable; a witness runs only on its own tmux server"
  witness_sha "$REPO/src/agent/claude/cli/witness-mode.sh" > /dev/null \
    || witness_refuse "no usable sha256 (shasum); stop could not prove the config it writes is its own"
  ws0="$(witness_resolved_workspace)"
  [ -n "$ws0" ] || witness_refuse "could not resolve this checkout's workspace"
  witness_workspace_has_live_core "$ws0" \
    && witness_refuse "a live core beats in this checkout's workspace ($ws0/state/cores)"

  . "$REPO/scripts/python-binary.sh"
  py="$(resolve_python "$REPO")"
  [ -n "$py" ] || witness_refuse "no runnable Python interpreter"
  # Auth comes from the caller's config dir by reference; nothing is copied.
  ccd="${CLAUDE_CONFIG_DIR:-}"
  /bin/bash "$REPO/src/agent/claude/cli/witness-claude.sh" --check && login=vault
  # A fresh config dir mirrors the owner's bypass-dialog choice; a caller's dir is used as it stands.
  if [ -z "$ccd" ]; then
    prod_ccd="$(witness_production_ccd)" || prod_ccd=""
    witness_settings_skip_bypass_prompt "$py" "$HOME/.claude/settings.json" \
      ${prod_ccd:+"$prod_ccd/settings.json"} && mirror_bypass=1
  fi

  mkdir -p "$WITNESS_WS"/tasks "$WITNESS_WS"/results "$WITNESS_WS"/state "$WITNESS_WS"/logs \
    || witness_refuse "cannot create $WITNESS_WS"
  chmod 700 "$WITNESS_ROOT"
  _ws="$WITNESS_WS" _ccd="$ccd" _out="$WITNESS_CONFIG" "$py" - <<'PY' || { rm -rf "$WITNESS_ROOT"; witness_refuse "could not write $WITNESS_CONFIG"; }
import json, os
cfg = {"workspace": {"path": os.environ["_ws"]}}
if os.environ["_ccd"]:
    cfg["core_config_dirs"] = [{"id": "claude-default", "type": "claude",
                                "env_name": "CLAUDE_CONFIG_DIR",
                                "value": os.environ["_ccd"], "synced": False}]
with open(os.environ["_out"], "x") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
  if ! sha="$(witness_sha "$WITNESS_CONFIG")"; then
    rm -f "$WITNESS_CONFIG"; rm -rf "$WITNESS_ROOT"
    witness_refuse "no usable sha256 (shasum) to record $WITNESS_CONFIG's ownership; nothing was launched"
  fi
  {
    echo "repo=$REPO"
    echo "socket=$WITNESS_SOCKET"
    echo "session=$WITNESS_SESSION"
    echo "workspace=$WITNESS_WS"
    echo "config_sha=$sha"
  } > "$WITNESS_RECORD"

  if [ "$(witness_resolved_workspace)" != "$WITNESS_WS" ]; then
    rm -f "$WITNESS_CONFIG"; rm -rf "$WITNESS_ROOT"
    witness_refuse "the workspace override did not take effect; nothing was launched"
  fi
  if [ -n "$mirror_bypass" ]; then
    wccd="$(env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
      bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null)"
    case "$wccd" in "$WITNESS_ROOT"/*) ;; *) mirror_bypass="" ;; esac
  fi
  if [ -n "$ccd" ]; then
    witness_settings_skip_bypass_prompt "$py" "$ccd/settings.json" \
      || echo "  ⚠ $ccd/settings.json does not set skipDangerousModePermissionPrompt and is not edited: the witness pane will stop at the Bypass Permissions dialog" >&2
  elif [ -n "$mirror_bypass" ]; then
    echo "  ✓ witness gets a fresh CLAUDE_CONFIG_DIR; skipDangerousModePermissionPrompt mirrored from your settings into it"
  else
    echo "  ⚠ neither your Claude settings nor production's set skipDangerousModePermissionPrompt, so it is not seeded: the witness pane will stop at the Bypass Permissions dialog" >&2
  fi
  if [ -n "$login" ]; then
    echo "  ✓ witness login: CLAUDE_CODE_OAUTH_TOKEN from the vault, passed only to the core's claude"
  else
    echo "  ⚠ no CLAUDE_CODE_OAUTH_TOKEN in the vault: the witness pane will need /login. One-time setup: run \`claude setup-token\`, then send \`vault set CLAUDE_CODE_OAUTH_TOKEN <token>\` via Slack or Discord" >&2
  fi
  echo "  ✓ witness '$name': workspace $WITNESS_WS"

  local launch_env=()
  [ -n "${SUTANDO_NOTIFIER_GRACE_PERIOD:-}" ] && launch_env+=("SUTANDO_NOTIFIER_GRACE_PERIOD=$SUTANDO_NOTIFIER_GRACE_PERIOD")
  [ -n "${SUTANDO_NOTIFIER_ROLE_POLL:-}" ] && launch_env+=("SUTANDO_NOTIFIER_ROLE_POLL=$SUTANDO_NOTIFIER_ROLE_POLL")
  [ -n "$mirror_bypass" ] && launch_env+=("SUTANDO_ACCEPT_BYPASS_PERMISSIONS=1")
  # A clean env, so nothing of the caller's (memory dir, inbox resolver, instance id) reaches the witness.
  exec env -i PATH="$PATH" HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-}" SHELL="${SHELL:-/bin/bash}" \
    TERM="${TERM:-xterm-256color}" LANG="${LANG:-en_US.UTF-8}" TMPDIR="${TMPDIR:-/tmp}" \
    ${launch_env[@]+"${launch_env[@]}"} \
    SUTANDO_TMUX_SOCKET="$WITNESS_SOCKET" SUTANDO_TMUX_SESSION="$WITNESS_SESSION" \
    SUTANDO_WORKSPACE_DIR="$WITNESS_WS" SUTANDO_TASKS_DIR="$WITNESS_WS/tasks" \
    SUTANDO_RESULTS_DIR="$WITNESS_WS/results" SUTANDO_CLAUDE_WORKING_DIR="$REPO" \
    /bin/bash "$REPO/src/agent/claude/cli/start-cli.sh" --witness-scrubbed "$name"
}

# The second stage, under the clean env: re-derive everything from the name and
# re-check it, so this stage never trusts what the first one exported.
witness_enter() {
  witness_paths "$1"
  [ -f "$WITNESS_RECORD" ] || witness_refuse "no witness record at $WITNESS_RECORD; start with --witness $1"
  [ "$(witness_record_get repo)" = "$REPO" ] || witness_refuse "witness '$1' belongs to $(witness_record_get repo)"
  [ "${SUTANDO_TMUX_SOCKET:-}" = "$WITNESS_SOCKET" ] && [ "${SUTANDO_TMUX_SESSION:-}" = "$WITNESS_SESSION" ] \
    || witness_refuse "the launch env does not name witness '$1''s socket and session"
  [ "${SUTANDO_WORKSPACE_DIR:-}" = "$WITNESS_WS" ] || witness_refuse "the launch env does not name the witness workspace"
  witness_assert_not_production "$SUTANDO_TMUX_SOCKET" "$SUTANDO_TMUX_SESSION"
  [ "$(witness_resolved_workspace)" = "$WITNESS_WS" ] || witness_refuse "this checkout no longer resolves to the witness workspace"
  witness_tmux_usable || witness_refuse "tmux is missing or not runnable; stop this witness: --witness-stop $1"
  WITNESS="$1"
}

witness_stop() {
  local name="$1" py server state role proc left="" want have _
  witness_paths "$name"
  witness_assert_not_production "$WITNESS_SOCKET" "$WITNESS_SESSION"
  [ -f "$WITNESS_RECORD" ] || witness_refuse "no witness '$name' at $WITNESS_ROOT"
  [ "$(witness_record_get repo)" = "$REPO" ] || witness_refuse "witness '$name' was started from $(witness_record_get repo); stop it from there"
  [ "$(witness_record_get socket)" = "$WITNESS_SOCKET" ] && [ "$(witness_record_get session)" = "$WITNESS_SESSION" ] \
    || witness_refuse "the witness record does not match the derived socket/session"

  . "$REPO/scripts/python-binary.sh"
  py="$(resolve_python "$REPO")"
  # The heartbeat writer resolves its workspace through the override, so stop it while that still holds.
  if [ -n "$py" ] && [ "$(witness_resolved_workspace)" = "$WITNESS_WS" ]; then
    env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
      SUTANDO_TMUX_SOCKET="$WITNESS_SOCKET" SUTANDO_TMUX_SESSION="$WITNESS_SESSION" \
      "$py" "$REPO/src/core_heartbeat.py" --stop > /dev/null 2>&1 || true
  fi
  for role in server core watcher; do
    [ -n "$(witness_record_get "$role")" ] || witness_fail "the witness record has no $role, so stop cannot prove it is gone; nothing was removed. Check that nothing of it runs (tmux -S $WITNESS_SOCKET ls; ps), then remove $WITNESS_ROOT and $WITNESS_CONFIG"
  done
  server="$(witness_record_get server)"
  state="$(witness_proc_state "$server")"
  if [ "$state" != dead ] || [ -S "$WITNESS_SOCKET" ]; then
    if witness_tmux_usable; then
      tmux -S "$WITNESS_SOCKET" kill-server 2>/dev/null || echo "  ⚠ tmux kill-server failed on $WITNESS_SOCKET" >&2
    else
      echo "  ⚠ tmux is missing or not runnable; cannot stop the server on $WITNESS_SOCKET" >&2
    fi
  fi
  for _ in $(seq 1 25); do state="$(witness_proc_state "$server")"; [ "$state" = alive ] || break; sleep 0.2; done
  [ "$state" = dead ] || witness_fail "the witness tmux server (pid ${server%%:*}) is $state, not proven dead; nothing was removed"
  if [ -S "$WITNESS_SOCKET" ] && witness_tmux_usable && tmux -S "$WITNESS_SOCKET" list-sessions > /dev/null 2>&1; then
    witness_fail "a tmux server still answers on $WITNESS_SOCKET"
  fi
  # Only the processes this witness recorded at launch; a recycled pid reads as dead.
  for role in core_claude core watcher; do
    proc="$(witness_record_get "$role")"
    [ -n "$proc" ] || continue
    [ "$(witness_proc_state "$proc")" = alive ] && { kill "${proc%%:*}" 2>/dev/null || true; }
  done
  for _ in $(seq 1 25); do
    left=""
    for role in core_claude core watcher; do
      proc="$(witness_record_get "$role")"
      [ -n "$proc" ] || continue
      state="$(witness_proc_state "$proc")"
      [ "$state" = dead ] || left="$left $role=${proc%%:*}($state)"
    done
    [ -n "$left" ] || break
    sleep 0.2
  done
  [ -z "$left" ] || witness_fail "recorded witness processes not proven gone:$left; nothing was removed"
  if [ -f "$WITNESS_CONFIG" ]; then
    want="$(witness_record_get config_sha)"
    if ! [[ "$want" =~ ^[0-9a-f]{64}$ ]] || ! have="$(witness_sha "$WITNESS_CONFIG")"; then
      witness_fail "no usable sha256 to prove $WITNESS_CONFIG is the witness's own; it is left in place"
    elif [ "$have" = "$want" ]; then
      rm -f "$WITNESS_CONFIG"
    else
      echo "  ⚠ $WITNESS_CONFIG changed since the witness wrote it — left in place" >&2
    fi
  fi
  case "$WITNESS_ROOT" in
    */sutando-witness/"$name") rm -rf "$WITNESS_ROOT" ;;
  esac
  echo "  ✓ witness '$name' stopped: tmux server $WITNESS_SOCKET gone, $WITNESS_ROOT removed"
}

# Entry from start-cli.sh. A witness flag anywhere but first is refused: a later
# position would otherwise fall through to a production launch.
witness_dispatch() {
  local i=0 a
  for a in "$@"; do
    i=$((i + 1))
    case "$a" in
      --witness=*|--witness-stop=*) witness_refuse "use '--witness <name>' / '--witness-stop <name>' (no '=')" ;;
      --witness|--witness-stop|--witness-scrubbed)
        [ "$i" = 1 ] || witness_refuse "$a must be the first argument" ;;
    esac
  done
  [ "$#" -le 2 ] || witness_refuse "witness mode takes no other arguments (got: $*)"
  case "${1:-}" in
    --witness) witness_start "${2:-}" ;;
    --witness-stop) witness_stop "${2:-}"; exit 0 ;;
    --witness-scrubbed) witness_enter "${2:-}" ;;
  esac
}
