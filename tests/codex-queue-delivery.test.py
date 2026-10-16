#!/usr/bin/env python3
"""src/delivery/codex_queue.py: the Codex core's delivery-mode policy and the `codex queue` call.

`codex` and the observer are fakes; no real Codex, node or tmux is run.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "delivery"))
import codex_queue  # noqa: E402


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Recorder:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class ModeTests(unittest.TestCase):
    """The mode is read from merged config in a throwaway repo root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sutando.config.json").write_text(json.dumps(
            {"core": {"runtime": "codex"}, "workspace": {"path": str(self.root / "ws")}}))

    def _mode(self, local=None):
        if local is not None:
            (self.root / "sutando.config.local.json").write_text(json.dumps(local))
        return codex_queue.delivery(self.root)

    def test_default_is_send_keys(self):
        self.assertEqual(self._mode(), {"delivery": "send-keys", "thread": ""})

    def test_opt_in_selects_queue_and_thread(self):
        got = self._mode({"core": {"codex": {"delivery": "queue", "thread": "t-1"}}})
        self.assertEqual(got, {"delivery": "queue", "thread": "t-1"})

    def test_unknown_mode_falls_back_to_send_keys(self):
        self.assertEqual(self._mode({"core": {"codex": {"delivery": "paste"}}})["delivery"], "send-keys")


class SendTests(unittest.TestCase):
    MESSAGE = 'Sutando task ready: task-1.txt. Read "$(touch /x)"; `id` & echo $HOME'

    def test_argv_carries_the_message_as_one_entry_without_a_shell(self):
        run = Recorder(Done(0, "Queued message m for thread t."))
        self.assertEqual(codex_queue.send("t", self.MESSAGE, run=run)[0], 0)
        argv, kwargs = run.calls[0]
        self.assertEqual(argv, ["codex", "queue", "--thread", "t", "--message", self.MESSAGE])
        self.assertNotIn("shell", kwargs)

    def test_nonzero_exit_is_a_failure_with_its_reason(self):
        code, reason = codex_queue.send("t", "m", run=Recorder(Done(1, "", "no rollout found for thread id t")))
        self.assertEqual(code, codex_queue.QUEUE_FAILED)
        self.assertIn("no rollout found", reason)

    def test_missing_codex_or_timeout_is_a_failure(self):
        for exc in (FileNotFoundError("codex"), subprocess.TimeoutExpired("codex", 30)):
            self.assertEqual(codex_queue.send("t", "m", run=Recorder(exc))[0], codex_queue.QUEUE_FAILED)

    def test_fake_codex_receives_the_exact_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "argv"
            fake = Path(d) / "codex"
            fake.write_text(f'#!/bin/bash\nfor a in "$@"; do printf "%s\\0" "$a"; done > "{log}"\n')
            fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_")}
            env["PATH"] = f"{d}:/usr/bin:/bin"
            r = subprocess.run([sys.executable, str(REPO / "src/delivery/codex_queue.py"), "send",
                                "--thread", "t", "--message", self.MESSAGE], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(log.read_text().split("\0")[:-1], ["queue", "--thread", "t", "--message", self.MESSAGE])
            self.assertFalse(Path("/x").exists())


class ThreadTests(unittest.TestCase):
    def _resolve(self, run, configured=""):
        orig_delivery, orig_which = codex_queue.delivery, codex_queue.shutil.which
        codex_queue.delivery = lambda repo_root=None: {"delivery": "queue", "thread": configured}
        codex_queue.shutil.which = lambda name: "/fake/node"
        try:
            return codex_queue.resolve_thread("/engine", "/sock", "sutando-core", run=run)
        finally:
            codex_queue.delivery, codex_queue.shutil.which = orig_delivery, orig_which

    def test_configured_thread_wins_without_discovery(self):
        run = Recorder(Done(0, "discovered"))
        self.assertEqual(self._resolve(run, configured="named"), (0, "named"))
        self.assertEqual(run.calls, [])

    def test_discovery_asks_the_observer(self):
        run = Recorder(Done(0, "0199-abc\n"))
        self.assertEqual(self._resolve(run), (0, "0199-abc"))
        argv = run.calls[0][0]
        self.assertEqual(argv[0], "/fake/node")
        self.assertTrue(argv[1].endswith("src/agent/codex/cli/codex-observer.mjs"))
        self.assertEqual(argv[2:], ["--core-thread", "--engine", "/engine", "--tmux-socket", "/sock",
                                    "--session", "sutando-core"])

    def test_no_persisted_turn_and_no_thread_keep_their_codes(self):
        self.assertEqual(self._resolve(Recorder(Done(4, "", "thread has no persisted turn")))[0], codex_queue.NO_TURN)
        self.assertEqual(self._resolve(Recorder(Done(3, "", "no core pane")))[0], codex_queue.NO_THREAD)
        self.assertEqual(self._resolve(Recorder(Done(1, "", "boom")))[0], 1)


if __name__ == "__main__":
    unittest.main()
