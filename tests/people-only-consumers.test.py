#!/usr/bin/env python3
"""Every engine consumer honours the people-only flag (`space.ag2.no_agent: true`).

Readers that hand room messages to an agent omit flagged ones and say how many;
reactors and local taskifiers never act on them. Each case carries its unflagged
control, so "drop everything" cannot satisfy it. No network: the gateway verbs and
the react sender are stubbed at the module seam each consumer already exposes.
"""
import argparse
import contextlib
import glob
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / "skills" / "agent-room-ops"
for p in (_ROOT / "src", _SKILL, _ROOT / "packages" / "ag2-sparrow"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ag2_sparrow.default_observer import ReactObserverHandler  # noqa: E402
from ag2_sparrow.event_consumer import TaskifyHandler  # noqa: E402

FLAG = "space.ag2.no_agent"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _page(*messages):
    return 200, json.dumps({"messages": list(messages), "cursor": None}).encode(), {}


def _msg(eid, body, flagged=False, nested=False, ts=1_700_000_000_000):
    m = {"sender": "@alice:hs", "event_id": eid, "ts": ts, "body": body}
    if flagged:
        if nested:
            m["content"] = {FLAG: True}
        else:
            m[FLAG] = True
    return m


class RoomOpsRead(unittest.TestCase):
    """(a) `room_ops read`: flagged messages are not in `messages`, the count is."""

    def _read(self, *messages, limit=5):
        m = _load("room_ops_read_po", _SKILL / "read.py")
        m.gateway = lambda: ("https://gw.example", {})
        m.http_request = lambda *a, **k: _page(*messages)
        return m.read_room("!r:hs", "@me:hs", limit, gate=None)  # gate=None: gateway enforces

    def test_a_flagged_message_is_omitted_and_counted(self):
        res = self._read(_msg("e1", "for people"), _msg("e2", "lunch?", flagged=True),
                         _msg("e3", "nested flag", flagged=True, nested=True),
                         _msg("e4", "for everyone"))
        self.assertTrue(res["ok"], res)
        self.assertEqual([x["event_id"] for x in res["messages"]], ["e1", "e4"])
        self.assertNotIn("lunch?", json.dumps(res["messages"]))
        self.assertEqual(res["people_only_omitted"], 2)
        self.assertEqual(res["people_only_note"], "2 people-only messages omitted")

    def test_an_unflagged_room_reads_as_before(self):
        res = self._read(_msg("e1", "hello"), _msg("e2", "world"))
        self.assertEqual([x["event_id"] for x in res["messages"]], ["e1", "e2"])
        self.assertEqual(res["people_only_omitted"], 0)
        self.assertNotIn("people_only_note", res, "a quiet result carries no omission line")

    def test_flagged_messages_do_not_spend_the_limit(self):
        res = self._read(_msg("e1", "a"), _msg("e2", "b", flagged=True), _msg("e3", "c"), limit=2)
        self.assertEqual([x["event_id"] for x in res["messages"]], ["e1", "e3"])
        self.assertTrue(res["complete"])


class RoomOpsHistory(unittest.TestCase):
    """(a) `room_ops history`: the window omits flagged messages, per room and in total."""

    def _history(self, *messages):
        m = _load("room_ops_history_po", _SKILL / "history.py")
        m.joined_rooms = lambda agent: {"ok": True, "rooms": ["!r:hs"]}
        m.gateway = lambda: ("https://gw.example", {})
        m.load_gate = lambda: {}
        m.gate_allows = lambda *a: True
        m.http_request = lambda *a, **k: _page(*messages)
        return m.history(1_600_000_000_000, 1_800_000_000_000, pages=1, agent_mxid="@me:hs")

    def test_flagged_messages_are_left_out_of_the_window(self):
        res = self._history(_msg("e1", "keep"), _msg("e2", "people only", flagged=True),
                            _msg("e3", "keep too"))
        row = res["rooms"][0]
        self.assertEqual([x["event_id"] for x in row["messages"]], ["e1", "e3"])
        self.assertEqual(row["people_only_omitted"], 1)
        self.assertEqual(res["people_only_omitted"], 1)
        self.assertEqual(res["people_only_note"], "1 people-only message omitted")
        self.assertIn("note", res, "collect_window's own retention note is not clobbered")

    def test_an_unflagged_window_counts_zero(self):
        res = self._history(_msg("e1", "keep"))
        self.assertEqual(res["rooms"][0]["people_only_omitted"], 0)
        self.assertEqual(res["people_only_omitted"], 0)
        self.assertNotIn("people_only_note", res)


class RoomOpsEventsSurface(unittest.TestCase):
    """(a) `room_ops events stream|pull`: flagged envelopes are neither printed nor returned."""

    def _env(self, eid, flagged):
        c = {"message_id": eid, "body": "x"}
        if flagged:
            c[FLAG] = True
        return {"event_id": eid, "type": "message.created", "room_id": "!r:hs",
                "actor_id": "@alice:hs", "content": c}

    def test_stream_omits_flagged_events_and_reports_the_count(self):
        import room_ops
        envs = [self._env("e1", False), self._env("e2", True), self._env("e3", False)]

        def fake_stream(cursor=None, on_event=None, max_events=None, **_):
            for i, e in enumerate(envs, 1):
                on_event(i, e)
            return len(envs)

        orig = room_ops._events.stream
        room_ops._events.stream = fake_stream
        out = io.StringIO()
        try:
            ns = argparse.Namespace(once=False, max_events=None, cursor_file=None, cursor=0,
                                    strict=False)
            with contextlib.redirect_stdout(out):
                rc = room_ops._events_stream(ns)
        finally:
            room_ops._events.stream = orig
        lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        printed = [l["event_id"] for l in lines if "event_id" in l]
        summary = lines[-1]
        self.assertEqual(rc, 0)
        self.assertEqual(printed, ["e1", "e3"])
        self.assertEqual(summary["events"], 2)
        self.assertEqual(summary["cursor"], 3, "the cursor still advances past a flagged event")
        self.assertEqual(summary["people_only_omitted"], 1)
        self.assertEqual(summary["people_only_note"], "1 people-only message omitted")

    def test_pull_omits_flagged_events_and_reports_the_count(self):
        import room_ops
        envs = [self._env("e1", False), self._env("e2", True)]
        orig = room_ops._events.pull
        room_ops._events.pull = lambda cursor=0, wait=0: {"ok": True, "events": list(envs),
                                                           "cursor": 2}
        try:
            ns = argparse.Namespace(events_cmd="pull", cursor=0, wait=0)
            res = room_ops._dispatch_events(ns)
        finally:
            room_ops._events.pull = orig
        self.assertEqual([e["event_id"] for e in res["events"]], ["e1"])
        self.assertEqual(res["people_only_omitted"], 1)
        self.assertEqual(res["cursor"], 2)

    def test_pull_without_flagged_events_is_unchanged(self):
        import room_ops
        orig = room_ops._events.pull
        room_ops._events.pull = lambda cursor=0, wait=0: {"ok": True,
                                                           "events": [self._env("e1", False)],
                                                           "cursor": 1}
        try:
            res = room_ops._dispatch_events(argparse.Namespace(events_cmd="pull", cursor=0, wait=0))
        finally:
            room_ops._events.pull = orig
        self.assertEqual(len(res["events"]), 1)
        self.assertEqual(res["people_only_omitted"], 0)
        self.assertNotIn("people_only_note", res)


def _event(eid, flagged, etype="message.created", actor="@alice:hs"):
    c = {"message_id": "m-" + eid, "body": "team lunch at noon?"}
    if flagged:
        c[FLAG] = True
    return {"event_id": eid, "type": etype, "room_id": "!r:hs", "actor_id": actor,
            "cursor": 1, "content": c}


class Taskify(unittest.TestCase):
    """(c) the local taskify handler never promotes a flagged event."""

    def _promote(self, event):
        d = tempfile.mkdtemp()
        h = TaskifyHandler(task_dir=d, agent_mxid="@me:hs", threshold=1, log=lambda *a: None)
        settled = h.offer(event)
        return settled, glob.glob(os.path.join(d, "*.txt")), h

    def test_a_flagged_message_writes_no_task_and_settles(self):
        settled, files, h = self._promote(_event("e1", True))
        self.assertEqual(files, [], "a people-only message must not become a local task")
        self.assertEqual(settled, ["e1"], "it settles so the inbox marks it consumed")
        self.assertFalse(h.has_pending())

    def test_a_flagged_edit_is_skipped_too(self):
        settled, files, _ = self._promote(_event("e1", True, etype="message.edited"))
        self.assertEqual(files, [])
        self.assertEqual(settled, ["e1"])

    def test_the_unflagged_control_still_promotes(self):
        _, files, _ = self._promote(_event("e1", False))
        self.assertEqual(len(files), 1)
        self.assertIn("team lunch at noon?", Path(files[0]).read_text())


class _Inner:
    def __init__(self):
        self.offered = []
        self.last_path = None

    def offer(self, event):
        self.offered.append(event)
        return [event.get("event_id")]

    def has_pending(self):
        return False


class Observer(unittest.TestCase):
    """(c) the 👀 observer stays silent on a flagged message; the chain still sees it."""

    def _run(self, event):
        reacts, inner = [], _Inner()
        h = ReactObserverHandler(inner, lambda room, mid, key: reacts.append((room, mid, key)),
                                 "@me:hs", log=lambda *a: None, max_age_s=0)
        out = h.offer(event)
        self.assertTrue(h.flush(5))
        return out, reacts, inner

    def test_no_reaction_on_a_flagged_message(self):
        out, reacts, inner = self._run(_event("e1", True))
        self.assertEqual(reacts, [], "👀 must not land on a people-only message")
        self.assertEqual(out, ["e1"], "offer() stays chain-transparent")
        self.assertEqual(len(inner.offered), 1, "the inner handler still decides on it")

    def test_the_unflagged_control_still_reacts(self):
        _, reacts, _ = self._run(_event("e1", False))
        self.assertEqual(reacts, [("!r:hs", "m-e1", "\U0001F440")])


class PackageCopyIsTheSameModule(unittest.TestCase):
    def test_the_package_copy_is_byte_identical_to_src(self):
        src = (_ROOT / "src" / "people_only.py").read_text()
        pkg = (_ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow" / "people_only.py").read_text()
        self.assertEqual(src, pkg, "run packages/ag2-sparrow/tools/sync_from_src.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
