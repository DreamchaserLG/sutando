#!/usr/bin/env python3
"""core-input-watch.py's escalate() must defer to a card another source
already raised for the same session — the reciprocal half of
pool_wedge_cards.input_watch_has_card(), which already defers the other way.

Real src/hitl store; no tmux, no subprocess.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
_MOD_PATH = os.path.join(_SRC, "core-input-watch.py")
_spec = importlib.util.spec_from_file_location("core_input_watch", _MOD_PATH)
civ = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(civ)

sys.path.insert(0, _SRC)
from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import HumanRequirement  # noqa: E402

SESSION = "sutando-worker-test"


class DuplicateCardArbitration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="civ-arbitration-")
        self.addCleanup(tmp.cleanup)
        self.manager = HitlManager(HitlStore(default_store(Path(tmp.name))))

    def test_escalate_defers_when_another_source_already_cards_this_session(self):
        # pool-wedge raised its CARD_HUMAN card first, in the same sweep that
        # then starts core-input-watch for the first time.
        self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?", SESSION)
        self.assertIsNone(req)
        cards = [r for r in self.manager.active() if (r.subject or {}).get("session") == SESSION]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].subject["source"], "pool-wedge")

    def test_escalate_still_cards_when_nothing_else_covers_this_session(self):
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(req)
        cards = [r for r in self.manager.active() if (r.subject or {}).get("session") == SESSION]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].subject["source"], "tui")

    def test_escalate_is_unaffected_by_a_different_sessions_card(self):
        self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="other seat",
            subject={"source": "pool-wedge", "session": "some-other-session", "wedge": "card_human"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(req)

    def test_a_resolved_other_sources_card_does_not_suppress(self):
        made = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human"}))
        self.manager.resolve(made.id)
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(req)

    def test_escalate_still_ignores_its_own_prior_card_for_dedup_not_arbitration(self):
        # Its own earlier TUI card must not look like "someone else covers
        # this" — the Manager's own dedup handles that case, not this check.
        first = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                             "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(first)
        second = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                              "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(second)
        self.assertEqual(first.id, second.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
