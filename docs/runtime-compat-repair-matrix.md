# Core CLI repair matrix

Sutando runs on Claude Code or the Codex CLI. This page lists every state the
CLI can be in, how Sutando detects it, and how to get from that state back to
a running core.

The code is `src/runtime_compat.py`. Both launchers call it before they replace
a core, through `src/agent/runtime-compat-gate.sh`. `health-check.py` reports the
same verdict as `core-cli-compat`.

## Check your install

```bash
python3 src/runtime_compat.py doctor            # both CLIs, with the fix for each
python3 src/runtime_compat.py doctor --json     # the same, for tools
```

## How Sutando picks a binary

Sutando looks in this order:

1. every `claude` or `codex` on `PATH`, in `PATH` order;
2. the Sutando-managed install (see below);
3. known install locations: `$HOME/.local/bin`, `$HOME/.claude/local` (Claude),
   `$HOME/.npm-global/bin` (or `$CODEX_INSTALL_BIN`), and the Homebrew bin folders.

Known locations are searched only when no `claude` or `codex` is on `PATH` at
all. A CLI you put on `PATH` is never replaced by one you did not.

Each one is launched with `--version` and `--help`. The first one that is
`ok` or `newer-untested` is used. So when the first `claude` on your `PATH`
works, Sutando launches exactly the command it launched before. When it does
not, Sutando launches the next working one by its full path and says so.

## The matrix

"Assisted fix" is `python3 src/runtime_compat.py repair --runtime <claude|codex> --yes`.
It installs a compatible CLI from npm into a Sutando-owned folder. It never
changes your own install, your global npm folder, or your CLI config. It runs
only with your consent: `--yes`, `SUTANDO_RUNTIME_REPAIR_CONSENT=1`, or a "y"
at the terminal prompt.

| Class | Detection | User-executable fix | Assisted fix | Verification | Test |
|---|---|---|---|---|---|
| `ok` | `--version` ≥ minimum, every required flag in `--help`, same major as tested | none | none | `doctor` shows `* [ok]` | `test_discovery_reads_version_and_capabilities` |
| `missing` | no binary on `PATH`, in the managed folder, or in a known location | Claude: `curl -fsSL https://claude.ai/install.sh \| bash`. Codex: `npm install -g --prefix ~/.local @openai/codex` | yes | `doctor`, then the launch continues | `test_missing_when_nothing_installed`, `test_missing_cli_stops_with_fix_and_resume_command` |
| `too-old` | version below the minimum (Claude 2.0.0, Codex 0.40.0) | Claude: `<path> update`. npm install: `npm install -g --prefix <its prefix> <package>@<major>` | yes | `doctor` | `test_too_old`, `test_plan_too_old_claude_uses_its_own_updater`, `test_plan_too_old_npm_install_updates_that_prefix` |
| `known-bad` | version is on the spec's known-bad list (empty today) | same as `too-old` | yes | `doctor` | `test_known_bad` |
| `incompatible` | version reads, but a flag the launcher passes is not in `--help` | same as `too-old` | yes | `doctor` | `test_incompatible_when_flag_missing`, `test_help_failure_is_incompatible` |
| `newer-untested` | major version above the tested one; flags present | none needed; to pin: `npm install -g --prefix ~/.local <package>@<tested major>` | not offered | the core starts with a warning | `test_newer_untested_is_usable_with_warning` |
| `broken` | not executable, a dangling link, a partial file, `--version` fails or hangs | reinstall with the `missing` command | yes | `doctor` | `test_broken_*` (7 cases) |
| `broken` (no Node.js) | the CLI exits with `env: node: No such file` | `brew install node` (Node.js 22+), or reinstall the desktop app | not offered | `doctor` | `test_plan_broken_node_missing_points_at_node` |
| several installs | more than one binary found | none; the first working one is used | none | `doctor` marks the chosen one with `*` | `test_multiple_path_installs_first_usable_wins`, `test_multiple_path_installs_keep_path_order_when_both_work`, `test_fallback_install_is_launched_by_path` |
| logged out (Codex) | `codex login status` exits non-zero | `codex login` (with `CODEX_HOME=<home>` when set) | not offered | the launch continues after login | `test_codex_logged_out_points_to_login`, `test_codex_logged_out_stops_then_login_lets_it_start`, `test_auth_recovery_waits_for_login_then_continues` |
| logged out (Claude) | `src/auth_preflight.py` finds no account or credentials | run `claude` in Terminal and complete `/login` | not offered | the launch continues; the boot gate re-checks | `test_claude_auth_uses_auth_preflight`, `test_logged_out_advisory_continues` |

