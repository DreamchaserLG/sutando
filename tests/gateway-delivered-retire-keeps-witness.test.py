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
import stat
import subprocess
import sys
import threading
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

PREFIX = "proactive-orphan-recovery-"


class Crash(RuntimeError):
    """The publisher dies here."""


class Killed(BaseException):
    """The process dies here: no handler in the code under test may absorb it."""


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
        self.assertIsNone(gb._file_generation(self.lane.results / "missing"))
        target = self.lane.results / "elsewhere"
        target.write_text("x\n")
        link = self.lane.results / "proactive-1.sending.1.1"
        link.symlink_to(target)
        self.assertEqual(gb._claim_receipt(link, "proactive-1"), ("unknown", None, None))

    def test_a_parked_claim_swapped_in_flight_and_unrestorable_is_kept_for_an_operator(self):
        item_id = gb._proactive_item_id("proactive-1", "parked")
        outbox._write_item(self.lane.results / ".outbox-ag2space-proactive", item_id,
                           {"item_id": item_id, "status": "PARKED", "attempts": 0, "payload": "parked\n"})
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("parked\n")
        calls = []
        real = gb.undelivered_quarantine.rename_noreplace

        def move_then_refuse_the_way_back(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError(5, "Input/output error")
            real(src, dst)
            Path(dst).write_text("a different generation\n")

        with mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", move_then_refuse_the_way_back):
            self.lane.restart()
        self.assertFalse((self.lane.results / "proactive-1.txt").exists())
        self.assertEqual(len(calls), 2, "the way back failed, so the file stays under its staged claim")
        kept = list(self.lane.results.glob("proactive-1.sending.*"))
        self.assertEqual([p.read_text() for p in kept], ["a different generation\n"])
        self.assertEqual(list((self.lane.results / "undelivered").glob("proactive-1-*.txt")), [])
        self.assertEqual(len(self.lane.posts), 0)

    def test_a_replaced_claim_whose_way_back_is_retaken_is_never_archived_as_delivered(self):
        item_id = gb._proactive_item_id("proactive-1", "delivered")
        outbox.record_delivered(self.lane.results / ".outbox-ag2space-proactive", item_id)
        claim = self.lane.results / f"proactive-1.sending.{_dead_pid()}.1"
        claim.write_text("delivered\n")
        real_receipt, real_move = gb._claim_receipt, gb.undelivered_quarantine.rename_noreplace
        calls = []

        def read_then_replace(*a, **kw):
            got = real_receipt(*a, **kw)
            tmp = claim.with_name(".swap")
            tmp.write_text("UNSENT B\n")
            os.replace(tmp, claim)
            return got

        def retake_before_the_way_back(src, dst):
            calls.append(src)
            if len(calls) == 2:
                Path(dst).write_text("CONCURRENT C\n")
            real_move(src, dst)

        with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_replace), \
                mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", retake_before_the_way_back):
            self.lane.restart()
        self.lane.drain()
        self.assertEqual(claim.read_text(), "CONCURRENT C\n")
        self.assertEqual(list(self.lane.results.glob(".proactive-1.delivered-*")), [])
        kept = [p for p in self.lane.results.glob("proactive-1.sending.*") if p != claim]
        self.assertEqual([p.read_text() for p in kept], ["UNSENT B\n"], "kept as a claim, re-decided later")
        archived = list((self.lane.results / "archive").glob("proactive-1*"))
        self.assertFalse(any("UNSENT B" in p.read_text() for p in archived), "B was never delivered")
        self.assertEqual(len(self.lane.posts), 0)

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
        restart = threading.Thread(target=self.lane.restart, daemon=True)
        restart.start()
        restart.join(2.0)
        wedged = restart.is_alive()
        if wedged:  # supply a writer so the blocked reader returns and the suite can go on
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            restart.join(5.0)
        self.assertFalse(wedged, "the restart blocked opening a FIFO claim")
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
            with self.assertRaises(Killed):
                self.lane.restart()
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


    def _open_fds(self) -> int:
        return len(os.listdir("/dev/fd"))

    def _read_unblocked(self, claim: Path, unblock=None):
        """The reader's state, whether it blocked, and the fds it left open."""
        got, before = [], self._open_fds()
        reader = threading.Thread(target=lambda: got.append(gb._claim_receipt(claim, "proactive-1")[0]),
                                  daemon=True)
        reader.start()
        reader.join(1.0)
        blocked = reader.is_alive()
        if blocked and unblock:
            unblock()
            reader.join(5.0)
        return (got or [None])[0], blocked, self._open_fds() - before

    def test_the_claim_reader_fails_closed_on_every_input_it_cannot_trust(self):
        limit = getattr(gb, "_CLAIM_READ_LIMIT", 1 << 20)
        real_fstat = os.fstat

        def as_device(fd):
            st = list(real_fstat(fd))
            st[0] = stat.S_IFCHR | 0o600
            return os.stat_result(st)

        def make(res, case):
            claim = res / f"proactive-1.sending.{_dead_pid()}.{case}"
            if case.startswith("fifo"):
                os.mkfifo(claim)
            elif case == "directory":
                claim.mkdir()
            elif case == "symlink":
                (res / "elsewhere").write_text("delivered\n")
                claim.symlink_to(res / "elsewhere")
            elif case == "oversized":
                claim.write_bytes(b"x" * (limit + 1))
            else:
                claim.write_text("never sent\n")
            return claim

        for case in ("fifo", "fifo-with-bytes", "directory", "device", "symlink", "oversized", "read-error"):
            with self.subTest(case=case):
                lane = Lane(gb, scratch("gw-claim-reader-"))  # each case alone: a failed one leaves no FIFO behind
                res = lane.results
                claim = make(res, case)
                holder = []
                if case == "fifo-with-bytes":
                    holder.append(os.open(claim, os.O_RDWR | os.O_NONBLOCK))
                    os.write(holder[0], b"queued bytes")
                unblock = {"fifo": lambda c=claim: os.close(os.open(c, os.O_WRONLY | os.O_NONBLOCK)),
                           "fifo-with-bytes": lambda: os.close(holder.pop())}.get(case)
                fault = (mock.patch.object(os, "fstat", as_device) if case == "device"
                         else mock.patch.object(os, "read", side_effect=OSError(5, "Input/output error"))
                         if case == "read-error" else contextlib.nullcontext())
                with fault:
                    state, blocked, leaked = self._read_unblocked(claim, unblock)
                self.assertFalse(blocked, "the reader blocked")
                self.assertEqual(state, "unknown", "fails closed")
                self.assertEqual(leaked, 0, "every branch closes its descriptor")
                if holder:
                    self.assertEqual(os.read(holder[0], 64), b"queued bytes", "no byte was consumed")
                    os.close(holder.pop())
                if case not in ("device", "read-error", "fifo", "fifo-with-bytes"):
                    lane.restart()
                    self.assertTrue(os.path.lexists(claim), "kept as its claim for an operator")
                    self.assertFalse((res / "proactive-1.txt").exists(), "never handed back live")

    def _b_survives(self, lane, a_body: str) -> None:
        """B was never sent, so it is neither archived as delivered nor lost; A is never resent."""
        hidden = [h.read_text() for h in lane.results.glob(".proactive-1.delivered-*")]
        self.assertNotIn("UNSENT B\n", hidden, "unsent B hidden as delivered")
        for _ in range(2):
            lane.drain()
        bodies = [p.get("body") for p in lane.posts]
        archived = [p.read_text() for p in (lane.results / "archive").glob("proactive-1*")]
        self.assertEqual(archived.count("UNSENT B\n"), bodies.count("UNSENT B"), "B archived unsent")
        self.assertNotIn(a_body, bodies, "A is never resent")
        kept = [p.read_text() for p in list(lane.results.glob("proactive-1*")) + list(lane.results.glob(".proactive-1*"))
                + list((lane.results / "undelivered").glob("proactive-1*")) if p.is_file()]
        self.assertTrue(bodies.count("UNSENT B") == 1 or "UNSENT B\n" in kept, f"B was lost ({bodies}, {kept})")

    def test_a_replacement_at_every_durable_boundary_is_neither_archived_unsent_nor_lost(self):
        faults = (("stage", "crash-after"), ("stage", "fail"), ("stage", "replace-at-target"),
                  ("rollback", "crash-after"), ("rollback", "fail"))
        for kind in ("DELIVERED", "PARKED"):
            for boundary, fault in faults:
                with self.subTest(kind=kind, boundary=boundary, fault=fault):
                    lane = Lane(gb, scratch("gw-boundary-"))
                    root = lane.results / ".outbox-ag2space-proactive"
                    a_body = f"{kind.lower()} A"
                    item_id = gb._proactive_item_id("proactive-1", a_body)
                    outbox._write_item(root, item_id, {"item_id": item_id, "status": kind, "attempts": 0,
                                                       "payload": a_body + "\n"})
                    claim = lane.results / f"proactive-1.sending.{_dead_pid()}.1"
                    claim.write_text(a_body + "\n")
                    real_receipt, real_move = gb._claim_receipt, gb.undelivered_quarantine.rename_noreplace
                    calls = []

                    def read_then_replace(*a, **kw):
                        got = real_receipt(*a, **kw)
                        if got[0] == kind and not calls and fault != "replace-at-target":
                            tmp = claim.with_name(".swap")
                            tmp.write_text("UNSENT B\n")
                            os.replace(tmp, claim)
                        return got

                    def move(src, dst):
                        calls.append(src)
                        n = len(calls)
                        at = 1 if boundary == "stage" else 2
                        if fault == "fail" and (n == at or (boundary == "rollback" and n > at)):
                            raise FileExistsError(17, "File exists")  # a failed rollback: every way out fails
                        real_move(src, dst)
                        if n == 1 and fault == "replace-at-target":
                            tmp = Path(dst).with_name(".swap")
                            tmp.write_text("UNSENT B\n")
                            os.replace(tmp, dst)
                        if n == at and fault == "crash-after":
                            raise Killed(f"killed after the {boundary} rename")

                    logs = []
                    with mock.patch.object(gb, "_claim_receipt", side_effect=read_then_replace), \
                            mock.patch.object(gb.undelivered_quarantine, "rename_noreplace", move), \
                            mock.patch.object(gb, "_log", logs.append):
                        try:
                            lane.restart()
                        except Killed:
                            pass
                    if fault == "fail" and boundary == "rollback":
                        self.assertTrue(any("changed after its receipt was read" in m for m in logs),
                                        f"an un-neutralized rollback is reported ({logs})")
                    dead = _dead_pid()
                    for c in lane.results.glob("proactive-1.sending.*"):  # its owner died
                        rest = c.name.split(".sending.", 1)[1].split(".", 1)
                        c.rename(c.with_name(f"proactive-1.sending.{dead}.{rest[1] if len(rest) > 1 else '0'}"))
                    lane.restart()
                    self._b_survives(lane, a_body)

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

        def other_generation(root, item_id, parked):
            outbox.requeue_item_with_epoch(root, item_id)
            tmp = parked.with_name(".swap")
            tmp.write_text("parked body\n")
            os.replace(tmp, parked)

        def other_item(root, item_id, parked):
            other = gb._proactive_item_id("proactive-1", "other body")
            outbox._write_item(root, other, {"item_id": other, "status": "QUEUED", "attempts": 0,
                                             "resend_epoch": 1, "payload": "other body\n"})
            tmp = parked.with_name(".swap")
            tmp.write_text("other body\n")
            os.replace(tmp, parked)

        for interleave in (delete, corrupt, queued_without_a_requeue, delivered, other_generation, other_item):
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
                    if seen:
                        interleave(root, item_id, Path(claim))
                    got = real(claim, stem)
                    seen.append(got[0])
                    return got

                with mock.patch.object(gb, "_claim_receipt", side_effect=interleaved):
                    lane.restart()
                self.assertEqual(seen[0], "PARKED")
                self.assertEqual(len(seen), 2, "re-read once after parking")
                for _ in range(2):
                    lane.drain()
                self.assertFalse((lane.results / "proactive-1.txt").exists(), "never handed back live")
                self.assertEqual(len(list((lane.results / "undelivered").glob("proactive-1-*.txt"))), 1,
                                 "kept parked for an operator")
                self.assertEqual(lane.posts, [], "a PARKED send is never repeated on doubt")


if __name__ == "__main__":
    unittest.main(verbosity=1)
