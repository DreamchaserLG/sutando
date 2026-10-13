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
            kind="core-blocked", runtime="claude", device={"id": SESSION},
            guard=f"pool-wedge:{SESSION}:human:deadbeef", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No", SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "permission")
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")
        self.assertEqual([r.id for r in self.manager.active()], [req.id])

    def test_escalate_blocked_human_always_wins_when_both_sides_share_the_fallback_kind(self):
        # A non-semantic gate makes escalate()'s own kind="core-blocked"
        # too, matching pool's -- same identity, different guard, must win.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", device={"id": SESSION},
            guard=f"pool-wedge:{SESSION}:human:deadbeef", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: usage limit", "trust",
                           "You have hit your session usage limit.\n❯ 1. Stop\n2. Wait", SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "core-blocked")
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")
        self.assertEqual([r.id for r in self.manager.active()], [req.id])

    def test_escalate_blocked_human_always_wins_over_a_legacy_effectless_pool_card(self):
        # Same, against a legacy pool-wedge card with no "effect" field.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", device={"id": SESSION},
            guard=f"pool-wedge:{SESSION}:human:deadbeef", message="pool-wedge card",
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
            kind="core-blocked", runtime="claude", device={"id": SESSION},
            guard=f"pool-wedge:{SESSION}:cause:needs-login", message="pool login card",
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
            kind="core-blocked", runtime="claude", device={"id": SESSION},
            guard=f"pool-wedge:{SESSION}:cause:needs-login,api-error", message="legacy cause card",
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
        # Sharing a session must not let one effect hide -- or retire --
        # the other; supersede()'s effect scoping must leave it untouched.
        frozen = self.manager.create(HumanRequirement(
            kind="confirmation", runtime="claude", message="pool-wedge frozen card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_frozen"}))
        req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                           "permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No", SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(self.manager.get(frozen.id).status, "pending")

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

    def test_an_enumeration_failure_during_retirement_still_returns_the_rich_card(self):
        # manager.active() raising on the retirement enumeration (the 3rd
        # call) must not surface as "nothing happened" while a card is live.
        from unittest import mock
        from hitl.manager import HitlManager
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", device={"id": SESSION},
            guard=f"pool-wedge:{SESSION}:human:deadbeef", message="pool-wedge card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        real_active = HitlManager.active
        calls = {"n": 0}

        def counting_active(mgr_self):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("retirement enumeration failed")
            return real_active(mgr_self)

        with mock.patch.object(HitlManager, "active", counting_active):
            req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                               "permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No", SESSION)
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, "permission")
        self.assertEqual(self.manager.get(backstop.id).status, "pending")  # cleanup failed, stayed up

        # The next identical call (no failure injected) retries retirement
        # and dedupes onto the same rich card.
        retried = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                               "permission", "Do you want to proceed?\n❯ 1. Yes\n  2. No", SESSION)
        self.assertEqual(retried.id, req.id)
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")

    def test_escalate_delegates_arbitration_to_the_shared_supersede(self):
        # A private inline reimplementation reproduces identical outcomes and
        # stays undetected above -- only mocking the real call catches it.
        from unittest import mock
        import hitl.session_ownership as so
        with mock.patch.object(so, "supersede", wraps=so.supersede) as spy:
            req = civ.escalate(self.manager, "blocked-human", "awaiting user: permission",
                               "permission", "Do you want to proceed?", SESSION)
        self.assertIsNotNone(req)
        self.assertTrue(spy.called)
        self.assertEqual(spy.call_args.kwargs.get("effect"), civ.GATE_EFFECT)

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
