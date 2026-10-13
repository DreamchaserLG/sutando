#!/usr/bin/env python3
"""src/hitl/session_ownership.py: the one shared same-session ownership
policy both pool_wedge_cards and core-input-watch defer to, each way.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC)

from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import HumanRequirement  # noqa: E402

from hitl.session_ownership import session_already_carded  # noqa: E402

SESSION = "sutando-worker-test"


class SessionAlreadyCarded(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="session-ownership-")
        self.addCleanup(tmp.cleanup)
        self.manager = HitlManager(HitlStore(default_store(Path(tmp.name))))

    def seed(self, source, session=SESSION):
        return self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="x",
            subject={"source": source, "session": session}))

    def test_an_active_card_from_another_source_counts(self):
        self.seed("tui")
        self.assertTrue(session_already_carded(self.manager, SESSION, by_source="pool-wedge"))

    def test_the_callers_own_source_never_counts(self):
        self.seed("pool-wedge")
        self.assertFalse(session_already_carded(self.manager, SESSION, by_source="pool-wedge"))

    def test_symmetric_the_other_direction_too(self):
        self.seed("pool-wedge")
        self.assertTrue(session_already_carded(self.manager, SESSION, by_source="tui"))

    def test_a_different_session_never_counts(self):
        self.seed("tui", session="some-other-session")
        self.assertFalse(session_already_carded(self.manager, SESSION, by_source="pool-wedge"))

    def test_a_resolved_card_no_longer_counts(self):
        made = self.seed("tui")
        self.manager.resolve(made.id)
        self.assertFalse(session_already_carded(self.manager, SESSION, by_source="pool-wedge"))

    def test_an_unset_source_never_counts_as_another_real_source(self):
        self.seed(None)
        self.assertFalse(session_already_carded(self.manager, SESSION, by_source="pool-wedge"))

    def test_no_manager_is_treated_as_no_ownership(self):
        self.assertFalse(session_already_carded(None, SESSION, by_source="pool-wedge"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
