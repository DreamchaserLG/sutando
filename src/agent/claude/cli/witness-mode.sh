#!/bin/bash
# --witness <name>: a throwaway core from THIS checkout on its own socket, session and workspace. --witness-stop stops only
# what it recorded and --witness-status reports what still points at it; none deletes a file. Refusals exit 2 first.

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
  # Matched on the whole string: a line-based grep would accept a name with an embedded newline.
  if ! [[ "$name" =~ ^[a-z0-9][a-z0-9-]{0,19}$ ]]; then
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
  local cores="$1/state/cores" found d
  if ! [ -e "$cores" ]; then
    # Absent only when the nearest existing ancestor is a searchable directory; else it could not be statted.
    d="$cores"; while d="${d%/*}"; [ -n "$d" ] && ! [ -e "$d" ]; do :; done
    [ -d "${d:-/}" ] && [ -x "${d:-/}" ] && return 1
    return 0
  fi
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


# One start or stop per name: an atomic mkdir owned by this process's <pid>:<start>, held across both
# launch stages (exec keeps the pid). It is never taken over; a lock left by a dead owner is removed by hand.
witness_lock() {
  local owner me
  me="$(witness_proc_id "$$")" || witness_refuse "cannot read this process's identity to own the witness lock"
  WITNESS_LOCK="${WITNESS_ROOT%/*}/.$WITNESS_NAME.lock"
  mkdir -p "${WITNESS_LOCK%/*}" 2>/dev/null || witness_refuse "cannot create ${WITNESS_LOCK%/*}"
  if ! mkdir "$WITNESS_LOCK" 2>/dev/null; then
    owner="$(cat "$WITNESS_LOCK/owner" 2>/dev/null)" || owner=""
    local lock="$WITNESS_LOCK"
    WITNESS_LOCK=""
    if [ -z "$owner" ]; then
      witness_refuse "witness '$WITNESS_NAME' is locked with no recorded owner (a start or stop died before writing it); if none runs, remove the lock and retry: rm -rf $(printf '%q' "$lock")"
    fi
    if [ "$(witness_proc_state "$owner")" = dead ]; then
      witness_refuse "witness '$WITNESS_NAME' is locked by a start or stop that is no longer running; if none runs, remove the lock and retry: rm -rf $(printf '%q' "$lock")"
    fi
    witness_refuse "witness '$WITNESS_NAME' is busy: a start or stop holds $lock"
  fi
  if ! printf '%s\n' "$me" > "$WITNESS_LOCK/owner" 2>/dev/null; then
    rmdir "$WITNESS_LOCK" 2>/dev/null
    WITNESS_LOCK=""
    witness_refuse "cannot record this process as the owner of its witness lock"
  fi
  trap witness_unlock EXIT
}

witness_unlock() {
  [ -n "${WITNESS_LOCK:-}" ] && [ "$(cat "$WITNESS_LOCK/owner" 2>/dev/null)" = "$(witness_proc_id "$$")" ] \
    && rm -rf "$WITNESS_LOCK"
  WITNESS_LOCK=""
}

# The second launch stage takes over the lock the first stage took under this same pid.
witness_adopt_lock() {
  WITNESS_LOCK="${WITNESS_ROOT%/*}/.$WITNESS_NAME.lock"
  [ "$(cat "$WITNESS_LOCK/owner" 2>/dev/null)" = "$(witness_proc_id "$$")" ] \
    || { WITNESS_LOCK=""; witness_refuse "this launch does not hold the lock for witness '$WITNESS_NAME'"; }
}

# Records one owned process as <role>=<pid>:<start>; fails if it cannot be read or written.
witness_record_pid() {
  local id
  id="$(witness_proc_id "$2")" && witness_record_set "$1" "$id"
}

witness_first_pane_pid() {
  local out
  out="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$1" -F '#{pane_pid}' 2>/dev/null)" || return 1
  printf '%s\n' "${out%%$'\n'*}"
}

# Called by start-cli.sh as each part comes up. The server and core pane come from new-session's own
# report at creation, so no created server waits on a later read to be recorded; then the watcher.
witness_record_server_core() {
  local spid ppid pid core
  read -r spid ppid _ <<< "${LAUNCHED_SESSION_REPORT:-}"
  witness_record_pid server "$spid" && witness_record_pid core "$ppid" || return 1
  core="$(witness_record_get core)"
  for pid in $(claude_named_pids); do
    [ "$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')" = "${core%%:*}" ] || continue
    witness_record_pid core_claude "$pid" || return 1
  done
}

witness_record_watcher() {
  local pid
  pid="$(witness_first_pane_pid "$WATCHER_SESSION")" && witness_record_pid watcher "$pid"
}

