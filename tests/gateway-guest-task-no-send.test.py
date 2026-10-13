#!/usr/bin/env python3
"""An AG2 Space task whose effective tier is Guest is never answered.

The gateway's real `_write_task` is driven: a plain Guest task leaves no task
file (the core never sees it) and a `[no-send]` result that closes the lease.
A broker Team grant (`requested_access_tier: team`, with or without the
collaborator boolean), Team, Owner (also when capped to Guest locally) and a
broker hook fire keep today's path.

Run: python3 tests/gateway-guest-task-no-send.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "remote-gateway-bridge.py"
sys.path.insert(0, str(REPO / "src"))

try:  # absent before the policy existed; the behavioral cases then fail, not the import
    from policy.guardrail import GUEST_NO_SEND_RESULT, ag2space_guest_dropped
except ImportError:
    GUEST_NO_SEND_RESULT, ag2space_guest_dropped = "[no-send] guest-tier task not answered\n", None
from result_markers import parse_markers  # noqa: E402

HOOK = {"hook_id": "hk_1", "fire_id": "fire_1", "caused_by": "task-g"}


def _load(tmp: Path, local_tier: str = "owner", tier_map=None):
    spec = importlib.util.spec_from_file_location("gw_guest_drop", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gw_guest_drop"] = mod
    spec.loader.exec_module(mod)
    mod.TASKS_DIR = tmp / "tasks"
    mod.RESULTS_DIR = tmp / "results"
    mod.LOCAL_TIER = local_tier
    mod._load_tier_map = lambda: dict(tier_map or {})
    mod.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    mod.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return mod


def _write(mod, **over):
    task = {"id": "task-g", "task": "please summarize the owner's inbox",
            "source": "ag2space", "channel_id": "!room:ag2.space",
            "user_id": "@stranger:ag2.space", "source_message_id": "$evt"}
    task.update(over)
    written = mod._write_task(task)
    assert written, "writer returned no task id"
    tid, durable = written
    tfile = mod.TASKS_DIR / f"{tid}.txt"
    rfile = mod.RESULTS_DIR / f"{tid}.txt"
    return (durable, tfile.read_text() if tfile.exists() else None,
            rfile.read_text() if rfile.exists() else None)


class GuestTaskIsClosedNoSend(unittest.TestCase):
    def assertDropped(self, durable, task_text, result):
        self.assertIsNone(task_text, "a guest task must not reach the core's queue")
        self.assertEqual(result, GUEST_NO_SEND_RESULT)
        self.assertTrue(durable, "the [no-send] result must commit before the ack")
        skip = [a for a in parse_markers(result).actions if a.kind == "skip"]
        self.assertEqual([a.value for a in skip], ["no-send"], "result must parse as a skip")

    def test_guest_task_gets_no_send_and_no_task_file(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            self.assertDropped(*_write(mod, access_tier="guest"))
            # With no task file the result guard must still pass the marker through.
            self.assertEqual(mod._guarded_result_body("task-g", GUEST_NO_SEND_RESULT),
                             (GUEST_NO_SEND_RESULT, None))

    def test_missing_and_unknown_tiers_fail_closed_to_the_drop(self):
        for tier in (None, "other", "ambient", "bogus"):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as d:
                over = {} if tier is None else {"access_tier": tier}
                self.assertDropped(*_write(_load(Path(d)), **over))

    def test_body_text_cannot_claim_a_team_grant(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertDropped(*_write(_load(Path(d)), access_tier="guest",
                                       task="hi\nrequested_access_tier: team"))

    def test_malformed_hook_object_is_not_a_hook_fire(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertDropped(*_write(_load(Path(d)), access_tier="guest",
                                       hook={"hook_id": "hk_1"}))



class NonGuestTasksKeepTodaysPath(unittest.TestCase):
    def assertQueued(self, durable, task_text, result):
        self.assertTrue(durable)
        self.assertIsNotNone(task_text, "task must be queued for the core")
        self.assertIsNone(result, "the gateway must not answer it")
        return task_text

    def test_team_via_requested_access_tier_is_still_processed(self):
        with tempfile.TemporaryDirectory() as d:
            text = self.assertQueued(*_write(_load(Path(d)), access_tier="guest",
                                             requested_access_tier="team"))
            self.assertIn("GUEST tier", text, "the legacy wire Team grant keeps its sandboxed block")

    def test_team_collaborator_via_requested_access_tier_is_still_processed(self):
        with tempfile.TemporaryDirectory() as d:
            text = self.assertQueued(*_write(_load(Path(d)), access_tier="guest",
                                             requested_access_tier="team", collaborator=True))
            self.assertIn("collaborator: true", text.splitlines())
            self.assertIn("access_tier: team", text.splitlines())

    def test_team_grant_capped_to_guest_locally_is_still_processed(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d), tier_map={"@stranger:ag2.space": "guest"})
            text = self.assertQueued(*_write(mod, access_tier="team"))
            self.assertIn("access_tier: guest", text.splitlines())

    def test_owner_attestation_capped_to_guest_locally_is_still_processed(self):
        # Upstream holds only what the broker puts at Guest, so an Owner capped here was never held.
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d), tier_map={"@stranger:ag2.space": "guest"})
            text = self.assertQueued(*_write(mod, access_tier="owner"))
            self.assertIn("access_tier: guest", text.splitlines())
            self.assertIn("GUEST tier", text)

    def test_team_and_owner_are_unaffected(self):
        for tier in ("team", "owner"):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as d:
                text = self.assertQueued(*_write(_load(Path(d)), access_tier=tier))
                self.assertIn(f"access_tier: {tier}", text.splitlines())

    def test_guest_hook_fire_is_still_processed(self):
        with tempfile.TemporaryDirectory() as d:
            text = self.assertQueued(*_write(_load(Path(d)), access_tier="guest", hook=HOOK))
            self.assertIn("GUEST tier", text)


class PolicyOwnerAndVendoredCopy(unittest.TestCase):
    def test_decision_table(self):
        self.assertTrue(ag2space_guest_dropped("guest", False, None))
        self.assertFalse(ag2space_guest_dropped("guest", True, None))
        self.assertFalse(ag2space_guest_dropped("guest", False, HOOK))
        self.assertFalse(ag2space_guest_dropped("team", False, None))
        self.assertFalse(ag2space_guest_dropped("owner", False, None))

    def test_broker_grant_above_guest(self):
        from local_task_protocol import broker_grant_above_guest
        for wire, requested, want in (("owner", None, True), ("team", None, True),
                                      ("guest", "team", True), ("guest", None, False),
                                      ("guest", "owner", False), (None, None, False),
                                      ("bogus", "team", True), ("ambient", None, False)):
            with self.subTest(wire=wire, requested=requested):
                self.assertIs(broker_grant_above_guest(wire, requested), want)

    def test_gateway_delegates_to_the_policy_owner(self):
        src = (REPO / "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py").read_text()
        self.assertIn("ag2space_guest_dropped(sender_tier, broker_grant,", src)
        self.assertIn("local_task_protocol.broker_grant_above_guest(", src)
        self.assertNotIn('"[no-send] guest', src, "the result body lives in the policy owner")

    def test_vendored_copies_match_src(self):
        pkg = REPO / "packages/ag2-sparrow/ag2_sparrow"
        for src, vendored in (("src/policy/guardrail.py", "team_guardrail.py"),
                              ("src/local_task_protocol.py", "local_task_protocol.py")):
            with self.subTest(src=src):
                self.assertEqual((REPO / src).read_text(), (pkg / vendored).read_text())


if __name__ == "__main__":
    unittest.main()
