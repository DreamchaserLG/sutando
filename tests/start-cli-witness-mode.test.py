#!/usr/bin/env python3
"""`start-cli.sh --witness <name>` boots a throwaway core beside production, and
`--witness-stop <name>` removes only what it made.

Real launcher, real tmux, real workspace resolution (a copied checkout with the
tracked sutando.config.json), a stub `claude`. A fake production workspace and a
decoy "production" tmux server stand in for the live core; the real default
socket is never touched.
Run: python3 tests/start-cli-witness-mode.test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux") or shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
PGREP_STUB = ('[ "$*" = "-ax claude" ] || exit 1\n'
              '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n')
CLAUDE_STUB = ('printf "%s\\n" "$@" > "$HOME/claude.argv"\n'
               'printf "%s" "${CLAUDE_CODE_OAUTH_TOKEN-}" > "$HOME/claude.token"\n'
               'echo $$ > "$HOME/claude.pid"\nsleep 120\n')
# A fake keychain behind the vault helper: answers only for $HOME/fake-vault-token, never the real one.
SECURITY_STUB = ('[ "$1" = find-generic-password ] || exit 1\n'
                 'case " $* " in *" -s CLAUDE_CODE_OAUTH_TOKEN "*) ;; *) exit 44 ;; esac\n'
                 '[ -s "$HOME/fake-vault-token" ] || exit 44\ncat "$HOME/fake-vault-token"\n')
FAKE_TOKEN = "sk-ant-oat01-FAKE-witness-token-0123456789"
SINGLETONS = ("state/shutdown.sentinel", "state/cores/testhost.alive", "state/core-supervisor.json",
              "state/session-starts.log", "logs/restart-attempts.log", "state/core-status.json")


def snapshot(root: Path) -> dict:
    """Every file and directory under root with its mtime and size."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return out