# After a failed launch: record whatever did come up, so the exit path and stop can reach it.
witness_record_partial() {
  witness_record_server_core || true
  witness_record_watcher || true
}

# Signals only processes whose recorded identity is alive right now.
witness_kill_recorded() {
  local role proc
  for role in core_claude core watcher server; do
    proc="$(witness_record_get "$role")"
    [ -n "$proc" ] && [ "$(witness_proc_state "$proc")" = alive ] && { kill "${proc%%:*}" 2>/dev/null || true; }
  done
  return 0
}

# Waits up to ~5s for every recorded process to be proven dead; prints the rest and fails if any remain.
witness_wait_recorded_gone() {
  local i role proc left=""
  for ((i = 0; i < 25; i++)); do
    left=""
    for role in server core core_claude watcher; do
      proc="$(witness_record_get "$role")"
      [ -z "$proc" ] || [ "$(witness_proc_state "$proc")" = dead ] || left="$left $role=${proc%%:*}"
    done
    [ -n "$left" ] || return 0
    sleep 0.2
  done
  printf '%s\n' "$left"
  return 1
}

# Launch succeeded: mark the record complete and release the lock (before any attach).
witness_launch_complete() {
  local role
  for role in server core watcher; do [ -n "$(witness_record_get "$role")" ] || exit 1; done
  witness_record_set complete 1 || exit 1
  trap - EXIT
  witness_unlock
}


# Every exit of launch's second stage before completion: stop what was recorded, delete nothing, and keep
# the lock unless at least one pid was recorded and every recorded pid is proven dead. An empty record proves nothing.
witness_launch_exit() {
  local rc=$? left role recorded=""
  [ "$rc" != 0 ] || rc=1
  for role in server core core_claude watcher; do [ -z "$(witness_record_get "$role")" ] || recorded=1; done
  witness_kill_recorded
  if [ -z "$recorded" ]; then
    left=" (nothing was recorded, so whatever it started cannot be identified)"
  fi
  if [ -n "$recorded" ] && left="$(witness_wait_recorded_gone)"; then
    witness_unlock
    echo "  ⚠ witness '$WITNESS_NAME' launch FAILED (exit $rc); its recorded processes are gone and nothing was deleted. Run --witness-stop $WITNESS_NAME for what is left." >&2
  else
    echo "  ⚠ witness '$WITNESS_NAME' launch FAILED (exit $rc); not proven gone:$left. Its lock is kept." >&2
    echo "    Check for its processes: tmux -S $(printf '%q' "$WITNESS_SOCKET") ls; pgrep -fl -- $(printf '%q' "$WITNESS_SESSION")" >&2
    echo "    Once none run, remove the lock by hand: rm -rf $(printf '%q' "$WITNESS_LOCK"); then run --witness-stop $WITNESS_NAME" >&2
  fi
  exit "$rc"
}