## What can go wrong during an assisted fix

| Failure | Detection | What Sutando does | What you do | Test |
|---|---|---|---|---|
| disk full | free space under the managed folder is below 512 MB, checked before any download; or npm reports `ENOSPC` | stops at once, no retry, old version kept | free the space it names, then rerun the assisted fix | `test_disk_full_checked_before_download`, `test_disk_full_during_download_is_not_retried` |
| blocked download | npm output names a network, proxy or certificate error | retries 3 times with 2 s and 4 s backoff, old version kept | check network, proxy or firewall; npm must reach `registry.npmjs.org`; rerun | `test_blocked_download_retries_with_backoff_then_guides`, `test_blocked_download_recovers_on_retry` |
| interrupted update | npm killed mid-install, or a leftover `.staging-*` folder whose process is gone | the half-written install never replaces the old one; the leftover is reported and removed by the next repair | nothing | `test_interrupted_update_keeps_old_version`, `test_leftover_staging_is_reported_then_cleaned` |
| new binary will not run | the staged or switched binary fails the same `--version` and `--help` probe | reports failure, old version kept | use the manual command | `test_unusable_download_keeps_old_version`, `test_link_that_will_not_launch_is_not_success` |
| npm missing | no `npm` on `PATH` | reports it | install Node.js 22+, or use the manual command | `test_npm_missing` |

Success is reported only after the switched binary has been launched with
`--version` and `--help` (`test_assisted_install_launches_before_reporting_success`).

## Resuming the same journey

The launcher does not restart onboarding after a fix:

- A restart with an unusable CLI stops before it touches the running core
  (`test_restart_with_unusable_cli_kills_nothing`).
- At a terminal, the launcher waits up to 10 minutes for the fix (your own
  command in another window, or a login), checks every 5 seconds, and then
  continues the same launch (`test_waits_for_the_user_fix_then_continues`).
- With consent, the assisted fix runs and the same launch continues
  (`test_consent_runs_the_repair_and_the_launch_continues`).
- Otherwise it prints the fix and the exact command to run again:
  `bash src/agent/start-cli.sh`.

## Where the managed install lives

`$SUTANDO_RUNTIMES_DIR`, else `$XDG_DATA_HOME/sutando/runtimes`, else
`$HOME/.local/share/sutando/runtimes`. Layout:

```
<root>/bin/<cli>                   link to the version in use (switched atomically)
<root>/<cli>/versions/<version>/   one npm prefix per installed version
<root>/<cli>/.staging-<pid>-<ts>/  an install in progress
```

Older versions stay in place, so going back is one link away.

## Settings

| Variable | Effect |
|---|---|
| `SUTANDO_SKIP_RUNTIME_COMPAT=1` | skip the check for this launch |
| `SUTANDO_RUNTIME_REPAIR_CONSENT=1` | consent to the assisted fix without a prompt |
| `SUTANDO_RUNTIME_WAIT_S` | seconds to wait for a fix; default 600 at a terminal, 0 otherwise |
| `SUTANDO_RUNTIMES_DIR` | the managed install root |
| `SUTANDO_RUNTIME_SYSTEM_DIRS` | `:`-separated folders searched instead of the Homebrew bin folders |

## Not covered yet

- The desktop installer's own flow (slice 2, in the desktop repo).
- `src/startup-runtime.sh` still runs its own Codex login preflight before
  services start. Folding it into this module is a follow-up.