def seed_production(ws: Path) -> None:
    an_hour_ago = time.time() - 3600
    for rel in SINGLETONS:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"production": true}\n')
    # A live production relay loop: a launch that ignores the witness must not start a second.
    (ws / "state/core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
    for dirpath, dirnames, filenames in os.walk(ws):
        for n in dirnames + filenames + [""]:
            os.utime(Path(dirpath) / n, (an_hour_ago, an_hour_ago))


class Harness:
    def __init__(self, linked: bool = True) -> None:
        if not TMUX:
            raise unittest.SkipTest("tmux not found")
        self.td = Path(tempfile.mkdtemp(prefix="wit"))
        self.root = self.td / "checkout"
        shutil.copytree(REPO / "src", self.root / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        shutil.copytree(REPO / "scripts", self.root / "scripts", symlinks=True)
        shutil.copy2(REPO / "sutando.config.json", self.root / "sutando.config.json")
        if linked:
            (self.root / ".git").write_text(f"gitdir: {self.td}/main/.git/worktrees/checkout\n")
        else:
            (self.root / ".git").mkdir()
        # Where a failed override would land: this checkout's own default workspace.
        self.checkout_ws = self.root / "workspace"
        seed_production(self.checkout_ws)
        # The production workspace a worker caller's env points at.
        self.prod_ws = self.td / "prod-ws"
        seed_production(self.prod_ws)
        self.tmp = self.td / "t"
        self.tmp.mkdir()
        bind = self.td / "bin"
        bind.mkdir()
        self.home = self.td / "home"
        self.home.mkdir()
        self.ccd = self.td / "ccd"
        self.ccd.mkdir()
        for stub, body in (("claude", CLAUDE_STUB), ("pgrep", PGREP_STUB), ("security", SECURITY_STUB),
                           ("lsof", "exit 1\n"), ("launchctl", "exit 1\n")):
            (bind / stub).write_text("#!/bin/bash\n" + body)
            (bind / stub).chmod(0o755)
        self.decoy_sock = self.td / "decoy.sock"
        subprocess.run([TMUX, "-S", str(self.decoy_sock), "new-session", "-d", "-s", "sutando-core", "sleep 300"],
                       check=True)
        self.env = {"PATH": f"{bind}:{Path(TMUX).parent}:/usr/bin:/bin:/usr/sbin",
                    "HOME": str(self.home), "TMPDIR": str(self.tmp),
                    # A worker caller's routing env: none of it may reach the witness.
                    "SUTANDO_TMUX_SOCKET": str(self.decoy_sock), "SUTANDO_TMUX_SESSION": "sutando-core",
                    "SUTANDO_WORKSPACE_DIR": str(self.prod_ws), "SUTANDO_TASKS_DIR": str(self.prod_ws / "tasks"),
                    "SUTANDO_RESULTS_DIR": str(self.prod_ws / "results"),
                    "SUTANDO_MEMORY_DIR": str(self.prod_ws / "memory"), "SUTANDO_INSTANCE_ID": "f" * 32,
                    "SUTANDO_CORE_SESSION": "1", "CLAUDE_CONFIG_DIR": str(self.ccd)}
        self.before = {"checkout": snapshot(self.checkout_ws), "prod": snapshot(self.prod_ws)}

    def witness(self, name: str):
        base = Path(os.path.realpath(self.tmp)) / "sutando-witness" / name
        return base, base / "tmux.sock", self.record(name).get("session", ""), base / "workspace"

    def record(self, name: str) -> dict:
        rec = Path(os.path.realpath(self.tmp)) / "sutando-witness" / name / "witness.env"
        if not rec.exists():
            return {}
        return dict(line.split("=", 1) for line in rec.read_text().splitlines() if "=" in line)

    def shim_dir(self, **shims: str) -> Path:
        """A directory of failing stand-ins, put first on PATH for one run."""
        d = Path(tempfile.mkdtemp(dir=self.td, prefix="shim"))
        for tool, body in shims.items():
            (d / tool).write_text("#!/bin/bash\n" + body)
            (d / tool).chmod(0o755)
        return d

    def path_without_tmux(self) -> str:
        """The run PATH with tmux's directory swapped for just a python3."""
        py = Path(tempfile.mkdtemp(dir=self.td, prefix="py"))
        (py / "python3").symlink_to(os.path.realpath(sys.executable))
        path = f"{self.td / 'bin'}:{py}:/usr/bin:/bin:/usr/sbin"
        if shutil.which("tmux", path=path):
            raise unittest.SkipTest("tmux is in a system directory on this host")
        return path

    def run_tty(self, *args, extra_env=None, timeout=90):
        """The launcher under a real pseudo-terminal, so it takes its TTY branch."""
        import pty
        import select
        env = {**self.env, **(extra_env or {})}
        pid, fd = pty.fork()
        if pid == 0:
            os.execve("/bin/bash", ["/bin/bash", str(self.root / "src/agent/claude/cli/start-cli.sh"), *args], env)
        out, deadline = b"", time.time() + timeout
        try:
            while time.time() < deadline:
                if not select.select([fd], [], [], 0.5)[0]:
                    continue
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        finally:
            os.close(fd)
        if time.time() >= deadline:
            os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    def heartbeat_writers(self) -> list:
        root = os.path.realpath(self.root)
        rows = subprocess.run(["ps", "-A", "-o", "pid=,command="], capture_output=True, text=True).stdout
        return [int(r.split()[0]) for r in rows.splitlines()
                if "core_heartbeat.py" in r and "--" not in r.split("core_heartbeat.py", 1)[1]
                and (root in r or str(self.root) in r)]

    def detached(self, command: str) -> int:
        """A process this test does not parent, so its exit is reaped elsewhere."""
        pid = int(subprocess.run(["/bin/bash", "-c", f"nohup {command} >/dev/null 2>&1 & echo $!"],
                                 capture_output=True, text=True).stdout)
        return pid

    def proc_id(self, pid: int) -> str:
        script = 'REPO="$1"; . "$REPO/src/agent/claude/cli/witness-mode.sh"; witness_proc_id "$2"'
        return subprocess.run(["/bin/bash", "-c", script, "id", str(REPO), str(pid)],
                              capture_output=True, text=True).stdout.rstrip("\n")

    def set_record(self, name: str, **fields):
        rec = Path(os.path.realpath(self.tmp)) / "sutando-witness" / name / "witness.env"
        lines = [l for l in rec.read_text().splitlines() if l.split("=", 1)[0] not in fields]
        rec.write_text("".join(l + "\n" for l in lines + [f"{k}={v}" for k, v in fields.items() if v is not None]))

    def server_pid(self, sock) -> int:
        return int(self.tm(sock, "display-message", "-p", "#{pid}").stdout.strip() or 0)

    def run(self, *args, entry="src/agent/claude/cli/start-cli.sh", extra_env=None, drop=()):
        env = {k: v for k, v in {**self.env, **(extra_env or {})}.items() if k not in drop}
        return subprocess.run(["/bin/bash", str(self.root / entry), *args],
                              env=env, capture_output=True, text=True, timeout=90)

    def tm(self, sock, *a):
        return subprocess.run([TMUX, "-S", str(sock), *a], capture_output=True, text=True)

    def production_untouched(self, case: unittest.TestCase):
        case.assertEqual(self.before["checkout"], snapshot(self.checkout_ws),
                         "the checkout's own workspace changed")
        case.assertEqual(self.before["prod"], snapshot(self.prod_ws), "the production workspace changed")
        case.assertEqual(self.tm(self.decoy_sock, "has-session", "-t", "=sutando-core").returncode, 0,
                         "the decoy production session is gone")

    def close(self):
        for name in ("t1", "t2", "t3"):
            if self.witness(name)[0].exists():
                self.run("--witness-stop", name)
            self.tm(self.witness(name)[1], "kill-server")
        self.tm(self.decoy_sock, "kill-server")
        shutil.rmtree(self.td, ignore_errors=True)


class WitnessLaunch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.run_ = cls.h.run("--witness", "t1")
        cls.root, cls.sock, cls.session, cls.ws = cls.h.witness("t1")

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_launch_succeeds(self):
        self.assertEqual(self.run_.returncode, 0, self.run_.stdout + self.run_.stderr)

    def test_core_runs_in_its_own_session_on_its_own_socket(self):
        sessions = self.h.tm(self.sock, "list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertIn(self.session, sessions)
        self.assertIn(self.session + "-watcher", sessions)
        self.assertNotIn("sutando-core", sessions)

    def test_production_singletons_untouched(self):
        self.h.production_untouched(self)
        self.assertTrue((self.h.prod_ws / "state/shutdown.sentinel").exists(), "production sentinel cleared")

    def test_notifier_is_aimed_at_the_witness_inbox_and_session(self):
        env = self.h.tm(self.sock, "show-environment", "-t", f"={self.session}-watcher").stdout
        for want in (f"SUTANDO_TASKS_DIR={self.ws}/tasks", f"SUTANDO_RESULTS_DIR={self.ws}/results",
                     f"SUTANDO_WORKSPACE_DIR={self.ws}", f"SUTANDO_TMUX_SOCKET={self.sock}",
                     f"SUTANDO_TMUX_SESSION={self.session}"):
            self.assertIn(want, env)
        self.assertNotIn(str(self.h.prod_ws), env)

    def test_caller_routing_env_does_not_reach_the_witness(self):
        glob = self.h.tm(self.sock, "show-environment", "-g").stdout
        for leaked in ("SUTANDO_MEMORY_DIR", "SUTANDO_INSTANCE_ID", str(self.h.prod_ws)):
            self.assertNotIn(leaked, glob)

    def test_workspace_override_resolves_to_the_witness(self):
        got = subprocess.run(["bash", str(self.h.root / "scripts/sutando-config.sh"), "workspace"],
                             env={"PATH": self.h.env["PATH"], "HOME": str(self.h.home)},
                             capture_output=True, text=True).stdout
        self.assertEqual(got, str(self.ws))

    def test_auth_is_the_callers_config_dir_by_reference(self):
        glob = self.h.tm(self.sock, "show-environment", "-g", "CLAUDE_CONFIG_DIR").stdout.strip()
        self.assertEqual(glob, f"CLAUDE_CONFIG_DIR={self.h.ccd}")
        self.assertEqual(sorted(p.name for p in self.h.ccd.iterdir()), [".claude.json"])

    def test_no_owner_surfaces_and_no_startup_ceremony(self):
        argv = (self.h.home / "claude.argv").read_text().split("\n")
        self.assertIn(self.session, argv)
        self.assertNotIn("--remote-control", argv)
        self.assertNotIn("--chrome", argv)
        self.assertNotIn("/startup", argv)

    def test_session_name_is_unique_to_this_root(self):
        self.assertRegex(self.session, r"^witness-t1-[0-9]+-core$")

    def test_launch_records_the_processes_it_owns(self):
        rec = self.h.record("t1")
        self.assertEqual(int(rec.get("server", "0:").split(":")[0]), self.h.server_pid(self.sock))
        for role in ("core", "watcher"):
            self.assertRegex(rec.get(role, ""), r"^[0-9]+:.+", role)

    def test_heartbeat_writer_logs_inside_the_witness(self):
        self.assertTrue((self.ws / "logs/core-heartbeat.log").exists(), "heartbeat log went to the shared default")

    def test_monitor_and_relay_never_started(self):
        self.assertFalse((self.ws / "state/core-supervisor.json").exists())
        self.assertFalse((self.ws / "state/core-supervisor-relay-loop.pid").exists())

    def test_without_a_vault_token_start_says_how_to_set_one_up(self):
        self.assertEqual((self.h.home / "claude.token").read_text(), "")
        self.assertIn("will need /login", self.run_.stderr)
        self.assertIn("claude setup-token", self.run_.stderr)
        self.assertIn("vault set CLAUDE_CODE_OAUTH_TOKEN <token>", self.run_.stderr)

    def test_a_callers_config_dir_is_never_given_the_bypass_seed(self):
        self.assertFalse((self.h.ccd / "settings.json").exists())
        self.assertIn("is not edited", self.run_.stderr)

    def test_second_launch_of_the_same_name_refuses(self):
        run = self.h.run("--witness", "t1")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("already exists", run.stderr)


class WitnessVaultLogin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        (cls.h.home / "fake-vault-token").write_text(FAKE_TOKEN)
        cls.run_ = cls.h.run("--witness", "t1")
        cls.root, cls.sock, cls.session, cls.ws = cls.h.witness("t1")

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_core_claude_gets_the_token_in_its_environment(self):
        self.assertEqual(self.run_.returncode, 0, self.run_.stdout + self.run_.stderr)
        self.assertEqual((self.h.home / "claude.token").read_text(), FAKE_TOKEN)
        self.assertIn("witness login", self.run_.stdout)
        self.assertNotIn("claude setup-token", self.run_.stderr)

    def test_token_is_never_written_to_disk(self):
        files = [p for p in self.root.rglob("*") if p.is_file()] + [self.h.root / "sutando.config.local.json"]
        self.assertIn(self.root / "witness.env", files)
        for p in files:
            self.assertNotIn(FAKE_TOKEN.encode(), p.read_bytes(), str(p))
        self.assertNotIn(FAKE_TOKEN, self.run_.stdout + self.run_.stderr)

    def test_token_is_not_in_tmux_or_any_argv(self):
        for args in (("show-environment", "-g"), ("show-environment", "-t", f"={self.session}"),
                     ("show-environment", "-t", f"={self.session}-watcher"), ("show-options", "-g")):
            self.assertNotIn(FAKE_TOKEN, self.h.tm(self.sock, *args).stdout, args)
        argv = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True).stdout
        self.assertNotIn(FAKE_TOKEN, argv)
        self.assertNotIn(FAKE_TOKEN, (self.h.home / "claude.argv").read_text())

    def test_only_the_core_pane_runs_the_login_wrapper(self):
        def pane_cmd(session):
            return self.h.tm(self.sock, "list-panes", "-t", f"={session}", "-F", "#{pane_start_command}").stdout
        self.assertIn("witness-claude.sh", pane_cmd(self.session))
        self.assertNotIn("witness-claude.sh", pane_cmd(self.session + "-watcher"))


class WitnessBypassMirror(unittest.TestCase):
    """With no CLAUDE_CONFIG_DIR the witness gets a fresh one; the bypass seed mirrors the owner's own."""

    def launch(self, owner_settings):
        h = Harness()
        self.addCleanup(h.close)
        if owner_settings is not None:
            (h.home / ".claude").mkdir()
            (h.home / ".claude/settings.json").write_text(owner_settings)
        run = h.run("--witness", "t1", drop=("CLAUDE_CONFIG_DIR",))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return h, run, h.witness("t1")[3] / ".claude-sutando/settings.json"

    def test_seeded_when_the_owner_set_it(self):
        src = '{"skipDangerousModePermissionPrompt": true}\n'
        h, run, seeded = self.launch(src)
        self.assertIs(json.loads(seeded.read_text()).get("skipDangerousModePermissionPrompt"), True)
        self.assertEqual((h.home / ".claude/settings.json").read_text(), src, "the owner's settings changed")
        self.assertIn("mirrored", run.stdout)

    def test_not_seeded_when_the_owner_did_not(self):
        for src in (None, '{"skipDangerousModePermissionPrompt": false}\n'):
            with self.subTest(src=src):
                h, run, seeded = self.launch(src)
                self.assertFalse(seeded.exists() and "skipDangerousModePermissionPrompt" in seeded.read_text())
                self.assertIn("not seeded", run.stderr)


def wait_gone(pids, seconds=10.0) -> list:
    """The pids still alive after waiting."""
    deadline = time.time() + seconds
    while True:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive or time.time() > deadline:
            return alive
        time.sleep(0.2)


class WitnessFailedLaunch(unittest.TestCase):
    """A launch that fails after creating processes stops what it recorded, deletes nothing,
    and leaves a record --witness-stop can clean up."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def watcher_fails(self):
        return self.h.shim_dir(tmux=f'case "$*" in *new-session*-watcher*) exit 42;; esac\nexec {TMUX} "$@"\n')

    def assert_failed_then_stoppable(self, rc, output):
        self.assertNotEqual(rc, 0, output)
        self.assertIn("launch FAILED", output)
        root, sock, _, _ = self.h.witness("t1")
        rec = self.h.record("t1")
        self.assertEqual(rec.get("intent"), "launch")
        self.assertNotIn("complete", rec)
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core")]
        self.assertEqual(wait_gone(pids), [], "a recorded process outlived the failed launch")
        self.assertTrue(root.exists() and (self.h.root / "sutando.config.local.json").exists(),
                        "a failed launch deleted files; only stop deletes")
        stop = self.h.run("--witness-stop", "t1")
        self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
        self.assertFalse(root.exists())
        self.assertFalse((self.h.root / "sutando.config.local.json").exists())
        self.h.production_untouched(self)

    def test_detached_launch_with_a_failing_watcher_stops_what_it_recorded(self):
        run = self.h.run("--witness", "t1", extra_env={"PATH": f"{self.watcher_fails()}:{self.h.env['PATH']}"})
        self.assert_failed_then_stoppable(run.returncode, run.stdout + run.stderr)

    def test_tty_launch_with_a_failing_watcher_stops_what_it_recorded(self):
        rc, out = self.h.run_tty("--witness", "t1", extra_env={"PATH": f"{self.watcher_fails()}:{self.h.env['PATH']}"})
        self.assert_failed_then_stoppable(rc, out)


class WitnessTeardown(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        run = self.h.run("--witness", "t2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.root, self.sock, self.session, self.ws = self.h.witness("t2")

    def tearDown(self):
        self.h.close()

    def test_stop_removes_only_witness_resources(self):
        run = self.h.run("--witness-stop", "t2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotEqual(self.h.tm(self.sock, "list-sessions").returncode, 0, "witness tmux server survived")
        self.assertFalse(self.root.exists(), "witness root not removed")
        self.assertFalse((self.h.root / "sutando.config.local.json").exists(), "override left behind")
        self.h.production_untouched(self)

    def test_stop_refuses_a_config_it_did_not_write_and_touches_nothing(self):
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core", "watcher")]
        cfg = self.h.root / "sutando.config.local.json"
        cfg.write_text('{"workspace": {"path": "/somewhere/else"}}\n')
        run = self.h.run("--witness-stop", "t2")
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("changed since the witness wrote it", run.stderr)
        self.assertEqual(cfg.read_text(), '{"workspace": {"path": "/somewhere/else"}}\n')
        for pid in pids:
            os.kill(pid, 0)
        self.assertTrue(self.root.exists())

    def test_stop_with_failing_tmux_keeps_state_and_fails(self):
        rec = self.h.record("t2")
        server = self.h.server_pid(self.sock)
        shims = self.h.shim_dir(tmux="exit 1\n")
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("stopped", run.stdout)
        os.kill(server, 0)
        self.assertTrue(self.root.exists() and self.sock.exists(), "witness state removed while its server lives")
        self.assertTrue((self.h.root / "sutando.config.local.json").exists())
        self.assertEqual(self.h.record("t2"), rec)
        again = self.h.run("--witness-stop", "t2")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse(self.root.exists())

    def test_stop_without_tmux_keeps_state_and_fails(self):
        server = self.h.server_pid(self.sock)
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": self.h.path_without_tmux()})
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        os.kill(server, 0)
        self.assertTrue(self.root.exists() and self.sock.exists())
        self.assertTrue((self.h.root / "sutando.config.local.json").exists())

    def test_stop_without_tmux_in_another_locale_keeps_state_and_fails(self):
        if "dim." not in subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True,
                                        text=True, env={**os.environ, "LC_ALL": "fr_FR.UTF-8"}).stdout:
            self.skipTest("no fr_FR locale that changes ps start times on this host")
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core", "watcher")]
        run = self.h.run("--witness-stop", "t2",
                         extra_env={"PATH": self.h.path_without_tmux(), "LC_ALL": "fr_FR.UTF-8"})
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("stopped", run.stdout)
        for pid in pids:
            os.kill(pid, 0)
        self.assertTrue(self.root.exists() and self.sock.exists(), "witness state removed while its server lives")
        self.assertTrue((self.h.root / "sutando.config.local.json").exists())
        self.assertEqual(self.h.record("t2"), rec)
        again = self.h.run("--witness-stop", "t2")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse(self.root.exists())

    def test_stop_without_tmux_in_another_timezone_keeps_state_and_fails(self):
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core", "watcher")]
        run = self.h.run("--witness-stop", "t2",
                         extra_env={"PATH": self.h.path_without_tmux(), "TZ": "Asia/Tokyo"})
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("stopped", run.stdout)
        for pid in pids:
            os.kill(pid, 0)
        self.assertTrue(self.root.exists() and self.sock.exists(), "witness state removed while its server lives")
        self.assertEqual(self.h.record("t2"), rec)

    def assertStopRefusedKeepingState(self, run, rec, pids):
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("stopped", run.stdout)
        for pid in pids:
            os.kill(pid, 0)
        self.assertTrue(self.root.exists(), "witness root removed while recorded processes live")
        self.assertTrue((self.h.root / "sutando.config.local.json").exists())
        self.assertEqual(self.h.record("t2"), rec)

    def test_stop_with_failing_ps_and_tmux_keeps_state_and_fails(self):
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core", "watcher")]
        shims = self.h.shim_dir(tmux="exit 1\n", ps="exit 42\n")
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertStopRefusedKeepingState(run, rec, pids)
        again = self.h.run("--witness-stop", "t2")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse(self.root.exists())

    def test_stop_with_failing_ps_after_the_server_died_keeps_a_live_recorded_process(self):
        self.h.tm(self.sock, "kill-server")
        # Detached, so its exit is reaped elsewhere and never lingers as this test's zombie.
        pid = int(subprocess.run(["/bin/bash", "-c", "nohup sleep 120 >/dev/null 2>&1 & echo $!"],
                                 capture_output=True, text=True).stdout)
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))
        script = 'REPO="$1"; . "$REPO/src/agent/claude/cli/witness-mode.sh"; witness_proc_id "$2"'
        ident = subprocess.run(["/bin/bash", "-c", script, "id", str(REPO), str(pid)],
                               capture_output=True, text=True).stdout.rstrip("\n")
        record = Path(os.path.realpath(self.h.tmp)) / "sutando-witness" / "t2" / "witness.env"
        lines = [l for l in record.read_text().splitlines() if not l.startswith("core=")]
        record.write_text("\n".join(lines + [f"core={ident}"]) + "\n")
        rec = self.h.record("t2")
        shims = self.h.shim_dir(ps="exit 42\n")
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertStopRefusedKeepingState(run, rec, [pid])
        again = self.h.run("--witness-stop", "t2")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        with self.assertRaises(ProcessLookupError, msg="the recorded process was not stopped on retry"):
            os.kill(pid, 0)

    def test_stop_refuses_a_record_missing_an_owned_process(self):
        record = Path(os.path.realpath(self.h.tmp)) / "sutando-witness" / "t2" / "witness.env"
        record.write_text("".join(l + "\n" for l in record.read_text().splitlines()
                                  if not l.startswith("watcher=")))
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core")]
        run = self.h.run("--witness-stop", "t2")
        self.assertStopRefusedKeepingState(run, rec, pids)
        self.assertIn("no watcher", run.stderr)
        self.assertTrue(self.sock.exists())

    def test_stop_never_kills_an_unrecorded_server_on_the_socket(self):
        server = self.h.server_pid(self.sock)
        os.kill(server, 15)
        self.assertEqual(wait_gone([server]), [])
        self.assertEqual(self.h.tm(self.sock, "new-session", "-d", "-s", "innocent", "sleep 300").returncode, 0)
        innocent = self.h.server_pid(self.sock)
        run = self.h.run("--witness-stop", "t2")
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("not the recorded one", run.stderr)
        os.kill(innocent, 0)
        self.assertEqual(self.h.tm(self.sock, "has-session", "-t", "=innocent").returncode, 0)
        self.assertTrue(self.root.exists())

    def test_stop_with_a_failing_process_table_read_kills_nothing(self):
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core", "watcher")]
        writers = self.h.heartbeat_writers()
        self.assertTrue(writers, "the witness heartbeat writer is not running")
        # Identity reads work, so stop gets past its lock; only the process table fails.
        shims = self.h.shim_dir(ps='case "$*" in *-A*) exit 42;; esac\nexec /bin/ps "$@"\n')
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertStopRefusedKeepingState(run, rec, pids)
        self.assertEqual(sorted(self.h.heartbeat_writers()), sorted(writers), "the heartbeat writer was stopped")

    def test_a_refused_stop_leaves_the_heartbeat_writer_running(self):
        writers = self.h.heartbeat_writers()
        self.assertTrue(writers, "the witness heartbeat writer is not running")
        self.h.set_record("t2", watcher=None)
        run = self.h.run("--witness-stop", "t2")
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("no watcher", run.stderr)
        self.assertEqual(sorted(self.h.heartbeat_writers()), sorted(writers), "a refused stop stopped the heartbeat")

    def test_a_failing_seq_cannot_end_the_survivor_wait(self):
        self.h.tm(self.sock, "kill-server")
        pid = self.h.detached("bash -c 'trap \"\" TERM; sleep 120'")
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        time.sleep(0.3)
        self.h.set_record("t2", core=self.h.proc_id(pid))
        rec = self.h.record("t2")
        shims = self.h.shim_dir(seq="exit 42\n")
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertStopRefusedKeepingState(run, rec, [pid])
        self.assertIn("not proven gone", run.stderr)

    def test_a_config_rewritten_during_its_removal_is_kept(self):
        cfg = self.h.root / "sutando.config.local.json"
        shims = self.h.shim_dir(shasum=(
            'n=$(cat "$0.n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$0.n"\n'
            'h=$(/usr/bin/shasum "$@") || exit $?\n'
            f'[ "$n" = 2 ] && printf \'{{"x": 1}}\\n\' > "{cfg}"\n'
            'printf "%s\\n" "$h"\n'))
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertTrue(cfg.exists(), "stop deleted a config written while it was removing its own")
        self.assertEqual(cfg.read_text(), '{"x": 1}\n')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_a_changed_config_that_still_names_the_witness_keeps_the_root(self):
        cfg = self.h.root / "sutando.config.local.json"
        data = json.loads(cfg.read_text())
        data["unrelated"] = True
        cfg.write_text(json.dumps(data) + "\n")
        run = self.h.run("--witness-stop", "t2")
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(self.root.exists(), "the root a kept config points at was deleted")
        self.assertTrue(self.ws.exists())
        self.assertEqual(json.loads(cfg.read_text()), data)

    def test_stop_is_refused_while_another_start_or_stop_holds_the_name(self):
        holder = self.h.detached("sleep 120")
        self.addCleanup(lambda: subprocess.run(["kill", str(holder)], capture_output=True))
        lock = self.root.parent / ".t2.lock"
        lock.mkdir()
        (lock / "owner").write_text(self.h.proc_id(holder) + "\n")
        rec = self.h.record("t2")
        pids = [int(rec[r].split(":", 1)[0]) for r in ("server", "core", "watcher")]
        run = self.h.run("--witness-stop", "t2")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("busy", run.stderr)
        for pid in pids:
            os.kill(pid, 0)
        self.assertTrue(self.root.exists())
        shutil.rmtree(lock)

    def test_stop_with_failing_hash_keeps_the_config(self):
        shims = self.h.shim_dir(shasum="exit 42\n")
        run = self.h.run("--witness-stop", "t2", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue((self.h.root / "sutando.config.local.json").exists(), "unprovable config deleted")
        self.assertTrue(self.root.exists(), "the record was dropped before cleanup finished")
        again = self.h.run("--witness-stop", "t2")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse((self.h.root / "sutando.config.local.json").exists())

    def test_same_name_in_another_root_survives_this_stop(self):
        other = Harness()
        try:
            run = other.run("--witness", "t2")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            b_root, b_sock, b_session, _ = other.witness("t2")
            b_claude = int((other.home / "claude.pid").read_text())
            # This stop's pgrep lists both claudes, as a real host's would.
            pids = f"{self.h.home}/claude.pid {other.home}/claude.pid"
            (self.h.td / "bin/pgrep").write_text(
                '#!/bin/bash\n[ "$*" = "-ax claude" ] || exit 1\n'
                f'for f in {pids}; do [ -s "$f" ] && echo "$(cat "$f") claude"; done\n')
            stop = self.h.run("--witness-stop", "t2")
            self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
            os.kill(b_claude, 0)
            self.assertEqual(other.tm(b_sock, "has-session", "-t", f"={b_session}").returncode, 0)
            self.assertTrue(b_root.exists())
            self.assertNotEqual(b_session, self.session)
        finally:
            other.close()

    def test_stop_from_another_checkout_refuses(self):
        other = Harness()
        try:
            run = other.run("--witness-stop", "t2", extra_env={"TMPDIR": str(self.h.tmp)})
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertEqual(self.h.tm(self.sock, "has-session", "-t", f"={self.session}").returncode, 0)
        finally:
            other.close()


class WitnessRefusals(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def assertRefusedCleanly(self, run, needle):
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn(needle, run.stderr)
        self.assertFalse((self.h.root / "sutando.config.local.json").exists())
        self.assertFalse((Path(os.path.realpath(self.h.tmp)) / "sutando-witness").exists())
        self.assertFalse((self.h.home / "claude.pid").exists(), "a claude was launched")
        self.h.production_untouched(self)

    def test_main_checkout_refuses(self):
        h = Harness(linked=False)
        try:
            run = h.run("--witness", "t1")
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertIn("not a linked git worktree", run.stderr)
            self.assertFalse((h.root / "sutando.config.local.json").exists())
        finally:
            h.close()

    def test_existing_local_config_refuses(self):
        cfg = self.h.root / "sutando.config.local.json"
        cfg.write_text("{}\n")
        run = self.h.run("--witness", "t1")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertEqual(cfg.read_text(), "{}\n")

    def test_live_core_in_checkout_workspace_refuses(self):
        alive = self.h.checkout_ws / "state/cores/testhost.alive"
        os.utime(alive, None)
        self.h.before["checkout"] = snapshot(self.h.checkout_ws)
        self.assertRefusedCleanly(self.h.run("--witness", "t1"), "live core")

    def test_an_unreadable_cores_dir_counts_as_a_live_core(self):
        shims = self.h.shim_dir(find='case "$*" in */state/cores*) exit 1;; esac\nexec /usr/bin/find "$@"\n')
        run = self.h.run("--witness", "t1", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertRefusedCleanly(run, "live core")

    def test_witness_flag_after_another_flag_refuses(self):
        self.assertRefusedCleanly(self.h.run("--restart", "--witness", "t1"), "must be the first argument")

    def test_dispatcher_routes_witness_past_the_production_reap(self):
        run = self.h.run("--restart", "--witness", "t1", entry="src/agent/start-cli.sh")
        self.assertRefusedCleanly(run, "must be the first argument")

    def test_dispatcher_refuses_witness_for_codex(self):
        run = self.h.run("--runtime", "codex", "--witness", "t1", entry="src/agent/start-cli.sh")
        self.assertRefusedCleanly(run, "only for the claude runtime")

    def test_equals_form_refuses(self):
        self.assertRefusedCleanly(self.h.run("--witness=t1"), "no '='")

    def test_extra_arguments_refuse(self):
        self.assertRefusedCleanly(self.h.run("--witness", "t1", "--visible"), "no other arguments")

    def test_bad_names_refuse(self):
        for name in ("", "../x", "Sutando", "a" * 21, "-x"):
            with self.subTest(name=name):
                self.assertRefusedCleanly(self.h.run("--witness", name), "name")

    def test_start_without_tmux_refuses(self):
        run = self.h.run("--witness", "t1", extra_env={"PATH": self.h.path_without_tmux()})
        self.assertRefusedCleanly(run, "tmux")

    def test_start_with_broken_tmux_refuses(self):
        shims = self.h.shim_dir(tmux="exit 1\n")
        run = self.h.run("--witness", "t1", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertRefusedCleanly(run, "tmux")

    def test_start_with_failing_hash_refuses(self):
        shims = self.h.shim_dir(shasum="exit 42\n")
        run = self.h.run("--witness", "t1", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertRefusedCleanly(run, "sha")

    def test_a_start_while_the_name_is_held_creates_nothing(self):
        holder = self.h.detached("sleep 120")
        self.addCleanup(lambda: subprocess.run(["kill", str(holder)], capture_output=True))
        lock = Path(os.path.realpath(self.h.tmp)) / "sutando-witness" / ".t1.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text(self.h.proc_id(holder) + "\n")
        run = self.h.run("--witness", "t1")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("busy", run.stderr)
        self.assertFalse((lock.parent / "t1").exists(), "a start created a root while another held the name")
        self.assertFalse((self.h.root / "sutando.config.local.json").exists())
        self.assertEqual((lock / "owner").read_text(), self.h.proc_id(holder) + "\n", "the held lock was taken")

    def test_a_lock_whose_owner_is_dead_is_taken_over(self):
        dead = self.h.detached("true")
        time.sleep(0.5)
        lock = Path(os.path.realpath(self.h.tmp)) / "sutando-witness" / ".t1.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text(f"{dead}:Thu Jan  1 00:00:00 1970\n")
        run = self.h.run("--witness", "t1")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(lock.exists(), "a completed launch kept its lock")

    def test_an_unsearchable_state_dir_counts_as_a_live_core(self):
        alive = self.h.checkout_ws / "state/cores/testhost.alive"
        os.utime(alive, None)
        self.h.before["checkout"] = snapshot(self.h.checkout_ws)
        state = self.h.checkout_ws / "state"
        state.chmod(0)
        try:
            run = self.h.run("--witness", "t1")
        finally:
            state.chmod(0o755)
        self.assertRefusedCleanly(run, "live core")

    def test_launch_that_cannot_read_its_own_identity_refuses_before_creating_anything(self):
        shims = self.h.shim_dir(ps='case "$*" in *lstart*) exit 42;; esac\nexec /bin/ps "$@"\n')
        run = self.h.run("--witness", "t1", extra_env={"PATH": f"{shims}:{self.h.env['PATH']}"})
        self.assertRefusedCleanly(run, "identity")

    def test_the_record_writer_reports_a_failed_write(self):
        d = Path(tempfile.mkdtemp(dir=self.h.td))
        (d / "witness.env").write_text("repo=x\n")
        (d / "witness.env.tmp").mkdir()
        script = ('REPO="$1"; . "$REPO/src/agent/claude/cli/witness-mode.sh"; WITNESS_RECORD="$2"; '
                  'witness_record_set server "1:x"')
        run = subprocess.run(["/bin/bash", "-c", script, "w", str(REPO), str(d / "witness.env")],
                             capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0, "a failed record write reported success")
        self.assertEqual((d / "witness.env").read_text(), "repo=x\n")

    def test_stop_of_unknown_witness_refuses(self):
        self.assertRefusedCleanly(self.h.run("--witness-stop", "t1"), "no witness")


class ProductionTargetGuard(unittest.TestCase):
    """The guard both stages call, fed the values a misderivation could produce."""

    def guard(self, *args, ambient=()):
        script = ('REPO="$1"; shift; . "$REPO/src/agent/claude/cli/witness-mode.sh"; '
                  'witness_assert_not_production "$@"')
        return subprocess.run(["/bin/bash", "-c", script, "guard", str(REPO), *args, *ambient],
                              capture_output=True, text=True)

    def test_default_socket_refused(self):
        self.assertEqual(self.guard("/tmp/sutando-tmux.sock", "witness-x-core").returncode, 2)

    def test_default_socket_refused_in_its_physical_spelling(self):
        # Only macOS aliases /tmp to /private/tmp; elsewhere that spelling is a different path.
        if os.path.realpath("/private/tmp") != os.path.realpath("/tmp"):
            self.skipTest("this host does not alias /private/tmp to /tmp")
        self.assertEqual(self.guard("/private/tmp/sutando-tmux.sock", "witness-x-core").returncode, 2)

    def test_default_session_refused(self):
        self.assertEqual(self.guard("/x/w.sock", "sutando-core").returncode, 2)

    def test_callers_own_socket_or_session_refused(self):
        self.assertEqual(self.guard("/x/w.sock", "witness-x-core", ambient=("/x/w.sock", "")).returncode, 2)
        self.assertEqual(self.guard("/x/w.sock", "witness-x-core", ambient=("", "witness-x-core")).returncode, 2)

    def test_non_witness_session_refused(self):
        self.assertEqual(self.guard("/x/w.sock", "sutando-worker-abc").returncode, 2)

    def test_witness_target_allowed(self):
        run = self.guard("/x/w.sock", "witness-x-core", ambient=("/tmp/sutando-tmux.sock", "sutando-core"))
        self.assertEqual(run.returncode, 0, run.stderr)



class ProcIdentity(unittest.TestCase):
    """A process recorded in one locale is the same process when checked in another."""

    def fn(self, func, pid, lc_all=None, tz=None):
        env = {"PATH": os.environ["PATH"], "LANG": "en_US.UTF-8", **({"LC_ALL": lc_all} if lc_all else {}),
               **({"TZ": tz} if tz else {})}
        script = 'REPO="$1"; . "$REPO/src/agent/claude/cli/witness-mode.sh"; "$2" "$3"'
        return subprocess.run(["/bin/bash", "-c", script, "id", str(REPO), func, pid],
                              capture_output=True, text=True, env=env)

    def state_in(self, rec, lc_all=None, tz=None):
        return self.fn("witness_proc_state", rec, lc_all, tz).stdout.strip()

    def test_a_failed_start_time_read_on_a_live_pid_is_unknown(self):
        probe = subprocess.Popen(["sleep", "60"])
        try:
            live = self.fn("witness_proc_id", str(probe.pid)).stdout.rstrip("\n")
            path = self.shim_path('case "$*" in *lstart*) exit 42;; esac\nexec /bin/ps "$@"\n')
            self.assertEqual(self.state(live, path), "unknown", "a pid whose identity could not be read got a verdict")
        finally:
            probe.kill()
            probe.wait()

    def test_a_live_process_stays_live_across_locales(self):
        probe = subprocess.Popen(["sleep", "60"])
        try:
            recorded = self.fn("witness_proc_id", str(probe.pid)).stdout.rstrip("\n")
            self.assertTrue(recorded.startswith(f"{probe.pid}:"), recorded)
            self.assertEqual(self.state_in(recorded, lc_all="fr_FR.UTF-8"), "alive",
                             "a live recorded process read as dead in another locale")
        finally:
            probe.kill()
            probe.wait()

    def test_a_live_process_stays_live_across_timezones(self):
        probe = subprocess.Popen(["sleep", "60"])
        try:
            recorded = self.fn("witness_proc_id", str(probe.pid)).stdout.rstrip("\n")
            self.assertEqual(self.state_in(recorded, tz="Asia/Tokyo"), "alive",
                             "a live recorded process read as dead in another time zone")
        finally:
            probe.kill()
            probe.wait()

    def state(self, rec, path=None):
        env = {"PATH": path or os.environ["PATH"]}
        script = 'REPO="$1"; . "$REPO/src/agent/claude/cli/witness-mode.sh"; witness_proc_state "$2"'
        return subprocess.run(["/bin/bash", "-c", script, "st", str(REPO), rec],
                              capture_output=True, text=True, env=env).stdout.strip()

    def shim_path(self, ps_body):
        d = Path(tempfile.mkdtemp(prefix="psshim"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "ps").write_text("#!/bin/bash\n" + ps_body)
        (d / "ps").chmod(0o755)
        return f"{d}:{os.environ['PATH']}"

    def test_state_is_alive_dead_or_unknown_and_only_evidence_is_dead(self):
        probe = subprocess.Popen(["sleep", "60"])
        live = self.fn("witness_proc_id", str(probe.pid)).stdout.rstrip("\n")
        try:
            self.assertEqual(self.state(live), "alive")
            self.assertEqual(self.state(f"{probe.pid}:Thu Jan  1 00:00:00 1970"), "dead",
                             "a pid now held by another process is not the recorded one")
            self.assertEqual(self.state(""), "unknown", "an empty record proves nothing")
            self.assertEqual(self.state(live, self.shim_path("exit 42\n")), "unknown",
                             "a failing ps read as a verdict")
            self.assertEqual(self.state(live, self.shim_path("exit 0\n")), "unknown",
                             "an empty process table read as a verdict")
        finally:
            probe.kill()
            probe.wait()
        self.assertEqual(self.state(live), "dead")
        self.assertEqual(self.state(live, self.shim_path("exit 42\n")), "unknown",
                         "a failing ps proves no death")

if __name__ == "__main__":
    unittest.main()
