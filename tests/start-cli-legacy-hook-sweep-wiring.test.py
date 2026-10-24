#!/usr/bin/env python3
"""
The legacy hook sweep runs only where a running core can no longer depend on the
copies: in start-cli.sh past the attach/adopt exits, after the launch settings are
built, and only when those settings carry every Sutando hook. A worker launch never
sweeps, and a launch without settings (node absent, a failed build) keeps the copies.

LaunchSchedules runs the real launchers end to end with process probes stubbed;
GatedSweep sources the sweep step from session-launch.sh with a marker installer.
"""

from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SESSION_LAUNCH = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"
WORKER = REPO / "skills" / "worker-pool" / "scripts" / "launch-worker-session.sh"
CALL = "sweep_legacy_claude_hooks_for_launch"
FAKEPID = "999999"
# Marker-driven stand-ins, the shape tests/start-cli-restart-verify.test.py uses: SESS_MARK is a
# tmux session on the socket, CORE_MARK holds the cmdline of the one live `claude --name <s>`.
TMUX_STUB = r"""#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
case "$sub" in
  has-session) [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  new-session) name=""; prev=""; for a in "$@"; do [ "$prev" = "-s" ] && name="$a"; prev="$a"; done
               touch "$SESS_MARK"; [ "${name%-watcher}" = "$name" ] && { echo "claude --name $name" > "$CORE_MARK"; touch "$PANE_MARK"; }; exit 0 ;;
  new-window) touch "$PANE_MARK"; echo 0; exit 0 ;;
  list-panes) [ -f "$SESS_MARK" ] || exit 1; [ -f "$PANE_MARK" ] && echo "@PID@"; exit 0 ;;
  kill-session) case "$*" in *-watcher*) exit 0 ;; esac; rm -f "$SESS_MARK" "$CORE_MARK" "$PANE_MARK"; exit 0 ;;
  *) exit 0 ;;
esac
""".replace("@PID@", FAKEPID)
PGREP_STUB = r"""#!/bin/bash
case "$*" in *core-input-watch*) exit 0 ;; esac
case "$*" in *claude*) [ -f "$CORE_MARK" ] && { echo "%s claude"; exit 0; }; exit 1 ;; esac
exit 1
""" % FAKEPID
PS_STUB = r"""#!/bin/bash
case "$*" in *ppid=*) echo 1; exit 0 ;; esac
want=""; prev=""; for a in "$@"; do [ "$prev" = "-p" ] && want="$a"; prev="$a"; done
[ "$want" = "%s" ] && [ -f "$CORE_MARK" ] || exit 0
case "$*" in *lstart=*) echo "Thu Jan  1 00:00:00 2026" ;; *) cat "$CORE_MARK" ;; esac
exit 0
""" % FAKEPID
# The real interpreter, with the core's long-lived daemons turned into no-ops.
PY_STUB = r"""#!/bin/bash
for a in "$@"; do case "$a" in *core_heartbeat.py|*core-input-watch.py|*core-supervisor-relay.py|*notifier*) exit 0 ;; esac; done
exec "$REAL_PY" "$@"
"""
WORKER_ENV = {"SUTANDO_TMUX_SESSION": "sutando-worker-" + "c" * 32, "SUTANDO_INSTANCE_ID": "c" * 32,
              "SUTANDO_CLAUDE_SESSION_ID": "11111111-2222-3333-4444-555555555555"}


