#!/usr/bin/env python3
"""src/task_transition.py: the one owner of a task's archive-then-report transition.

Every case drives the PRODUCTION operation on a real filesystem (#4399, rounds 5-6,
kewei-red / susans-MacBook-Pro): two transitions in one frozen second from separate
processes leave two notices; a name collision inside one process is retried, never
replaced; a task replaced between the generation check and the claim survives under
its own name; a retaken name leaves the claimed bytes quarantined; an archived record
under the same name is never overwritten and the notice names the `.N` it landed
under; the directories are pinned handles, so one swapped for a symlink after
pre-flight is refused; and a crash after each persisted stage (claim, placement,
reserved notice, publication) is recovered by the next pass exactly once.

Run: python3 tests/task-transition.test.py
"""
from __future__ import annotations

import json
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


class Crash(RuntimeError):
    """The process dies here."""


def _task(tasks: Path, name: str, ask: str) -> Path:
    p = tasks / name
    p.write_text(f"timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: {ask}\n")
    return p


def _replace(live: Path, ask: str) -> None:
    tmp = live.with_name(f".{live.name}.new")
    tmp.write_text(f"timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: {ask}\n")
    os.replace(tmp, live)


def _notice(text: str | None):
    """A renderer that says `text` whatever landed (None: nothing to say)."""
    return lambda landed: text


def _naming(landed: dict) -> str | None:
    """A renderer that names what landed, the way the skill's does."""
    return ("archived: " + ", ".join(f"{k}->{v}" for k, v in sorted(landed.items())) + "\n") if landed else None


def _transition_in_child(root: str, name: str, body: str, out) -> None:
    ws = Path(root)
    gen = tt.generation_of(ws / "tasks" / name)
    out.put(tt.transition(ws / "tasks", ws / "results", ws / "tasks" / "archive",
                          moves={name: gen}, render=_notice(body), prefix=PREFIX, now=FROZEN))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.tasks, self.results = self.ws / "tasks", self.ws / "results"
        self.archive = self.tasks / "archive"
        self.tasks.mkdir(parents=True)
        self.results.mkdir()

    def run_transition(self, names: list[str], render=_notice("notice\n"), **kw) -> dict:
        moves = {n: tt.generation_of(self.tasks / n) for n in names}
        return tt.transition(self.tasks, self.results, self.archive, moves=moves, render=render,
                             prefix=PREFIX, **kw)

    def notices(self) -> list[Path]:
        return sorted(self.results.glob(f"{PREFIX}*.txt"))

    def journals(self) -> list[Path]:
        return sorted(self.archive.glob(f"{tt.JOURNAL_PREFIX}*.json")) if self.archive.is_dir() else []

    def live_names(self) -> list[str]:
        return sorted(x.name for x in self.tasks.iterdir())

    def archive_names(self) -> list[str]:
        return sorted(x.name for x in self.archive.iterdir() if not x.name.startswith("."))

    def replace_on_second_check(self, p: Path, ask: str):
        """Susan's and Kewei's round-6 repro: the live name is rewritten the instant
        the move's own generation check returns (the pre-flight's came first)."""
        real, calls = tt._source_problem, []

        def check_then_replace(fd, name, gen):
            r = real(fd, name, gen)
            calls.append(name)
            if calls.count(p.name) == 2:
                _replace(p, ask)
            return r

        return mock.patch.object(tt, "_source_problem", side_effect=check_then_replace)


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
        self.assertEqual([o["recovered"] for o in outs], [[], []], "a live pass's journal is never taken")
        self.assertEqual(len({o["result"] for o in outs}), 2)
        self.assertEqual(sorted(p.read_text() for p in self.notices()),
                         ["notice task-a.txt\n", "notice task-b.txt\n"])
        self.assertEqual(self.archive_names(), ["task-a.txt", "task-b.txt"])
        self.assertEqual(sorted(x.name for x in self.archive.iterdir() if x.name.startswith(".")), [])

    def test_a_name_collision_in_one_process_is_retried_never_replaced(self):
        _task(self.tasks, "task-a.txt", "A")
        _task(self.tasks, "task-b.txt", "B")
        real_hex, names = tt.secrets.token_hex, iter(["aaaa", "aaaa", "bbbb"])
        # Only the 4-byte result-name token is scripted; staging, claim (5) and journal (6) stay real.
        with mock.patch.object(tt.secrets, "token_hex",
                               side_effect=lambda n=32: next(names) if n == 4 else real_hex(n)):
            first = self.run_transition(["task-a.txt"], _notice("first\n"), now=FROZEN)
            second = self.run_transition(["task-b.txt"], _notice("second\n"), now=FROZEN)
        self.assertEqual((first["errors"], second["errors"]), ([], []))
        self.assertEqual(Path(first["result"]).name, f"{PREFIX}1800000000-{os.getpid()}-aaaa.txt")
        self.assertEqual(Path(second["result"]).name, f"{PREFIX}1800000000-{os.getpid()}-bbbb.txt")
        self.assertEqual(Path(first["result"]).read_text(), "first\n")
        self.assertEqual([p.name for p in self.results.iterdir() if p.name.startswith(".")], [],
                         "no staging left behind by the refused attempt")
        self.assertEqual(self.journals(), [])

    def test_the_commit_is_result_publishs_exclusive_mode(self):
        with mock.patch.object(tt.result_publish, "publish_text",
                               wraps=tt.result_publish.publish_text) as m:
            self.run_transition([], _notice("notice\n"))
        self.assertEqual(m.call_count, 1)
        self.assertTrue(m.call_args.kwargs.get("exclusive"))

    def test_an_undeliverable_body_is_refused_and_the_archive_stays_journaled(self):
        _task(self.tasks, "task-a.txt", "A")
        out = self.run_transition(["task-a.txt"], _notice("x" * (tt.PROACTIVE_MAX_BODY_B + 1)))
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("undeliverable", out["errors"][0])
        self.assertIn("recorded in tasks/archive/.notice-", out["errors"][0])
        self.assertIsNone(out["result"])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertEqual(len(self.journals()), 1)
        with self.assertRaises(tt.TransitionRefused):
            tt.publish_exclusive(self.results, PREFIX, "x" * (tt.PROACTIVE_MAX_BODY_B + 1))

    def test_a_body_at_the_ceiling_is_published(self):
        out = self.run_transition([], _notice("x" * tt.PROACTIVE_MAX_BODY_B))
        self.assertEqual(out["errors"], [])
        self.assertEqual(Path(out["result"]).stat().st_size, tt.PROACTIVE_MAX_BODY_B)


