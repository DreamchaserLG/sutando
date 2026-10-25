#!/usr/bin/env python3
"""Design contracts for the tutorial's v2 marker (design Appendix A and §9), before any v2 code exists.

- A reference validator for Appendix A's room, journal, operation and review variants, and the A.10
  transitions written as functions: every state the lifecycle reaches, refusals included, validates.
- The load barrier (§9): a record that is visible but not yet durable is synced before anything acts
  on it, so a power loss can never leave a remote effect, or a later local step, without its record.

Both are models; when load_marker_v2(), save_marker_v2() and the state machine land, these tests call
them instead."""
from __future__ import annotations

import copy
import itertools
import unittest

HEX, ID = "a" * 64, "b" * 32
REASONS = ("invariant-failure", "struct-mismatch", "replay-budget")
CAPTURED = ("observed",) + REASONS


class Invalid(Exception):
    pass


def need(cond: bool, why: str) -> None:
    if not cond:
        raise Invalid(why)


def closed(obj: dict, required: set, optional: set = frozenset(), where: str = "") -> None:
    need(isinstance(obj, dict), f"{where}: not an object")
    need(required <= set(obj), f"{where}: missing {sorted(required - set(obj))}")
    need(set(obj) <= required | optional, f"{where}: unknown {sorted(set(obj) - required - optional)}")


OP_COMMON = {"index", "client", "root", "start", "end", "wire_sha256", "struct_v", "struct_sha256", "plan_id",
             "base_ref", "base_sha256", "transmissions", "state"}
OP_EXTRA = {"ready": {"update_b64"}, "send-unknown": {"update_b64"}, "landed": {"landed_at"}, "failed": {"failure"}}


def operation(op: dict, index: int) -> None:
    need(op.get("state") in OP_EXTRA, f"op {index}: state")
    closed(op, OP_COMMON | OP_EXTRA[op["state"]], where=f"op {index}")
    need(op["index"] == index, f"op {index}: index")
    t = op["transmissions"]
    closed(t, {"reserved", "first_at", "last_at", "last_outcome"}, where="transmissions")
    need(0 <= t["reserved"] <= 20, "reserved")
    need((t["reserved"] == 0) == (t["first_at"] is None) == (t["last_at"] is None) == (t["last_outcome"] is None),
         "transmissions: reserved 0 iff never sent")
    if op["state"] in ("send-unknown", "landed"):
        need(t["reserved"] >= 1, "sent needs a reservation")
    if op["state"] == "send-unknown":
        need(t["last_outcome"] == "unknown", "send-unknown outcome")
    if op["state"] == "failed":
        closed(op["failure"], {"reason", "observed_clock", "at"}, where="op failure")
        need(op["failure"]["reason"] in REASONS, "failure reason")
        need((op["failure"]["observed_clock"] is None) == (op["failure"]["reason"] == "replay-budget"), "clock")


J_COMMON = {"state", "op_id", "kind", "prior_current", "proposed", "target", "target_sha256", "plan", "started_at"}
J_EXTRA = {"creating": set(), "filling": {"create_snapshot", "next_op", "operations"},
           "observing": {"create_snapshot", "next_op", "operations", "failure", "capture"}}


def journal(j: dict) -> None:
    need(j.get("state") in J_EXTRA, "journal state")
    closed(j, J_COMMON | J_EXTRA[j["state"]], where="journal")
    if j["state"] == "creating":
        need(j["kind"] is None, "creating kind")
        return
    need(j["kind"] is not None, "kind bound at J1")
    ops, n, plan = j["operations"], j["next_op"], j["plan"]
    for i, op in enumerate(ops):
        operation(op, i)
    need(all(op["state"] == "landed" for op in ops[:n]), "ops before next_op landed")
    if j["state"] == "filling":
        need(0 <= n < len(plan) and len(ops) in (n, n + 1), "filling position")
        need(len(ops) == n or ops[n]["state"] in ("ready", "send-unknown"), "filling current op")
        return
    if j["failure"] is None:
        need(n == len(plan) == len(ops), "observing complete")
    else:
        f = j["failure"]
        need(f["op"] == n and len(ops) == n + 1 and ops[n]["state"] == "failed"
             and ops[n]["failure"]["reason"] == f["reason"], "observing failure")
    need(j["capture"] is None or len(j["capture"]["ranges"]) == len(ops), "capture ranges")


