#!/usr/bin/env python3
"""On stand-down the standby clears a prompt it staged and never got submitted,
only when every guard holds: a session watcher is ready for the inbox, the
composer holds exactly that staged prompt, the session is idle, and the task is
handled (a result, archived, or announced by the session watcher's sweep).

tmux is the fake from tests/claude-task-notifier.test.py. The session watcher is
a real process whose argv is a session-role watcher and whose sentinel names it,
so the notifier's readiness check runs unmodified.
Run: python3 tests/claude-task-notifier-standdown.test.py
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "claude_task_notifier_harness", Path(__file__).resolve().parent / "claude-task-notifier.test.py")
_h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_h)
FakeTmuxHarness = _h.FakeTmuxHarness
NOTIFIER = _h.NOTIFIER
REPO = _h.REPO
IDLE_FOOTER = _h.IDLE_FOOTER
BUSY_STATUS = _h.BUSY_STATUS

DISPATCH = REPO / "src/delivery/task_dispatch.py"
IDLE_STATUS = IDLE_FOOTER.split("\n", 1)[1]


class StandDownClearTests(FakeTmuxHarness):

    def setUp(self):
        super().setUp()
        self.staged_dir = self.state_dir / "task-notifier-staged"
        self.alerts = self.root / "osascript.calls"
        stub = self.bin / "osascript"
        stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{self.alerts}"\n')
        stub.chmod(0o755)

    # -- fixtures -------------------------------------------------------------

    def session_watcher(self):
        """A live session-role watcher for this inbox whose readiness sentinel names it."""
        fake = self.root / "sw" / "watch-tasks-stream.sh"
        fake.parent.mkdir(exist_ok=True)
        fake.write_text("while :; do sleep 1; done\n")
        proc = subprocess.Popen(["/bin/bash", str(fake), str(self.tasks_dir), "--role", "session",
                                 "--inbox", str(self.tasks_dir)], start_new_session=True)
        self.addCleanup(self._stop, proc)
        (self.state_dir / "watch-tasks-stream.pid").write_text(f"{proc.pid}\n")
        return proc

    @staticmethod
    def _stop(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)

    def stage(self, name, composer=None, status=IDLE_STATUS, record=True):
        """The pane shows `composer` (default: exactly name's prompt); the standby's record says it staged the prompt."""
        self.write_task(name)
        prompt = self.expected_prompt(name)
        self.pane_file.write_text(f"❯ {prompt if composer is None else composer}\n{status}\n")
        if record:
            subprocess.run([sys.executable, str(DISPATCH), "partial-mark", str(self.staged_dir), name, "4242"],
                           input=prompt, text=True, check=True)
        return prompt

    def clear(self, timeout=15):
        return subprocess.run(["/bin/bash", str(NOTIFIER), "--clear-staged"], env=self._env(),
                              cwd=str(self.root), capture_output=True, text=True, timeout=timeout)

    def composer(self):
        rows = [r for r in self.pane_file.read_text().splitlines() if r.startswith("❯")]
        return rows[-1][1:].strip() if rows else None

    def assert_left(self, res, pane_before, why, alerted=True):
        self.assertNotIn("CLEAR", self.sendkeys_log_text(), "the composer was cleared")
        self.assertEqual(self.pane_file.read_text(), pane_before, "the composer changed")
        self.assertIn("leaving", res.stderr)
        self.assertIn(why, res.stderr)
        log = (self.logs_dir / "claude-task-notifier.log").read_text()
        self.assertIn(why, log, "the reason was not logged")
        time.sleep(0.5)  # the alert is backgrounded
        got = self.alerts.read_text() if self.alerts.exists() else ""
        if alerted:
            self.assertIn("blocking task delivery", got, "the owner was not told through the composer-block alert")
        else:
            self.assertEqual(got, "")

    # -- the happy path -------------------------------------------------------

    def test_a_prompt_the_standby_left_staged_is_cleared_once_its_task_is_answered(self):
        # Delivered for real: every Enter swallowed, so the prompt stays staged.
        self.write_task("task-ok.txt")
        self.swallow_enter_flag.write_text("1")
        first = self.run_event("task-ok.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1",
                                                        "SUTANDO_NOTIFIER_SUBMIT_RETRIES": "2"}, timeout=20)
        self.assertIn("NOT confirmed", first.stderr)
        self.assertEqual(self.composer(), self.expected_prompt("task-ok.txt"))
        self.assertTrue((self.staged_dir / "task-ok.txt").is_file(), "the standby kept no record of what it staged")
        self.session_watcher()
        self.write_result("task-ok.txt")
        res = self.clear()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CLEAR C-a C-k", self.sendkeys_log_text())
        self.assertEqual(self.composer(), "", "the composer is not empty after the clear")
        self.assertIn("composer verified empty", (self.logs_dir / "claude-task-notifier.log").read_text())
        self.assertFalse((self.staged_dir / "task-ok.txt").exists(), "the staged record outlived the clear")

    def test_a_task_the_session_watcher_announces_counts_as_taken_over(self):
        self.stage("task-sweep.txt")
        self.session_watcher()
        res = self.clear()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("CLEAR C-a C-k", self.sendkeys_log_text())
        self.assertEqual(self.composer(), "")

    def test_a_submitted_prompt_leaves_no_staged_record(self):
        self.write_task("task-sent.txt")
        res = self.run_event("task-sent.txt", env_extra={"SUTANDO_NOTIFIER_COMPLETION_TIMEOUT": "1"})
        self.assertIn("ENTER", self.sendkeys_log_text(), res.stderr)
        self.assertFalse((self.staged_dir / "task-sent.txt").exists())

    # -- each guard failing: nothing is cleared -------------------------------

    def test_owner_text_mixed_in_is_left(self):
        prompt = self.stage("task-mix.txt", composer=None)
        self.pane_file.write_text(f"❯ {prompt} and also check my calendar\n{IDLE_STATUS}\n")
        self.session_watcher()
        self.write_result("task-mix.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "with other text mixed in")

    def test_a_partial_prompt_is_left(self):
        prompt = self.expected_prompt("task-part.txt")
        self.stage("task-part.txt", composer=prompt[:120])
        self.session_watcher()
        self.write_result("task-part.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "only part of task-part.txt's prompt")

    def test_a_session_mid_turn_is_left(self):
        self.stage("task-busy.txt", status=BUSY_STATUS)
        self.session_watcher()
        self.write_result("task-busy.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "the session is not idle")

    def test_a_task_not_yet_handled_is_left(self):
        # No result, still in tasks/, and a handler claim holds it: no sweep announces it.
        self.stage("task-held.txt")
        claims = self.state_dir / "task-event-handler-claims"
        claims.mkdir(parents=True)
        (claims / "task-held.txt").write_text("claim\n")
        self.session_watcher()
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "has not taken it over")

    def test_no_session_watcher_is_left(self):
        self.stage("task-nosw.txt")
        self.write_result("task-nosw.txt")
        before = self.pane_file.read_text()
        res = self.clear()
        self.assertNotEqual(res.returncode, 0)
        self.assert_left(res, before, "no session-role watcher is ready", alerted=False)

    def test_an_exact_prompt_with_no_staged_record_is_left(self):
        self.stage("task-norec.txt", record=False)
        self.session_watcher()
        self.write_result("task-norec.txt")
        before = self.pane_file.read_text()
        self.assert_left(self.clear(), before, "did not stage", alerted=False)


if __name__ == "__main__":
    unittest.main()
