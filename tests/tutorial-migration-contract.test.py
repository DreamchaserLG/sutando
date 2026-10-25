#!/usr/bin/env python3
"""Design contracts for the tutorial's v1 -> v2 migration, measured against the deployed writer
(cf87f324, the fixtures of tutorial-deployed-fence.test.py) before any migration code exists.

- The litter predicate: the temp files a fenced deployed run leaves are exactly what it matches,
  and a cleanup by it under the lock keeps a deployed writer out, where a prefix cleanup does not.
- Conversion inputs: every record the deployed writer leaves is one row of the conversion matrix.
- The M3 gap: with the v1 path absent and no fence yet, a rolled-back writer rewrites its page, and
  only the v1 inode (not its digest) shows it."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tutorial_fence", HERE / "tutorial-deployed-fence.test.py")
fence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fence)
base, ROOM, ME = fence.base, fence.ROOM, fence.ME

# The design's cleanup predicate (§12): tempfile.mkstemp(prefix=".getting-started.") names, a
# regular file, never the lock. The implementation must call one shared predicate this test pins.
LITTER = re.compile(r"\.getting-started\.[a-z0-9_]{8}")
LOCK = ".getting-started.lock"
KEPT = (LOCK, ".getting-started.v2.k3j9x0qa", "getting-started.json", "getting-started.v1.bak",
        "getting-started.v2.json")


def litter(directory: Path) -> set:
    return {p.name for p in directory.iterdir()
            if p.name != LOCK and LITTER.fullmatch(p.name) and p.is_file() and not p.is_symlink()}


def prefix_litter(directory: Path) -> set:
    """The cleanup the review warns about: everything under the deployed temp prefix."""
    return {p.name for p in directory.iterdir() if p.name.startswith(".getting-started.") and p.is_file()}


# The deployed rows of the conversion matrix (§12): the state a record carries, and its exact keys.
DEPLOYED_ROWS = {"complete": {"kind", "state", "hash", "updated_at"},
                 "creating": {"state", "operation_id", "updated_at"},
                 "pending": {"kind", "state", "operation_id", "updated_at"}}

CREATE_FAILS = "        if os.environ.get('STUB_CREATE_FAIL'): raise RuntimeError('create lost')\n"


class MigrationContractTest(unittest.TestCase):
    setUp = fence.DeployedFenceTest.setUp
    db, writes, reset, edit = (fence.DeployedFenceTest.db, fence.DeployedFenceTest.writes,
                               fence.DeployedFenceTest.reset, fence.DeployedFenceTest.edit)
    deployed, seed = fence.DeployedFenceTest.deployed, fence.DeployedFenceTest.seed

    def argv(self, ws: Path, name: str = "Blue") -> list:
        return [sys.executable, str(self.tmp / "runner.py"), str(self.script), str(ws), "publish",
                "--room", ROOM, "--name", name, "--core", "codex", "--agent-mxid", ME,
                "--room-commons", str(self.tmp / "room_commons.py"), *fence.SUMMON]

    def env(self, **extra):
        return base.isolated_child_env(STUB_STATE=str(self.state), STUB_BY=ME, **extra)

    def test_a_fenced_run_leaves_exactly_the_litter_the_predicate_matches(self):
        ws = self.tmp / "ws"
        self.seed(ws)
        marker = fence.fence(ws)
        directory = marker.parent
        for name in ("Blue", "Green", "Blue"):
            self.assertEqual(self.deployed(ws, name)[0], 4)
        left = {p.name for p in directory.iterdir()} - {LOCK, "getting-started.json", "getting-started.v1.bak"}
        self.assertEqual(len(left), 3, left)
        for name in KEPT[1:]:
            if not (directory / name).exists():
                (directory / name).write_text("{}\n")
        (directory / ".getting-started.linkxxxx").symlink_to(directory / LOCK)
        self.assertEqual(litter(directory), left)
        self.assertTrue(set(KEPT).isdisjoint(litter(directory)))
        self.assertIn(LOCK, prefix_litter(directory), "the prefix cleanup would take the lock")

    def held_cleanup(self, ws: Path, predicate) -> tuple:
        """Hold the lock as v2 will, clean up by `predicate`, then start a deployed run. Returns its
        exit code (None: still waiting on the lock) and the room calls it made while we held it."""
        self.seed(ws)
        directory = fence.fence(ws).parent
        self.assertEqual(self.deployed(ws, "Green")[0], 4)
        self.reset()
        with open(directory / LOCK, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            for name in predicate(directory):
                (directory / name).unlink()
            run = subprocess.Popen(self.argv(ws, "Green"), env=self.env(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                code = run.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
            inside = list(self.db()["calls"])
        self.assertEqual(run.wait(timeout=120), 4)
        self.assertEqual(self.writes(), [])
        return code, inside

    def test_a_prefix_cleanup_under_the_lock_lets_a_deployed_writer_in(self):
        code, inside = self.held_cleanup(self.tmp / "ws", prefix_litter)
        self.assertEqual(code, 4, "it ran to the end while the lock was held")
        self.assertIn(["pages", None], inside)

    def test_the_predicate_cleanup_keeps_a_deployed_writer_waiting_on_the_lock(self):
        code, inside = self.held_cleanup(self.tmp / "ws", litter)
        self.assertEqual((code, inside), (None, []))

    def run_deployed(self, ws: Path, **extra) -> int:
        return subprocess.run(self.argv(ws), env=self.env(**extra), capture_output=True, text=True,
                              timeout=120).returncode

    def test_every_record_the_deployed_writer_leaves_is_a_conversion_row(self):
        runner = self.tmp / "runner.py"
        hook = "    def execute(self, room, action, arguments, operation_id=None):\n"
        self.assertEqual(fence.RUNNER.count(hook), 1)
        runner.write_text(fence.RUNNER.replace(hook, hook + CREATE_FAILS))
        left = {}
        for row, extra, code in (("complete", {}, 0), ("creating", {"STUB_CREATE_FAIL": "1"}, 4),
                                 ("pending", {"STUB_FAIL": "append"}, 4)):
            ws = self.tmp / ("ws-" + row)
            self.state.write_text(json.dumps({"pages": {}, "calls": [], "next": 1}))
            self.assertEqual(self.run_deployed(ws, **extra), code, row)
            data = json.loads((ws / "state" / "tutorial" / "getting-started.json").read_text())
            self.assertEqual(data["version"], 1)
            left[row] = data["rooms"][ROOM]
        self.assertEqual({row: e["state"] for row, e in left.items()}, {row: row for row in left})
        self.assertEqual({row: set(e) for row, e in left.items()}, DEPLOYED_ROWS)

    def test_in_the_m3_gap_a_rolled_back_writer_rewrites_its_page_once(self):
        """The residual §1 and §12 scope their claims to: v1 unlinked, no fence yet, a rollback."""
        ws = self.tmp / "ws"
        kind = self.seed(ws)
        self.edit(kind)
        directory = ws / "state" / "tutorial"
        v1 = directory / "getting-started.json"
        source = hashlib.sha256(v1.read_bytes()).hexdigest()
        os.link(v1, directory / "getting-started.v1.bak")
        (directory / "getting-started.v2.json").write_text(json.dumps(
            {"version": 2, "migration": {"phase": "converted", "source_sha256": source}}))
        v1.unlink()
        self.assertEqual(self.run_deployed(ws), 0)
        self.assertNotIn("OWNER NOTES", self.db()["pages"][kind])
        self.assertTrue(any(c[0] == "replace" and c[1] == kind for c in self.writes()), self.writes())
        # The record can be byte-identical to the source (same render, same second): only the
        # inode, a new one from os.replace, tells the next v2 start that a deployed run wrote here.
        self.assertTrue(v1.is_file())
        self.assertFalse(os.path.samefile(v1, directory / "getting-started.v1.bak"))


if __name__ == "__main__":
    unittest.main()