SUMMARY = {"index", "client", "end", "struct_sha256", "state", "reserved", "failure_reason"}
SUMMARY_STATES = ("ready", "send-unknown", "landed", "failed")
CANDIDATE = {"kind", "op_id", "proposed", "target_sha256", "operations", "capture", "failure"}
R_COMMON = {"review_id", "reason", "at", "superseded_by", "findings"}
R_EXTRA = {**{r: CANDIDATE for r in CAPTURED}, "page-missing": CANDIDATE, "create-unrecoverable": CANDIDATE,
           "legacy-in-flight": {"kind", "op_id", "v1_state", "v1_entry_sha256"},
           "marker-lost": {"candidates", "candidates_sha256"}}


def summary(s: dict, i: int, allowed: tuple) -> None:
    closed(s, SUMMARY, where=f"summary {i}")
    need(s["index"] == i and s["state"] in allowed, f"summary {i}: state {s['state']}")
    need((s["failure_reason"] is None) == (s["state"] != "failed"), f"summary {i}: failure reason")


def review(r: dict) -> None:
    need(r.get("reason") in R_EXTRA, "review reason")
    closed(r, R_COMMON | R_EXTRA[r["reason"]], where="review " + r["reason"])
    reason = r["reason"]
    if reason in CAPTURED:
        need(r["capture"] is not None, "captured review without capture")
        need((r["failure"] is None) == (reason == "observed"), "failure iff a failure reason")
        for i, s in enumerate(r["operations"]):
            summary(s, i, ("landed", "failed"))
    elif reason == "page-missing":
        need(r["capture"] is None, "page-missing capture")
        ops = r["operations"]
        for i, s in enumerate(ops):
            summary(s, i, SUMMARY_STATES)
        need(all(s["state"] == "landed" for s in ops[:-1]), "page-missing: only the last op may be open")
    elif reason == "create-unrecoverable":
        need(r["kind"] is None and r["operations"] == [] and r["capture"] is None, "create-unrecoverable")
    elif reason == "marker-lost":
        kinds = [c["kind"] for c in r["candidates"]]
        need(kinds and kinds == sorted(set(kinds)), "marker-lost candidates")


ROOM = {"current", "superseded", "rejected", "unselected", "journal", "active_review_id", "reviews", "decisions"}


def room(rm: dict) -> None:
    closed(rm, ROOM, where="room")
    if rm["journal"] is not None:
        journal(rm["journal"])
        need(rm["active_review_id"] is None, "a journal and an active review at once")
    for key, r in rm["reviews"].items():
        need(key == r["review_id"], "review key")
        review(r)
    need(set(rm["decisions"]) <= set(rm["reviews"]), "decision without a review")
    active = rm["active_review_id"]
    if active is not None:
        need(active in rm["reviews"] and rm["reviews"][active]["superseded_by"] is None
             and active not in rm["decisions"], "active review")
    for key, r in rm["reviews"].items():
        need(key == active or key in rm["decisions"] or r["superseded_by"] is not None, f"orphan review {key}")


def marker(m: dict) -> None:
    closed(m, {"version", "migration", "migrated_from", "rooms"}, where="file")
    need(m["version"] == 2, "version")
    for rm in m["rooms"].values():
        room(rm)


# A.10's transitions, each one write. `fresh` mints review ids.
def fresh(counter=itertools.count()):
    return f"{next(counter):032x}"


def new_room() -> dict:
    return {"current": None, "superseded": [], "rejected": [], "unselected": [], "journal": None,
            "active_review_id": None, "reviews": {}, "decisions": {}}


def j0(m: dict, r: str, plan=(5, 9)) -> None:
    rm = m["rooms"].setdefault(r, new_room())
    rm["journal"] = {"state": "creating", "op_id": "op-1", "kind": None, "prior_current": rm["current"],
                     "proposed": {"kind": None}, "target": "x" * plan[-1], "target_sha256": HEX,
                     "plan": list(plan), "started_at": "t"}


def j1(m, r):
    m["rooms"][r]["journal"].update(state="filling", kind="html-a", create_snapshot={}, next_op=0, operations=[])


def j2(m, r):
    j = m["rooms"][r]["journal"]
    j["operations"].append({"index": j["next_op"], "client": 7, "root": "t", "start": 0, "end": 4,
                            "wire_sha256": HEX, "struct_v": 1, "struct_sha256": HEX, "plan_id": HEX,
                            "base_ref": "ops/op-1/0.base", "base_sha256": HEX, "state": "ready",
                            "update_b64": "AA==", "transmissions": {"reserved": 0, "first_at": None,
                                                                    "last_at": None, "last_outcome": None}})


