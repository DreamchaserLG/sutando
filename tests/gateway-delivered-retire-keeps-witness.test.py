#!/usr/bin/env python3
"""A delivered proactive notice keeps its inode linked and is never sent twice.

The orphan-check publisher (src/task_transition.py) proves a commit by a witness link
of the notice's inode, so a delivered notice must never be unlinked. And a claimed
notice whose outbox item says DELIVERED, or whose record cannot be read cleanly, must
never be re-queued by the restart's orphan recovery. Drives the real gateway drain
(`_post_proactive`), its outbox lane, the real orphan recovery, and the real transition.

Run: python3 tests/gateway-delivered-retire-keeps-witness.test.py
"""
from __future__ import annotations

import contextlib
import os
import signal
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import()

from _helpers.proactive_lane import Lane, scratch  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import task_transition as tt  # noqa: E402
from ag2_sparrow import outbox  # noqa: E402

# A retained original's writer must hold still for SETTLE_SEC before its bytes are taken; the
# readiness tests below restore that, every other test here pins another property and skips the wait.
gb._DECIDED_SETTLE_SEC = 0.0

PREFIX = "proactive-orphan-recovery-"


class Crash(RuntimeError):
    """The publisher dies here."""


class Killed(BaseException):
    """The process dies here: no handler in the code under test may absorb it."""


class Blocked(BaseException):
    """The deadline passed while the code under test was still blocked."""


@contextlib.contextmanager
def _deadline(seconds: float):
    """Interrupt a blocked syscall in the main thread (coverage traces only that thread here)."""
    def fire(*_):
        raise Blocked()
    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


