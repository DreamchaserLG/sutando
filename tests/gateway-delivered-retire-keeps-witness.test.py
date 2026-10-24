#!/usr/bin/env python3
"""A delivered proactive notice whose archive rename fails keeps its inode linked.

The orphan-check publisher (src/task_transition.py) proves a commit by a witness link
of the notice's inode. The gateway's confirmed-delivery path used to unlink the claimed
result when it could not move it into results/archive/, which dropped that link: the
publisher's recovery then read the already-delivered notice as never committed and sent
it again. This drives the real `_post_proactive` drain and the real recovery.

Run: python3 tests/gateway-delivered-retire-keeps-witness.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))
_ISOLATED = tempfile.mkdtemp(prefix="gw-delivered-witness-")
os.environ["CLAUDE_CONFIG_DIR"] = _ISOLATED
os.environ["REMOTE_PROACTIVE_ROOM"] = ""

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402
import task_transition as tt  # noqa: E402

ROOM = "!OwnerDmAbCdEf:ag2.space"
PREFIX = "proactive-orphan-recovery-"


class Crash(RuntimeError):
    """The publisher dies here."""


@unittest.skipIf(os.geteuid() == 0, "root renames into a mode-555 directory")
class DeliveredRetireKeepsTheWitness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = Path(tmp.name)
        self.tasks, self.results = ws / "tasks", ws / "results"
        self.tasks.mkdir()
        self.results.mkdir()
        (self.tasks / "task-a.txt").write_text("timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: A\n")

    def transition(self, **kw):
        moves = {n: tt.generation_of(self.tasks / n) for n in kw.pop("names", [])}
        return tt.transition(self.tasks, self.results, self.tasks / "archive", moves=moves,
                             render=kw.pop("render", lambda landed: "archived: A\n" if landed else None),
                             prefix=PREFIX)

    def drain(self, archive: Path) -> list:
        posts = []

        def fake_req(method, path, payload=None, timeout=None):
            if method == "POST":
                posts.append(payload)
            return {"ok": True, "event_id": "$evt"}

        saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE)
        gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = self.results, archive
        gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = ROOM, fake_req, None
        gb._ROUTING.update(owner_dm=ROOM, loaded=True, next=gb.time.time() + 3600)
        try:
            with mock.patch.object(gb, "_record_proactive_receipt"):
                gb._post_proactive()
        finally:
            (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE) = saved
            gb._ROUTING.update(owner_dm="", loaded=False, next=0.0)
        return posts

    def test_a_delivered_notice_that_cannot_be_archived_is_not_sent_again(self):
        real_publish = tt._publish_at

        def publish_then_die(*a, **kw):
            real_publish(*a, **kw)
            raise Crash("killed after the commit, before the journal went")

        with mock.patch.object(tt, "_publish_at", side_effect=publish_then_die), self.assertRaises(Crash):
            self.transition(names=["task-a.txt"])
        (notice,) = sorted(self.results.glob(f"{PREFIX}*.txt"))
        archive = self.results / "archive"
        archive.mkdir(mode=0o555)
        self.addCleanup(archive.chmod, 0o755)
        posts = self.drain(archive)
        self.assertEqual(len(posts), 1, "delivered once")
        self.assertFalse(notice.exists(), "retired from the live name")
        witness = next(self.results.glob(".*.witness"))
        self.assertEqual(os.stat(witness).st_nlink, 2, "the delivered inode is still linked")
        out = self.transition()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(sorted(self.results.glob(f"{PREFIX}*.txt")), [], "no second notice")
        self.assertEqual(self.drain(archive), [], "nothing more to deliver")
        self.assertEqual(sorted((self.tasks / "archive").glob(".notice-*.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