class TestGenerationGuard(Base):
    def test_generation_is_the_bytes_not_the_name(self):
        p = _task(self.tasks, "task-a.txt", "A")
        before = tt.generation_of(p)
        _replace(p, "A rewritten")
        self.assertNotEqual(before, tt.generation_of(p))
        self.assertIsNone(tt.generation_of(self.tasks / "absent.txt"))

    def test_replaced_after_planning_refuses_everything(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        _replace(p, "new")
        out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                            render=_notice("notice\n"), prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("refused to move 'task-a.txt': changed since it was planned", out["errors"][0])
        self.assertIn("nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], out["archived"], self.notices()), (None, {}, []))
        self.assertIn("new", p.read_text())
        self.assertEqual(self.journals(), [], "no journal is written for a refused plan")

    def test_replaced_between_the_generation_check_and_the_claim_puts_the_new_task_back(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        with self.replace_on_second_check(p, "new"):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("refused to move 'task-a.txt': changed since it was planned; it is back under "
                      "its name, live", out["errors"][0])
        self.assertEqual((out["archived"], out["quarantined"], out["result"]), ({}, {}, None))
        self.assertIn("new", p.read_text(), "the new task survives under its own name")
        self.assertEqual(self.live_names(), ["archive", "task-a.txt"], "no claim left behind")
        self.assertEqual(sorted(self.archive.iterdir()), [], "nothing archived, no journal kept")

    def test_a_name_retaken_during_the_give_back_quarantines_the_claim(self):
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        real_link = os.link

        def retaken(src, dst, **kw):
            if str(src).startswith("task-a.txt.claim-") and str(dst) == "task-a.txt":
                _task(self.tasks, "task-a.txt", "newer still")  # a third writer wins the name
            return real_link(src, dst, **kw)

        with self.replace_on_second_check(p, "new"), mock.patch.object(tt.os, "link", side_effect=retaken):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("quarantined as tasks/task-a.txt.claim-", out["errors"][0])
        self.assertIn("rename it back to requeue", out["errors"][0])
        kept = [x for x in self.tasks.iterdir() if x.name.startswith("task-a.txt.claim-")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(out["quarantined"], {"task-a.txt": kept[0].name})
        self.assertIn("new", kept[0].read_text())
        self.assertIn("newer still", p.read_text())
        self.assertEqual(self.archive_names(), [])
        self.assertEqual(self.journals(), [], "nothing landed: nothing to publish, journal closed")

    def test_a_writer_recreating_the_live_name_during_the_move_is_never_touched(self):
        """After the claim rename the live name is free; a task written there is
        a new task the move never sees, and the notice describes the old bytes."""
        p = _task(self.tasks, "task-a.txt", "old")
        gen = tt.generation_of(p)
        real_move = tt.task_archive.move_without_clobbering

        def move_then_recreate(src, dest, **kw):
            _task(self.tasks, "task-a.txt", "new")
            return real_move(src, dest, **kw)

        with mock.patch.object(tt.task_archive, "move_without_clobbering", side_effect=move_then_recreate):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertIn("old", (self.archive / "task-a.txt").read_text())
        self.assertIn("new", p.read_text())
        self.assertEqual(Path(out["result"]).read_text(), "archived: task-a.txt->task-a.txt\n")

    def test_a_failed_placing_move_puts_the_claim_back(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with mock.patch.object(tt.task_archive, "move_without_clobbering",
                               side_effect=PermissionError(1, "EPERM")):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertIn("the archive move failed", out["errors"][0])
        self.assertIn("back under its name, live", out["errors"][0])
        self.assertIn("task: A", p.read_text())
        self.assertEqual(self.live_names(), ["archive", "task-a.txt"])
        self.assertIsNone(out["result"], "nothing landed, nothing to say")

    def test_the_move_is_task_archives_contract_through_the_directory_handles(self):
        p = _task(self.tasks, "task-a.txt", "A")
        with mock.patch.object(tt.task_archive, "claim_generation",
                               wraps=tt.task_archive.claim_generation) as c, \
                mock.patch.object(tt.task_archive, "place_claim", wraps=tt.task_archive.place_claim) as m:
            landed = tt.archive_generation(self.tasks, "task-a.txt", tt.generation_of(p), self.archive)
        self.assertEqual(landed, self.archive / "task-a.txt")
        self.assertEqual((c.call_count, m.call_count), (1, 1))
        self.assertTrue(m.call_args.args[0].startswith("task-a.txt.claim-"))
        self.assertIsInstance(m.call_args.kwargs["src_dir_fd"], int)
        self.assertIsInstance(m.call_args.kwargs["dst_dir_fd"], int)

    def test_a_missing_generation_is_refused(self):
        _task(self.tasks, "task-a.txt", "A")
        out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": None},
                            render=_notice(None), prefix=PREFIX)
        self.assertIn("no generation was captured", out["errors"][0])
        self.assertTrue((self.tasks / "task-a.txt").exists())


class TestNoClobberAndSymlinks(Base):
    def test_an_archived_record_is_never_overwritten_and_the_notice_names_the_landing(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text("earlier\n")
        (self.archive / "task-a.txt.1").write_text("earlier still\n")
        out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt.2"})
        self.assertEqual((self.archive / "task-a.txt").read_text(), "earlier\n")
        self.assertIn("task: A", (self.archive / "task-a.txt.2").read_text())
        self.assertFalse((self.tasks / "task-a.txt").exists())
        self.assertEqual(Path(out["result"]).read_text(), "archived: task-a.txt->task-a.txt.2\n")

    def test_names_that_are_not_bare_are_refused_before_anything_moves(self):
        (self.ws / "secret.txt").write_text("stay")
        absolute = str(self.ws / "secret.txt")
        dotted = _task(self.tasks, "task-x.txt", "X")
        g = tt.generation_of  # real generations: only the bare-name guard stands in the way
        out = tt.transition(self.tasks, self.results, self.archive,
                            moves={"../secret.txt": g(self.ws / "secret.txt"), absolute: g(Path(absolute)),
                                   "": g(self.tasks), "..": g(self.ws), "./task-x.txt": g(dotted)},
                            render=_notice("notice\n"), prefix=PREFIX)
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

    def test_a_symlinked_archive_dir_is_refused_at_preflight(self):
        _task(self.tasks, "task-a.txt", "A")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        self.archive.symlink_to(outside, target_is_directory=True)
        out = self.run_transition(["task-a.txt"])
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("is a symlink; nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], sorted(outside.iterdir())), (None, []))
        self.assertTrue((self.tasks / "task-a.txt").exists())

    def test_an_archive_dir_swapped_for_a_symlink_after_preflight_is_refused(self):
        """Kewei's round-6 TOCTOU: the directory is checked, then replaced by
        `archive -> outside` before the move. The move works through the handle
        pinned at pre-flight, sees the name no longer leads to it, and refuses;
        nothing reaches `outside`."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        real = tt._Journal.create

        def swap_after_journal(*a, **kw):
            j = real(*a, **kw)
            for x in self.archive.iterdir():  # empty the pinned dir so the name can be re-pointed
                x.unlink()
            self.archive.rmdir()
            self.archive.symlink_to(outside, target_is_directory=True)
            return j

        with mock.patch.object(tt._Journal, "create", side_effect=swap_after_journal):
            out = tt.transition(self.tasks, self.results, self.archive, moves={"task-a.txt": gen},
                                render=_naming, prefix=PREFIX)
        self.assertEqual(len(out["errors"]), 1, out)
        self.assertIn("tasks/archive/ was replaced since pre-flight", out["errors"][0])
        self.assertEqual((out["result"], out["archived"]), (None, {}))
        self.assertEqual(sorted(outside.iterdir()), [], "nothing followed the symlink")
        self.assertIn("task: A", p.read_text())

    def test_an_uncreatable_archive_dir_publishes_nothing(self):
        _task(self.tasks, "task-a.txt", "A")
        self.archive.write_text("a file where the dir should be")
        out = self.run_transition(["task-a.txt"])
        self.assertIn("could not create", out["errors"][0])
        self.assertIn("nothing published, nothing moved", out["errors"][0])
        self.assertEqual((out["result"], self.notices()), (None, []))

    def test_nothing_to_move_publishes_without_a_journal(self):
        out = self.run_transition([], _notice("notice\n"))
        self.assertEqual(out["errors"], [])
        self.assertIsNotNone(out["result"])
        self.assertEqual(sorted(self.archive.iterdir()), [], "created empty, no journal")


class TestJournalRecovery(Base):
    """Kewei's round-6 contract: a crash after each persisted stage is recovered
    by the next pass exactly once — one copy of every generation, live or
    archived; one notice iff publication committed; nothing done twice."""

    def crash_after(self, stage: str):
        """A context in which the owner dies right after persisting `stage`
        (`claimed`, `placed`, `publishing`) or after publishing (`published`)."""
        real_set, real_publish = tt._Journal.set, tt._publish_at

        def set_then_crash(self_, **fields):
            real_set(self_, **fields)
            if fields.get("stage") == stage:
                raise Crash(stage)

        def publish_then_crash(*a, **kw):
            path = real_publish(*a, **kw)
            raise Crash("published")

        if stage == "published":
            return mock.patch.object(tt, "_publish_at", side_effect=publish_then_crash)
        return mock.patch.object(tt._Journal, "set", set_then_crash)

    def invariants(self, generation: str, ask: str, notice_committed: bool):
        copies = [x for x in list(self.tasks.iterdir()) + list(self.archive.iterdir())
                  if x.is_file() and not x.name.startswith(".") and tt.generation_of(x) == generation]
        self.assertEqual(len(copies), 1, f"exactly one copy of the generation: {copies}")
        self.assertIn(ask, copies[0].read_text())
        self.assertEqual(len(self.notices()), 1 if notice_committed else 0)
        self.assertEqual(self.journals(), [])
        self.assertEqual([x.name for x in self.archive.iterdir() if x.name.startswith(".")], [])

    def recover(self) -> dict:
        return self.run_transition([], _notice(None))

    def test_crash_after_the_claim_is_placed_and_published_by_the_next_pass(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("claimed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(self.notices(), [], "no notice before publication committed")
        self.assertTrue(any(x.name.startswith("task-a.txt.claim-") for x in self.tasks.iterdir()))
        self.assertEqual(json.loads(self.journals()[0].read_text())["stage"], "claimed")
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.assertIn("archived: task-a.txt->task-a.txt", Path(out["recovered"][0]["result"]).read_text())
        self.assertIn("Published by a later pass", Path(out["recovered"][0]["result"]).read_text())
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [], "a second recovery pass finds nothing")

    def test_crash_after_the_placement_is_found_by_generation_and_published(self):
        """The claim was placed but the journal still says `claimed`: recovery
        finds the archived copy by its generation, under its `.N` landing."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        self.archive.mkdir()
        (self.archive / "task-a.txt").write_text("earlier\n")
        real_place = tt.task_archive.place_claim

        def place_then_crash(*a, **kw):
            real_place(*a, **kw)
            raise Crash("after placement, before the journal")

        with mock.patch.object(tt.task_archive, "place_claim", side_effect=place_then_crash), \
                self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["recovered"][0]["landed"], {"task-a.txt": "task-a.txt.1"})
        body = Path(out["recovered"][0]["result"]).read_text()
        self.assertIn("task-a.txt is archived as tasks/archive/task-a.txt.1", body)
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual((self.archive / "task-a.txt").read_text(), "earlier\n")
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_after_the_placed_stage_publishes_once(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("placed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(self.notices(), [])
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_after_the_notice_name_was_reserved_publishes_at_that_name_once(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("publishing"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        reserved = json.loads(self.journals()[0].read_text())["result"]
        self.assertEqual(self.notices(), [])
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(Path(out["recovered"][0]["result"]).name, reserved)
        self.assertEqual(Path(out["recovered"][0]["result"]).read_text(), "archived: task-a.txt->task-a.txt\n")
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_crash_after_publication_sends_no_second_notice(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("published"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(len(self.journals()), 1, "the journal outlived the commit")
        out = self.recover()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["recovered"][0]["result"], str(self.notices()[0]))
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_a_finished_pass_leaves_nothing_for_recovery(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        self.invariants(gen, "task: A", notice_committed=True)
        self.assertEqual(self.recover()["recovered"], [])

    def test_a_failed_publish_is_finished_by_the_next_pass_without_moving_again(self):
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        self.results.rmdir()
        self.results.write_text("not a directory")
        out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["archived"], {"task-a.txt": "task-a.txt"})
        self.assertIn("result write failed", out["errors"][0])
        self.assertIn("published by the next pass", out["errors"][0])
        self.assertEqual(json.loads(self.journals()[0].read_text())["stage"], "publishing")
        self.results.unlink()
        self.results.mkdir()
        with mock.patch.object(tt.task_archive, "place_claim", side_effect=AssertionError("moved again")):
            again = self.recover()
        self.assertEqual(again["errors"], [])
        self.assertEqual(again["recovered"][0]["landed"], {"task-a.txt": "task-a.txt"})
        self.invariants(gen, "task: A", notice_committed=True)

    def test_a_live_passs_journal_is_never_taken(self):
        _task(self.tasks, "task-a.txt", "A")
        real = tt._Journal.create
        seen = []

        def create_then_peek(*a, **kw):
            j = real(*a, **kw)
            seen.append(self.recover()["recovered"])  # another pass runs while this one holds the lock
            return j

        with mock.patch.object(tt._Journal, "create", side_effect=create_then_peek):
            out = self.run_transition(["task-a.txt"], _naming)
        self.assertEqual(out["errors"], [])
        self.assertEqual(seen, [[]])
        self.assertEqual(len(self.notices()), 1)

    def test_the_handles_stay_open_across_recovery(self):
        """Recovery places through the handles opened at this pass's pre-flight:
        an archive dir swapped for a symlink in between is refused, not followed."""
        p = _task(self.tasks, "task-a.txt", "A")
        gen = tt.generation_of(p)
        with self.crash_after("claimed"), self.assertRaises(Crash):
            self.run_transition(["task-a.txt"], _naming)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        real = tt._Journal.take

        def take_then_swap(*a, **kw):
            j = real(*a, **kw)
            kept = {x.name: x.read_bytes() for x in self.archive.iterdir()}
            for x in self.archive.iterdir():
                x.unlink()
            self.archive.rmdir()
            self.archive.symlink_to(outside, target_is_directory=True)
            for n, b in kept.items():
                (outside / n).write_bytes(b)
            return j

        with mock.patch.object(tt._Journal, "take", side_effect=take_then_swap):
            out = self.recover()
        self.assertTrue(any("was replaced since pre-flight" in e for e in out["errors"]), out)
        self.assertEqual([x.name for x in outside.iterdir() if not x.name.startswith(".")], [])
        self.assertTrue(any(x.name.startswith("task-a.txt.claim-") for x in self.tasks.iterdir()),
                        "the claim is still there for the next recovery")

    def test_pending_notices_lists_the_journals_read_only(self):
        self.archive.mkdir()
        (self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json").write_text("{}")
        (self.archive / "task-old.txt").write_text("x")
        self.assertEqual(tt.pending_notices(self.archive), [f"{tt.JOURNAL_PREFIX}1-2-abc.json"])
        self.assertEqual(tt.pending_notices(self.ws / "nowhere"), [])

    def test_an_unreadable_journal_is_reported_and_left(self):
        self.archive.mkdir()
        bad = self.archive / f"{tt.JOURNAL_PREFIX}1-2-abc.json"
        bad.write_text("not json")
        out = self.recover()
        self.assertIn("is unreadable; left for the operator", out["errors"][0])
        self.assertTrue(bad.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
