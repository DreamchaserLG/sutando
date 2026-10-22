#!/usr/bin/env python3
"""Copy the printed guidance into real Bash/Zsh PTYs and observe tmux clients.

Runtime cases require environment.json and runtime-endpoint.c beside the
supplied SUTANDO_RUNTIME_SOCKET. The endpoint is synthetic: these tests prove
terminal addressing and lifecycle, never authentication or conversation.
Only host timer installation is suppressed; the product launcher is unchanged.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
import create_worker as cw
import spawn_worker as sw
import worker_identity as wi


def supplied_environment():
    socket = os.environ.get("SUTANDO_RUNTIME_SOCKET")
    root = Path(socket).parent if socket else None
    env = dict(os.environ)
    if root and (root / "environment.json").is_file():
        env.update(json.loads((root / "environment.json").read_text()))
    return root, env


def shell_argv(shell, command):
    flags = ["--noprofile", "--norc"] if shell == "bash" else ["-f"]
    return [shell, *flags, "-c", command]


class TestShellArguments(unittest.TestCase):
    def test_socket_and_exact_target_are_literal_arguments_in_both_shells(self):
        _, env = supplied_environment()
        pairs = [
            ("/tmp/plain.sock", "sutando-worker-" + "a" * 32),
            ("/tmp/socket with spaces", "worker"),
            ("=relative-socket", "worker"),
            ("/tmp/quotes'\" $HOME $(printf BAD) `printf BAD`;|&<>*?[!]~\\.sock",
             "worker'\" $HOME $(printf BAD) `printf BAD`;|&<>*?[!]~\\"),
        ]
        for shell in ("bash", "zsh"):
            self.assertIsNotNone(shutil.which(shell, path=env.get("PATH")), shell)
            for socket, session in pairs:
                with self.subTest(shell=shell, socket=socket):
                    command = sw.attach_command(socket, session)
                    script = "tmux() { printf '%s\\0' \"$@\"; }; " + command
                    r = subprocess.run(shell_argv(shell, script), env=env,
                                       capture_output=True, timeout=5)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(r.stdout.split(b"\0")[:-1], [
                        b"-S", os.fsencode(socket), b"attach", b"-t",
                        os.fsencode("=" + session)])


class TestTerminalGuidance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime_root, cls.base_env = supplied_environment()
        if (not cls.runtime_root or
                not (cls.runtime_root / "environment.json").is_file() or
                not (cls.runtime_root / "runtime-endpoint.c").is_file()):
            raise unittest.SkipTest("needs the supplied disposable environment and synthetic endpoint")
        for tool in ("tmux", "bash", "zsh", "cc"):
            if not shutil.which(tool, path=cls.base_env.get("PATH")):
                raise RuntimeError(f"required terminal test capability missing: {tool}")
        cls.build = tempfile.TemporaryDirectory(prefix="attach-bin-", dir=cls.runtime_root)
        cls.addClassCleanup(cls.build.cleanup)
        cls.endpoint = Path(cls.build.name) / "claude"
        subprocess.run(["cc", str(cls.runtime_root / "runtime-endpoint.c"),
                        "-o", str(cls.endpoint)], env=cls.base_env, check=True,
                       capture_output=True, timeout=30)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="attach-", dir=self.runtime_root)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.ws = self.root / "workspace"
        self.folder = self.root / "project"
        self.folder.mkdir()
        self.logs = (self.runtime_root / "diagnostics" / "worker-attach-guidance"
                     / self._testMethodName / self.root.name)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.env = dict(self.base_env)
        old_ws = self.base_env["SUTANDO_WORKSPACE_DIR"]
        for key, value in self.env.items():
            if value == old_ws or value.startswith(old_ws + "/"):
                self.env[key] = str(self.ws) + value[len(old_ws):]
        self.env.update({
            "PATH": str(self.endpoint.parent) + os.pathsep + self.base_env["PATH"],
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.root / "registry"),
            "SUTANDO_RUN_DIR": str(self.root / "run"),
            "SUTANDO_INSTANCE_ID": "", "SUTANDO_TELEMETRY": "0",
            "SUTANDO_OBS_ENDPOINT": "", "SUTANDO_OBS_METRICS_ENDPOINT": "",
            "ANTHROPIC_BASE_URL": "http://factory-runtime.invalid",
            "ANTHROPIC_API_KEY": "synthetic-unused", "TERM": "xterm-256color",
        })
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.frozen = []
        self.launches = []
        self.retained = []

    def log(self, name, text):
        (self.logs / name).write_text(text[-65536:])

    def event(self, **fields):
        with (self.logs / "observations.jsonl").open("a") as out:
            out.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")

    def run_command(self, argv, *, env=None, check=True):
        child_env = {**self.env, **(env or {})}
        r = subprocess.run(argv, env=child_env, cwd=REPO, capture_output=True,
                           text=True, timeout=30)
        if check:
            self.assertEqual(r.returncode, 0, (argv, r.stdout, r.stderr))
        return r

    def tmux(self, *args, check=True):
        return self.run_command(["tmux", "-S", str(self.socket), *args], check=check)

    def wait_for(self, predicate, description, timeout=8):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {description}; diagnostics: {self.logs}")

    def start_server(self, filename):
        self.socket = self.root / filename
        self.env["SUTANDO_TMUX_SOCKET"] = str(self.socket)
        output = (self.logs / "tmux-server.log").open("w")
        self.addCleanup(output.close)
        self.server = subprocess.Popen(["tmux", "-D", "-S", str(self.socket),
                                        "-f", os.devnull], env=self.env, cwd=REPO,
                                       stdout=output, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop_server)
        self.wait_for(lambda: self.socket.exists(), "foreground tmux socket")
        self.assertIsNone(self.server.poll())
        # An empty foreground server reports "no current target", not absence.
        self.tmux("new-session", "-d", "-s", "attach-control", str(self.endpoint))
        self.event(server_pid=self.server.pid, socket=str(self.socket))

    def stop_server(self):
        for pid in self.frozen:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)
        pids = self.tmux("list-panes", "-a", "-F", "#{pane_pid}", check=False).stdout.splitlines()
        self.tmux("kill-server", check=False)
        try:
            self.server.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=3)
            self.fail("foreground tmux did not stop")
        for pid in pids:
            self.wait_for(lambda: self.run_command(
                ["ps", "-p", pid, "-o", "pid="], check=False).stdout.strip() == "",
                f"pane process {pid} to exit")
        self.event(cleanup="server reaped; pane processes gone", server_exit=self.server.returncode)

    def create(self, *, fail_after_launch=False, json_output=False):
        original_spawn = sw.spawn

        def runner(argv, **kwargs):
            r = self.run_command(argv, check=False, **kwargs)
            if argv == ["bash", str(REPO / sw.LAUNCHER)]:
                index = len(self.launches)
                self.log(f"launch-{index}.stdout", r.stdout)
                self.log(f"launch-{index}.stderr", r.stderr)
                self.launches.append(r.returncode)
                self.event(launcher_argv=argv, launcher_exit=r.returncode,
                           injected_failure=fail_after_launch)
                self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
                if fail_after_launch:
                    return subprocess.CompletedProcess(argv, 23, r.stdout,
                        "synthetic unsuccessful result after a real successful launch")
            return r

        def spawn(*args, **kwargs):
            try:
                return original_spawn(*args, runner=runner, **kwargs)
            except sw.SpawnRetained as e:
                self.retained.append(e)
                raise

        out, err = io.StringIO(), io.StringIO()
        args = ["--workspace", str(self.ws), "--repo", str(REPO),
                "--folder", str(self.folder), "--runtime", "claude",
                "--socket", str(self.socket), "--label", "attach regression"]
        if json_output:
            args.append("--json")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sw, "ensure_remedy_timer", return_value={}) as timer, \
                mock.patch.object(sw, "spawn", side_effect=spawn), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cw.main(args)
        self.log("create.stdout", out.getvalue())
        self.log("create.stderr", err.getvalue())
        self.event(create_argv=args, create_exit=rc)
        self.assertEqual(rc, 2 if fail_after_launch else 0, err.getvalue())
        self.assertEqual(timer.call_count, 0 if fail_after_launch else 1, err.getvalue())
        if fail_after_launch:
            self.assertEqual(out.getvalue(), "")
            command = err.getvalue().split("Attach with `", 1)[1].rsplit("` to finish", 1)[0]
            wid = self.retained[-1].worker_id
        elif json_output:
            return json.loads(out.getvalue())
        else:
            self.assertEqual(err.getvalue(), "")
            wid = out.getvalue().splitlines()[0].split()[1]
            command = next(line.strip().removeprefix("session   ")
                           for line in out.getvalue().splitlines() if line.startswith("  session   "))
        self.assertEqual(shlex.split(command), ["tmux", "-S", str(self.socket),
                         "attach", "-t", "=" + wi.tmux_session_name(wid)])
        return wid, command

    def observe_worker(self, wid):
        name = wi.tmux_session_name(wid)
        def ready():
            r = self.tmux("capture-pane", "-p", "-J", "-t", "=" + name + ":", check=False)
            self.log("worker-pane.log", r.stdout + r.stderr)
            return "synthetic endpoint ready" in r.stdout
        self.wait_for(ready, "endpoint readiness")
        pid = int(self.tmux("display-message", "-p", "-t", "=" + name + ":", "#{pane_pid}").stdout)
        args = self.run_command(["ps", "-p", str(pid), "-o", "args="]).stdout.strip()
        self.assertIn("--name " + name, args)
        session_id = wi.sessions(self.ws, wid)[0]["session_id"]
        self.assertIn("--session-id " + session_id, args)
        self.assertIn("/startup --worker", args)
        watcher = name + "-watcher"
        self.tmux("has-session", "-t", "=" + watcher)
        self.event(worker_id=wid, endpoint_pid=pid, endpoint_argv=args, watcher=watcher)
        return name, pid, watcher

    def snapshot(self):
        paths = [self.ws / "state/workers", self.ws / "deliveries", self.ws / "state/roster.json"]
        return {str(p.relative_to(self.ws)): p.read_bytes() if p.is_file() else None
                for root in paths for p in ([root, *root.rglob("*")] if root.is_dir() else [root])
                if p.exists()}

    def add_inbox_witness(self, wid):
        inbox = self.ws / "deliveries" / wid
        (inbox / "task-attach.accepted").write_bytes(b"")
        (inbox / ".attach-content").write_bytes(b"preserve this inbox content\x00\n")

    def terminal(self, shell, command, expected, label, *, read_delay=0):
        self.assertEqual(self.tmux("list-clients").stdout, "")
        started = time.monotonic()
        # Keep the slave open in the parent so child exit cannot discard unread output.
        fd, slave = pty.openpty()
        try:
            pid = os.fork()
        except BaseException:
            os.close(slave)
            os.close(fd)
            raise
        if pid == 0:
            try:
                os.close(fd)
                os.login_tty(slave)
                os.chdir(self.folder)
                os.execvpe(shell, shell_argv(shell, command), self.env)
            except BaseException as error:
                os.write(2, f"terminal child failed: {error}\n".encode())
                os._exit(127)
        status, seen, output = None, [], bytearray()
        try:
            time.sleep(read_delay)
            while time.monotonic() - started < 5:
                ready, _, _ = select.select([fd], [], [], 0.03)
                if ready:
                    with contextlib.suppress(OSError):
                        output.extend(os.read(fd, 8192))
                rows = self.tmux("list-clients", "-F", "#{client_tty}\t#{session_name}").stdout.splitlines()
                for row in rows:
                    tty, session = row.split("\t")
                    seen.append(session)
                    self.assertEqual(session, expected, f"wrong attached session: {row}")
                    self.tmux("detach-client", "-t", tty)
                done, status_value = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = os.waitstatus_to_exitcode(status_value)
                    while select.select([fd], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(fd, 8192)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                    break
            self.assertIsNotNone(status, "printed command did not finish promptly")
            if expected:
                self.assertEqual(seen, [expected])
                self.assertEqual(status, 0, output.decode(errors="replace"))
            else:
                self.assertEqual(seen, [])
                self.assertNotEqual(status, 0)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIn(b"can't find session", output)
            self.assertEqual(self.tmux("list-clients").stdout, "")
        finally:
            self.log(f"{label}-{shell}.terminal", output.decode(errors="replace"))
            self.event(shell=shell, command=command, expected=expected, observed=seen,
                       exit=status, read_delay=read_delay,
                       elapsed_seconds=round(time.monotonic() - started, 3))
            if status is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(slave)
            os.close(fd)

    def exercise_lifecycle(self, filename, *, read_delay=0):
        self.start_server(filename)
        wid, command = self.create()
        name, pid, watcher = self.observe_worker(wid)
        self.add_inbox_witness(wid)
        before = self.snapshot()
        for shell in ("bash", "zsh"):
            self.terminal(shell, command, name, "created")
        watcher_pid = int(self.tmux("display-message", "-p", "-t", "=" + watcher + ":", "#{pane_pid}").stdout)
        # Hold the real supervisor so its exit poll cannot race the attach assertion.
        os.kill(watcher_pid, signal.SIGSTOP)
        self.frozen.append(watcher_pid)
        os.kill(pid, signal.SIGINT)
        self.wait_for(lambda: self.tmux("has-session", "-t", "=" + name, check=False).returncode != 0,
                      "interrupted exact worker to disappear")
        self.tmux("has-session", "-t", "=" + watcher)
        self.event(interrupted_pid=pid, held_watcher_pid=watcher_pid)
        for shell in ("bash", "zsh"):
            self.terminal(shell, command, None, "watcher-only", read_delay=read_delay)
        self.tmux("new-session", "-d", "-s", name + "-other", str(self.endpoint))
        for shell in ("bash", "zsh"):
            self.terminal(shell, command, None, "multiple-prefixes", read_delay=read_delay)
        self.tmux("rename-session", "-t", "=" + watcher, "attach-watcher")
        for shell in ("bash", "zsh"):
            self.terminal(shell, command, None, "other-prefix-only", read_delay=read_delay)
        self.tmux("new-session", "-d", "-s", name, str(self.endpoint))
        for shell in ("bash", "zsh"):
            self.terminal(shell, command, name, "restored")
        self.assertEqual(self.snapshot(), before)
        self.event(preserved="worker records, roster, inbox names and bytes")

    def test_ordinary_socket_exact_session_lifecycle(self):
        self.exercise_lifecycle("ordinary.sock")

    def test_space_socket_exact_session_lifecycle(self):
        self.exercise_lifecycle("socket with spaces.sock")

    def test_delayed_reader_keeps_missing_session_diagnostic(self):
        self.exercise_lifecycle("delayed socket.sock", read_delay=0.7)

    def test_retained_diagnostic_after_real_successful_launch(self):
        self.start_server("retained socket.sock")
        made = self.create(json_output=True)
        self.assertEqual(made["tmux"], {"socket": str(self.socket),
                         "session_name": wi.tmux_session_name(made["worker_id"])})
        self.assertEqual(made["env"]["SUTANDO_TMUX_SOCKET"], str(self.socket))
        self.assertEqual(made["env"]["SUTANDO_TMUX_SESSION"], made["tmux"]["session_name"])
        self.assertTrue(made["started"])
        original = self.snapshot()
        wid, command = self.create(fail_after_launch=True)
        name, _, _ = self.observe_worker(wid)
        retained = self.retained[-1]
        self.assertEqual(retained.tmux_socket, str(self.socket))
        self.assertEqual(retained.tmux_session, name)
        self.assertEqual(retained.delivery_dir, str(self.ws / "deliveries" / wid))
        self.assertEqual(self.launches, [0, 0])
        self.assertNotIn(wid, cw.existing_workers(self.ws))
        self.assertTrue(wi.current_path(self.ws, wid).is_file())
        self.add_inbox_witness(wid)
        before = self.snapshot()
        self.assertEqual({key: before[key] for key in original}, original)
        for shell in ("bash", "zsh"):
            self.terminal(shell, command, name, "retained")
        self.assertEqual(self.snapshot(), before)
        self.event(retained_coordinates={"socket": retained.tmux_socket, "session": name},
                   preserved="existing roster and workers; retained records and inbox")


if __name__ == "__main__":
    unittest.main(verbosity=2)