def j2p(m, r):
    op = m["rooms"][r]["journal"]["operations"][-1]
    op.update(state="send-unknown")
    op["transmissions"] = {"reserved": 1, "first_at": "t", "last_at": "t", "last_outcome": "unknown"}


def j3_landed(m, r):
    j = m["rooms"][r]["journal"]
    op = j["operations"][-1]
    del op["update_b64"]
    op.update(state="landed", landed_at="t")
    j["next_op"] += 1
    if j["next_op"] == len(j["plan"]):
        j.update(state="observing", failure=None, capture=None)


def j4b(m, r):
    j = m["rooms"][r]["journal"]
    j["capture"] = {"ranges": [{"op": i} for i in range(len(j["operations"]))], "findings": []}


def summaries(ops):
    return [{"index": o["index"], "client": o["client"], "end": o["end"], "struct_sha256": o["struct_sha256"],
             "state": o["state"], "reserved": o["transmissions"]["reserved"],
             "failure_reason": o["failure"]["reason"] if o["state"] == "failed" else None} for o in ops]


def mint(rm: dict, rv: dict) -> str:
    """One write: the review, made active; the previous active one superseded by it."""
    v = rv["review_id"] = fresh()
    rv.update(at="t", superseded_by=None)
    if rm["active_review_id"] is not None:
        rm["reviews"][rm["active_review_id"]]["superseded_by"] = v
    rm["reviews"][v], rm["active_review_id"], rm["journal"] = rv, v, None
    return v


def j5(m, r, reason=None):
    rm = m["rooms"][r]
    j = rm["journal"]
    candidate = {"kind": j["kind"], "op_id": j["op_id"], "proposed": j["proposed"],
                 "target_sha256": j["target_sha256"], "operations": summaries(j.get("operations", []))}
    if reason == "page-missing":
        rv = {**candidate, "reason": reason, "capture": None, "failure": None, "findings": ["page-missing"]}
    elif reason == "create-unrecoverable":
        rv = {**candidate, "reason": reason, "capture": None, "failure": None, "findings": [reason]}
    else:
        f = j["failure"]
        rv = {**candidate, "reason": f["reason"] if f else "observed", "capture": j["capture"], "failure": f,
              "findings": []}
    return mint(rm, rv)


def refuse_keep_new(m, r, missing=False):
    """§7: the candidate changed or vanished. A fresh active review, and no decision."""
    rm = m["rooms"][r]
    old = rm["reviews"][rm["active_review_id"]]
    rv = {k: copy.deepcopy(old[k]) for k in CANDIDATE}
    rv.update(reason="page-missing" if missing else "observed", findings=["page-missing"] if missing else [],
              capture=None if missing else {"ranges": [], "findings": []}, failure=None)
    return mint(rm, rv)


def marker_lost(m, r, kinds=("html-a", "html-b")):
    rm = m["rooms"].setdefault(r, new_room())
    return mint(rm, {"reason": "marker-lost", "findings": ["marker-lost"], "candidates_sha256": HEX,
                     "candidates": [{"kind": k, "review_snapshot_sha256": HEX} for k in kinds]})


def empty_file():
    return {"version": 2, "migration": {}, "migrated_from": {}, "rooms": {}}


