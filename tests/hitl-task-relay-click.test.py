#!/usr/bin/env python3
"""remote-gateway-bridge: a card click on the TASK relay is applied to the HITL
store and consumed; ordinary messages, and clicks while the owner is unknown, stay on the task path."""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hitl-relay-click-"))
for p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if p not in sys.path:
        sys.path.insert(0, p)
from ag2_sparrow._dirs import set_dirs  # noqa: E402

set_dirs(task_dir=TMP / "tasks", result_dir=TMP / "results", state_dir=TMP / "state")
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402

from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import Action, HumanRequirement  # noqa: E402

OWNER = "@owner:ag2.space"


class TaskRelayClickTests(unittest.TestCase):
    def setUp(self):
        os.environ["SPARROW_HA_OWNER"] = OWNER
        rgb._HITL_OWNER["mxid"] = ""
        self.mgr = HitlManager(HitlStore(default_store(TMP)))
        self.req = self.mgr.create(HumanRequirement(
            kind="permission", runtime="claude", message="Claude wants to run Bash: ls",
            guard=f"hook:{os.urandom(4).hex()}", device={"id": "core-1", "name": "core-1"},
            actions=[Action(id="allow", kind="allow_once", label="Allow"),
                     Action(id="deny", kind="reject_once", label="Deny")]))

    def _on_disk(self):
        """The requirement as a fresh manager reads it from the store file."""
        return HitlManager(HitlStore(default_store(TMP))).get(self.req.id)

    @contextlib.contextmanager
    def _registry(self, owner):
        """A gateway whose /v1/agents row for this agent names `owner`; yields the reads."""
        reads = []

        def req(method, path, *a, **k):
            reads.append(path)
            return {"agents": [{"id": "@me:ag2.space", "owner": owner}]}
        with mock.patch.object(rgb, "_req", req), \
                mock.patch.object(rgb, "_reenroll_identity", lambda: "@me:ag2.space"):
            yield reads

    def _task(self, **kw):
        t = {"id": f"task-{os.urandom(5).hex()}", "channel_id": "!dm:ag2.space",
             "user_id": OWNER, "source_message_id": "$click", "task": "Allow"}
        t.update(kw)
        return t

    def test_non_owner_typed_label_reply_stays_a_message(self):
        """A non-owner's typed "Deny" is a message and must reach the task path;
        a non-owner's real click is still consumed. Own card id: the store
        outlives one test and requirement_for_event returns the first match."""
        self.mgr.record_projection(self.req.id, self.req.revision, "$card-nonowner")
        t = self._task(user_id="@someone-else:ag2.space", reply_to_event="$card-nonowner", task="Deny")
        self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)
        click = self._task(user_id="@someone-else:ag2.space",
                           hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "deny", "guard": self.req.guard})
        self.assertEqual(rgb._handle_hitl_action(click), "ignored")
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_hitl_action_on_the_task_is_applied_and_consumed(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertTrue(rgb._handle_hitl_action(t))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "allow")

    def test_reply_to_the_card_with_a_label_is_applied(self):
        self.mgr.record_projection(self.req.id, self.req.revision, "$card")
        self.assertTrue(rgb._handle_hitl_action(self._task(reply_to_event="$card", task="Deny")))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")

    def test_ordinary_message_stays_on_the_task_path(self):
        self.assertFalse(rgb._handle_hitl_action(self._task(task="Allow")))
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_no_owner_anywhere_leaves_the_click_as_a_task_and_says_so(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        logs = []

        def offline(*a, **k):
            raise OSError("gateway unreachable")
        with mock.patch.object(rgb, "_req", offline), mock.patch.object(rgb, "_log", logs.append):
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self._on_disk().chosen_action)
        self.assertTrue(any("hitl: card click ignored, owner unknown" in m for m in logs), logs)

    def test_registry_with_no_owner_row_fails_closed_too(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        logs = []
        with self._registry(""), mock.patch.object(rgb, "_log", logs.append):
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self._on_disk().chosen_action)
        self.assertTrue(any("owner unknown" in m for m in logs), logs)

    def test_an_ordinary_reply_with_no_owner_logs_nothing(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        logs = []
        with self._registry(""), mock.patch.object(rgb, "_log", logs.append):
            self.assertIs(rgb._handle_hitl_action(self._task(reply_to_event="$not-a-card", task="hi")), False)
        self.assertFalse(any("owner unknown" in m for m in logs), logs)

    def test_unset_variable_uses_the_registry_owner(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        with self._registry(OWNER) as reads:
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertEqual(rgb._handle_hitl_action(t), "applied")
            self.assertEqual(self._on_disk().chosen_action, "allow", "the store file records the click")
            rgb._handle_hitl_action(self._task(task="Allow"))
            rgb._handle_hitl_action(self._task(hitl_action=dict(t["hitl_action"])))
        self.assertEqual(len(reads), 1, "the registry is read once per process")

    def test_registry_owner_does_not_let_a_non_owner_resolve(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        with self._registry(OWNER):
            click = self._task(user_id="@someone-else:ag2.space",
                               hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                            "action_id": "allow", "guard": self.req.guard})
            self.assertEqual(rgb._handle_hitl_action(click), "ignored")
        self.assertIsNone(self._on_disk().chosen_action)

    def test_explicit_variable_wins_over_the_registry(self):
        os.environ["SPARROW_HA_OWNER"] = "@configured:ag2.space"
        with self._registry(OWNER) as reads:
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertEqual(rgb._handle_hitl_action(t), "ignored", "the registry owner is not the configured one")
            t2 = self._task(user_id="@configured:ag2.space", hitl_action=dict(t["hitl_action"]))
            self.assertEqual(rgb._handle_hitl_action(t2), "applied")
        self.assertEqual(reads, [], "an explicit owner never reads the registry")

    def test_event_channel_claimant_uses_the_registry_owner(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        claimant = rgb._HitlClicks(rgb._hitl_reply_handler("", log=lambda m: None), log=lambda m: None)

        def ev(actor):
            return {"type": "message.created", "event_id": f"$e{os.urandom(3).hex()}", "room_id": "!dm:ag2.space",
                    "actor_id": actor, "content": {"space.ag2.hitl.action": {
                        "hitl_id": self.req.id, "expected_revision": self.req.revision,
                        "action_id": "allow", "guard": self.req.guard}}}
        logs = []
        claimant._log = logs.append
        with self._registry(""), mock.patch.object(rgb, "_log", logs.append):
            self.assertFalse(claimant.claims(ev(OWNER)), "unknown owner: not claimed")
        self.assertTrue(any("owner unknown" in m for m in logs), logs)
        rgb._HITL_OWNER["mxid"] = ""
        with self._registry(OWNER):
            self.assertTrue(claimant.claims(ev("@someone-else:ag2.space")))
            claimant.offer(ev("@someone-else:ag2.space"))
            self.assertIsNone(self._on_disk().chosen_action, "a non-owner never resolves")
            e = ev(OWNER)
            self.assertTrue(claimant.claims(e))
            claimant.offer(e)
        self.assertEqual(self._on_disk().chosen_action, "allow")

    def test_redelivered_click_is_consumed_without_a_second_apply(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertTrue(rgb._handle_hitl_action(t))
        rgb._queue_review_control_result(t)  # what the poll loop does after a consume
        rev = self.mgr.get(self.req.id).revision
        self.assertTrue(rgb._handle_hitl_action(t))
        self.assertEqual(self.mgr.get(self.req.id).revision, rev)

    def test_redelivered_label_reply_is_consumed_while_its_control_result_waits(self):
        """The fallback form carries no hitl_action. While the [no-send] control
        result is still queued (results plane down), a redelivery must be held
        as consumed, not fall through to _write_task as the owner's chat."""
        self.mgr.record_projection(self.req.id, self.req.revision, "$card-redeliver")
        t = self._task(reply_to_event="$card-redeliver", task="Deny")
        self.assertTrue(rgb._handle_hitl_action(t))
        rgb._queue_review_control_result(t)  # POST /v1/results has not succeeded yet
        rev = self.mgr.get(self.req.id).revision
        out = rgb._handle_hitl_action(t)
        self.assertTrue(out, out)
        self.assertFalse(str(out).startswith("rejected:"), out)
        self.assertEqual(self.mgr.get(self.req.id).revision, rev)
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")


    def test_stale_click_is_consumed_and_the_owner_is_told(self):
        stale = {"hitl_id": self.req.id, "expected_revision": self.req.revision + 5,
                 "action_id": "allow", "guard": self.req.guard}
        t = self._task(hitl_action=stale)
        out = rgb._handle_hitl_action(t)
        self.assertTrue(str(out).startswith("rejected:"), out)
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)
        body = "That click did not apply" if str(out).startswith("rejected:") else "[no-send]"
        rgb._queue_review_control_result(t, body=body)
        rec = json.loads(rgb._control_result_path(t["id"]).read_text())
        self.assertIn("did not apply", rec["body"])

    def test_a_click_on_a_turn_requesting_card_is_applied_and_stays_a_task(self):
        """turn_on_action: the store records the click, and the same relay task goes on to the core
        (False = ordinary message) so the executor runs in the turn that follows, not later."""
        req = self.mgr.create(HumanRequirement(
            kind="choice", runtime="report-feedback", message="file the report?",
            guard=f"fb_{os.urandom(5).hex()}", device={"id": f"rf-{os.urandom(2).hex()}"},
            actions=[Action(id="file", kind="confirmation", label="File this bug report")],
            turn_on_action=True))
        t = self._task(hitl_action={"hitl_id": req.id, "expected_revision": req.revision,
                                    "action_id": "file", "guard": req.guard}, task="File this bug report")
        self.assertIs(rgb._handle_hitl_action(t), False, "not consumed: the core takes a turn on it")
        self.assertEqual(t.get("hitl_click"), "true", "the forwarded task says it is a recorded click")
        self.assertIn("hitl_click", rgb._TASK_FIELDS)
        self.assertLess(rgb._TASK_FIELDS.index("hitl_click"), rgb._TASK_FIELDS.index("task"), "a header below task: is body")
        after = self.mgr.get(req.id)
        self.assertEqual((after.status, after.chosen_action), ("in_progress", "file"))
        # Crash seam: applied, then died before _write_task; the redelivered task must pass
        # through again (idempotent write), not be rejected as stale — or the turn is lost.
        again = rgb._handle_hitl_action(t)
        self.assertIs(again, False, "the same click re-offered still owes its turn")
        self.assertEqual(t.get("hitl_click"), "true")
        same = self.mgr.get(req.id)
        self.assertEqual((same.status, same.chosen_action, same.revision), (after.status, after.chosen_action, after.revision), "store unchanged")
        # a DIFFERENT click on the now-answered card is stale as before
        other = self._task(hitl_action={"hitl_id": req.id, "expected_revision": req.revision,
                                        "action_id": "file", "guard": req.guard}, task="File this bug report", id="task-other")
        other["hitl_action"]["expected_revision"] = req.revision  # the pre-click revision, as a stale client would send
        other["hitl_action"]["action_id"] = "file"
        stale = dict(other); stale["hitl_action"] = dict(other["hitl_action"], action_id="skip")
        self.assertTrue(str(rgb._handle_hitl_action(stale)).startswith("rejected:"))

    def test_applied_click_closes_silently(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertEqual(rgb._handle_hitl_action(t), "applied")
        rgb._queue_review_control_result(t)
        self.assertEqual(json.loads(rgb._control_result_path(t["id"]).read_text())["body"], "[no-send]")


if __name__ == "__main__":
    unittest.main()