@unittest.skipUnless(shutil.which("node"), "node is required to build the launch settings")
class LaunchSchedules(unittest.TestCase):
    """The launchers run for real against a scratch checkout; only tmux, pgrep, ps and claude
    are stand-ins. A legacy project settings file is present throughout."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.root = self.td / "repo"
        shutil.copytree(REPO / "src", self.root / "src", symlinks=True)
        shutil.copytree(REPO / "scripts", self.root / "scripts", symlinks=True)
        (self.root / "skills/worker-pool/scripts").mkdir(parents=True)
        shutil.copy2(WORKER, self.root / "skills/worker-pool/scripts/launch-worker-session.sh")
        (self.root / "hooks").mkdir()
        (self.root / "hooks" / "skip-ask-user-question.py").write_text("")
        self.ws = self.td / "ws"
        (self.ws / "state").mkdir(parents=True)
        (self.ws / "state" / "core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
        (self.root / "scripts/sutando-config.sh").write_text(
            '#!/bin/bash\ncase "$1" in\n  workspace) echo "%s";;\n  claude-sutando-config-dir) echo "%s/.claude-sutando";;\n'
            '  python-bin) echo python3;;\n  core-runtime) echo claude;;\n  host-label) echo testhost;;\n  *) echo "";;\nesac\n'
            % (self.ws, self.ws))
        # The real sweep still runs; the wrapper only records that it was called.
        real = self.root / "src/install-claude-hooks.real.sh"
        (self.root / "src/install-claude-hooks.sh").rename(real)
        self.marker = self.root / "sweep-ran.marker"
        (self.root / "src/install-claude-hooks.sh").write_text(
            f"#!/usr/bin/env bash\necho ran >> '{self.marker}'\nexec bash '{real}' \"$@\"\n")
        self.legacy = self.root / ".claude" / "settings.json"
        self.legacy.parent.mkdir()
        owned = f"bash '{self.root}/src/check-pending-tasks.sh'"
        self.legacy.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": owned}, {"type": "command", "command": "echo operator"}]}]}}))
        self.legacy_bytes = self.legacy.read_bytes()
        bind = self.td / "bin"
        bind.mkdir()
        (self.td / "home").mkdir()
        for name, body in (("tmux", TMUX_STUB), ("pgrep", PGREP_STUB), ("ps", PS_STUB),
                           ("claude", "#!/bin/bash\nexit 0\n"), ("py", PY_STUB)):
            (bind / name).write_text(body)
            (bind / name).chmod(0o755)
        self.sess, self.core, self.pane = self.td / "sess", self.td / "core", self.td / "pane"
        self.delivery = self.td / "pool_delivery.py"
        self.delivery.write_text("# test-only\n")
        self.env = hermetic_env(
            self.td / "home", PATH=f"{bind}:{Path(shutil.which('node')).parent}:/usr/bin:/bin",
            SESS_MARK=str(self.sess), CORE_MARK=str(self.core), PANE_MARK=str(self.pane), SUTANDO_PY=str(bind / "py"),
            REAL_PY=sys.executable, ANTHROPIC_BASE_URL="http://127.0.0.1:9", SUTANDO_TEST_MODE="1",
            SUTANDO_WORKSPACE=str(self.ws), SUTANDO_RESTART_GRACE_S="1")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def live_core(self, in_pane=True):
        self.sess.write_text("")
        self.core.write_text("claude --name sutando-core\n")
        if in_pane:
            self.pane.write_text("")

    def run_core(self, *args, env=None):
        return subprocess.run(["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh"), *args],
                              env=env or self.env, capture_output=True, text=True, timeout=120)

    def hc(self):
        spec = importlib.util.spec_from_file_location("hc_sweep_wiring", REPO / "src" / "health-check.py")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        return mod

    def probe(self):
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": "claude", "SUTANDO_CLAUDE_WORKING_DIR": ""}):
            return self.hc().check_claude_hook_registration(repo_dir=self.root, config_dir=self.ws / ".claude-sutando",
                                                            workspace_dir=self.ws)

    def record(self):
        return self.ws / "state" / "core-launch-settings.json"

    def assert_untouched(self, run):
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(self.marker.exists(), "the installer ran while a core still depends on the copies")
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes, "the legacy settings file changed")
        self.assertFalse(self.record().exists(), "a launch record was written without a launch")

    def test_no_op_attach_sweeps_nothing(self):
        self.live_core()
        run = self.run_core()
        self.assertIn("already running", run.stdout + run.stderr)
        self.assert_untouched(run)

    def test_orphan_adopt_sweeps_nothing(self):
        self.core.write_text("claude --name sutando-core\n")
        run = self.run_core()
        self.assertIn("reusing it", run.stdout + run.stderr)
        self.assert_untouched(run)

    def test_a_worker_launch_beside_a_live_core_sweeps_nothing(self):
        self.live_core()
        run = subprocess.run(["/bin/bash", str(self.root / "skills/worker-pool/scripts/launch-worker-session.sh")],
                             env={**self.env, **WORKER_ENV, "SUTANDO_POOL_DELIVERY_SCRIPT": str(self.delivery)},
                             capture_output=True, text=True, timeout=120)
        self.assertIn("Started worker session", run.stdout)
        self.assert_untouched(run)

    def test_restart_with_a_full_payload_sweeps_once_and_records_the_launch(self):
        self.live_core()
        run = self.run_core("--restart")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.marker.read_text().count("ran"), 1)
        hooks = json.loads(self.legacy.read_text())["hooks"]
        self.assertEqual([h["command"] for g in hooks["Stop"] for h in g["hooks"]], ["echo operator"])
        sys.path.insert(0, str(REPO / "src"))
        import claude_hooks_settings as chs
        rec = json.loads(self.record().read_text())
        self.assertEqual((rec["repo"], rec["pid"], rec["session"], rec["other_core_live"]),
                         (str(self.root), int(FAKEPID), "sutando-core", False))
        self.assertTrue(rec["start"])
        self.assertTrue(chs.owned_launch_records(self.root) <= chs.records_in(rec["settings"]))

    def test_a_later_launch_without_settings_retires_the_record_and_health_refuses_to_sweep(self):
        # Production path twice: a full launch, then the core dies and a launch with no node
        # (so no --settings) brings up a core that runs the settings-file copies again.
        self.live_core()
        run = self.run_core("--restart")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(self.record().exists())
        for m in (self.sess, self.core, self.pane):
            m.unlink(missing_ok=True)
        self.legacy.write_bytes(self.legacy_bytes)
        self.marker.unlink()
        nonode = {**self.env, "PATH": self.env["PATH"].split(":", 1)[0] + ":/usr/bin:/bin"}
        run = self.run_core(env=nonode)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("node unavailable", run.stderr)
        self.assertFalse(self.record().exists(), "the first launch's record survived a launch without settings")
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        out = self.probe()
        self.assertIn("no launch record for the running core", out["detail"])
        self.assertFalse(out["_live_core_has_launch_hooks"])
        self.hc().apply_claude_hooks_fix([out], stream=io.StringIO())
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes, "--fix swept the copies the live core runs")

    def _private(self, *, session):
        self.core.write_text("claude --name sutando-core\n")
        if session:
            self.sess.write_text("")
        env = {**self.env, "SUTANDO_TMUX_SOCKET": str(self.td / "private.sock")}
        run = self.run_core(env=env)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("running outside", run.stderr)
        self.assertIn("legacy hook entries kept", run.stderr)
        self.assertFalse(self.marker.exists(), "swept while a same-name core outside the socket still runs")
        self.assertEqual(self.legacy.read_bytes(), self.legacy_bytes)
        rec = json.loads(self.record().read_text())
        self.assertTrue(rec["other_core_live"])
        out = self.probe()
        self.assertIn("another same-name core was live outside the socket", out["detail"])
        self.assertFalse(out["_live_core_has_launch_hooks"])
        return run

    def test_a_private_socket_launch_beside_an_outside_core_keeps_the_copies(self):
        run = self._private(session=False)
        self.assertIn("starting a session on this socket", run.stderr)

    def test_a_private_socket_heal_beside_an_outside_core_keeps_the_copies(self):
        run = self._private(session=True)
        self.assertIn("healing core window", run.stderr)


@unittest.skipUnless(shutil.which("node"), "node is required to build the launch settings")
class GatedSweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        for rel in ("src/agent/claude/cli/session-launch.sh", "src/agent/claude/cli/build-core-settings.mjs",
                    "src/agent/claude/cli/owned-hooks.json", "src/agent/claude/cli/retired-hooks.json", "src/claude_hooks_settings.py",
                    "src/skill_hooks.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, self.root / rel)
        self.marker = self.root / "sweep-ran.marker"
        installer = self.root / "src/install-claude-hooks.sh"
        installer.write_text(f"#!/usr/bin/env bash\necho ran >> '{self.marker}'\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _built(self):
        return subprocess.run(
            ["node", str(self.root / "src/agent/claude/cli/build-core-settings.mjs"), "/g.py",
             "--owned-hooks", str(self.root)], capture_output=True, text=True, check=True).stdout

    def _sweep(self, settings_args):
        script = ('. "$REPO/src/agent/claude/cli/session-launch.sh"; PY=python3; SETTINGS_ARGS=("$@"); '
                  f'{CALL}; echo "rc=$?"')
        return subprocess.run(["/bin/bash", "-c", script, "x", *settings_args], capture_output=True, text=True,
                              timeout=30, env=hermetic_env(REPO=str(self.root)))

    def test_carried_settings_sweep_once(self):
        r = self._sweep(["--settings", self._built()])
        self.assertIn("rc=0", r.stdout, r.stderr)
        self.assertEqual(self.marker.read_text().count("ran"), 1)

    def test_no_settings_keeps_the_copies(self):
        r = self._sweep([])
        self.assertIn("rc=0", r.stdout)
        self.assertFalse(self.marker.exists())
        self.assertIn("legacy hook entries kept", r.stderr)

    def test_settings_missing_a_hook_keep_the_copies(self):
        partial = json.loads(self._built())
        partial["hooks"].pop("UserPromptSubmit")
        r = self._sweep(["--settings", json.dumps(partial)])
        self.assertIn("rc=0", r.stdout)
        self.assertFalse(self.marker.exists())
        self.assertIn("lack a Sutando hook", r.stderr)

    def test_a_failing_sweep_does_not_abort_the_launch(self):
        (self.root / "src/install-claude-hooks.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
        r = self._sweep(["--settings", self._built()])
        self.assertIn("rc=0", r.stdout)
        self.assertIn("legacy hook sweep failed", r.stderr)


if __name__ == "__main__":
    unittest.main()