# A first-stage failure after the root exists: nothing was started, and its files are left for the user.
witness_start_fail() {
  echo "start-cli witness: launch FAILED — $*; nothing was started or deleted. Run --witness-stop $WITNESS_NAME for what is left." >&2
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
  local name="$1" ws0 ccd py sha tmp login="" mirror_bypass="" prod_ccd wccd
  witness_paths "$name"
  witness_assert_not_production "$WITNESS_SOCKET" "$WITNESS_SESSION" \
    "${SUTANDO_TMUX_SOCKET:-}" "${SUTANDO_TMUX_SESSION:-}"
  witness_assert_linked_worktree
  [ -e "$WITNESS_ROOT" ] && witness_refuse "witness '$name' already exists at $WITNESS_ROOT. If it runs, stop it: --witness-stop $name. If it is stopped, remove its local config first, confirm with --witness-status $name, then remove that root"
  [ -e "$WITNESS_CONFIG" ] && witness_refuse "$WITNESS_CONFIG exists; witness mode writes its own and will not overwrite or share one. If a stopped witness wrote it, remove it, then that witness's root (check with --witness-status <name>)"
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

  witness_lock
  # Re-checked under the lock, so a concurrent same-name start cannot have passed them too.
  [ -e "$WITNESS_ROOT" ] && witness_refuse "witness '$name' already exists at $WITNESS_ROOT. If it runs, stop it: --witness-stop $name. If it is stopped, remove its local config first, confirm with --witness-status $name, then remove that root"
  [ -e "$WITNESS_CONFIG" ] && witness_refuse "$WITNESS_CONFIG exists; witness mode writes its own and will not overwrite or share one. If a stopped witness wrote it, remove it, then that witness's root (check with --witness-status <name>)"
  mkdir -m 700 "$WITNESS_ROOT" 2>/dev/null || witness_refuse "cannot create $WITNESS_ROOT"
  # The record, with its intent marker, exists before anything else is made, so stop can always reach it.
  printf 'intent=launch\nrepo=%s\nsocket=%s\nsession=%s\nworkspace=%s\n' \
    "$REPO" "$WITNESS_SOCKET" "$WITNESS_SESSION" "$WITNESS_WS" > "$WITNESS_RECORD" \
    || { echo "start-cli witness: launch FAILED — could not write $WITNESS_RECORD; nothing was started or deleted. $WITNESS_ROOT is left for you to remove." >&2; exit 1; }
  mkdir -p "$WITNESS_WS"/tasks "$WITNESS_WS"/results "$WITNESS_WS"/state "$WITNESS_WS"/logs \
    || witness_start_fail "cannot create $WITNESS_WS"
  # Written whole to a temp file beside it, then published by a no-clobber link: the config is never partial.
  tmp="$REPO/.sutando.config.local.json.witness-$name.$$.tmp"
  _ws="$WITNESS_WS" _ccd="$ccd" _out="$tmp" "$py" - <<'PY' || { rm -f "$tmp"; witness_start_fail "could not write the witness config"; }
import json, os
cfg = {"workspace": {"path": os.environ["_ws"]}}
if os.environ["_ccd"]:
    cfg["core_config_dirs"] = [{"id": "claude-default", "type": "claude",
                                "env_name": "CLAUDE_CONFIG_DIR",
                                "value": os.environ["_ccd"], "synced": False}]
with open(os.environ["_out"], "x") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
PY
  sha="$(witness_sha "$tmp")" && witness_record_set config_sha "$sha" \
    || { rm -f "$tmp"; witness_start_fail "no usable sha256 (shasum) to record the config's ownership"; }
  ln "$tmp" "$WITNESS_CONFIG" 2>/dev/null || { rm -f "$tmp"; witness_start_fail "could not publish $WITNESS_CONFIG"; }
  rm -f "$tmp"

  [ "$(witness_resolved_workspace)" = "$WITNESS_WS" ] \
    || witness_start_fail "the workspace override did not take effect; nothing was launched"
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
  witness_adopt_lock
  trap witness_launch_exit EXIT
  # new-session reports the server and pane pids it created, so they are recorded from creation itself.
  NEW_SESSION_REPORT_ARGS=(-P -F '#{pid} #{pane_pid}')
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

# Unless nothing answers on the socket, or what answers is the recorded server alive, calls <on-fail> (refuse or fail).
witness_check_socket_owner() {
  local server="$1" on_fail="$2" answering
  [ -e "$WITNESS_SOCKET" ] || return 0
  witness_tmux_usable || "$on_fail" "tmux is missing or not runnable, so what holds $WITNESS_SOCKET cannot be identified"
  if answering="$(tmux -S "$WITNESS_SOCKET" display-message -p '#{pid}' 2>&1)"; then
    [ -n "$server" ] && [ "$answering" = "${server%%:*}" ] && [ "$(witness_proc_state "$server")" = alive ] \
      || "$on_fail" "the tmux server answering on $WITNESS_SOCKET (pid $answering) is not the recorded one"
  else
    case "$answering" in
      *"no server running"*|*"error connecting"*) ;;
      *) "$on_fail" "cannot tell what holds $WITNESS_SOCKET ($answering)" ;;
    esac
  fi
}

# The checks stop and clean share: the record names this witness of this checkout.
witness_check_record() {
  [ -f "$WITNESS_RECORD" ] || witness_refuse "no witness '$WITNESS_NAME' at $WITNESS_ROOT"
  [ "$(witness_record_get intent)" = launch ] || witness_refuse "$WITNESS_RECORD has no launch intent marker"
  [ "$(witness_record_get repo)" = "$REPO" ] || witness_refuse "witness '$WITNESS_NAME' was started from $(witness_record_get repo); run this from there"
  [ "$(witness_record_get socket)" = "$WITNESS_SOCKET" ] && [ "$(witness_record_get session)" = "$WITNESS_SESSION" ] \
    && [ "$(witness_record_get workspace)" = "$WITNESS_WS" ] \
    || witness_refuse "the witness record does not match the derived socket/session/workspace"
}

