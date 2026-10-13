#!/usr/bin/env python3
"""src/hitl/session_ownership.py: the one shared same-session ownership
policy both pool_wedge_cards and core-input-watch defer to, each way.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC)

from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import HumanRequirement  # noqa: E402

from hitl.session_ownership import session_already_carded, claim_session, supersede  # noqa: E402

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

    def test_an_effect_scoped_check_ignores_a_different_effect(self):
        self.manager.create(HumanRequirement(
            kind="confirmation", runtime="claude", message="x",
            subject={"source": "pool-wedge", "session": SESSION, "effect": "turn-frozen"}))
        self.assertFalse(session_already_carded(self.manager, SESSION, by_source="tui",
                                                 effect="gate"))

    def test_an_effect_scoped_check_matches_the_same_effect(self):
        self.manager.create(HumanRequirement(
            kind="confirmation", runtime="claude", message="x",
            subject={"source": "pool-wedge", "session": SESSION, "effect": "gate"}))
        self.assertTrue(session_already_carded(self.manager, SESSION, by_source="tui",
                                                effect="gate"))


class ClaimSession(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="claim-session-")
        self.addCleanup(tmp.cleanup)
        self.manager = HitlManager(HitlStore(default_store(Path(tmp.name))))

    def test_claims_and_creates_when_unowned(self):
        made = claim_session(self.manager, SESSION, by_source="pool-wedge", effect="gate",
                             build_requirement=lambda: HumanRequirement(
                                 kind="core-blocked", runtime="claude", message="x",
                                 subject={"source": "pool-wedge", "session": SESSION,
                                          "effect": "gate"}))
        self.assertIsNotNone(made)
        self.assertEqual(len(self.manager.active()), 1)

    def test_refuses_and_does_not_call_build_when_already_owned(self):
        self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="other",
            subject={"source": "tui", "session": SESSION, "effect": "gate"}))
        called = []

        def build():
            called.append(True)
            return HumanRequirement(kind="core-blocked", runtime="claude", message="x",
                                    subject={"source": "pool-wedge", "session": SESSION,
                                             "effect": "gate"})

        made = claim_session(self.manager, SESSION, by_source="pool-wedge", effect="gate",
                             build_requirement=build)
        self.assertIsNone(made)
        self.assertEqual(called, [])  # never built once the claim was refused
        self.assertEqual(len(self.manager.active()), 1)

    def test_no_manager_claims_nothing(self):
        self.assertIsNone(claim_session(None, SESSION, by_source="pool-wedge", effect="gate",
                                        build_requirement=lambda: (_ for _ in ()).throw(
                                            AssertionError("must not be called"))))

    def test_two_concurrent_producers_racing_the_same_claim_only_one_wins(self):
        # Two independent adapters (own HitlManager/HitlStore, same
        # directory), released together so they genuinely race the claim.
        store_dir = Path(self.manager.store.root)
        barrier = threading.Barrier(2)
        results = []

        def race(by_source):
            m = HitlManager(HitlStore(store_dir))
            barrier.wait()
            made = claim_session(m, SESSION, by_source=by_source, effect="gate",
                                 build_requirement=lambda: HumanRequirement(
                                     kind="core-blocked", runtime="claude", message=by_source,
                                     guard=f"race:{by_source}",
                                     subject={"source": by_source, "session": SESSION,
                                              "effect": "gate"}))
            results.append(made)

        threads = [threading.Thread(target=race, args=(src,))
                  for src in ("pool-wedge", "tui")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got {results}")
        self.assertEqual(len(self.manager.active()), 1)


class Supersede(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="supersede-")
        self.addCleanup(tmp.cleanup)
        self.manager = HitlManager(HitlStore(default_store(Path(tmp.name))))

    def test_creates_and_retires_the_backstop(self):
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        made = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                         effect="gate", build_requirement=lambda: HumanRequirement(
                             kind="permission", runtime="claude", message="tui card",
                             subject={"source": "tui", "session": SESSION, "effect": "gate"}),
                         legacy_match=lambda subj: subj.get("wedge") == "card_human")
        self.assertEqual(made.status, "pending")
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")
        self.assertEqual([r.id for r in self.manager.active()], [made.id])

    def test_retirement_is_bounded_to_the_named_backstop_source(self):
        # An unrelated third producer sharing session+effect must survive --
        # only `backstop_source` is a candidate for retirement.
        unrelated = self.manager.create(HumanRequirement(
            kind="confirmation", runtime="claude", message="unrelated producer",
            guard="unrelated-guard", blocked_task_ids=["task-999"],
            subject={"source": "some-other-watcher", "session": SESSION, "effect": "gate"}))
        supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                 effect="gate", build_requirement=lambda: HumanRequirement(
                     kind="permission", runtime="claude", message="tui card", guard="tui-guard",
                     subject={"source": "tui", "session": SESSION, "effect": "gate"}),
                 legacy_match=lambda subj: subj.get("wedge") == "card_human")
        self.assertEqual(self.manager.get(unrelated.id).status, "pending")

    def test_refuses_on_a_dedup_collision_with_a_foreign_source_record(self):
        # create()'s dedup can match an existing other-source record; this
        # generic writer refuses (None) rather than hand back that record.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool card",
            device={"id": SESSION}, guard="SAME_GUARD",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        made = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                         effect="gate", build_requirement=lambda: HumanRequirement(
                             kind="core-blocked", runtime="claude", message="tui card",
                             device={"id": SESSION}, guard="SAME_GUARD",  # same tuple
                             subject={"source": "tui", "session": SESSION, "effect": "gate"}),
                         legacy_match=lambda subj: subj.get("wedge") == "card_human")
        self.assertIsNone(made)
        self.assertEqual(self.manager.get(backstop.id).status, "pending")

    def test_a_refused_collision_never_mutates_the_foreign_record(self):
        # create()'s dedup for kind="auth" rewrites a matching record's
        # guard/revision regardless of guard equality -- must check first.
        foreign = self.manager.create(HumanRequirement(
            kind="auth", runtime="claude", message="foreign auth",
            device={"id": SESSION}, guard="FOREIGN_GUARD",
            subject={"source": "pool-wedge", "session": SESSION, "effect": "login"}))
        made = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                         effect="login", build_requirement=lambda: HumanRequirement(
                             kind="auth", runtime="claude", message="tui auth",
                             device={"id": SESSION}, guard="TUI_GUARD",  # different guard
                             subject={"source": "tui", "session": SESSION, "effect": "login"}),
                         legacy_match=lambda subj: subj.get("wedge") == "card_login")
        self.assertIsNone(made)
        after = self.manager.get(foreign.id)
        self.assertEqual(after.guard, "FOREIGN_GUARD")
        self.assertEqual(after.revision, foreign.revision)

    def test_a_source_less_record_is_still_a_collision_candidate(self):
        # A source-less third producer is not `by_source` either -- skipping
        # it from the check would let create() mutate it in place.
        foreign = self.manager.create(HumanRequirement(
            kind="auth", runtime="claude", message="detector auth",
            device={"id": SESSION}, guard="DETECTOR_GUARD", subject={"session": SESSION}))
        made = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                         effect="login", build_requirement=lambda: HumanRequirement(
                             kind="auth", runtime="claude", message="tui auth",
                             device={"id": SESSION}, guard="TUI_GUARD",
                             subject={"source": "tui", "session": SESSION, "effect": "login"}),
                         legacy_match=lambda subj: subj.get("wedge") == "card_login")
        self.assertIsNone(made)
        after = self.manager.get(foreign.id)
        self.assertEqual(after.guard, "DETECTOR_GUARD")
        self.assertEqual(after.revision, foreign.revision)

    def test_a_refusal_leaves_the_known_backstop_untouched_too(self):
        # The refusal skips the retirement loop entirely, so a concurrently
        # active, genuinely-named backstop must survive the refusal too.
        detector = self.manager.create(HumanRequirement(
            kind="auth", runtime="claude", message="detector auth",
            device={"id": SESSION}, guard="DETECTOR_GUARD", subject={"session": SESSION}))
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool login backstop",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_login",
                     "effect": "login"}))
        made = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                         effect="login", build_requirement=lambda: HumanRequirement(
                             kind="auth", runtime="claude", message="tui auth",
                             device={"id": SESSION}, guard="TUI_GUARD",
                             subject={"source": "tui", "session": SESSION, "effect": "login"}),
                         legacy_match=lambda subj: subj.get("wedge") == "card_login")
        self.assertIsNone(made)
        after_detector = self.manager.get(detector.id)
        self.assertEqual(after_detector.guard, "DETECTOR_GUARD")
        self.assertEqual(after_detector.revision, detector.revision)
        self.assertEqual(self.manager.get(backstop.id).status, "pending")

    def test_the_outer_lock_keeps_a_concurrent_foreign_insert_from_racing_the_check(self):
        # supersede() holds ONE lock across check-then-create; delay the check
        # just long enough for a second writer to prove it genuinely blocks.
        store_dir = Path(self.manager.store.root)
        manager_a = HitlManager(HitlStore(store_dir))
        manager_b = HitlManager(HitlStore(store_dir))
        gate = threading.Event()
        proceed = threading.Event()
        real_find_duplicate = HitlManager.find_duplicate

        def slow_find_duplicate(mgr_self, req):
            result = real_find_duplicate(mgr_self, req)
            if mgr_self is manager_a:
                gate.set()
                proceed.wait(timeout=5)
            return result

        result = {}

        def run_a():
            with mock.patch.object(HitlManager, "find_duplicate", slow_find_duplicate):
                result["a"] = supersede(
                    manager_a, SESSION, by_source="tui", backstop_source="pool-wedge",
                    effect="login", build_requirement=lambda: HumanRequirement(
                        kind="auth", runtime="claude", message="tui auth",
                        device={"id": SESSION}, guard="TUI_GUARD",
                        subject={"source": "tui", "session": SESSION, "effect": "login"}))

        t_a = threading.Thread(target=run_a)
        t_a.start()
        self.assertTrue(gate.wait(timeout=5), "a never reached its delayed check")

        started_b = threading.Event()
        finished_b = threading.Event()

        def run_b():
            started_b.set()
            manager_b.create(HumanRequirement(
                kind="auth", runtime="claude", message="foreign auth",
                device={"id": SESSION}, guard="FOREIGN_GUARD",
                subject={"source": "detector", "session": SESSION, "effect": "login"}))
            finished_b.set()

        t_b = threading.Thread(target=run_b)
        t_b.start()
        try:
            self.assertTrue(started_b.wait(timeout=5))
            import time as _time
            _time.sleep(0.05)
            self.assertFalse(finished_b.is_set(),
                             "b ran to completion while a's supersede() still held the lock")
        finally:
            # Release a's delayed check on any outcome, or both threads block
            # for their 5s timeouts, racing this test's own tmp-dir cleanup.
            proceed.set()
            t_a.join(timeout=5)
            t_b.join(timeout=5)
        made = result["a"]
        self.assertIsNotNone(made)
        self.assertEqual((made.subject or {}).get("source"), "tui")  # a's own record, not b's

    def test_a_retirement_failure_never_hides_the_created_record(self):
        # made is already persisted once create() returns; a later retirement
        # failure must not turn into "nothing happened" for the caller.
        backstop = self.manager.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="pool card",
            subject={"source": "pool-wedge", "session": SESSION, "wedge": "card_human",
                     "effect": "gate"}))
        # A stable guard/device matching the real TUI path, so the retry
        # dedupes on a deliberate guard match, not a double-empty collapse.
        build = lambda: HumanRequirement(kind="permission", runtime="claude", message="tui card",
                                         guard=f"tui:{SESSION}:gate:deadbeef", device={"id": SESSION, "name": SESSION},
                                         subject={"source": "tui", "session": SESSION, "effect": "gate"})
        with self.assertLogs("hitl.session_ownership", level="WARNING") as log, \
                mock.patch.object(HitlManager, "resolve", side_effect=OSError("disk full")):
            made = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                             effect="gate", build_requirement=build,
                             legacy_match=lambda subj: subj.get("wedge") == "card_human")
        # Four observable parts of the partial-write contract, not just "it didn't raise":
        # 1. the rich card itself, 2. both records' statuses, 3. the emitted signal.
        self.assertIsNotNone(made)
        self.assertEqual((made.subject or {}).get("source"), "tui")
        self.assertEqual(self.manager.get(made.id).status, "pending")
        self.assertEqual(self.manager.get(backstop.id).status, "pending")
        self.assertTrue(any("failed to retire backstop" in m for m in log.output))

        # 4. a subsequent, unobstructed call retries retirement and dedupes onto the same rich card.
        retried = supersede(self.manager, SESSION, by_source="tui", backstop_source="pool-wedge",
                            effect="gate", build_requirement=build,
                            legacy_match=lambda subj: subj.get("wedge") == "card_human")
        self.assertEqual(retried.id, made.id)
        self.assertEqual(self.manager.get(backstop.id).status, "resolved")

    def test_no_manager_supersedes_nothing(self):
        self.assertIsNone(supersede(None, SESSION, by_source="tui", backstop_source="pool-wedge",
                                    effect="gate", build_requirement=lambda: (_ for _ in ()).throw(
                                        AssertionError("must not be called"))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
