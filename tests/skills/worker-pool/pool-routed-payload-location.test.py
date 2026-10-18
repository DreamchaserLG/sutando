#!/usr/bin/env python3
"""A routed task's payload lives beside its sentinel, not in tasks/ (#4836).

The core's readers list tasks/*.txt and treat every entry as the core's; a
payload left there after routing is adopted, answered, and the outbox item is
spent before the worker's answer. So: body first, then sentinel, then the tasks/
copy goes; the resolver hands the worker the body; a finished body is archived
where history readers look; a withdrawn hold puts the body back in tasks/.
"""
import contextlib
import importlib
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))
pd, rt, pr, rie, prh = (importlib.import_module(m) for m in
                        ("pool_delivery", "pool_router", "pool_roster", "resolve_inbox_entry",
                         "pool_route_handler"))

A = "a" * 32
B = "b" * 32
ROOM = "!bound:example.test"
TEXT = "id: task-1\nsource: ag2space\nchannel_id: !bound:example.test\ntask: hello\n"


def roster(bindings):
    return {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}}, "bindings": bindings}


class T(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir()
        (self.ws / "tasks" / "task-1.txt").write_text(TEXT)
        self.task = {"id": "task-1", "source": "ag2space", "channel_id": ROOM}

    def tearDown(self):
        self.td.cleanup()

    def test_routed_payload_leaves_tasks_and_sits_beside_the_sentinel(self):
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        body = pd.body_path(self.ws, A, "task-1")
        self.assertTrue(body.is_file(), "body beside the sentinel")
        self.assertEqual(body.read_text(), TEXT)
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel")
        self.assertEqual(sorted(p.name for p in (self.ws / "tasks").glob("task-*.txt")), [],
                         "no reader of tasks/*.txt sees a routed task")

    def test_core_target_keeps_the_payload_in_tasks(self):
        out = rt.route(self.ws, self.task, roster({}))
        self.assertEqual(out["targets"], [pr.CORE])
        self.assertTrue((self.ws / "tasks" / "task-1.txt").is_file())
        self.assertFalse(list((self.ws / "deliveries").glob("*/task-1.body")))

    def test_fan_out_gives_every_recipient_a_body_before_the_copy_goes(self):
        out = rt.route(self.ws, self.task, roster({ROOM: [A, B]}))
        self.assertEqual(sorted(out["delivered"]), sorted([A, B]))
        for w in (A, B):
            self.assertEqual(pd.body_path(self.ws, w, "task-1").read_text(), TEXT)
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def test_replay_after_a_crash_between_body_and_sentinel_delivers_once(self):
        # A pass died after staging the body: no sentinel, no tasks/ copy either
        # (a later pass may have retired it). The replay must still deliver.
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        pd.body_path(self.ws, A, "task-1").write_text(TEXT)
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        self.assertEqual(out["skipped"], [])
        out2 = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out2["already"], [A])
        self.assertEqual(len(list(folder.glob("task-1.*"))), 2, "one body, one sentinel")

    def test_no_payload_anywhere_is_still_no_payload(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["skipped"], [A])
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_resolver_hands_the_worker_the_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        entry = str(self.ws / "deliveries" / A / "task-1.txt")
        got = rie.resolve(entry)
        self.assertEqual(got, Path(os.path.abspath(pd.body_path(self.ws, A, "task-1"))))
        self.assertEqual(got.read_text(), TEXT)
        p = subprocess.run([sys.executable, str(SCRIPTS / "resolve_inbox_entry.py"), entry],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.splitlines(),
                         [os.path.abspath(pd.body_path(self.ws, A, "task-1")), "task-1.txt"],
                         "the payload is the body; the task's name stays task-1.txt")

    def test_resolver_still_finds_a_pre_move_payload_in_tasks(self):
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        (folder / "task-1.txt").touch()
        got = rie.resolve(str(folder / "task-1.txt"))
        self.assertEqual(got, Path(os.path.abspath(self.ws / "tasks" / "task-1.txt")))

    def test_read_payload_and_residue_see_the_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(pd.read_payload(self.ws, "task-1"), TEXT)
        self.assertNotEqual(pd.residue(self.ws, A, "task-1"), "stale-sentinel")

    def test_finished_body_is_archived_by_prune_and_the_sentinel_retired(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        (self.ws / "results" / "task-1.txt").write_text("done\n")
        pd.mark_done(self.ws, A, "task-1", published=True)
        acts = pd.prune_spent(self.ws, A)
        self.assertIn("task-1", acts["retired"])
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertEqual(pd.archived_payload(self.ws, "task-1").read_text(), TEXT)
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_withdrawn_hold_returns_the_body_to_the_core(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        pd.clear_pending(self.ws, A, "task-1")
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())
        self.assertEqual((self.ws / "tasks" / "task-1.txt").read_text(), TEXT)
        self.assertFalse((self.ws / "deliveries" / A / "task-1.txt").exists())

    def test_body_is_never_a_sentinel(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(rt.holders(self.ws, "task-1"), [A])
        names = pd.sentinel_names("task-1")
        self.assertNotIn("task-1" + pd.BODY_SUFFIX, names)
        self.assertIsNone(pd.parse_sentinel("task-1" + pd.BODY_SUFFIX))

    # A recipient folder that is a symlink must never lead a lookup outside deliveries/.
    def _alias(self):
        outside = Path(tempfile.mkdtemp(dir=self.td.name, prefix="outside-"))
        (outside / "task-1.body").write_text("OUTSIDE-SECRET\n")
        (outside / "task-1.txt").touch()
        os.symlink(outside, self.ws / "deliveries" / A)
        (self.ws / "tasks" / "task-1.txt").unlink()
        return outside

    def test_an_aliased_recipient_folder_is_refused_not_read(self):
        self._alias()
        with self.assertRaises(pd.RecipientAliasError):
            pd.payload_location(self.ws, "task-1")
        with self.assertRaises(pd.RecipientAliasError):
            pd.payload_location(self.ws, "task-1", A)
        with self.assertRaises(pd.RecipientAliasError):
            pd.read_payload(self.ws, "task-1")
        with self.assertRaises(OSError):
            prh.read_task(str(self.ws / "tasks" / "task-1.txt"), self.ws)

    def test_the_resolver_refuses_a_sentinel_seen_through_an_alias(self):
        self._alias()
        with self.assertRaises(pd.NotDelivered):
            rie.resolve(str(self.ws / "deliveries" / A / "task-1.txt"))

    def test_no_deliveries_root_means_no_body(self):
        (self.ws / "deliveries").rmdir()
        (self.ws / "tasks" / "task-1.txt").unlink()
        self.assertIsNone(pd.payload_location(self.ws, "task-1"))

    def test_read_task_with_the_payload_gone_everywhere_still_raises(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        with self.assertRaises(FileNotFoundError):
            prh.read_task(str(self.ws / "tasks" / "task-1.txt"), self.ws)

    def test_read_task_follows_a_moved_payload_to_its_body(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        got = prh.read_task(str(self.ws / "tasks" / "task-1.txt"), self.ws)
        self.assertEqual((got["id"], got["channel_id"]), ("task-1", ROOM))

    def test_core_target_with_no_payload_is_skipped(self):
        (self.ws / "tasks" / "task-1.txt").unlink()
        out = rt.route(self.ws, self.task, roster({}))
        self.assertEqual(out["skipped"], [pr.CORE])

    # An existing body counts as an earlier pass only when it IS the payload.
    def _plant(self, make):
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        make(folder / "task-1.body")
        return folder

    def _refused_and_untouched(self, folder):
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertFalse((folder / "task-1.txt").exists(), "no sentinel over a foreign body")
        self.assertTrue((self.ws / "tasks" / "task-1.txt").is_file(), "the core keeps its copy")

    def test_a_planted_symlink_body_is_refused_without_a_sentinel(self):
        target = Path(self.td.name) / "elsewhere"
        target.write_text("PLANTED\n")
        self._refused_and_untouched(self._plant(lambda b: os.symlink(target, b)))

    def test_a_planted_body_with_other_bytes_is_refused_without_a_sentinel(self):
        self._refused_and_untouched(self._plant(lambda b: b.write_text("PLANTED\n")))

    def test_an_earlier_pass_copy_with_the_same_bytes_is_accepted(self):
        folder = self._plant(lambda b: b.write_text(TEXT))
        out = rt.route(self.ws, self.task, roster({ROOM: A}))
        self.assertEqual(out["delivered"], [A])
        self.assertTrue((folder / "task-1.txt").is_file())
        self.assertFalse((self.ws / "tasks" / "task-1.txt").exists())

    def _stage(self, existing):
        folder = self.ws / "deliveries" / A
        folder.mkdir()
        if existing is not None:
            existing(folder / "task-1.body")
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, dfd)
        return folder, dfd

    def test_stage_eexist_on_the_same_inode_is_a_prior_pass(self):
        src = self.ws / "tasks" / "task-1.txt"
        folder, dfd = self._stage(lambda b: os.link(src, b))
        rt._stage_body(src, "task-1.body", dfd)
        self.assertTrue(os.path.samefile(src, folder / "task-1.body"))

    def test_stage_eexist_on_other_bytes_fails_loudly(self):
        src = self.ws / "tasks" / "task-1.txt"
        folder, dfd = self._stage(lambda b: b.write_text("OTHER\n"))
        with self.assertRaises(rt.ConflictingDelivery):
            rt._stage_body(src, "task-1.body", dfd)
        self.assertEqual((folder / "task-1.body").read_text(), "OTHER\n")

    def test_stage_eexist_on_a_fifo_fails_loudly(self):
        src = self.ws / "tasks" / "task-1.txt"
        _, dfd = self._stage(lambda b: os.mkfifo(b))
        with self.assertRaises(rt.ConflictingDelivery):
            rt._stage_body(src, "task-1.body", dfd)

    def test_same_body_check_refuses_what_it_cannot_open_or_compare(self):
        src = self.ws / "tasks" / "task-1.txt"
        _, dfd = self._stage(lambda b: b.write_text(TEXT))
        with self.assertRaises(rt.UnreadableEvidence):
            rt._require_same_body(src, "task-missing.body", dfd, "task-1")
        with self.assertRaises(rt.UnreadableEvidence):
            rt._require_same_body(self.ws / "tasks" / "gone.txt", "task-1.body", dfd, "task-1")

    # A sentinel is retired only once its body reached tasks/archive/.
    def _finished(self):
        rt.route(self.ws, self.task, roster({ROOM: A}))
        (self.ws / "results" / "task-1.txt").write_text("done\n")
        pd.mark_done(self.ws, A, "task-1", published=True)

    def test_a_failed_archive_keeps_the_sentinel_and_the_body(self):
        self._finished()
        with mock.patch.object(pd.os, "rename", side_effect=PermissionError(13, "denied")):
            acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], [])
        self.assertIn("task-1", acts["kept"])
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file(), "sentinel kept")
        self.assertEqual(pd.body_path(self.ws, A, "task-1").read_text(), TEXT, "body kept")
        self.assertFalse(pd.archived_payload(self.ws, "task-1").exists())
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], ["task-1"], "the next pass retires it once the archive works")

    def test_an_existing_archive_lets_the_body_go_and_retires(self):
        self._finished()
        arch = pd.archived_payload(self.ws, "task-1")
        arch.parent.mkdir(parents=True)
        arch.write_text(TEXT)
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], ["task-1"])
        self.assertFalse(pd.body_path(self.ws, A, "task-1").exists())

    def test_a_non_regular_body_is_not_archived_and_the_sentinel_stays(self):
        self._finished()
        b = pd.body_path(self.ws, A, "task-1")
        b.unlink()
        b.mkdir()
        acts = pd.prune_spent(self.ws, A)
        self.assertEqual(acts["retired"], [])
        self.assertTrue((self.ws / "deliveries" / A / "task-1.txt").is_file())

    def test_a_sentinel_with_no_body_retires_as_before(self):
        self._finished()
        pd.body_path(self.ws, A, "task-1").unlink()
        self.assertEqual(pd.prune_spent(self.ws, A)["retired"], ["task-1"])


