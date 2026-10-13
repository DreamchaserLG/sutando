#!/usr/bin/env python3
"""core-input-watch.py's escalate() always creates its own card for a gate or
login episode, never deferring to pool_wedge_cards.py's CARD_HUMAN/CARD_LOGIN
— a backstop only, by that module's own docstring, that defers to this one.
The design is one-directional; this file pins this side of it.

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

    def test_escalate_blocked_human_always_wins_over_an_active_pool_human_card(self):
        # The richer permission card must always get created, AND the
        # backstop must not coexist for the whole episode.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No", SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "permission")
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")
        self.assertEqual([r.id for r in self.manager.active()], [req.id])

    def test_escalate_blocked_human_always_wins_over_a_legacy_effectless_pool_card(self):
        # Same, against a legacy pool-wedge card with no "effect" field.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No", SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "permission")
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")

    def test_escalate_logged_out_always_wins_over_an_active_pool_login_card(self):
        # The richer auth card must never be suppressed by the weaker
        # plain-jump card, and the backstop must not coexist all episode.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool login card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_login",
                     "effect": "login"}))
        req = civ.escalate(self.manager, "logged-out", "core not authenticated", None, None, SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "auth")
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")
        self.assertEqual([r.id for r in self.manager.active()], [req.id])

    def test_escalate_logged_out_retires_a_legacy_card_cause_needs_login_backstop(self):
        # An even older pool shape, pre-dating CARD_LOGIN as its own kind.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="legacy cause card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_cause",
                     "cause": ["needs-login", "api-error"]}))
        req = civ.escalate(self.manager, "logged-out", "core not authenticated", None, None, SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")
        self.assertEqual([r.id for r in self.manager.active()], [req.id])

    def test_escalate_logged_out_created_first_is_unaffected_by_arrival_order(self):
        req = civ.escalate(self.manager, "logged-out", "core not authenticated", None, None, SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "auth")

    def test_escalate_still_cards_when_nothing_else_covers_this_session(self):
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(req)
        cards = [r for r in self.manager.active() if (r.subject or {}).get("session") == SESSION]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].subject["source"], "tui")

    def test_escalate_is_unaffected_by_a_different_effect_on_the_same_session(self):
        # A frozen-turn confirmation and a gate question are different
        # concerns; sharing a session must not let one hide the other.
        self.manager.create(HumanRequirement(
            kind="confirmation", runtime="claude", message="pool-wedge frozen card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_frozen"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(req)

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

    def test_escalate_never_calls_claim_session_for_either_gate_effect(self):
        # Structural pin: escalate() always wins by construction, never by
        # winning a check-and-create race through the shared ownership gate.
        from unittest import mock
        import hitl.session_ownership as so
        with mock.patch.object(so, "claim_session", wraps=so.claim_session) as spy:
            for state, kind, prompt in (("blocked-human", "permission", "Do you want to proceed?"),
                                        ("logged-out", None, None)):
                req = civ.escalate(self.manager, state, "detail", kind, prompt, SESSION)
                self.assertIsNotNone(req)
            self.assertFalse(spy.called)

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