witness_stop() {
  local name="$1" py server role proc left
  witness_paths "$name"
  witness_assert_not_production "$WITNESS_SOCKET" "$WITNESS_SESSION"
  [ -f "$WITNESS_RECORD" ] || witness_refuse "no witness '$name' at $WITNESS_ROOT"
  witness_lock
  # Every check runs before the first side effect, and refuses (exit 2).
  witness_check_record
  if [ "$(witness_record_get complete)" = 1 ]; then
    for role in server core watcher; do
      [ -n "$(witness_record_get "$role")" ] || witness_refuse "the completed witness record has no $role, so stop cannot prove it is gone"
    done
  fi
  for role in server core core_claude watcher; do
    proc="$(witness_record_get "$role")"
    [ -z "$proc" ] || [ "$(witness_proc_state "$proc")" != unknown ] \
      || witness_refuse "cannot tell whether the recorded $role (pid ${proc%%:*}) is alive"
  done
  server="$(witness_record_get server)"
  witness_check_socket_owner "$server" witness_refuse

  . "$REPO/scripts/python-binary.sh"
  py="$(resolve_python "$REPO")"
  # The heartbeat writer resolves its workspace through the override, so stop it while that still holds.
  if [ -n "$py" ] && [ "$(witness_resolved_workspace)" = "$WITNESS_WS" ]; then
    env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
      SUTANDO_TMUX_SOCKET="$WITNESS_SOCKET" SUTANDO_TMUX_SESSION="$WITNESS_SESSION" \
      "$py" "$REPO/src/core_heartbeat.py" --stop > /dev/null 2>&1 || true
  fi
  witness_kill_recorded
  left="$(witness_wait_recorded_gone)" || witness_fail "recorded witness processes not proven gone:$left"
  witness_check_socket_owner "" witness_fail
  trap - EXIT
  witness_unlock

  echo "  ✓ witness '$name' stopped: every recorded process is gone. Nothing was deleted; left in place:"
  echo "    $WITNESS_ROOT  (its workspace)"
  echo "    $WITNESS_RECORD  (its record, inside that root)"
  [ -e "$WITNESS_CONFIG" ] && echo "    $WITNESS_CONFIG  (this checkout's local config)"
  echo "  To remove them, in this order:"
  [ -e "$WITNESS_CONFIG" ] && echo "    - first the local config this witness wrote, $WITNESS_CONFIG"
  echo "    - check that nothing still points into the root (exits 0 only when none does):"
  echo "        bash $(printf '%q' "$REPO/src/agent/start-cli.sh") --witness-status $name"
  echo "    - then the root, $WITNESS_ROOT"
}


# Entry from start-cli.sh. A witness flag anywhere but first is refused: a later
# position would otherwise fall through to a production launch.
# Read-only: for each local config it knows of, whether that config points into the witness root.
# Exits 0 only when every one definitely does not; an unreadable or unparsable one is UNKNOWN.
witness_status() {
  local name="$1" py
  witness_paths "$name"
  . "$REPO/scripts/python-binary.sh"
  py="$(resolve_python "$REPO")"
  [ -n "$py" ] || witness_refuse "no runnable Python interpreter to read $WITNESS_CONFIG"
  if [ -e "$WITNESS_ROOT" ]; then echo "  witness '$name': root $WITNESS_ROOT is present"
  else echo "  witness '$name': root $WITNESS_ROOT is absent"; fi
  if _root="$WITNESS_ROOT" "$py" - "$WITNESS_CONFIG" <<'PY'
import json, os, sys
root = os.path.realpath(os.environ["_root"])

def inside(value):
    path = os.path.expanduser(value) if isinstance(value, str) else ""
    if not os.path.isabs(path):
        return False
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)

def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)

ok = True
for path in sys.argv[1:]:
    if not os.path.lexists(path):
        print(f"    {path}: absent, so it does not point into the root")
        continue
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"    {path}: UNKNOWN, cannot be read as JSON ({type(e).__name__})")
        ok = False
        continue
    if any(inside(s) for s in strings(data)):
        print(f"    {path}: POINTS INTO the root")
        ok = False
    else:
        print(f"    {path}: does not point into the root")
sys.exit(0 if ok else 1)
PY
  then
    echo "  Nothing known points into $WITNESS_ROOT; it is safe to remove."
  else
    echo "  Do not remove $WITNESS_ROOT yet: fix or remove each config above, then rerun --witness-status $name." >&2
    exit 1
  fi
}

witness_dispatch() {
  local i=0 a
  for a in "$@"; do
    i=$((i + 1))
    case "$a" in
      --witness|--witness-stop|--witness-status|--witness-scrubbed)
        [ "$i" = 1 ] || witness_refuse "$a must be the first argument" ;;
    esac
  done
  [ "$#" -le 2 ] || witness_refuse "witness mode takes no other arguments (got: $*)"
  case "${1:-}" in
    --witness) witness_start "${2:-}" ;;
    --witness-stop) witness_stop "${2:-}"; exit 0 ;;
    --witness-status) witness_status "${2:-}"; exit 0 ;;
    --witness-scrubbed) witness_enter "${2:-}" ;;
  esac
}