_LIMIT = getattr(gb, "_CLAIM_READ_LIMIT", 1 << 20)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@unittest.skipIf(os.geteuid() == 0, "root renames into a mode-555 directory")
class DeliveredNoticeKeepsItsLinkAndIsNeverResent(unittest.TestCase):
    def setUp(self):
        self.lane = Lane(gb, scratch("gw-delivered-witness-"))
        self.archive = self.lane.results / "archive"

    def transition(self, names=()):
        moves = {n: tt.generation_of(self.lane.tasks / n) for n in names}
        return tt.transition(self.lane.tasks, self.lane.results, self.lane.tasks / "archive", moves=moves,
                             render=lambda landed: "archived: A\n" if landed else None, prefix=PREFIX)

    def unwritable_archive(self):
        self.archive.chmod(0o555)
        self.addCleanup(self.archive.chmod, 0o755)

    def no_hiding(self):
        """Renames into the archive or to a hidden name fail; the drain's own claim still works."""
        real = Path.rename

        def blocked(dst) -> bool:
            dst = Path(dst)
            return dst.parent == self.archive or dst.name.startswith(".")

        def rename(src, dst):
            if blocked(dst):
                raise OSError(5, "Input/output error")
            return real(src, dst)

        real_noreplace = gb.undelivered_quarantine.rename_noreplace

        def noreplace(src, dst, *a, **kw):
            if blocked(dst):
                raise OSError(5, "Input/output error")
            return real_noreplace(src, dst, *a, **kw)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(Path, "rename", rename))
        stack.enter_context(mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", noreplace))
        return stack

    def test_a_delivered_notice_that_cannot_be_archived_is_not_sent_again(self):
        (self.lane.tasks / "task-a.txt").write_text("timestamp: 2026-09-11T13:25:00Z\nsource: chat\ntask: A\n")
        real_publish = tt._publish_at

        def publish_then_die(*a, **kw):
            real_publish(*a, **kw)
            raise Crash("killed after the commit, before the journal went")

        with mock.patch.object(tt, "_publish_at", side_effect=publish_then_die), self.assertRaises(Crash):
            self.transition(["task-a.txt"])
        (notice,) = sorted(self.lane.results.glob(f"{PREFIX}*.txt"))
        self.unwritable_archive()
        self.lane.drain()
        self.assertEqual(len(self.lane.posts), 1, "delivered once")
        self.assertFalse(notice.exists(), "retired from the live name")
        witness = next(self.lane.results.glob(".*.witness"))
        self.assertEqual(os.stat(witness).st_nlink, 2, "the delivered inode is still linked")
        out = self.transition()
        self.assertEqual(out["errors"], [], out)
        self.assertEqual(sorted(self.lane.results.glob(f"{PREFIX}*.txt")), [], "no second notice")
        self.lane.drain()
        self.assertEqual(len(self.lane.posts), 1, "nothing more is sent")

    def test_a_dead_owners_delivered_claim_is_not_read_as_absent_and_requeued(self):
        """Mini's probe: the item id is #5857's `<stem>~<body digest>`, and its record says DELIVERED."""
        self.lane.write("proactive-1.txt", "hello owner\n")
        self.unwritable_archive()
        with self.no_hiding():
            self.lane.drain()
        self.assertEqual(len(self.lane.posts), 1)
        (claim,) = self.lane.results.glob("proactive-1.sending.*")
        item_id = gb._proactive_item_id("proactive-1", "hello owner")
        self.assertEqual(outbox.receipt_state(self.lane.results / ".outbox-ag2space-proactive", item_id),
                         "DELIVERED")
        dead = claim.with_name(f"proactive-1.sending.{_dead_pid()}.1")
        claim.rename(dead)
        self.lane.restart()
        self.assertFalse((self.lane.results / "proactive-1.txt").exists(), "not re-queued")
        (hidden,) = self.lane.results.glob(".proactive-1.delivered-*")
        self.assertEqual(hidden.read_text(), "hello owner\n")
        self.archive.chmod(0o755)
        self.lane.drain()
        self.assertEqual(len(self.lane.posts), 1, "never sent twice")
        self.assertEqual([p.read_text() for p in self.archive.iterdir()], ["hello owner\n"])
        self.assertEqual(list(self.lane.results.glob(".proactive-1.delivered-*")), [])

    def test_with_no_rename_possible_a_delivered_claim_keeps_its_name(self):
        self.lane.write("proactive-1.txt", "hello owner\n")
        self.unwritable_archive()
        with self.no_hiding():
            self.lane.drain()
        (claim,) = self.lane.results.glob("proactive-1.sending.*")
        self.assertEqual(claim.read_text(), "hello owner\n", "the delivered inode keeps a name")

    def dead_claim_with_record(self, spoil) -> Path:
        item_id = gb._proactive_item_id("proactive-1", "delivered")
        root = self.lane.results / ".outbox-ag2space-proactive"
        outbox.record_delivered(root, item_id, provider="ag2space-proactive")
        record = outbox._item_path(root, item_id)
        spoil(record)
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("delivered\n")
        self.lane.restart()
        return claim

    def assert_kept(self, claim: Path) -> None:
        self.assertFalse((self.lane.results / "proactive-1.txt").exists(), "not re-queued for a second send")
        self.assertEqual(claim.read_text(), "delivered\n")

    def test_an_unreadable_delivered_receipt_never_requeues_the_claim(self):
        def unreadable(record):
            record.chmod(0)
            self.addCleanup(record.chmod, 0o644)

        self.assert_kept(self.dead_claim_with_record(unreadable))

    def test_a_torn_delivered_receipt_never_requeues_the_claim(self):
        self.assert_kept(self.dead_claim_with_record(lambda r: r.write_text('{"status": "DELIV')))

    def test_a_record_claiming_to_be_absent_never_requeues_the_claim(self):
        self.assert_kept(self.dead_claim_with_record(lambda r: r.write_text('{"status": "absent"}')))

    def test_a_delivered_claim_that_cannot_be_hidden_keeps_its_claim_name(self):
        item_id = gb._proactive_item_id("proactive-1", "delivered")
        outbox.record_delivered(self.lane.results / ".outbox-ag2space-proactive", item_id)
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("delivered\n")
        with self.no_hiding():
            self.lane.restart()
        self.assert_kept(claim)

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_claim_is_never_requeued(self):
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("delivered\n")
        claim.chmod(0)
        self.addCleanup(claim.chmod, 0o644)
        self.lane.restart()
        self.assertFalse((self.lane.results / "proactive-1.txt").exists(), "it may have been sent")
        self.assertTrue(claim.exists())

    def test_a_skip_marker_claim_was_never_an_item_and_is_requeued(self):
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("[no-send]\n")
        self.lane.restart()
        self.assertEqual((self.lane.results / "proactive-1.txt").read_text(), "[no-send]\n")

    def test_a_dead_owners_claim_follows_its_recorded_lifecycle_state(self):
        """PARKED is terminal (parked, never live, never resent); QUEUED/READY/CLAIMED go back live
        and the outbox settles them (at most one POST, from main's crash-receipt path)."""
        root = self.lane.results / ".outbox-ag2space-proactive"
        for status in ("QUEUED", "READY", "CLAIMED", "PARKED"):
            with self.subTest(status=status):
                stem = f"proactive-{status.lower()}"
                item_id = gb._proactive_item_id(stem, status)
                outbox._write_item(root, item_id, {"item_id": item_id, "status": status, "attempts": 0,
                                              "payload": f"{status}\n"})
                claim = self.lane.results / f"{stem}.sending.{_dead_pid()}.1"
                claim.write_text(f"{status}\n")
                before = len(self.lane.posts)
                self.lane.restart()
                live = self.lane.results / f"{stem}.txt"
                if status == "PARKED":
                    self.assertFalse(live.exists(), "PARKED never goes back live")
                    self.assertFalse(claim.exists())
                    parked = list((self.lane.results / "undelivered").glob(f"{stem}-*.txt"))
                    self.assertEqual([p.read_text() for p in parked], ["PARKED\n"])
                    self.lane.drain()
                    self.assertEqual(len(self.lane.posts), before, "PARKED is never resent")
                else:
                    self.assertEqual(live.read_text(), f"{status}\n", "back live for the outbox to settle")
                    self.assertFalse(claim.exists())
                    self.lane.drain()
                    self.assertLessEqual(len(self.lane.posts) - before, 1, "at most one POST")
                    self.assertFalse(live.exists(), "settled by the drain")

    def test_a_claim_replaced_after_its_receipt_was_read_is_not_hidden_as_delivered(self):
        item_id = gb._proactive_item_id("proactive-1", "delivered")
        outbox.record_delivered(self.lane.results / ".outbox-ag2space-proactive", item_id)
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("delivered\n")
        real = gb._claim_receipt

        def read_then_replace(*a, **kw):
            got = real(*a, **kw)
            tmp = claim.with_name(".swap")
            tmp.write_text("UNSENT BODY B\n")
            os.replace(tmp, claim)
            return got

        with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_replace):
            self.lane.restart()
        hidden = list(self.lane.results.glob(".proactive-1.delivered-*"))
        self.assertFalse(any("UNSENT BODY B" in h.read_text() for h in hidden), "B was never sent")
        self.assertFalse((self.lane.results / "proactive-1.txt").exists())
        self.assertEqual(claim.read_text(), "UNSENT BODY B\n", "kept for an operator")

    def test_a_claim_that_vanishes_or_is_a_symlink_has_no_generation_and_is_unknown(self):
        target = self.lane.results / "elsewhere"
        target.write_text("x\n")
        link = self.lane.results / "proactive-1.sending.1.1"
        link.symlink_to(target)
        self.assertEqual(gb._claim_receipt(link, "proactive-1"), ("unknown", None, None))

    def test_an_operator_requeue_between_the_parked_receipt_and_the_move_keeps_the_body_live(self):
        root = self.lane.results / ".outbox-ag2space-proactive"
        item_id = gb._proactive_item_id("proactive-1", "parked body")
        outbox._write_item(root, item_id, {"item_id": item_id, "status": "PARKED", "attempts": 0,
                                           "payload": "parked body\n"})
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("parked body\n")
        real = gb._claim_receipt
        seen = []

        def read_then_operator_requeues(*a, **kw):
            got = real(*a, **kw)
            if not seen:
                seen.append(got[0])
                self.assertEqual(outbox.requeue_item_with_epoch(root, item_id)[0], outbox.RequeueOutcome.REQUEUED)
                self.assertEqual(gb.undelivered_quarantine.restore(self.lane.results, "proactive-1")[0],
                                 gb.undelivered_quarantine.RestoreOutcome.NOTHING_QUARANTINED)
            return got

        with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_operator_requeues):
            self.lane.restart()
        self.assertEqual(seen, ["PARKED"])
        self.assertEqual((self.lane.results / "proactive-1.txt").read_text(), "parked body\n",
                         "an acknowledged requeue leaves its body live")
        self.assertEqual(list((self.lane.results / "undelivered").glob("proactive-1-*.txt")), [])
        self.assertFalse(claim.exists())

    def test_a_still_parked_claim_stays_parked_after_the_recheck(self):
        root = self.lane.results / ".outbox-ag2space-proactive"
        item_id = gb._proactive_item_id("proactive-1", "parked body")
        outbox._write_item(root, item_id, {"item_id": item_id, "status": "PARKED", "attempts": 0,
                                           "payload": "parked body\n"})
        (self.lane.results / f"proactive-1.sending.{_dead_pid()}.1").write_text("parked body\n")
        self.lane.restart()
        self.assertFalse((self.lane.results / "proactive-1.txt").exists())
        self.assertEqual(len(list((self.lane.results / "undelivered").glob("proactive-1-*.txt"))), 1)

    def test_a_claim_with_no_record_at_all_is_still_requeued(self):
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("never sent\n")
        self.lane.restart()
        self.assertEqual((self.lane.results / "proactive-1.txt").read_text(), "never sent\n")


    def test_a_dead_owners_fifo_claim_never_wedges_the_restart(self):
        fifo = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        os.mkfifo(fifo)
        try:
            with _deadline(2.0):
                self.lane.restart()
        except Blocked:
            self.fail("the restart blocked opening a FIFO claim")
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode), "kept as its claim for an operator")
        self.assertFalse((self.lane.results / "proactive-1.txt").exists())

    def _delivered_claim_swapped_to_unsent_b(self):
        item_id = gb._proactive_item_id("proactive-1", "delivered A")
        outbox.record_delivered(self.lane.results / ".outbox-ag2space-proactive", item_id)
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("delivered A\n")
        real = gb._claim_receipt

        def read_then_replace(*a, **kw):
            got = real(*a, **kw)
            if got[0] == "DELIVERED":
                tmp = claim.with_name(".swap")
                tmp.write_text("UNSENT B\n")
                os.replace(tmp, claim)
            return got
        return claim, read_then_replace

    def _b_is_never_archived_unsent(self):
        hidden = [h.read_text() for h in self.lane.results.glob(".proactive-1.delivered-*")]
        self.assertNotIn("UNSENT B\n", hidden, "unsent B hidden as delivered")
        for _ in range(2):
            self.lane.drain()
        archived = [p.read_text() for p in (self.lane.results / "archive").glob("proactive-1*")]
        posted = [p.get("body") for p in self.lane.posts].count("UNSENT B")
        self.assertEqual(archived.count("UNSENT B\n"), posted, f"B archived without its POST ({posted})")
        self.assertLessEqual(posted, 1)

    def test_a_crash_right_after_the_first_rename_never_archives_unsent_bytes(self):
        claim, read_then_replace = self._delivered_claim_swapped_to_unsent_b()
        real_move = gb.undelivered_quarantine.rename_noreplace

        def die_after_first_rename(src, dst):
            real_move(src, dst)
            raise Killed("killed after the first rename")

        with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_replace), \
                mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", die_after_first_rename):
            try:  # a replaced claim is never renamed now, so there may be no rename to die after
                self.lane.restart()
            except Killed:
                pass
        dead = _dead_pid()
        for c in self.lane.results.glob("proactive-1.sending.*"):  # its owner died with the crash
            rest = c.name.split(".sending.", 1)[1].split(".", 1)
            c.rename(c.with_name(f"proactive-1.sending.{dead}.{rest[1] if len(rest) > 1 else '0'}"))
        self._b_is_never_archived_unsent()
        self.lane.restart()
        self._b_is_never_archived_unsent()

    def test_a_failed_rollback_and_set_aside_never_leaves_unsent_bytes_as_delivered(self):
        claim, read_then_replace = self._delivered_claim_swapped_to_unsent_b()
        real_move = gb.undelivered_quarantine.rename_noreplace
        calls = []

        def only_the_first_rename_succeeds(src, dst):
            calls.append(src)
            if len(calls) == 1:
                return real_move(src, dst)
            raise FileExistsError(17, "File exists")

        with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_replace), \
                mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", only_the_first_rename_succeeds):
            self.lane.restart()
        self._b_is_never_archived_unsent()

    def test_a_parked_record_that_vanishes_before_the_recheck_is_never_resent(self):
        root = self.lane.results / ".outbox-ag2space-proactive"
        item_id = gb._proactive_item_id("proactive-1", "parked body")
        outbox._write_item(root, item_id, {"item_id": item_id, "status": "PARKED", "attempts": 0,
                                           "payload": "parked body\n"})
        (self.lane.results / f"proactive-1.sending.{_dead_pid()}.1").write_text("parked body\n")
        real = gb._claim_receipt
        receipts = []

        def delete_after_the_parked_read(*a, **kw):
            got = real(*a, **kw)
            receipts.append(got[0])
            if got[0] == "PARKED":
                outbox._item_path(root, item_id).unlink()
            return got

        with mock.patch.object(gb, "_claim_receipt", side_effect=delete_after_the_parked_read):
            self.lane.restart()
        self.assertEqual(receipts, ["PARKED", "absent"])
        self.assertFalse((self.lane.results / "proactive-1.txt").exists(), "never handed back live")
        self.assertEqual(len(list((self.lane.results / "undelivered").glob("proactive-1-*.txt"))), 1)
        self.lane.drain()
        self.assertEqual(self.lane.posts, [], "a PARKED send is never repeated")


    def _bad_claim(self, make, fault=contextlib.nullcontext):
        """The reader on one bad input: it returns "unknown" within a deadline, closes every
        descriptor, reads at most the bound, and a real restart keeps the claim and its bytes."""
        lane = Lane(gb, scratch("gw-claim-reader-"))
        claim = lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        after = make(lane.results, claim)
        read = []
        before = len(os.listdir("/dev/fd"))
        try:
            with _deadline(1.0), fault():
                real_read = os.read  # the fault's, when it replaces os.read

                def counted(fd, n):
                    got = real_read(fd, n)
                    read.append(len(got))
                    return got
                with mock.patch.object(os, "read", counted):
                    state = gb._claim_receipt(claim, "proactive-1")[0]
        except Blocked:
            self.fail("the reader blocked")
        self.assertEqual(state, "unknown", "fails closed")
        self.assertEqual(len(os.listdir("/dev/fd")), before, "every branch closes its descriptor")
        self.assertLessEqual(sum(read), _LIMIT + 1, "the read is bounded")
        if callable(after):
            after()
        return lane, claim, read

    def _restart_keeps(self, lane, claim):
        try:
            with _deadline(2.0):
                lane.restart()
        except Blocked:
            self.fail("the restart blocked")
        self.assertTrue(os.path.lexists(claim), "kept as its claim for an operator")
        self.assertFalse((lane.results / "proactive-1.txt").exists(), "never handed back live")

    def test_claim_reader_fifo_without_a_writer(self):
        lane, claim, _ = self._bad_claim(lambda res, c: os.mkfifo(c))
        self._restart_keeps(lane, claim)

    def test_claim_reader_fifo_with_queued_bytes_consumes_none(self):
        holder = []

        def make(res, c):
            os.mkfifo(c)
            holder.append(os.open(c, os.O_RDWR | os.O_NONBLOCK))
            os.write(holder[0], b"queued bytes")

            def after():
                self.assertEqual(os.read(holder[0], 64), b"queued bytes", "no byte was consumed")
            return after
        try:
            self._bad_claim(make)
        finally:
            for fd in holder:
                os.close(fd)

    def test_claim_reader_directory(self):
        lane, claim, read = self._bad_claim(lambda res, c: c.mkdir())
        self.assertEqual(read, [], "nothing is read")
        self._restart_keeps(lane, claim)

    def test_claim_reader_device(self):
        real_fstat = os.fstat

        def as_device(fd):
            st = list(real_fstat(fd))
            st[0] = stat.S_IFCHR | 0o600
            return os.stat_result(st)
        _, _, read = self._bad_claim(lambda res, c: c.write_text("never sent\n"),
                                     lambda: mock.patch.object(os, "fstat", as_device))
        self.assertEqual(read, [], "nothing is read")

    def test_claim_reader_symlink_to_a_regular_file_leaves_the_target_untouched(self):
        def make(res, c):
            target = res / "elsewhere"
            target.write_text("never sent\n")
            mtime = target.stat().st_mtime_ns
            c.symlink_to(target)
            return lambda: self.assertEqual((target.read_text(), target.stat().st_mtime_ns), ("never sent\n", mtime))
        lane, claim, read = self._bad_claim(make)
        self.assertEqual(read, [], "nothing is read")
        self._restart_keeps(lane, claim)

    def test_claim_reader_symlink_to_a_fifo_leaves_its_writer_and_bytes_untouched(self):
        holder = []

        def make(res, c):
            target = res / "elsewhere.fifo"
            os.mkfifo(target)
            holder.append(os.open(target, os.O_RDWR | os.O_NONBLOCK))
            os.write(holder[0], b"writer bytes")
            c.symlink_to(target)

            def after():
                self.assertEqual(os.read(holder[0], 64), b"writer bytes", "the writer's bytes are untouched")
                os.write(holder[0], b"still open")  # the writer end is intact
                self.assertEqual(os.read(holder[0], 64), b"still open")
            return after
        try:
            lane, claim, _ = self._bad_claim(make)
            self._restart_keeps(lane, claim)
        finally:
            for fd in holder:
                os.close(fd)

    def test_claim_reader_oversized_consumes_nothing(self):
        lane, claim, read = self._bad_claim(lambda res, c: c.write_bytes(b"x" * (_LIMIT + 1)))
        self.assertEqual(read, [], "refused from fstat() before any read")
        self._restart_keeps(lane, claim)

    def test_claim_reader_bounds_a_file_that_grew_after_fstat(self):
        real_fstat = os.fstat

        def stale(fd):
            st = list(real_fstat(fd))
            st[6] = 12  # st_size as fstat() saw it before the file grew
            return os.stat_result(st)
        _, _, read = self._bad_claim(lambda res, c: c.write_bytes(b"x" * (_LIMIT + 4096)),
                                     lambda: mock.patch.object(os, "fstat", stale))
        self.assertEqual(sum(read), _LIMIT + 1, "reads exactly one byte past the bound, then stops")

    def test_claim_reader_read_error(self):
        def failing(*a):
            raise OSError(5, "Input/output error")
        self._bad_claim(lambda res, c: c.write_text("never sent\n"),
                        lambda: mock.patch.object(os, "read", failing))

    def test_claim_reader_without_a_no_follow_primitive(self):
        self._bad_claim(lambda res, c: c.write_text("never sent\n"),
                        lambda: mock.patch.object(os, "O_NOFOLLOW", 0))

    def _b_survives(self, lane, a_body: str, kind: str) -> None:
        """B was never sent, so a second real drain claims and delivers it once under its own item
        key, while A keeps its terminal state; a further restart and drain resurrect neither."""
        hidden = [h.read_text() for h in lane.results.glob(".proactive-1.delivered-*")]
        self.assertNotIn("UNSENT B\n", hidden, "unsent B hidden as delivered")
        root = lane.results / ".outbox-ag2space-proactive"
        a_id, b_id = gb._proactive_item_id("proactive-1", a_body), gb._proactive_item_id("proactive-1", "UNSENT B")
        self.assertNotEqual(a_id, b_id)
        for _ in range(2):
            lane.drain()
        bodies = [p.get("body") for p in lane.posts]
        archived = [p.read_text() for p in (lane.results / "archive").glob("proactive-1*")]
        self.assertEqual(archived.count("UNSENT B\n"), bodies.count("UNSENT B"), "B archived unsent")
        self.assertEqual(bodies.count("UNSENT B"), 1, f"B stayed eligible and was sent once ({bodies})")
        self.assertFalse(any((lane.results / "undelivered").glob("proactive-1*")), "B was never parked")
        settled = (outbox.receipt_state(root, a_id), outbox.receipt_state(root, b_id))
        self.assertEqual(settled[0], kind, f"A ({a_id}) keeps its terminal state")
        self.assertEqual(settled[1], "DELIVERED", f"B ({b_id}) reached one terminal delivery")
        lane.restart()
        for _ in range(2):
            lane.drain()
        self.assertEqual([p.get("body") for p in lane.posts], bodies, "a fresh restart and drain send nothing")
        self.assertNotIn(a_body, bodies, "A is never resent")
        self.assertEqual((outbox.receipt_state(root, a_id), outbox.receipt_state(root, b_id)), settled,
                         "no scanner, recovery or retry path moved A or B")
        self.assertEqual(lane.where("proactive-1"), "archived")

    def _settled_once(self, lane, a_body: str, kind: str) -> None:
        """After the owner's death: a restart and real drains send B once under its own key, A keeps
        its terminal state and every placed copy of A is A's exact bytes; a further pass moves nothing."""
        dead = _dead_pid()
        for c in lane.results.glob("proactive-1.sending.*"):
            rest = c.name.split(".sending.", 1)[1].split(".", 1)
            c.rename(c.with_name(f"proactive-1.sending.{dead}.{rest[1] if len(rest) > 1 else '0'}"))
        lane.restart()
        root = lane.results / ".outbox-ag2space-proactive"
        a_id, b_id = gb._proactive_item_id("proactive-1", a_body), gb._proactive_item_id("proactive-1", "UNSENT B")
        for _ in range(2):
            lane.drain()
        bodies = [p.get("body") for p in lane.posts]
        self.assertEqual(bodies, ["UNSENT B"], f"B sent once, A never ({bodies})")
        self.assertEqual((outbox.receipt_state(root, a_id), outbox.receipt_state(root, b_id)), (kind, "DELIVERED"))
        placed = [p.read_text() for p in list(lane.results.glob(".proactive-1.delivered-*"))
                  + list((lane.results / "undelivered").glob("proactive-1-*"))]
        archived = [p.read_text() for p in (lane.results / "archive").glob("proactive-1*")]
        self.assertNotIn("UNSENT B\n", placed, "B never hidden as delivered or parked")
        self.assertEqual(archived.count("UNSENT B\n"), 1, "B archived once, after its POST")
        self.assertTrue(all(t == a_body + "\n" for t in placed + [a for a in archived if a != "UNSENT B\n"]),
                        f"every placed copy of A is A's bytes ({placed}, {archived})")
        lane.restart()
        for _ in range(2):
            lane.drain()
        self.assertEqual([p.get("body") for p in lane.posts], bodies, "a fresh restart and drains send nothing")
        self.assertEqual((outbox.receipt_state(root, a_id), outbox.receipt_state(root, b_id)), (kind, "DELIVERED"))

    def test_a_writer_holding_the_claims_descriptor_never_reaches_a_placed_copy(self):
        """B written through a descriptor opened before recovery, at each cut of the placement
        protocol (check -> semantic rename of a private copy -> retain), with and without a crash."""
        cuts = (("before-placement", False), ("after-placement", False), ("after-placement", True),
                ("after-retain", False), ("after-retain", True))
        for kind in ("DELIVERED", "PARKED"):
            for cut, crash in cuts:
                with self.subTest(kind=kind, cut=cut, crash=crash):
                    lane = Lane(gb, scratch("gw-retained-fd-"))
                    root = lane.results / ".outbox-ag2space-proactive"
                    a_body = f"{kind.lower()} A"
                    a_id = gb._proactive_item_id("proactive-1", a_body)
                    outbox._write_item(root, a_id, {"item_id": a_id, "status": kind, "attempts": 0,
                                                    "payload": a_body + "\n"})
                    claim = lane.results / f"proactive-1.sending.{_dead_pid()}.1"
                    claim.write_text(a_body + "\n")
                    writer = os.open(claim, os.O_WRONLY)  # retained before recovery stages anything
                    self.addCleanup(os.close, writer)

                    def write_b():
                        os.ftruncate(writer, 0)
                        os.pwrite(writer, b"UNSENT B\n", 0)
                    real_move = gb.undelivered_quarantine.rename_noreplace
                    real_publish = getattr(gb, "_publish_text_whole", None)

                    def around(dst: Path, commit):
                        semantic = dst.parent.name == "undelivered" or ".delivered-" in dst.name
                        if semantic and cut == "before-placement":
                            write_b()  # the check has returned; the semantic commit has not run
                        out = commit()
                        if (semantic and cut == "after-placement") or (".decided-" in dst.name and cut == "after-retain"):
                            write_b()
                            if crash:
                                raise Killed(f"killed {cut}")
                        return out

                    def move(src, dst):
                        return around(Path(dst), lambda: real_move(src, dst))

                    def publish(dst, *a, **k):
                        return around(Path(dst), lambda: real_publish(dst, *a, **k))
                    publish_patch = (mock.patch.object(gb, "_publish_text_whole", publish) if real_publish
                                     else contextlib.nullcontext())
                    with mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", move), publish_patch:
                        try:
                            lane.restart()
                        except Killed:
                            pass
                    self._settled_once(lane, a_body, kind)

    def _sent_record(self, body: str) -> dict:
        """The outbox record a real drain writes for `body`, as the item a later park would keep."""
        lane = Lane(gb, scratch("gw-record-"))
        lane.write("proactive-1.txt", body)
        lane.drain()
        root = lane.results / ".outbox-ag2space-proactive"
        record = outbox.read_item(root, gb._proactive_item_id("proactive-1", body.strip()))
        return {k: v for k, v in record.items() if k not in ("provider_ref", "delivered_at", "provider")}

    def _requeue_by_cli(self, lane, item_id: str) -> "subprocess.CompletedProcess":
        root = lane.results / ".outbox-ag2space-proactive"
        cmd = gb._proactive_requeue_command(root, item_id, __import__("hashlib").sha256(b"parked body\n").hexdigest())
        argv = [sys.executable if c == "python3" else c for c in __import__("shlex").split(cmd)]
        from _helpers.hermetic_env import isolated_child_env
        return subprocess.run(argv, cwd=REPO, env=isolated_child_env(), capture_output=True, text=True, timeout=60)

    def test_an_acknowledged_requeue_of_a_body_being_parked_is_sent_once_after_any_crash(self):
        """The real CLI requeues the item after recovery read PARKED (epoch 0) and before it moves the
        only body; a crash right after that move, or after the newer epoch is seen but before the hand
        back, must still end in exactly one POST under epoch 1 after a restart and real drains."""
        for cut in ("after-the-move", "before-the-hand-back"):
            with self.subTest(cut=cut):
                lane = Lane(gb, scratch("gw-requeue-crash-"))
                root = lane.results / ".outbox-ag2space-proactive"
                item_id = gb._proactive_item_id("proactive-1", "parked body")
                outbox._write_item(root, item_id, {**self._sent_record("parked body\n"), "status": "PARKED"})
                (lane.results / f"proactive-1.sending.{_dead_pid()}.1").write_text("parked body\n")
                real, cli = gb._claim_receipt, []

                def read_then_requeue(claim, stem):
                    got = real(claim, stem)
                    if not cli:
                        cli.append(self._requeue_by_cli(lane, item_id))
                    return got
                real_move, logs = gb.undelivered_quarantine.rename_noreplace, []

                def die_after_the_move(src, dst):
                    real_move(src, dst)
                    if Path(dst).parent.name == "undelivered":
                        raise Killed("killed after the move")

                real_publish = getattr(gb, "_publish_text_whole", None)

                def die_after_publishing_into_undelivered(dst, *a, **k):
                    out = real_publish(dst, *a, **k)
                    if Path(dst).parent.name == "undelivered":
                        raise Killed("killed after the move")
                    return out

                def die_before_the_hand_back(*a, **k):
                    raise Killed("killed before the hand back")
                def after_the_move():
                    stack = contextlib.ExitStack()  # patches apply on entry, so build it only when used
                    stack.enter_context(
                        mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", die_after_the_move))
                    if real_publish:
                        stack.enter_context(
                            mock.patch.object(gb, "_publish_text_whole", die_after_publishing_into_undelivered))
                    return stack
                fault = (after_the_move()
                         if cut == "after-the-move" else
                         mock.patch.object(gb, "_hand_back_decided" if hasattr(gb, "_hand_back_decided")
                                           else "_hand_back", die_before_the_hand_back))
                with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_requeue), fault, \
                        mock.patch.object(gb, "_log", logs.append):
                    with self.assertRaises(Killed):
                        lane.restart()
                self.assertEqual(cli[0].returncode, 0, cli[0].stderr)
                self.assertIn("result: requeued", cli[0].stdout)
                self.assertIn("body: nothing-quarantined", cli[0].stdout, "the only body was still a claim")
                self.assertFalse(any("handed back" in m for m in logs), "no success logged before the hand back")
                self.assertEqual(outbox.receipt(root, item_id), ("QUEUED", 1))
                dead = _dead_pid()
                for c in lane.results.glob("proactive-1.sending.*"):
                    rest = c.name.split(".sending.", 1)[1].split(".", 1)
                    c.rename(c.with_name(f"proactive-1.sending.{dead}.{rest[1] if len(rest) > 1 else '0'}"))
                lane.restart()
                for _ in range(4):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["parked body"], "exactly one POST")
                self.assertEqual(outbox.receipt(root, item_id), ("DELIVERED", 1), "delivered under epoch 1")
                self.assertEqual(list((lane.results / "undelivered").glob("proactive-1-*.txt")), [])
                lane.restart()
                for _ in range(2):
                    lane.drain()
                self.assertEqual(len(lane.posts), 1, "a further restart and drains send nothing")

    def _held_original(self, kind: str):
        """A dead owner's terminal claim A, with a writer's descriptor held on its inode across recovery."""
        lane = Lane(gb, scratch("gw-later-generations-"))
        root = lane.results / ".outbox-ag2space-proactive"
        a_body = f"{kind.lower()} A"
        a_id = gb._proactive_item_id("proactive-1", a_body)
        outbox._write_item(root, a_id, {"item_id": a_id, "status": kind, "attempts": 0, "payload": a_body + "\n"})
        claim = lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text(a_body + "\n")
        writer = os.open(claim, os.O_WRONLY)
        self.addCleanup(os.close, writer)
        original = os.fstat(writer).st_ino

        def write(body: bytes):
            os.ftruncate(writer, 0)
            os.pwrite(writer, body, 0)

        def visible_inodes():
            paths = [*lane.results.glob("proactive-1*"), *lane.results.glob(".proactive-1.delivered-*"),
                     *(lane.results / "archive").glob("proactive-1*"), *(lane.results / "undelivered").glob("proactive-1*")]
            return {os.lstat(p).st_ino for p in paths}
        return lane, root, a_id, write, lambda: self.assertNotIn(original, visible_inodes(), "the original stays hidden")

    def test_a_generation_written_after_restart_returns_is_sent_once_by_ordinary_drains(self):
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT B\n")
                for _ in range(6):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "B sent once, with no restart")
                hidden()
                lane.restart()
                for _ in range(3):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "no repeat")
                self.assertEqual(outbox.receipt_state(root, a_id), kind, "A keeps its disposition")

    def test_a_generation_written_after_the_last_was_delivered_is_sent_and_leaves_its_archive(self):
        """B is delivered first (by drains, or as the reviewed head does, by a second restart); C is
        then written through the same descriptor and must go out once without touching B's archive."""
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT B\n")
                for _ in range(3):
                    lane.drain()
                lane.restart()
                for _ in range(3):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"])
                b_archive = [p for p in (lane.results / "archive").glob("proactive-1*") if p.read_text() == "UNSENT B\n"]
                self.assertEqual(len(b_archive), 1)
                write(b"UNSENT C\n")
                for _ in range(6):
                    lane.drain()
                lane.restart()
                for _ in range(6):
                    lane.drain()
                self.assertEqual(b_archive[0].read_text(), "UNSENT B\n", "B's archive keeps B's bytes")
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B", "UNSENT C"], "C sent once")
                hidden()
                self.assertEqual(outbox.receipt_state(root, a_id), kind, "A keeps its disposition")
                for body in ("UNSENT B", "UNSENT C"):
                    self.assertEqual(outbox.receipt_state(root, gb._proactive_item_id("proactive-1", body)), "DELIVERED")

    def _fault_then_clear(self, fault):
        """B written after restart; three drains under `fault`, then ordinary drains with it cleared."""
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT B\n")
                real, calls = gb._reconcile_decided, []

                def counted(r):
                    calls.append(r.name)
                    return real(r)
                with fault(), mock.patch.object(gb, "_reconcile_decided", counted):
                    for n in range(3):
                        lane.drain()
                        self.assertEqual(len(calls), n + 1, "each failed drain retries once, no storm")
                self.assertEqual(lane.posts, [], "nothing sent while the fault holds")
                for _ in range(2):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"],
                                 "a later ordinary drain sends B once the fault clears")
                for _ in range(3):
                    lane.drain()
                lane.restart()
                lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "no repeat")
                self.assertEqual(outbox.receipt_state(root, a_id), kind)
                hidden()

    def test_a_snapshot_that_cannot_be_placed_is_retried_by_a_later_drain(self):
        self._fault_then_clear(lambda: mock.patch.object(
            gb, "_publish_text_whole", side_effect=OSError(28, "No space left on device")))

    def test_an_unreadable_generation_is_retried_by_a_later_drain(self):
        self._fault_then_clear(lambda: mock.patch.object(
            gb, "_claim_receipt", return_value=("unknown", None, None)))

    def test_a_snapshot_whose_hand_back_is_blocked_is_decided_again_by_a_later_drain(self):
        """B's snapshot cannot go live because a newer file holds the live name; the drain that sends
        that file frees the name, and a later ordinary drain hands B back and sends it once."""
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT B\n")
                lane.write("proactive-1.txt", "NEWER C\n")
                for _ in range(4):
                    lane.drain()
                self.assertEqual(sorted(p.get("body") for p in lane.posts), ["NEWER C", "UNSENT B"],
                                 "both sent once, with no restart")
                lane.restart()
                for _ in range(2):
                    lane.drain()
                self.assertEqual(len(lane.posts), 2, "no repeat")
                self.assertEqual(list(lane.results.glob("proactive-1.sending.*")), [], "no snapshot left behind")
                hidden()

    def test_new_bytes_whose_digest_cannot_be_recorded_are_sent_once_and_recorded_later(self):
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT B\n")
                real = gb.undelivered_quarantine.rename_noreplace

                def refuse_the_record(src, dst):
                    if gb._DECIDED_INFIX in Path(dst).name:
                        raise OSError(1, "Operation not permitted")
                    return real(src, dst)
                with mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", refuse_the_record):
                    lane.drain()
                for _ in range(3):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "sent once")
                digest = __import__("hashlib").sha256(b"UNSENT B\n").hexdigest()[:32]
                self.assertEqual(len(list(lane.results.glob(f".proactive-1{gb._DECIDED_INFIX}{digest}-*"))), 1,
                                 "a later drain recorded B's digest")
                lane.restart()
                lane.drain()
                self.assertEqual(len(lane.posts), 1, "no repeat")
                hidden()

    def test_a_retained_original_that_is_gone_leaves_no_seen_entry(self):
        lane = Lane(gb, scratch("gw-decided-prune-"))
        r = lane.results / f".proactive-1{gb._DECIDED_INFIX}{'0' * 32}-ex.1"
        r.write_text("x\n")
        for _ in range(2):
            lane.drain()
        key = (os.lstat(next(lane.results.glob(".proactive-1.decided-*"))).st_dev,
               os.lstat(next(lane.results.glob(".proactive-1.decided-*"))).st_ino)
        self.assertIn(key, gb._DECIDED_SEEN)
        for p in lane.results.glob(".proactive-1.decided-*"):
            p.unlink()  # only the test removes one; the bridge never does
        lane.drain()
        self.assertNotIn(key, gb._DECIDED_SEEN)

    def _retained(self, lane) -> Path:
        (r,) = lane.results.glob(".proactive-1.decided-*")
        return r

    def _age(self, lane, seconds: float = 5.0) -> None:
        """Make the retained original look written `seconds` ago (its writer has paused that long)."""
        when = __import__("time").time_ns() - int(seconds * 1e9)
        os.utime(self._retained(lane), ns=(when, when))

    def test_a_dead_owners_snapshot_blocked_at_restart_is_sent_by_a_later_drain(self):
        """A snapshot claim left by a process that died: the restart's one try is blocked by live C;
        once C is sent, an ordinary drain decides the snapshot again. A live owner's stays fenced."""
        lane = Lane(gb, scratch("gw-dead-snapshot-"))
        dead = lane.results / f"proactive-1.sending.{_dead_pid()}.s1"
        dead.write_text("UNSENT B\n")
        fenced = lane.results / f"proactive-2.sending.{os.getppid()}.s1"
        fenced.write_text("LIVE OWNER'S\n")
        lane.write("proactive-1.txt", "NEWER C\n")
        lane.restart()
        self.assertTrue(dead.exists(), "the restart's try was blocked by C")
        for _ in range(4):
            lane.drain()
        self.assertEqual(sorted(p.get("body") for p in lane.posts), ["NEWER C", "UNSENT B"],
                         "B sent by an ordinary drain, with no second restart")
        self.assertFalse(dead.exists())
        self.assertEqual(fenced.read_text(), "LIVE OWNER'S\n", "a live owner's snapshot is never taken")
        for _ in range(2):
            lane.drain()
        self.assertEqual(len(lane.posts), 2, "no repeat")

    def test_a_drain_decides_each_pending_snapshot_once_however_many_originals_its_stem_has(self):
        lane = Lane(gb, scratch("gw-snapshot-bound-"))
        for n, body in enumerate((b"old one\n", b"old two\n")):
            digest = __import__("hashlib").sha256(body).hexdigest()[:32]
            (lane.results / f".proactive-1{gb._DECIDED_INFIX}{digest}-ex.{n}").write_bytes(body)
        snapshot = lane.results / f"proactive-1.sending.{os.getpid()}.s1"
        snapshot.write_text("UNSENT B\n")
        real, calls = gb._decide_orphan_claim, []

        def counted(c):
            calls.append(Path(c).name)
            return real(c)
        with mock.patch.object(gb, "_decide_orphan_claim", counted), \
                mock.patch.object(gb, "_hand_back", lambda claim, original: None):  # the live name stays held
            lane.drain()
        self.assertEqual(calls.count(snapshot.name), 1, f"one decision per pending snapshot per drain ({calls})")

    def test_an_unknown_receipt_for_readable_new_bytes_is_not_settled(self):
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT B\n")
                b_record = outbox._item_path(root, gb._proactive_item_id("proactive-1", "UNSENT B"))
                b_record.parent.mkdir(parents=True, exist_ok=True)
                b_record.write_text("{")  # unreadable outbox record for B's item
                with mock.patch.object(gb, "RESULTS_DIR", lane.results):
                    state, generation, epoch = gb._claim_receipt(self._retained(lane), "proactive-1")
                self.assertEqual((state, len(generation or ""), epoch), ("unknown", 64, None),
                                 "the production shape: unknown with a valid digest")
                self.assertFalse(self._reconcile_in(lane), "unknown is not settled")
                for _ in range(3):
                    lane.drain()
                self.assertEqual(lane.posts, [], "nothing sent while the record is unreadable")
                b_record.unlink()  # repaired: no retained-file stat changes
                for _ in range(2):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "sent once the record reads")
                lane.restart()
                lane.drain()
                self.assertEqual(len(lane.posts), 1, "no repeat")
                hidden()

    def test_an_unknown_receipt_for_the_recorded_bytes_is_retried_until_the_record_reads(self):
        """The production shape (unknown, <valid digest>, None) on the recorded generation: an outbox
        record that cannot be read is not settled; once an operator's requeue replaces it, a later
        ordinary drain hands the body back and sends it once, though no retained-file stat changed."""
        lane = Lane(gb, scratch("gw-unknown-recorded-"))
        root = lane.results / ".outbox-ag2space-proactive"
        body = b"parked body\n"
        digest = __import__("hashlib").sha256(body).hexdigest()
        r = lane.results / f".proactive-1{gb._DECIDED_INFIX}{digest[:32]}-e0.1"
        r.write_bytes(body)
        (lane.results / "undelivered").mkdir()
        (lane.results / "undelivered" / "proactive-1-1.txt").write_bytes(body)
        record = {**self._sent_record("parked body\n"), "status": "PARKED", "resend_epoch": 0}
        item_id = gb._proactive_item_id("proactive-1", "parked body")
        outbox._write_item(root, item_id, record)
        outbox._item_path(root, item_id).write_text("{")  # unreadable for a while
        with mock.patch.object(gb, "RESULTS_DIR", lane.results):
            self.assertEqual(gb._claim_receipt(r, "proactive-1"), ("unknown", digest, None))
        for _ in range(3):
            lane.drain()
        self.assertEqual(lane.posts, [])
        outbox._write_item(root, item_id, {**record, "status": "QUEUED", "resend_epoch": 1})
        for _ in range(3):
            lane.drain()
        self.assertEqual([p.get("body") for p in lane.posts], ["parked body"], "handed back and sent once")

    def _reconcile_in(self, lane) -> bool:
        with mock.patch.object(gb, "RESULTS_DIR", lane.results), \
                mock.patch.object(gb, "ARCHIVE_RESULTS_DIR", lane.results / "archive"):
            return gb._reconcile_decided(self._retained(lane))

    def _with_real_settle(self):
        patcher = mock.patch.object(gb, "_DECIDED_SETTLE_SEC", __import__("ag2_sparrow.result_ready",
                                                                           fromlist=["x"]).SETTLE_SEC, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_paused_writer_never_gets_a_prefix_sent(self):
        self._with_real_settle()
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"UNSENT")  # the writer pauses here
                for _ in range(2):
                    lane.drain()
                write(b"UNSENT B\n")
                self._age(lane)
                for _ in range(2):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "only the whole body is sent")
                hidden()

    def test_a_writer_that_truncates_first_never_leaves_an_empty_live_file(self):
        self._with_real_settle()
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                write(b"")
                self._age(lane)
                for _ in range(2):
                    lane.drain()
                self.assertFalse((lane.results / "proactive-1.txt").exists(), "no empty live file")
                write(b"UNSENT B\n")
                self._age(lane)
                for _ in range(2):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"])

    def test_a_longer_stems_archive_is_not_proof_of_a_copy(self):
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                (lane.results / "archive" / "proactive-1-longer-123.txt").write_text("UNSENT B\n")
                write(b"UNSENT B\n")
                for _ in range(4):
                    lane.drain()
                self.assertEqual([p.get("body") for p in lane.posts], ["UNSENT B"], "sent, not taken as archived")

    def test_a_live_file_rewritten_after_the_reconcile_is_not_proof_of_a_copy(self):
        for kind in ("DELIVERED", "PARKED"):
            with self.subTest(kind=kind):
                lane, root, a_id, write, hidden = self._held_original(kind)
                lane.restart()
                live = lane.write("proactive-1.txt", "UNSENT B\n")
                write(b"UNSENT B\n")
                real = gb._reconcile_changed_decided

                def then_rewrite_live():
                    real()
                    live.write_text("OTHER\n")  # the producer rewrites its live file after the probe
                with mock.patch.object(gb, "_reconcile_changed_decided", then_rewrite_live):
                    lane.drain()
                for _ in range(3):
                    lane.drain()
                self.assertEqual(sorted(p.get("body") for p in lane.posts), ["OTHER", "UNSENT B"],
                                 "B sent once from its own snapshot")

    def test_two_deliveries_of_one_stem_in_one_tick_never_share_an_archive(self):
        lane = Lane(gb, scratch("gw-archive-collision-"))
        with mock.patch.object(gb.time, "time_ns", return_value=1792900000000000000):
            for body in ("B\n", "C\n"):
                lane.write("proactive-1.txt", body)
                lane.drain()
        archived = sorted(p.read_text() for p in (lane.results / "archive").glob("proactive-1-*.txt"))
        self.assertEqual(archived, ["B\n", "C\n"], "C never replaced B's archive")

    def test_a_competing_drainer_cannot_move_a_snapshot_between_its_proof_and_the_commit(self):
        """A dead owner's snapshot of B is the proof the reconcile relies on. Right before the new
        digest is committed, a competing drainer moves the dead owner's snapshot live and a newer C
        replaces it there. The reconcile must own that snapshot first, so B is still sent once."""
        lane = Lane(gb, scratch("gw-snapshot-race-"))
        dead = lane.results / f"proactive-1.sending.{_dead_pid()}.s1"
        dead.write_text("UNSENT B\n")
        (lane.results / f".proactive-1{gb._DECIDED_INFIX}{'0' * 32}-ex.1").write_text("UNSENT B\n")
        real, raced = gb.undelivered_quarantine.rename_noreplace, []

        def competitor_before_the_commit(src, dst):
            if gb._DECIDED_INFIX in Path(dst).name and not raced:
                raced.append(True)
                if dead.exists():  # the competing drainer's real retry, then C published over the live name
                    real(dead, lane.results / "proactive-1.txt")
                    __import__("ag2_sparrow.result_publish", fromlist=["x"]).publish_text(
                        lane.results / "proactive-1.txt", "NEWER C\n")
            return real(src, dst)
        with mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", competitor_before_the_commit), \
                mock.patch.object(gb, "_retry_pending_snapshots", lambda: None):  # only the reconcile's proof runs
            lane.drain()
        for _ in range(4):
            lane.drain()
        lane.restart()
        lane.drain()
        self.assertTrue(raced)
        self.assertIn("UNSENT B", [p.get("body") for p in lane.posts], "B is never lost behind the race")
        self.assertEqual([p.get("body") for p in lane.posts].count("UNSENT B"), 1)

    def test_an_operator_requeue_of_a_settled_original_is_handed_back_by_a_drain(self):
        """PARKED e0 settles and is cached; the operator's requeue makes it QUEUED e1 without touching
        the retained file. A later ordinary drain, not only a restart, hands it back and sends it once."""
        lane = Lane(gb, scratch("gw-cached-requeue-"))
        root = lane.results / ".outbox-ag2space-proactive"
        body = b"parked body\n"
        digest = __import__("hashlib").sha256(body).hexdigest()
        (lane.results / f".proactive-1{gb._DECIDED_INFIX}{digest[:32]}-e0.1").write_bytes(body)
        item_id = gb._proactive_item_id("proactive-1", "parked body")
        outbox._write_item(root, item_id, {**self._sent_record("parked body\n"), "status": "PARKED",
                                           "resend_epoch": 0})
        for _ in range(2):
            lane.drain()
        self.assertEqual(lane.posts, [])
        self.assertEqual(outbox.requeue_item_with_epoch(root, item_id)[:2],
                         (outbox.RequeueOutcome.REQUEUED, 1))
        for _ in range(3):
            lane.drain()
        self.assertEqual([p.get("body") for p in lane.posts], ["parked body"], "handed back by a drain")
        self.assertEqual(outbox.receipt(root, item_id), ("DELIVERED", 1))

    def test_a_persistently_failing_archive_never_replaces_a_hidden_witness(self):
        lane = Lane(gb, scratch("gw-hidden-collision-"))
        real = gb.undelivered_quarantine.place_as

        def archive_full(src, directory, names, *a, **k):
            if Path(directory).name == "archive":
                raise OSError(28, "No space left on device")
            return real(src, directory, names, *a, **k)
        with mock.patch.object(gb.undelivered_quarantine, "place_as", archive_full), \
                mock.patch.object(gb.time, "time_ns", return_value=1792900000000000000):
            for body in ("B\n", "C\n"):
                lane.write("proactive-1.txt", body)
                lane.drain()
        self.assertEqual([p.get("body") for p in lane.posts], ["B", "C"])
        hidden = sorted(p.read_text() for p in lane.results.glob(".proactive-1.delivered-*"))
        self.assertEqual(hidden, ["B\n", "C\n"], "C never replaced B's hidden witness")

    def test_an_unchanged_retained_original_costs_a_drain_only_a_stat(self):
        lane = Lane(gb, scratch("gw-decided-cost-"))
        r = lane.results / f".proactive-1{gb._DECIDED_INFIX}{'0' * 32}-ex.1"
        r.write_text("x\n")
        for _ in range(2):  # the first records its bytes (a rename, which moves ctime); the second settles
            lane.drain()
        with mock.patch.object(gb, "_reconcile_decided", side_effect=AssertionError("read again")):
            lane.drain()
            lane.drain()

    def test_a_staged_claim_is_invisible_to_every_delivered_scanner(self):
        """Each reader of results/ names that could take a staged claim for a delivered (or drained)
        file: the gateway's reaper, its archive probe (which the dispatch veto also asks) and its drain,
        the other bridges' startup sweep, and the pending-questions drain probe. The task-orphan
        classifier and dedup probes match only `<id>.txt[.sending]`; outbox state is keyed by item."""
        import importlib.util
        from ag2_sparrow import proactive_recovery as vendored_recovery
        sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
        spec = importlib.util.spec_from_file_location(
            "pending_questions_ask", REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_ask.py")
        ask = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("pending_questions_ask", ask)
        spec.loader.exec_module(ask)
        import proactive_recovery as src_recovery
        res = self.lane.results
        staged = res / f"proactive-1.sending.{os.getpid()}.{1 << 60}"
        staged.write_text("UNSENT B\n")
        retained = res / f".proactive-1{gb._DECIDED_INFIX}{'0' * 32}-e0.1"
        snapshot = res / f".proactive-1.txt.{os.getpid()}.{'0' * 16}.tmp"  # result_publish's staging
        for hidden in (retained, snapshot):
            hidden.write_text("UNSENT B\n")
        with mock.patch.object(gb, "RESULTS_DIR", res), \
                mock.patch.object(gb, "ARCHIVE_RESULTS_DIR", res / "archive"):
            gb._reap_hidden_delivered()
            self.assertFalse(gb._delivered_copy_exists("proactive-1"), "archive probe")
        self.assertEqual(staged.read_text(), "UNSENT B\n", "the reaper left it")
        self.assertEqual(list((res / "archive").iterdir()), [], "nothing archived")
        for mod in (src_recovery, vendored_recovery):
            for name in (staged.name, retained.name, snapshot.name):
                self.assertIsNone(mod._recovery_target(name), f"{mod.__name__} sweep ignores {name}")
        for hidden in (retained, snapshot):
            self.assertEqual(hidden.read_text(), "UNSENT B\n", f"the reaper left {hidden.name}")
        self.assertFalse(ask.drained(res, "proactive-1.txt"), "pending-questions reads it as not yet drained")
        self.lane.drain()
        self.assertEqual([p.get("body") for p in self.lane.posts], ["UNSENT B"],
                         "the retained original's new bytes go out once, from a snapshot")
        self.assertEqual(staged.read_text(), "UNSENT B\n", "a live owner's claim is left alone")

    def test_a_parked_claim_goes_back_live_only_on_a_requeues_own_transition(self):
        def delete(root, item_id, parked):
            outbox._item_path(root, item_id).unlink()

        def corrupt(root, item_id, parked):
            outbox._item_path(root, item_id).write_text('{"status": "QUEU')

        def queued_without_a_requeue(root, item_id, parked):
            outbox._write_item(root, item_id, {"item_id": item_id, "status": "QUEUED", "attempts": 0,
                                               "payload": "parked body\n"})

        def delivered(root, item_id, parked):
            outbox.record_delivered(root, item_id)

        def other_item_in_the_same_inode(root, item_id, retained):
            other = gb._proactive_item_id("proactive-1", "other body")
            outbox._write_item(root, other, {"item_id": other, "status": "PARKED", "attempts": 0,
                                             "resend_epoch": 1, "payload": "other body\n"})
            with open(retained, "r+b") as fh:  # a writer's retained descriptor: same inode, new bytes
                fh.truncate(0)
                fh.write(b"other body\n")

        for interleave in (delete, corrupt, queued_without_a_requeue, delivered, other_item_in_the_same_inode):
            with self.subTest(interleave=interleave.__name__):
                lane = Lane(gb, scratch("gw-parked-proof-"))
                root = lane.results / ".outbox-ag2space-proactive"
                item_id = gb._proactive_item_id("proactive-1", "parked body")
                outbox._write_item(root, item_id, {"item_id": item_id, "status": "PARKED", "attempts": 0,
                                                   "payload": "parked body\n"})
                (lane.results / f"proactive-1.sending.{_dead_pid()}.1").write_text("parked body\n")
                real = gb._claim_receipt
                seen = []

                def interleaved(claim, stem):
                    if len(seen) == 1:
                        interleave(root, item_id, Path(claim))
                    got = real(claim, stem)
                    seen.append(got[0])
                    return got

                with mock.patch.object(gb, "_claim_receipt", side_effect=interleaved):
                    lane.restart()
                self.assertEqual(seen[0], "PARKED")
                for _ in range(2):
                    lane.drain()
                lane.restart()
                lane.drain()
                self.assertFalse((lane.results / "proactive-1.txt").exists(), "never handed back live")
                parked = [p.read_text() for p in (lane.results / "undelivered").glob("proactive-1-*.txt")]
                self.assertEqual(parked.count("parked body\n"), 1, f"A kept parked once for an operator ({parked})")
                self.assertEqual(lane.posts, [], "a PARKED send is never repeated on doubt")



class DecidedClaimEdges(unittest.TestCase):
    """The reconciler's and placer's refusal paths, each called directly on one lane."""

    def setUp(self):
        self.lane = Lane(gb, scratch("gw-decided-edges-"))
        self.res = self.lane.results
        self.root = self.res / ".outbox-ag2space-proactive"
        for name, value in (("RESULTS_DIR", self.res), ("ARCHIVE_RESULTS_DIR", self.res / "archive")):
            patcher = mock.patch.object(gb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.logs = []
        patcher = mock.patch.object(gb, "_log", self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def retained(self, body: bytes, status: str = "QUEUED", epoch: int = 1) -> Path:
        item = gb._proactive_item_id("proactive-1", body.decode().strip())
        outbox._write_item(self.root, item, {"item_id": item, "status": status, "attempts": 0,
                                             "resend_epoch": epoch, "payload": body.decode()})
        digest = __import__("hashlib").sha256(body).hexdigest()[:32]
        r = self.res / f".proactive-1{gb._DECIDED_INFIX}{digest}-e0.7"
        r.write_bytes(body)
        return r

    def test_a_name_it_does_not_own_is_left_alone(self):
        r = self.res / f".proactive-1{gb._DECIDED_INFIX}not-a-digest"
        r.write_text("x\n")
        gb._reconcile_decided(r)
        self.assertEqual(r.read_text(), "x\n")

    def test_an_unreadable_retained_original_is_kept(self):
        r = self.res / f".proactive-1{gb._DECIDED_INFIX}{'0' * 32}-e0.1"
        r.mkdir()
        gb._reconcile_decided(r)
        self.assertTrue(r.is_dir())

    def test_new_bytes_are_published_as_a_snapshot_even_when_they_cannot_be_recorded(self):
        r = self.res / f".proactive-1{gb._DECIDED_INFIX}{'0' * 32}-e0.1"
        r.write_text("new bytes\n")
        with mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", side_effect=OSError(5, "EIO")):
            gb._reconcile_decided(r)
        self.assertEqual(r.read_text(), "new bytes\n", "the original stays where it is")
        claims = [c for c in self.res.glob("proactive-1.sending.*")]
        self.assertEqual([c.read_text() for c in claims], ["new bytes\n"], "its bytes went out as a snapshot")
        self.assertNotEqual(os.lstat(claims[0]).st_ino, os.lstat(r).st_ino)
        self.assertTrue(gb._reconcile_decided(r), "the pending snapshot is decided again; the copy is found")
        copies = [p for p in [*self.res.glob("proactive-1.sending.*"), self.res / "proactive-1.txt"] if p.exists()]
        self.assertEqual([p.read_text() for p in copies], ["new bytes\n"], "nothing is published twice")

    def test_a_requeue_is_not_handed_back_over_a_newer_live_file(self):
        r = self.retained(b"body\n")
        (self.res / "proactive-1.txt").write_text("a newer body\n")
        gb._reconcile_decided(r)
        self.assertEqual((self.res / "proactive-1.txt").read_text(), "a newer body\n")
        self.assertTrue(r.exists(), "the decision epoch is not advanced")

    def test_a_requeue_already_live_is_only_recorded(self):
        r = self.retained(b"body\n")
        (self.res / "proactive-1.txt").write_text("body\n")
        gb._reconcile_decided(r)
        self.assertFalse(r.exists())
        self.assertEqual(len(list(self.res.glob(f".proactive-1{gb._DECIDED_INFIX}*-e1.7"))), 1)

    def test_a_requeue_with_no_copy_left_is_published_fresh(self):
        r = self.retained(b"body\n")
        gb._reconcile_decided(r)
        self.assertEqual((self.res / "proactive-1.txt").read_text(), "body\n")
        self.assertTrue(any("handed back" in m for m in self.logs))

    def test_a_parked_copy_that_cannot_move_is_retried_later(self):
        r = self.retained(b"body\n")
        parked = self.res / "undelivered"
        parked.mkdir()
        (parked / "proactive-1-1.txt").write_text("body\n")
        with mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", side_effect=OSError(5, "EIO")):
            gb._reconcile_decided(r)
        self.assertFalse((self.res / "proactive-1.txt").exists())
        self.assertTrue(r.exists(), "the decision epoch is not advanced")

    def test_a_copy_that_cannot_be_placed_leaves_the_claim(self):
        item = gb._proactive_item_id("proactive-1", "parked body")
        outbox._write_item(self.root, item, {"item_id": item, "status": "PARKED", "attempts": 0,
                                             "payload": "parked body\n"})
        claim = self.res / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("parked body\n")
        with mock.patch.object(gb, "_publish_text_whole", side_effect=OSError(28, "No space left on device")):
            gb._recover_orphan_proactive()
        self.assertEqual(claim.read_text(), "parked body\n")
        self.assertEqual(list((self.res / "undelivered").glob("proactive-1-*")), [], "nothing parked")

    def test_a_superseded_copy_needs_the_claims_own_bytes(self):
        claim = self.res / "proactive-1.sending.1.1"
        claim.write_text("body\n")
        self.assertIsNone(gb._retire_superseded_parked_copy(claim, "proactive-1", None, 0))
        self.assertTrue(claim.exists())

    def test_a_claim_that_is_not_utf8_is_unknown(self):
        claim = self.res / "proactive-1.sending.1.1"
        claim.write_bytes(b"\xff\xfe not text\n")
        self.assertEqual(gb._claim_receipt(claim, "proactive-1"), ("unknown", None, None))

if __name__ == "__main__":
    unittest.main(verbosity=1)
