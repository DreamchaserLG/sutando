#!/usr/bin/env python3
"""A transient unreadable pane must not clear an active wedge card.

Through the real remedy sweep: the pane classifier, state reader/writer,
decision ladder, card creation and resolve_cleared all run against a temporary
workspace. Only process I/O and unrelated watcher provisioning are stubbed.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))

import pool_remedy as rem  # noqa: E402

sup, ps, wc, wi = rem.sup, rem.ps, rem.wc, rem.wi
from hitl.schema import ActionReply  # noqa: E402

WID = "7c54b230a8d94ea9b86f52d70134ac68"
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
GATE = f"  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n  Esc to cancel\n{FOOTER}\n"
FROZEN = f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n"


class UnreadablePaneHoldsTheWedge(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pool-wedge-unreadable-")
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        (self.ws / "state").mkdir()
        self.now = 1000.0
        self.pane = GATE
        path = self.ws / "state" / "roster.json"
        path.write_text(json.dumps(
            {"workers": {WID: {"state": "live", "runtime": "claude", "label": WID}}}))
        wi.record_session(self.ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
        wi.start_incarnation(self.ws, WID, "s1", tmux_socket=str(self.ws / "tmux.sock"),
                             tmux_session=wi.tmux_session_name(WID))
        self.task_id = "task-wedge-unreadable-0001"
        (self.ws / "tasks").mkdir(exist_ok=True)
        (self.ws / "tasks" / f"{self.task_id}.txt").write_text(
            f"id: {self.task_id}\nrequested_worker: {WID}\ntask: no-op fixture\n")
        (self.ws / "deliveries" / WID).mkdir(parents=True, exist_ok=True)
        (self.ws / "deliveries" / WID / f"{self.task_id}.txt").touch()

    def runner(self, argv, **kwargs):
        if "capture-pane" in argv:
            if self.pane is None:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, self.pane, "")
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "inbox-holders" in argv:
            return subprocess.CompletedProcess(argv, 0, "4242 session\n", "")
        raise AssertionError(f"unexpected process operation: {argv}")

    def sweep(self):
        self.now += 300.0
        real_tick, real_raise = sup.tick, wc.raise_card
        output = io.StringIO()
        with mock.patch.object(sup, "tick", side_effect=lambda ws, now, **kw:
                               real_tick(ws, now, runner=self.runner, **kw)), \
                mock.patch.object(wc, "raise_card", side_effect=lambda ws, wid, which, **kw:
                                  real_raise(ws, wid, which, runner=self.runner,
                                             routed=lambda *_: False)), \
                mock.patch.object(rem, "ensure_supervisors", return_value={}), \
                mock.patch.object(rem, "ensure_input_watches", return_value={}), \
                mock.patch.object(wc, "drive_escapes", return_value={}), \
                mock.patch.object(rem.time, "time", return_value=self.now), \
                mock.patch.object(rem.subprocess, "run",
                                  side_effect=AssertionError("unexpected subprocess")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(rem.main(["--workspace", str(self.ws), "--repo", str(REPO),
                                       "--sweep"]), 0)
        return json.loads(output.getvalue())

    def active_cards(self):
        return wc.manager_for(self.ws).active()

    def raise_card_to_sustain(self):
        for _ in range(2):
            self.assertEqual(self.sweep()["decisions"][WID], ps.NOTHING)
        result = self.sweep()
        self.assertEqual(result["decisions"][WID], ps.CARD_HUMAN)
        cards = self.active_cards()
        self.assertEqual(len(cards), 1)
        return cards[0]

    def test_an_unreadable_capture_holds_a_pending_card(self):
        card = self.raise_card_to_sustain()
        self.pane = None  # capture-pane now fails (returncode 1)
        result = self.sweep()
        self.assertEqual(result["decisions"][WID], ps.NOTHING)
        self.assertEqual(result["cards_closed"], [])
        cards = self.active_cards()
        self.assertEqual([c.id for c in cards], [card.id])
        self.assertEqual(cards[0].status, "pending")

    def test_an_unreadable_capture_holds_a_pressed_card(self):
        card = self.raise_card_to_sustain()
        m = wc.manager_for(self.ws)
        m.apply_action(ActionReply(hitl_id=card.id, expected_revision=card.revision,
                                   action_id="open_terminal", guard=card.guard))
        self.assertEqual(wc.manager_for(self.ws).get(card.id).status, "in_progress")
        self.pane = None
        result = self.sweep()
        self.assertEqual(result["decisions"][WID], ps.NOTHING)
        self.assertEqual(result["cards_closed"], [])
        self.assertEqual(wc.manager_for(self.ws).get(card.id).status, "in_progress")

    def test_recovery_still_closes_the_card_once_the_pane_is_actually_readable_again(self):
        # The hold is for missing evidence only; real recovery still works.
        card = self.raise_card_to_sustain()
        self.pane = None
        self.sweep()
        self.pane = f"❯ \n{FOOTER}\n"
        result = self.sweep()
        self.assertEqual(result["cards_closed"], [card.id])
        self.assertEqual(self.active_cards(), [])

    def test_a_card_outlives_missing_ladder_state_plus_an_unreadable_capture(self):
        # The card is durable HITL state; losing the separate ladder-state
        # file must not, with one unreadable tick, read as positive clearance.
        card = self.raise_card_to_sustain()
        sup.state_path(self.ws).unlink()
        self.pane = None
        result = self.sweep()
        self.assertEqual(result["decisions"][WID], ps.NOTHING)
        self.assertEqual(result["cards_closed"], [])
        self.assertEqual([c.id for c in self.active_cards()], [card.id])


class UnreadablePaneHoldsTheFrozenFrameIdentity(UnreadablePaneHoldsTheWedge):
    def setUp(self):
        super().setUp()
        self.pane = FROZEN

    def raise_card_to_sustain(self):
        # The first tick only seeds last_pane_id; "stuck" isn't detected until
        # the next one repeats it, so sustain lands one tick later than GATE/HUMAN.
        for _ in range(3):
            self.assertEqual(self.sweep()["decisions"][WID], ps.NOTHING)
        result = self.sweep()
        self.assertEqual(result["decisions"][WID], ps.CARD_FROZEN)
        cards = self.active_cards()
        self.assertEqual(len(cards), 1)
        return cards[0]

    def test_an_unreadable_capture_between_two_identical_frozen_frames_holds_the_card(self):
        # The gap must not erase the frame identity "stuck" relies on, or the
        # same frame that follows reads as unrecognized and the card resets.
        card = self.raise_card_to_sustain()
        self.pane = None
        result = self.sweep()
        self.assertEqual(result["decisions"][WID], ps.NOTHING)
        self.assertEqual(result["cards_closed"], [])
        self.pane = FROZEN
        result = self.sweep()
        self.assertEqual(result["cards_closed"], [])
        self.assertEqual([c.id for c in self.active_cards()], [card.id])

    def test_an_unreadable_capture_between_two_identical_frozen_frames_holds_a_pressed_card(self):
        card = self.raise_card_to_sustain()
        m = wc.manager_for(self.ws)
        m.apply_action(ActionReply(hitl_id=card.id, expected_revision=card.revision,
                                   action_id="open_terminal", guard=card.guard))
        self.pane = None
        self.sweep()
        self.pane = FROZEN
        result = self.sweep()
        self.assertEqual(result["cards_closed"], [])
        self.assertEqual(wc.manager_for(self.ws).get(card.id).status, "in_progress")


if __name__ == "__main__":
    unittest.main(verbosity=2)
