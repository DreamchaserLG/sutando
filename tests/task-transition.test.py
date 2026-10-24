#!/usr/bin/env python3
"""src/task_transition.py: the one owner of a task's report-then-archive transition.

Every case drives the PRODUCTION operation on a real filesystem (#4399, round 5,
kewei-red): two transitions in one frozen second from separate processes leave two
notices; a name collision inside one process is retried, never replaced; a task
replaced between plan and move is refused and the new generation stays live; an
archived record under the same name is never overwritten; a symlinked task or
archive dir is refused before anything is published; an undeliverable body is
refused before anything moves.

Run: python3 tests/task-transition.test.py
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import task_transition as tt  # noqa: E402

PREFIX = "proactive-orphan-recovery-"
FROZEN = 1800000000.0


def _task(tasks: Path, name: str, ask: str) -> Path:
    p = tasks / name
    p.write_text(f"timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: {ask}\n")
    return p


def _replace(live: Path, ask: str) -> None:
    tmp = live.with_name(f".{live.name}.new")
    tmp.write_text(f"timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: {ask}\n")
    os.replace(tmp, live)


def _transition_in_child(root: str, name: str, body: str, out) -> None:
    ws = Path(root)
    gen = tt.generation_of(ws / "tasks" / name)
    out.put(tt.transition(ws / "tasks", ws / "results", ws / "tasks" / "archive",
                          moves={name: gen}, body=body, prefix=PREFIX, now=FROZEN))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.tasks, self.results = self.ws / "tasks", self.ws / "results"
        self.archive = self.tasks / "archive"
        self.tasks.mkdir(parents=True)
        self.results.mkdir()

    def run_transition(self, names: list[str], body: str | None = "notice\n", **kw) -> dict:
        moves = {n: tt.generation_of(self.tasks / n) for n in names}
        return tt.transition(self.tasks, self.results, self.archive, moves=moves, body=body,
                             prefix=PREFIX, **kw)

    def notices(self) -> list[Path]:
        return sorted(self.results.glob(f"{PREFIX}*.txt"))


class TestExclusivePublication(Base):
    def test_two_processes_in_one_frozen_second_leave_two_notices(self):
        _task(self.tasks, "task-a.txt", "A")
        _task(self.tasks, "task-b.txt", "B")
        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        procs = [ctx.Process(target=_transition_in_child, args=(str(self.ws), n, f"notice {n}\n", q))
                 for n in ("task-a.txt", "task-b.txt")]
        for p in procs:
            p.start()
        outs = [q.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(30)
        self.assertEqual([o["errors"] for o in outs], [[], []])
        self.assertEqual(len({o["result"] for o in outs}), 2)
        self.assertEqual(len(self.notices()), 2)
        self.assertEqual(sorted(p.read_text() for p in self.notices()),
                         ["notice task-a.txt\n", "notice task-b.txt\n"])
        self.assertEqual(sorted(p.name for p in self.archive.iterdir()), ["task-a.txt", "task-b.txt"])

    def test_a_name_collision_in_one_process_is_retried_never_replaced(self):
        _task(self.tasks, "task-a.txt", "A")
        _task(self.tasks, "task-b.txt", "B")
        real_hex, names = tt.secrets.token_hex, iter(["aaaa", "aaaa", "bbbb"])
        # Only the result-name token is scripted; stage_text's 8-byte staging token stays real.
        with mock.patch.object(tt.secrets, "token_hex",
                               side_effect=lambda n=32: next(names) if n == 4 else real_hex(n)):
            first = self.run_transition(["task-a.txt"], "first\n", now=FROZEN)
            second = self.run_transition(["task-b.txt"], "second\n", now=FROZEN)
        self.assertEqual((first["errors"], second["errors"]), ([], []))
        self.assertEqual(Path(first["result"]).name, f"{PREFIX}1800000000-{os.getpid()}-aaaa.txt")
        self.assertEqual(Path(second["result"]).name, f"{PREFIX}1800000000-{os.getpid()}-bbbb.txt")
        self.assertEqual(Path(first["result"]).read_text(), "first\n")
        self.assertEqual([p.name for p in self.results.iterdir() if p.name.startswith(".")], [],
                         "no staging left behind by the refused attempt")

    def test_an_undeliverable_body_is_refused_before_any_move(self):
        _task(self.tasks, "task-a.txt", "A")
        out = self.run_transition(["task-a.txt"], "x" * (tt.PROACTIVE_MAX_BODY_B + 1))
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("undeliverable", out["errors"][0])
        self.assertIn("nothing moved", out["errors"][0])
        self.assertEqual((out["result"], out["archived"], self.notices()), (None, {}, []))
        self.assertTrue((self.tasks / "task-a.txt").exists())

    def test_a_body_at_the_ceiling_is_published(self):
        out = self.run_transition([], "x" * tt.PROACTIVE_MAX_BODY_B)
        self.assertEqual(out["errors"], [])
        self.assertEqual(Path(out["result"]).stat().st_size, tt.PROACTIVE_MAX_BODY_B)


class TestGenerationGuard(Base):
    def test_generation_is_the_bytes_not_the_name(self):
        p = _task(self.tasks, "task-a.txt", "A")
        before = tt.generation_of(p)
        _replace(p, "A rewritten")
        self.assertNotEqual(before, tt.generation_of(p))
        self.assertIsNone(tt.generation_of(self.tasks / "absent.txt"))

    def test_replaced_before_the_publish_refuses_everything(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        _replace(p, "new")
        out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                            body="notice\n", prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("refused to move 'task-a.txt': changed since it was planned", out["errors"][0])
        self.assertIn("nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], out["archived"], self.notices()), (None, {}, []))
        self.assertIn("new", p.read_text())

    def test_replaced_after_the_publish_leaves_the_new_generation_live(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        real = tt.publish_exclusive

        def publish_then_replace(*a, **kw):
            target = real(*a, **kw)
            _replace(p, "new")
            return target

        with mock.patch.object(tt, "publish_exclusive", side_effect=publish_then_replace):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                body="notice\n", prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("changed since it was planned", out["errors"][0])
        self.assertIn("the next pass reports it again", out["errors"][0])
        self.assertEqual(out["archived"], {})
        self.assertIn("new", p.read_text())
        self.assertFalse(self.archive.joinpath("task-a.txt").exists())
        self.assertEqual(len(self.notices()), 1)

    def test_replaced_between_link_and_unlink_keeps_the_new_generation_live(self):
        """The narrowest window: the archive link is made, then the live name is
        rewritten before the unlink. The old bytes are archived; the new file stays."""
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        real_link = os.link

        def link_then_replace(src, dst, **kw):
            real_link(src, dst, **kw)
            if Path(src) == p:
                _replace(p, "new")

        with mock.patch.object(tt.os, "link", side_effect=link_then_replace):
            landed = tt.archive_generation(self.tasks, "task-a.txt", gen, self._archive_dir())
        self.assertEqual(landed, self.archive / "task-a.txt")
        self.assertIn("old", landed.read_text())
        self.assertIn("new", p.read_text(), "the replacement is a live task, never unlinked")

    def test_a_missing_generation_is_refused(self):
        _task(self.tasks, "task-a.txt", "A")
        out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": None},
                            body=None, prefix=PREFIX)
        self.assertIn("no generation was captured", out["errors"][0])
        self.assertTrue((self.tasks / "task-a.txt").exists())

    def _archive_dir(self) -> Path:
        self.archive.mkdir(exist_ok=True)
        return self.archive


class TestNoClobberAndSymlinks(Base):
    def test_an_archived_record_is_never_overwritten(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text("earlier\n")
        (self.archive / "task-a.txt.1").write_text("earlier still\n")
        out = self.run_transition(["task-a.txt"], None)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt.2"})
        self.assertEqual((self.archive / "task-a.txt").read_text(), "earlier\n")
        self.assertIn("task: A", (self.archive / "task-a.txt.2").read_text())
        self.assertFalse((self.tasks / "task-a.txt").exists())

    def test_names_that_are_not_bare_are_refused_before_the_publish(self):
        (self.ws / "secret.txt").write_text("stay")
        absolute = str(self.ws / "secret.txt")
        dotted = _task(self.tasks, "task-x.txt", "X")
        g = tt.generation_of  # real generations: only the bare-name guard stands in the way
        out = tt.transition(self.tasks, self.results, self.archive,
                            moves={"../secret.txt": g(self.ws / "secret.txt"), absolute: g(Path(absolute)),
                                   "": g(self.tasks), "..": g(self.ws), "./task-x.txt": g(dotted)},
                            body="notice\n", prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 5, out)
        for err in out["errors"]:
            self.assertIn("refused to move", err)
            self.assertIn("not a bare filename", err)
        self.assertEqual((out["result"], self.notices()), (None, []))
        self.assertEqual((self.ws / "secret.txt").read_text(), "stay")
        self.assertFalse((self.tasks / "secret.txt").exists())
        self.assertTrue(dotted.exists())

    def test_a_symlinked_task_is_refused_and_its_target_untouched(self):
        real = Path(self.tmp.name) / "real.txt"
        real.write_text("task: via link\n")
        (self.tasks / "task-link.txt").symlink_to(real)
        out = self.run_transition(["task-link.txt"])
        self.assertIn("refused to move 'task-link.txt': a symlink, not a task file", out["errors"][0])
        self.assertEqual((out["result"], out["archived"]), (None, {}))
        self.assertTrue(real.exists() and (self.tasks / "task-link.txt").is_symlink())

    def test_a_symlinked_archive_dir_is_refused_before_the_publish(self):
        _task(self.tasks, "task-a.txt", "A")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.archive.symlink_to(outside, target_is_directory=True)
        out = self.run_transition(["task-a.txt"])
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("is a symlink; nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], sorted(outside.iterdir())), (None, []))
        self.assertTrue((self.tasks / "task-a.txt").exists())

    def test_an_uncreatable_archive_dir_publishes_nothing(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.write_text("a file where the dir should be")
        out = self.run_transition(["task-a.txt"])
        self.assertIn("could not create", out["errors"][0])
        self.assertIn("nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], self.notices()), (None, []))

    def test_nothing_to_move_publishes_without_touching_the_archive_dir(self):
        out = self.run_transition([], "notice\n")
        self.assertEqual(out["errors"], [])
        self.assertIsNotNone(out["result"])
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