class ResolvedIdentity(unittest.TestCase):
    """The watcher keys results, claims, held and dedupe on the task's name: the
    resolver's second line, never the payload's basename."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name).resolve() / "ws"
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "tasks" / "task-1.txt").write_text(TEXT)
        rt.route(self.ws, {"id": "task-1", "source": "ag2space", "channel_id": ROOM}, roster({ROOM: A}))
        self.inbox = self.ws / "deliveries" / A
        self.body = pd.body_path(self.ws, A, "task-1")

    def tearDown(self):
        self.td.cleanup()

    def shim(self, entry, resolver):
        script = (f'WORKSPACE_DIR="{self.ws}"; source "{REPO}/src/inbox-resolve.sh"; '
                  f'resolve_inbox_entry "$1"; echo "rc=$?"')
        env = dict(os.environ, SUTANDO_INBOX_RESOLVER=str(resolver))
        return subprocess.run(["bash", "-c", script, "_", str(entry)], capture_output=True, text=True,
                              env=env, timeout=30)

    def test_the_shim_passes_the_task_name_on_beside_the_payload(self):
        r = self.shim(self.inbox / "task-1.txt", SCRIPTS / "resolve-inbox-entry")
        self.assertEqual(r.stdout.splitlines(), [str(self.body), "task-1.txt", "rc=0"], r.stderr)

    def test_the_shim_refuses_an_unusable_task_name(self):
        for bad in ("../task-1.txt", ".task-1.txt", "task-1.body", "task 1.txt"):
            fake = Path(self.td.name) / "fake.sh"
            fake.write_text(f"#!/bin/sh\nprintf '%s\\n%s\\n' '{self.body}' '{bad}'\n")
            fake.chmod(0o755)
            r = self.shim(self.inbox / "task-1.txt", fake)
            self.assertEqual(r.stdout.splitlines(), ["rc=3"], f"{bad!r}: {r.stdout} {r.stderr}")

    def sweep(self):
        """One worker watcher over the inbox, with the shipped resolver, until it
        has decided about the sentinel (an announce or a refusal)."""
        out, err = Path(self.td.name) / "w.out", Path(self.td.name) / "w.err"
        env = dict(os.environ, SUTANDO_INBOX_RESOLVER=str(SCRIPTS / "resolve-inbox-entry"),
                   SUTANDO_WORKSPACE_DIR=str(self.ws), SUTANDO_RESULTS_DIR=str(self.ws / "results"),
                   SUTANDO_INSTANCE=A)
        with open(out, "w") as fo, open(err, "w") as fe:
            proc = subprocess.Popen(["bash", str(REPO / "src" / "watch-tasks-stream.sh"), str(self.inbox),
                                     "--role", "standby", "--inbox", str(self.inbox)],
                                    stdout=fo, stderr=fe, env=env, start_new_session=True)
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if "TASK_FILE:" in out.read_text() or "already answered" in err.read_text():
                    break
                time.sleep(0.25)
            time.sleep(1.0)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=30)
        return out.read_text(), err.read_text()

    def test_an_unanswered_routed_task_is_announced_by_its_body(self):
        out, err = self.sweep()
        # The body sits in the worker's own inbox, so it is announced by its bare name.
        self.assertEqual(out.splitlines(), ["TASK_FILE: task-1.body"], err[-800:])

    def test_an_answered_routed_task_is_not_dispatched_again_after_a_restart(self):
        (self.ws / "results" / "task-1.txt").write_text("answered already\n")
        out, err = self.sweep()
        self.assertNotIn("TASK_FILE:", out, err[-800:])
        self.assertIn("already answered, not dispatching again: task-1.body", err, err[-800:])


if __name__ == "__main__":
    unittest.main(verbosity=1)