class SchemaTransitionTest(unittest.TestCase):
    def walk(self, steps):
        m = empty_file()
        for step in steps:
            step(m)
            marker(m)
        return m

    def test_every_write_of_a_full_fill_validates_from_a_clean_first_use(self):
        r = "!room"
        steps = [lambda m: j0(m, r), lambda m: j1(m, r)]
        for _ in range(2):
            steps += [lambda m: j2(m, r), lambda m: j2p(m, r), lambda m: j3_landed(m, r)]
        steps += [lambda m: j4b(m, r), lambda m: j5(m, r)]
        m = self.walk(steps)
        self.assertEqual(m["rooms"][r]["reviews"][m["rooms"][r]["active_review_id"]]["reason"], "observed")

    def test_a_page_missing_from_filling_is_a_valid_review_whatever_the_open_operation(self):
        r = "!room"
        for opened in ([], [j2], [j2, j2p]):
            with self.subTest(open=[f.__name__ for f in opened]):
                m = self.walk([lambda m: j0(m, r), lambda m: j1(m, r)] + [lambda m, f=f: f(m, r) for f in opened]
                              + [lambda m: j5(m, r, "page-missing")])
                states = [s["state"] for s in m["rooms"][r]["reviews"][m["rooms"][r]["active_review_id"]]["operations"]]
                self.assertEqual(states, [{j2: "ready", j2p: "send-unknown"}[opened[-1]]] if opened else [])

    def test_create_unrecoverable_from_creating_validates(self):
        self.walk([lambda m: j0(m, "!room"), lambda m: j5(m, "!room", "create-unrecoverable")])

    def test_refused_keep_new_and_select_mint_a_fresh_active_review_without_a_decision(self):
        r = "!room"
        fill = [lambda m: j0(m, r, (5,)), lambda m: j1(m, r), lambda m: j2(m, r), lambda m: j2p(m, r),
                lambda m: j3_landed(m, r), lambda m: j4b(m, r), lambda m: j5(m, r)]
        for refusal in (lambda m: refuse_keep_new(m, r), lambda m: refuse_keep_new(m, r, missing=True)):
            m = self.walk(fill + [refusal])
            rm = m["rooms"][r]
            self.assertEqual(len(rm["reviews"]), 2)
            self.assertEqual(rm["decisions"], {})
        m = self.walk([lambda m: marker_lost(m, "!lost"), lambda m: marker_lost(m, "!lost", ("html-a",))])
        self.assertEqual(len(m["rooms"]["!lost"]["reviews"]), 2)

    def test_the_validator_rejects_what_the_lifecycle_never_writes(self):
        m = self.walk([lambda m: j0(m, "!room")])
        bad = copy.deepcopy(m)
        bad["rooms"]["!room"]["journal"]["capture"] = None
        self.assertRaises(Invalid, marker, bad)
        bad = copy.deepcopy(m)
        bad["rooms"]["!room"] = {"journal": m["rooms"]["!room"]["journal"]}
        self.assertRaises(Invalid, marker, bad)
        m = self.walk([lambda m: marker_lost(m, "!lost")])
        orphan = copy.deepcopy(m)
        orphan["rooms"]["!lost"]["active_review_id"] = None
        self.assertRaises(Invalid, marker, orphan)


# The load barrier. Steps of one publish: local records and the remote effects they authorise.
STEPS = (("J0", "local"), ("create", "remote"), ("J1", "local"), ("J2", "local"), ("J2'", "local"),
         ("apply", "remote"), ("J3", "local"))


def traces(barrier: bool, starts: int = 3):
    """Every state after a power loss, over up to `starts` processes, each of which may fail the directory
    sync of any save, or crash, at any point. A local step is absent, visible or durable."""
    out, seen, todo = set(), set(), [(("absent",) * len(STEPS), 0)]
    while todo:
        state, n = todo.pop()
        if (state, n) in seen:
            continue
        seen.add((state, n))
        lost = tuple("absent" if s == "visible" else s for s in state)
        out.add(lost)
        if n < starts:
            todo.append((lost, n + 1))
        if n >= starts:
            continue
        loaded = [state]
        if barrier:
            loaded = [tuple("durable" if s == "visible" else s for s in state)]
        for s in loaded:
            s = list(s)
            for k, (_, where) in enumerate(STEPS):
                if s[k] != "absent":
                    continue
                if where == "remote":
                    s[k] = "done"
                    todo.append((tuple(s), n + 1))
                    continue
                s[k] = "visible"
                todo.append((tuple(s), n + 1))  # the directory sync failed: this process stops
                s[k] = "durable"
                todo.append((tuple(s), n + 1))
    return out


def gaps(state: tuple) -> list:
    """A present step after an absent one: a record lost under an effect, or a later step, it authorised."""
    present = [s != "absent" for s in state]
    return [STEPS[k][0] for k in range(1, len(state)) if present[k] and not all(present[:k])]


class LoadBarrierTest(unittest.TestCase):
    def test_with_the_barrier_no_power_loss_leaves_an_effect_without_its_record(self):
        self.assertEqual({s for s in traces(barrier=True) if gaps(s)}, set())

    def test_without_it_a_visible_j0_authorises_a_create_that_survives_j0(self):
        lost = {s for s in traces(barrier=False) if gaps(s)}
        self.assertIn(("absent", "done") + ("absent",) * 5, lost)


if __name__ == "__main__":
    unittest.main()
