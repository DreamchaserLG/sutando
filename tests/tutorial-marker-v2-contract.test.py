#!/usr/bin/env python3
"""Design contracts for the tutorial's v2 marker (design Appendix A and §9), before any v2 code exists.

- A reference validator for Appendix A.1 to A.8, and every A.10 write as a function building a complete
  record: every state the lifecycle reaches, refusals and terminal outcomes included, validates, and
  each write the design forbids is rejected.
- The load barrier (§9), staged: fixed paths, then validation, then the sidecars validation names. A
  file and directory-entry model shows no power loss leaves a durable record without what it needs.

Both are models; when load_marker_v2(), save_marker_v2() and the state machine land, these tests call
them instead."""
from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import json
import re
import struct
import unittest

REASONS = ("invariant-failure", "struct-mismatch", "replay-budget")
CAPTURED = ("observed",) + REASONS
OP_STATES = ("ready", "send-unknown", "landed", "failed")
OUTCOMES = (None, "unknown", "acked", "timeout", "error")
TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
V1_SOURCES = ("v1-complete", "v1-foreign", "v1-interrupted", "v1-previous", "v1-updating")


class Invalid(Exception):
    pass


def need(cond, why: str) -> None:
    if not cond:
        raise Invalid(why)


def closed(obj, required: set, optional: set = frozenset(), where: str = "") -> None:
    need(isinstance(obj, dict), f"{where}: not an object")
    need(set(required) <= set(obj), f"{where}: missing {sorted(set(required) - set(obj))}")
    need(set(obj) <= set(required) | set(optional), f"{where}: unknown {sorted(set(obj) - set(required) - set(optional))}")


def is_int(x, lo=None, hi=None) -> bool:
    return type(x) is int and (lo is None or x >= lo) and (hi is None or x <= hi)


def is_hex(x, n=64) -> bool:
    return isinstance(x, str) and len(x) == n and all(c in "0123456789abcdef" for c in x)


def is_id(x) -> bool:
    return is_hex(x, 32)


def is_ts(x) -> bool:
    return isinstance(x, str) and bool(TS.fullmatch(x))


def is_kind(x) -> bool:
    return isinstance(x, str) and x.startswith("html-") and len(x) > 5


def is_str(x) -> bool:
    return isinstance(x, str)


def opt(check, x) -> bool:
    return x is None or check(x)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def canonical(x) -> bytes:
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def candidates_digest(candidates: list) -> str:
    """§8: u64(count), then per candidate bytes(kind), bytes(token or empty), u8 null flag."""
    def b(x: bytes) -> bytes:
        return struct.pack(">Q", len(x)) + x
    out = struct.pack(">Q", len(candidates))
    for c in candidates:
        token = c["review_snapshot_sha256"]
        out += b(c["kind"].encode()) + b((token or "").encode()) + bytes([token is None])
    return sha(out)


PROPOSED = {"revision", "name", "core", "audience", "agent_mxid", "prompts", "roles", "media", "list_sha256",
            "target_sha256", "kind"}


def proposed(p, where="proposed") -> None:
    closed(p, PROPOSED, where=where)
    need(is_str(p["revision"]) and p["revision"], f"{where}: revision")
    need(all(is_str(p[k]) for k in ("name", "core", "audience", "agent_mxid")), f"{where}: strings")
    need(all(isinstance(p[k], list) and all(map(is_str, p[k])) for k in ("prompts", "roles")), f"{where}: lists")
    need(isinstance(p["media"], dict) and all(is_str(v) for v in p["media"].values()), f"{where}: media")
    need(is_hex(p["list_sha256"]) and is_hex(p["target_sha256"]) and opt(is_kind, p["kind"]), f"{where}: digests")


def current(c, where="current") -> None:
    need(isinstance(c, dict) and type(c.get("opaque")) is bool, f"{where}: opaque")
    if not c["opaque"]:
        closed(c, {"opaque", "kind", "op_id", "revision", "proposed", "target_sha256", "accepted_review_id",
                   "accepted_review_snapshot_sha256", "accepted_observed_sha256"}, where=where)
        proposed(c["proposed"], where + ".proposed")
        need(is_kind(c["kind"]) and c["kind"] == c["proposed"]["kind"], f"{where}: kind")
        need(is_id(c["op_id"]) and c["revision"] == c["proposed"]["revision"], f"{where}: op_id, revision")
        need(c["target_sha256"] == c["proposed"]["target_sha256"], f"{where}: target")
        need(is_id(c["accepted_review_id"]) and is_hex(c["accepted_review_snapshot_sha256"])
             and is_hex(c["accepted_observed_sha256"]), f"{where}: acceptance")
        return
    closed(c, {"opaque", "kind", "revision", "proposed", "source", "review_id"}, {"v1_entry_sha256"}, where)
    need(is_kind(c["kind"]) and opt(is_str, c["revision"]) and c["proposed"] is None, f"{where}: opaque fields")
    if c["source"] == "manual-selection":
        need(is_id(c["review_id"]) and "v1_entry_sha256" not in c, f"{where}: manual-selection")
    else:
        need(c["source"] in V1_SOURCES, f"{where}: source")
        need(c["review_id"] is None and is_hex(c.get("v1_entry_sha256")), f"{where}: v1 source")


def operation(op, i: int, op_id: str) -> None:
    need(isinstance(op, dict) and op.get("state") in OP_STATES, f"op {i}: state")
    extra = {"ready": {"update_b64"}, "send-unknown": {"update_b64"}, "landed": {"landed_at"},
             "failed": {"failure"}}[op["state"]]
    closed(op, {"index", "client", "root", "start", "end", "wire_sha256", "struct_v", "struct_sha256", "plan_id",
                "base_ref", "base_sha256", "transmissions", "state"} | extra, where=f"op {i}")
    need(op["index"] == i and is_int(op["client"], 1, 2 ** 53 - 1) and is_str(op["root"]) and op["root"], f"op {i}")
    need(op["start"] == 0 and type(op["start"]) is int and is_int(op["end"], 1) and op["struct_v"] == 1, f"op {i}: range")
    need(all(is_hex(op[k]) for k in ("wire_sha256", "struct_sha256", "plan_id", "base_sha256")), f"op {i}: digests")
    need(op["base_ref"] == f"ops/{op_id}/{i}.base", f"op {i}: base_ref is not canonical")
    t = op["transmissions"]
    closed(t, {"reserved", "first_at", "last_at", "last_outcome"}, where=f"op {i}: transmissions")
    need(is_int(t["reserved"], 0, 20) and opt(is_ts, t["first_at"]) and opt(is_ts, t["last_at"])
         and t["last_outcome"] in OUTCOMES, f"op {i}: transmissions")
    need((t["reserved"] == 0) == (t["first_at"] is None) == (t["last_at"] is None) == (t["last_outcome"] is None),
         f"op {i}: reserved 0 iff never sent")
    if op["state"] in ("ready", "send-unknown"):
        try:
            base64.b64decode(op["update_b64"], validate=True)
        except (ValueError, TypeError):
            raise Invalid(f"op {i}: update_b64") from None
    if op["state"] in ("send-unknown", "landed"):
        need(t["reserved"] >= 1, f"op {i}: sent needs a reservation")
    if op["state"] == "send-unknown":
        need(t["last_outcome"] == "unknown", f"op {i}: send-unknown outcome")
    if op["state"] == "landed":
        need(is_ts(op["landed_at"]), f"op {i}: landed_at")
    if op["state"] == "failed":
        f = op["failure"]
        closed(f, {"reason", "observed_clock", "at"}, where=f"op {i}: failure")
        need(f["reason"] in REASONS and is_ts(f["at"]) and opt(lambda x: is_int(x, 0), f["observed_clock"]),
             f"op {i}: failure fields")
        need((f["observed_clock"] is None) == (f["reason"] == "replay-budget"), f"op {i}: observed clock")


def failure(f, where) -> None:
    closed(f, {"op", "reason", "at"}, where=where)
    need(is_int(f["op"], 0) and f["reason"] in REASONS and is_ts(f["at"]), f"{where}: fields")


def capture(c, digests: list, where="capture") -> None:
    """digests: each operation's struct_sha256, in order."""
    closed(c, {"read_at", "review_token_v", "review_snapshot_sha256", "observed_sha256", "doc_sha256",
               "foreign_text_items", "ranges", "findings"}, where=where)
    need(is_ts(c["read_at"]) and c["review_token_v"] == 1 and type(c["review_token_v"]) is int, f"{where}: header")
    need(opt(is_hex, c["review_snapshot_sha256"]) and is_hex(c["observed_sha256"]) and is_hex(c["doc_sha256"]),
         f"{where}: digests")
    need((c["review_snapshot_sha256"] is None) == (c["foreign_text_items"] is None)
         and opt(lambda x: is_int(x, 0), c["foreign_text_items"]), f"{where}: token and foreign items")
    need(isinstance(c["ranges"], list) and len(c["ranges"]) == len(digests), f"{where}: one range per op")
    bad = []
    for i, r in enumerate(c["ranges"]):
        closed(r, {"op", "digest", "struct_ok"}, where=f"{where}.ranges[{i}]")
        need(r["op"] == i and opt(is_hex, r["digest"]) and type(r["struct_ok"]) is bool, f"{where}.ranges[{i}]")
        need(r["struct_ok"] == (r["digest"] == digests[i]), f"{where}.ranges[{i}]: struct_ok")
        if not r["struct_ok"]:
            bad.append(f"struct-mismatch:{i}")
    f = c["findings"]
    need(isinstance(f, list) and f == sorted(set(f)), f"{where}: findings sorted and unique")
    need([x for x in f if x.startswith("struct-mismatch:")] == sorted(bad), f"{where}: struct findings")
    need(set(f) - set(bad) <= {"bytes-differ", "non-empty-after-create"}, f"{where}: unknown finding")


J_COMMON = {"state", "op_id", "kind", "prior_current", "proposed", "target", "target_sha256", "plan", "started_at"}
J_EXTRA = {"creating": set(), "filling": {"create_snapshot", "next_op", "operations"},
           "observing": {"create_snapshot", "next_op", "operations", "failure", "capture"}}


def journal(j) -> None:
    need(isinstance(j, dict) and j.get("state") in J_EXTRA, "journal state")
    closed(j, J_COMMON | J_EXTRA[j["state"]], where="journal")
    need(is_id(j["op_id"]) and opt(is_kind, j["kind"]) and is_ts(j["started_at"]), "journal: header")
    if j["prior_current"] is not None:
        current(j["prior_current"], "journal.prior_current")
    proposed(j["proposed"], "journal.proposed")
    need(j["proposed"]["kind"] == j["kind"], "journal: proposed.kind")
    need(is_str(j["target"]) and j["target_sha256"] == sha(j["target"].encode())
         == j["proposed"]["target_sha256"], "journal: target digest")
    data, plan = j["target"].encode(), j["plan"]
    need(isinstance(plan, list) and plan and all(is_int(b, 1) for b in plan), "journal: plan")
    need(all(a < b for a, b in zip(plan, plan[1:])) and plan[-1] == len(data), "journal: plan boundaries")
    for b in plan:
        try:
            data[:b].decode()
        except UnicodeDecodeError:
            raise Invalid("journal: plan splits a code point") from None
    if j["state"] == "creating":
        need(j["kind"] is None, "creating: kind")
        return
    need(j["kind"] is not None, "kind bound at J1")
    s = j["create_snapshot"]
    closed(s, {"read_at", "doc_sha256", "text_sha256"}, where="create_snapshot")
    need(is_ts(s["read_at"]) and is_hex(s["doc_sha256"]) and is_hex(s["text_sha256"]), "create_snapshot")
    ops, n = j["operations"], j["next_op"]
    need(isinstance(ops, list) and is_int(n, 0) and len(ops) <= len(plan), "journal: operations")
    for i, op in enumerate(ops):
        operation(op, i, j["op_id"])
    need(all(op["state"] == "landed" for op in ops[:n]), "ops before next_op landed")
    if j["state"] == "filling":
        need(n < len(plan) and len(ops) in (n, n + 1), "filling position")
        need(len(ops) == n or ops[n]["state"] in ("ready", "send-unknown"), "filling current op")
        return
    if j["failure"] is None:
        need(n == len(plan) == len(ops), "observing complete")
    else:
        f = j["failure"]
        failure(f, "journal.failure")
        need(f["op"] == n and len(ops) == n + 1 and ops[n]["state"] == "failed"
             and ops[n]["failure"]["reason"] == f["reason"], "observing failure")
    if j["capture"] is not None:
        capture(j["capture"], [op["struct_sha256"] for op in ops], "journal.capture")


def summary(s, i: int) -> None:
    closed(s, {"index", "client", "end", "struct_sha256", "state", "reserved", "failure_reason"}, where=f"summary {i}")
    need(s["index"] == i and is_int(s["client"], 1, 2 ** 53 - 1) and is_int(s["end"], 1)
         and is_hex(s["struct_sha256"]) and s["state"] in OP_STATES and is_int(s["reserved"], 0, 20), f"summary {i}")
    if s["state"] in ("send-unknown", "landed"):
        need(s["reserved"] >= 1, f"summary {i}: a sent operation has a reservation")
    if s["state"] == "failed":
        need(s["failure_reason"] in REASONS, f"summary {i}: failure reason")
    else:
        need(s["failure_reason"] is None, f"summary {i}: failure reason")


CANDIDATE = {"kind", "op_id", "proposed", "target_sha256", "operations", "capture", "failure"}
R_COMMON = {"review_id", "reason", "at", "superseded_by", "findings"}
R_EXTRA = {**{r: CANDIDATE for r in CAPTURED}, "page-missing": CANDIDATE, "create-unrecoverable": CANDIDATE,
           "legacy-in-flight": {"kind", "op_id", "v1_state", "v1_entry_sha256"},
           "marker-lost": {"candidates", "candidates_sha256"}}


def candidate_vector(cs, where, empty=False) -> None:
    need(isinstance(cs, list) and (empty or cs), f"{where}: candidates")
    for c in cs:
        closed(c, {"kind", "review_snapshot_sha256"}, where=f"{where}: candidate")
        need(is_kind(c["kind"]) and opt(is_hex, c["review_snapshot_sha256"]), f"{where}: candidate fields")
    kinds = [c["kind"] for c in cs]
    need(kinds == sorted(set(kinds)), f"{where}: candidates sorted by kind, unique")


def review(r) -> None:
    need(isinstance(r, dict) and r.get("reason") in R_EXTRA, "review reason")
    reason, where = r["reason"], "review " + r["reason"]
    closed(r, R_COMMON | R_EXTRA[reason], where=where)
    need(is_id(r["review_id"]) and is_ts(r["at"]) and opt(is_id, r["superseded_by"]), f"{where}: header")
    need(isinstance(r["findings"], list) and all(map(is_str, r["findings"])), f"{where}: findings")
    if reason == "legacy-in-flight":
        need(opt(is_kind, r["kind"]) and opt(is_str, r["op_id"]) and r["v1_state"] in ("creating", "pending", "updating")
             and is_hex(r["v1_entry_sha256"]) and r["findings"] == ["v1-" + r["v1_state"]], f"{where}: fields")
        return
    if reason == "marker-lost":
        candidate_vector(r["candidates"], where)
        need(r["candidates_sha256"] == candidates_digest(r["candidates"]), f"{where}: candidates_sha256")
        need(r["findings"] == ["marker-lost"], f"{where}: findings")
        return
    proposed(r["proposed"], where + ".proposed")
    need(r["kind"] == r["proposed"]["kind"] and is_id(r["op_id"]), f"{where}: kind, op_id")
    need(r["target_sha256"] == r["proposed"]["target_sha256"], f"{where}: target")
    ops = r["operations"]
    need(isinstance(ops, list), f"{where}: operations")
    for i, s in enumerate(ops):
        summary(s, i)
    f = r["failure"]
    if f is not None:
        failure(f, where + ".failure")
        need(ops and f["op"] == len(ops) - 1 and ops[-1]["state"] == "failed"
             and ops[-1]["failure_reason"] == f["reason"], f"{where}: failure names the last operation")
    if reason == "create-unrecoverable":
        need(r["kind"] is None and ops == [] and r["capture"] is None and f is None, f"{where}: fields")
        need(r["findings"] == ["create-unrecoverable"], f"{where}: findings")
        return
    need(is_kind(r["kind"]), f"{where}: kind")
    if reason == "page-missing":
        need(r["capture"] is None and r["findings"] == ["page-missing"], f"{where}: fields")
        need(all(s["state"] == "landed" for s in ops[:-1]), f"{where}: only the last op may be open")
        need(f is None or ops[-1]["state"] == "failed", f"{where}: failure")
        return
    need(r["capture"] is not None, f"{where}: a captured review without a capture")
    capture(r["capture"], [s["struct_sha256"] for s in ops], where + ".capture")
    need((f is None) == (reason == "observed") and (f is None or f["reason"] == reason), f"{where}: failure")
    need(all(s["state"] == "landed" for s in (ops if f is None else ops[:-1])), f"{where}: operations landed")
    need(r["findings"] == r["capture"]["findings"], f"{where}: findings")


def decision(d, room_key: str, r: dict) -> None:
    need(isinstance(d, dict) and is_str(d.get("choice")), "decision choice")
    choice, where = d["choice"], "decision " + str(d.get("choice"))
    common = {"room", "review_id", "at", "choice"}
    if choice == "keep-new":
        closed(d, common | {"kind", "op_id", "review_snapshot_sha256"}, where=where)
        need(r["reason"] in CAPTURED and r["capture"]["review_snapshot_sha256"] is not None
             and d["review_snapshot_sha256"] == r["capture"]["review_snapshot_sha256"], f"{where}: token")
    elif choice == "keep-old":
        closed(d, common | {"kind", "op_id"}, where=where)
        need(r["reason"] != "marker-lost", f"{where}: on marker-lost")
    elif choice.startswith("select:") or choice == "vanished":
        closed(d, common | {"candidates", "candidates_sha256"}, where=where)
        need(r["reason"] == "marker-lost", f"{where}: only on marker-lost")
        candidate_vector(d["candidates"], where, empty=choice == "vanished")
        need((d["candidates"] == []) == (choice == "vanished"), f"{where}: vanished iff no candidate")
        need(d["candidates_sha256"] == candidates_digest(d["candidates"]), f"{where}: candidates_sha256")
        if choice not in ("select:none", "vanished"):
            need(choice[7:] in {c["kind"] for c in d["candidates"]}, f"{where}: kind not a candidate")
    else:
        raise Invalid(f"{where}: unknown choice")
    need(d["room"] == room_key and d["review_id"] == r["review_id"] and is_ts(d["at"]), f"{where}: identity")
    if choice in ("keep-new", "keep-old"):
        need(d["kind"] == r["kind"] and d["op_id"] == r["op_id"], f"{where}: names its review's candidate")


ROOM = {"current", "superseded", "rejected", "unselected", "journal", "active_review_id", "reviews", "decisions"}


def room(rm, key: str, v1_entries: dict) -> None:
    closed(rm, ROOM, where=f"room {key}")
    reviews, decisions, active = rm["reviews"], rm["decisions"], rm["active_review_id"]
    need(isinstance(reviews, dict) and isinstance(decisions, dict), "room: maps")
    for k, r in reviews.items():
        review(r)
        need(k == r["review_id"], "review key")
        if r["reason"] == "legacy-in-flight":
            need(r["v1_entry_sha256"] == v1_entries.get(key), "legacy-in-flight: v1 entry digest")
    for k, d in decisions.items():
        need(k in reviews, "a decision without a review")
        decision(d, key, reviews[k])
    need(active is None or (active in reviews and reviews[active]["superseded_by"] is None
                            and active not in decisions), "active review")
    if rm["journal"] is not None:
        journal(rm["journal"])
        need(active is None, "a journal and an active review at once")
    # Supersession: every chain names reviews of this room, never repeats, and ends at authority.
    for k in reviews:
        seen, at = set(), k
        while reviews[at]["superseded_by"] is not None:
            seen.add(at)
            at = reviews[at]["superseded_by"]
            need(at in reviews, f"review {k}: superseded_by a review that doesn't exist")
            need(at not in seen, f"review {k}: a supersession cycle")
        need(at == active or at in decisions, f"review {k}: its chain ends at no active or decided review")

    def decided(review_id, choices):
        d = decisions.get(review_id)
        need(d is not None and any(d["choice"] == c or (c == "select:" and d["choice"].startswith(c))
                                   for c in choices), f"room: {review_id} has no {choices} decision")
        return d
    entries = ([rm["current"]] if rm["current"] is not None else []) + list(rm["superseded"])
    need(isinstance(rm["superseded"], list), "superseded")
    for c in entries:
        current(c)
        if not c["opaque"]:
            d = decided(c["accepted_review_id"], ("keep-new",))
            need(d["review_snapshot_sha256"] == c["accepted_review_snapshot_sha256"]
                 and d["kind"] == c["kind"] and d["op_id"] == c["op_id"], "current: its keep-new decision")
        elif c["source"] == "manual-selection":
            need(decided(c["review_id"], ("select:",))["choice"] == "select:" + c["kind"], "current: its selection")
    need(isinstance(rm["rejected"], list), "rejected")
    for x in rm["rejected"]:
        closed(x, {"kind", "op_id", "revision", "review_id"}, where="rejected")
        r = reviews.get(x["review_id"])
        need(r is not None and r["reason"] in R_EXTRA and "proposed" in r, "rejected: names a candidate review")
        decided(x["review_id"], ("keep-old",))
        need(x["kind"] == r["kind"] and x["op_id"] == r["op_id"] and x["revision"] == r["proposed"]["revision"],
             "rejected: copies its review")
    need(isinstance(rm["unselected"], list), "unselected")
    for x in rm["unselected"]:
        closed(x, {"kind", "review_snapshot_sha256", "review_id"}, where="unselected")
        d = decided(x["review_id"], ("select:",))
        need({"kind": x["kind"], "review_snapshot_sha256": x["review_snapshot_sha256"]} in d["candidates"]
             and d["choice"] != "select:" + x["kind"], "unselected: an unchosen candidate of its selection")


def marker(m) -> None:
    closed(m, {"version", "migration", "migrated_from", "rooms"}, where="file")
    need(m["version"] == 2 and type(m["version"]) is int, "version")
    mg, mf = m["migration"], m["migrated_from"]
    closed(mg, {"phase", "source", "converted_at", "fenced_at"}, where="migration")
    need(mg["phase"] in ("converted", "fenced") and is_ts(mg["converted_at"]), "migration: phase")
    need((mg["fenced_at"] is None) == (mg["phase"] == "converted") and opt(is_ts, mg["fenced_at"]), "fenced_at")
    src = mg["source"]
    need(isinstance(src, dict) and src.get("kind") in ("legacy", "none"), "migration.source.kind")
    if src["kind"] == "legacy":
        closed(src, {"kind", "v1_sha256", "v1_size", "backup"}, where="migration.source")
        need(is_hex(src["v1_sha256"]) and is_int(src["v1_size"], 0) and src["backup"] == "getting-started.v1.bak",
             "migration.source fields")
    else:
        closed(src, {"kind"}, where="migration.source")
    closed(mf, {"at", "v1_rooms"}, where="migrated_from")
    need(mf["at"] == mg["converted_at"] and isinstance(mf["v1_rooms"], dict), "migrated_from.at")
    need(src["kind"] == "legacy" or mf["v1_rooms"] == {}, "a none source has no v1 rooms")
    need(isinstance(m["rooms"], dict), "rooms")
    v1_entries = {}
    for key, v in mf["v1_rooms"].items():
        closed(v, {"entry_sha256", "entry"}, where="migrated_from room")
        need(isinstance(v["entry"], dict) and v["entry_sha256"] == sha(canonical(v["entry"])), "v1 entry digest")
        need(key in m["rooms"], "a v1 room missing from rooms")
        v1_entries[key] = v["entry_sha256"]
    for key, rm in m["rooms"].items():
        need(is_str(key) and key, "room key")
        room(rm, key, v1_entries)


# A.10's writes, each one function building the complete record it writes. Ids are minted in order.
T = "2026-10-25T00:00:00Z"
H = "c" * 64
_ids = itertools.count(1)


def mint_id() -> str:
    return f"{next(_ids):032x}"


def m2(v1_rooms: "dict | None") -> dict:
    """M2: the whole file. None is a clean first use (source none)."""
    if v1_rooms is None:
        return {"version": 2, "rooms": {}, "migrated_from": {"at": T, "v1_rooms": {}},
                "migration": {"phase": "converted", "source": {"kind": "none"}, "converted_at": T, "fenced_at": None}}
    m = {"version": 2, "rooms": {}, "migrated_from": {"at": T, "v1_rooms": {}},
         "migration": {"phase": "converted", "converted_at": T, "fenced_at": None,
                       "source": {"kind": "legacy", "v1_sha256": H, "v1_size": 10, "backup": "getting-started.v1.bak"}}}
    for key, entry in v1_rooms.items():
        digest = sha(canonical(entry))
        m["migrated_from"]["v1_rooms"][key] = {"entry_sha256": digest, "entry": entry}
        rm = m["rooms"][key] = new_room()
        if entry.get("state") in ("creating", "pending"):
            mint(rm, {"reason": "legacy-in-flight", "kind": entry.get("kind"), "op_id": entry.get("operation_id"),
                      "v1_state": entry["state"], "v1_entry_sha256": digest, "findings": ["v1-" + entry["state"]]})
        else:
            rm["current"] = {"opaque": True, "kind": entry["kind"], "revision": None, "proposed": None,
                             "source": "v1-complete", "review_id": None, "v1_entry_sha256": digest}
    return m


def m4(m):
    m["migration"].update(phase="fenced", fenced_at=T)


def new_room() -> dict:
    return {"current": None, "superseded": [], "rejected": [], "unselected": [], "journal": None,
            "active_review_id": None, "reviews": {}, "decisions": {}}


def a_proposed(kind=None, target="Getting started, two chunks", revision="r1") -> dict:
    return {"revision": revision, "name": "Blue", "core": "codex", "audience": "owner", "agent_mxid": "@a:x",
            "prompts": ["p"], "roles": ["r"], "media": {"m": "u"}, "list_sha256": H,
            "target_sha256": sha(target.encode()), "kind": kind}


def j0(m, r, target="Getting started, two chunks", revision="r1"):
    rm = m["rooms"].setdefault(r, new_room())
    data = target.encode()
    rm["journal"] = {"state": "creating", "op_id": mint_id(), "kind": None, "prior_current": copy.deepcopy(rm["current"]),
                     "proposed": a_proposed(None, target, revision), "target": target,
                     "target_sha256": sha(data), "plan": [len(data) // 2, len(data)], "started_at": T}


def j1(m, r, kind="html-a"):
    j = m["rooms"][r]["journal"]
    j.update(state="filling", kind=kind, next_op=0, operations=[],
             create_snapshot={"read_at": T, "doc_sha256": H, "text_sha256": H})
    j["proposed"]["kind"] = kind


def j2(m, r):
    j = m["rooms"][r]["journal"]
    i = j["next_op"]
    j["operations"].append({
        "index": i, "client": 7 + i, "root": "t", "start": 0, "end": 4, "wire_sha256": H, "struct_v": 1,
        "struct_sha256": f"{i:064x}", "plan_id": H, "base_ref": f"ops/{j['op_id']}/{i}.base", "base_sha256": H,
        "state": "ready", "update_b64": "AA==",
        "transmissions": {"reserved": 0, "first_at": None, "last_at": None, "last_outcome": None}})


def j2p(m, r):
    op = m["rooms"][r]["journal"]["operations"][-1]
    t = op["transmissions"]
    op["state"] = "send-unknown"
    op["transmissions"] = {"reserved": t["reserved"] + 1, "first_at": t["first_at"] or T, "last_at": T,
                           "last_outcome": "unknown"}


def to_failed(j, reason, clock):
    op = j["operations"][-1]
    op.pop("update_b64", None)
    op.update(state="failed", failure={"reason": reason, "observed_clock": clock, "at": T})
    j.update(state="observing", failure={"op": op["index"], "reason": reason, "at": T}, capture=None)


def j2p_cap(m, r):
    """J2′ refused at a cap: failed (replay-budget), and observing, in one write."""
    to_failed(m["rooms"][r]["journal"], "replay-budget", None)


def j3_landed(m, r):
    j = m["rooms"][r]["journal"]
    op = j["operations"][-1]
    del op["update_b64"]
    op.update(state="landed", landed_at=T)
    j["next_op"] += 1
    if j["next_op"] == len(j["plan"]):
        j.update(state="observing", failure=None, capture=None)


def j3_failed(m, r, reason="invariant-failure"):
    to_failed(m["rooms"][r]["journal"], reason, 2)


def j3_ready(m, r, outcome="timeout"):
    op = m["rooms"][r]["journal"]["operations"][-1]
    op["state"] = "ready"
    op["transmissions"]["last_outcome"] = outcome


def a_capture(digests, token=H, mismatch=()) -> dict:
    ranges = [{"op": i, "digest": "d" * 64 if i in mismatch else d, "struct_ok": i not in mismatch}
              for i, d in enumerate(digests)]
    return {"read_at": T, "review_token_v": 1, "review_snapshot_sha256": token, "observed_sha256": H,
            "doc_sha256": H, "foreign_text_items": None if token is None else 0, "ranges": ranges,
            "findings": sorted(f"struct-mismatch:{i}" for i in mismatch)}


def j4b(m, r, token=H):
    j = m["rooms"][r]["journal"]
    j["capture"] = a_capture([op["struct_sha256"] for op in j["operations"]], token)


def summaries(ops):
    return [{"index": o["index"], "client": o["client"], "end": o["end"], "struct_sha256": o["struct_sha256"],
             "state": o["state"], "reserved": o["transmissions"]["reserved"],
             "failure_reason": o["failure"]["reason"] if o["state"] == "failed" else None} for o in ops]


def mint(rm: dict, rv: dict) -> str:
    """One write: the review, made active; the previous active one superseded by it."""
    v = rv["review_id"] = mint_id()
    rv.update(at=T, superseded_by=None)
    if rm["active_review_id"] is not None:
        rm["reviews"][rm["active_review_id"]]["superseded_by"] = v
    rm["reviews"][v], rm["active_review_id"], rm["journal"] = rv, v, None
    return v


def j5(m, r, reason=None):
    rm = m["rooms"][r]
    j = rm["journal"]
    rv = {"kind": j["kind"], "op_id": j["op_id"], "proposed": copy.deepcopy(j["proposed"]),
          "target_sha256": j["target_sha256"], "operations": summaries(j.get("operations", [])),
          "failure": copy.deepcopy(j.get("failure"))}
    if reason in ("page-missing", "create-unrecoverable"):
        rv.update(reason=reason, capture=None, findings=[reason])
    else:
        f = j["failure"]
        rv.update(reason=f["reason"] if f else "observed", capture=copy.deepcopy(j["capture"]),
                  findings=list(j["capture"]["findings"]))
    return mint(rm, rv)


def keep_new(m, r):
    rm = m["rooms"][r]
    v = rm["active_review_id"]
    rv = rm["reviews"][v]
    token = rv["capture"]["review_snapshot_sha256"]
    rm["decisions"][v] = {"room": r, "review_id": v, "at": T, "choice": "keep-new", "kind": rv["kind"],
                          "op_id": rv["op_id"], "review_snapshot_sha256": token}
    if rm["current"] is not None:
        rm["superseded"].append(rm["current"])
    rm["current"] = {"opaque": False, "kind": rv["kind"], "op_id": rv["op_id"], "revision": rv["proposed"]["revision"],
                     "proposed": copy.deepcopy(rv["proposed"]), "target_sha256": rv["target_sha256"],
                     "accepted_review_id": v, "accepted_review_snapshot_sha256": token,
                     "accepted_observed_sha256": rv["capture"]["observed_sha256"]}
    rm["active_review_id"] = None


def keep_old(m, r, v=None):
    """§6: an active candidate (create-unrecoverable included) is rejected; legacy-in-flight and stale are not."""
    rm = m["rooms"][r]
    v = v or rm["active_review_id"]
    rv = rm["reviews"][v]
    rm["decisions"][v] = {"room": r, "review_id": v, "at": T, "choice": "keep-old", "kind": rv["kind"],
                          "op_id": rv["op_id"]}
    if v == rm["active_review_id"]:
        if "proposed" in rv:
            rm["rejected"].append({"kind": rv["kind"], "op_id": rv["op_id"], "revision": rv["proposed"]["revision"],
                                   "review_id": v})
        rm["active_review_id"] = None


def refuse_keep_new(m, r, missing=False):
    """§7: the candidate changed or vanished. A fresh active review, and no decision."""
    rm = m["rooms"][r]
    old = rm["reviews"][rm["active_review_id"]]
    rv = {k: copy.deepcopy(old[k]) for k in ("kind", "op_id", "proposed", "target_sha256", "operations", "failure")}
    if missing:
        rv.update(reason="page-missing", capture=None, findings=["page-missing"])
    else:
        rv.update(reason=old["failure"]["reason"] if old["failure"] else "observed", findings=[],
                  capture=a_capture([s["struct_sha256"] for s in rv["operations"]], "e" * 64))
    return mint(rm, rv)


def vector(kinds, token=H):
    return [{"kind": k, "review_snapshot_sha256": token} for k in sorted(kinds)]


def marker_lost(m, r, kinds=("html-a", "html-b")):
    """§8: minted only when listing shows at least one same-title page."""
    rm = m["rooms"].setdefault(r, new_room())
    cs = vector(kinds)
    return mint(rm, {"reason": "marker-lost", "findings": ["marker-lost"], "candidates": cs,
                     "candidates_sha256": candidates_digest(cs)})


def select(m, r, reread, choice):
    """§8 at an active marker-lost review, with the re-read vector. Same vector: the selection. A changed,
    non-empty vector: a fresh review, no decision. No candidate left: the `vanished` terminal decision."""
    rm = m["rooms"][r]
    v = rm["active_review_id"]
    cs = vector(reread)
    if not cs:
        rm["decisions"][v] = {"room": r, "review_id": v, "at": T, "choice": "vanished", "candidates": [],
                              "candidates_sha256": candidates_digest([])}
        rm["active_review_id"] = None
        return
    if candidates_digest(cs) != rm["reviews"][v]["candidates_sha256"]:
        mint(rm, {"reason": "marker-lost", "findings": ["marker-lost"], "candidates": cs,
                  "candidates_sha256": candidates_digest(cs)})
        return
    rm["decisions"][v] = {"room": r, "review_id": v, "at": T, "choice": choice, "candidates": cs,
                          "candidates_sha256": candidates_digest(cs)}
    chosen = choice[7:]
    rm["unselected"] += [{**c, "review_id": v} for c in cs if c["kind"] != chosen]
    rm["current"] = None if choice == "select:none" else {
        "opaque": True, "kind": chosen, "revision": None, "proposed": None, "source": "manual-selection",
        "review_id": v}
    rm["active_review_id"] = None


R = "!room"


def fill(m, r=R, ops=2):
    j0(m, r)
    j1(m, r)
    for _ in range(ops):
        j2(m, r)
        j2p(m, r)
        j3_landed(m, r)


class SchemaTransitionTest(unittest.TestCase):
    def walk(self, start, steps):
        m = start
        marker(m)
        for step in steps:
            before = copy.deepcopy(m)
            step(m)
            marker(m)
            self.assertNotEqual(m, before, getattr(step, "__name__", step))
        return m

    def test_every_write_of_a_full_fill_validates_from_a_clean_first_use(self):
        steps = [lambda m: j0(m, R), lambda m: j1(m, R)]
        for _ in range(2):
            steps += [lambda m: j2(m, R), lambda m: j2p(m, R), lambda m: j3_ready(m, R), lambda m: j2p(m, R),
                      lambda m: j3_landed(m, R)]
        steps += [lambda m: j4b(m, R), lambda m: j5(m, R), lambda m: keep_new(m, R), m4]
        m = self.walk(m2(None), steps)
        self.assertEqual(m["rooms"][R]["current"]["opaque"], False)

    def test_a_legacy_migration_then_a_refresh_validates(self):
        v1 = {"!done": {"kind": "html-old", "state": "complete", "hash": "h", "updated_at": T},
              "!lost": {"state": "creating", "operation_id": "o", "updated_at": T}}
        m = self.walk(m2(v1), [m4, lambda m: fill(m, "!done"), lambda m: j4b(m, "!done"), lambda m: j5(m, "!done"),
                               lambda m: keep_new(m, "!done"), lambda m: keep_old(m, "!lost")])
        self.assertEqual(m["rooms"]["!done"]["superseded"][0]["source"], "v1-complete")
        self.assertEqual(m["rooms"]["!lost"]["rejected"], [], "legacy-in-flight rejects no revision")

    def test_each_failure_path_reaches_a_valid_review(self):
        for name, tail in (("cap", [j2p_cap]), ("failed", [j3_failed]),
                           ("mismatch", [lambda m, r: j3_failed(m, r, "struct-mismatch")])):
            with self.subTest(name):
                m = self.walk(m2(None), [lambda m: j0(m, R), lambda m: j1(m, R), lambda m: j2(m, R),
                                         lambda m: j2p(m, R)] + [lambda m, f=f: f(m, R) for f in tail]
                              + [lambda m: j4b(m, R), lambda m: j5(m, R), lambda m: keep_old(m, R)])
                self.assertEqual(len(m["rooms"][R]["rejected"]), 1)

    def test_a_page_missing_from_filling_is_a_valid_review_whatever_the_open_operation(self):
        for opened in ([], [j2], [j2, j2p]):
            with self.subTest(open=[f.__name__ for f in opened]):
                m = self.walk(m2(None), [lambda m: j0(m, R), lambda m: j1(m, R)]
                              + [lambda m, f=f: f(m, R) for f in opened] + [lambda m: j5(m, R, "page-missing")])
                rv = m["rooms"][R]["reviews"][m["rooms"][R]["active_review_id"]]
                self.assertEqual([s["state"] for s in rv["operations"]],
                                 [{j2: "ready", j2p: "send-unknown"}[opened[-1]]] if opened else [])

    def test_create_unrecoverable_from_creating_validates_and_keep_old_closes_it(self):
        m = self.walk(m2(None), [lambda m: j0(m, R), lambda m: j5(m, R, "create-unrecoverable"),
                                 lambda m: keep_old(m, R)])
        self.assertEqual(m["rooms"][R]["rejected"][0]["kind"], None)

    def test_refused_keep_new_and_select_mint_a_fresh_active_review_without_a_decision(self):
        def filled(m):
            fill(m)
            j4b(m, R)
            j5(m, R)
        for missing in (False, True):
            m = self.walk(m2(None), [filled, lambda m: refuse_keep_new(m, R, missing)])
            rm = m["rooms"][R]
            self.assertEqual((len(rm["reviews"]), rm["decisions"]), (2, {}))
        m = self.walk(m2(None), [lambda m: marker_lost(m, "!lost"), lambda m: select(m, "!lost", ("html-a",), "select:html-a")])
        rm = m["rooms"]["!lost"]
        self.assertEqual((len(rm["reviews"]), rm["decisions"]), (2, {}))
        self.walk(m, [lambda m: select(m, "!lost", ("html-a",), "select:html-a")])

    def test_both_selections_and_a_vanished_vector_reach_a_valid_terminal(self):
        for reread, choice in ((("html-a", "html-b"), "select:html-b"), (("html-a", "html-b"), "select:none"),
                               ((), "select:none")):
            with self.subTest(choice=choice, reread=reread):
                m = self.walk(m2(None), [lambda m: marker_lost(m, "!lost"),
                                         lambda m: select(m, "!lost", reread, choice)])
                rm = m["rooms"]["!lost"]
                self.assertIsNone(rm["active_review_id"])
                if not reread:
                    self.assertEqual(rm["decisions"][next(iter(rm["decisions"]))]["choice"], "vanished")
                    self.walk(m, [lambda m: j0(m, "!lost")])

    def test_a_stale_keep_old_records_only_its_decision(self):
        def two(m):
            fill(m)
            j4b(m, R)
            j5(m, R)
            refuse_keep_new(m, R)
        m = self.walk(m2(None), [two])
        stale = next(k for k, v in m["rooms"][R]["reviews"].items() if v["superseded_by"])
        m = self.walk(m, [lambda m: keep_old(m, R, stale)])
        self.assertIsNotNone(m["rooms"][R]["active_review_id"])

    def mutate(self, m, path, value):
        bad = copy.deepcopy(m)
        at = bad
        for k in path[:-1]:
            at = at[k]
        if value is DELETE:
            del at[path[-1]]
        else:
            at[path[-1]] = value
        return bad

    def test_the_validator_rejects_what_the_lifecycle_never_writes(self):
        m = m2(None)
        fill(m)
        j4b(m, R)
        j5(m, R)
        v = m["rooms"][R]["active_review_id"]
        rv = ["rooms", R, "reviews", v]
        cases = {
            "a J4b capture without its fields": rv + ["capture", "observed_sha256"],
            "a review summary of a sent operation with reserved 0": rv + ["operations", 1, "reserved"],
            "a dangling superseded_by": rv + ["superseded_by"],
            "a review id that isn't an id": rv + ["review_id"],
            "a capture range that disagrees with its digest": rv + ["capture", "ranges", 0, "struct_ok"],
            "a non-integer version": ["version"],
            "an unknown room key": ["rooms", R, "notes"],
        }
        values = {"a J4b capture without its fields": DELETE, "a review summary of a sent operation with reserved 0": 0,
                  "a dangling superseded_by": "f" * 32, "a review id that isn't an id": "x",
                  "a capture range that disagrees with its digest": False, "a non-integer version": 2.0,
                  "an unknown room key": "x"}
        for name, path in cases.items():
            with self.subTest(name):
                self.assertRaises(Invalid, marker, self.mutate(m, path, values[name]))
        j0m = m2(None)
        j0(j0m, R)
        self.assertRaises(Invalid, marker, self.mutate(j0m, ["rooms", R, "journal", "proposed"], {"kind": None}))
        self.assertRaises(Invalid, marker, self.mutate(j0m, ["rooms", R, "journal", "plan"], [3]))
        self.assertRaises(Invalid, marker, self.mutate(j0m, ["rooms", R], {"journal": j0m["rooms"][R]["journal"]}))
        keep_new(m, R)
        self.assertRaises(Invalid, marker, self.mutate(m, ["rooms", R, "current"], {"opaque": False}))
        self.assertRaises(Invalid, marker, self.mutate(m, ["rooms", R, "decisions", v], {}))
        self.assertRaises(Invalid, marker, self.mutate(m, ["rooms", R, "current", "accepted_review_id"], "f" * 32))
        lost = m2(None)
        marker_lost(lost, "!lost")
        select(lost, "!lost", ("html-a",), "select:html-a")
        ids = list(lost["rooms"]["!lost"]["reviews"])
        self.assertRaises(Invalid, marker, self.mutate(lost, ["rooms", "!lost", "active_review_id"], None))
        self.assertRaises(Invalid, marker, self.mutate(lost, ["rooms", "!lost", "reviews", ids[0], "superseded_by"],
                                                       ids[0]))
        self.assertRaises(Invalid, marker, self.mutate(lost, ["rooms", "!lost", "reviews", ids[0], "superseded_by"],
                                                       "f" * 32))
        cyc = self.mutate(lost, ["rooms", "!lost", "reviews", ids[1], "superseded_by"], ids[0])
        cyc["rooms"]["!lost"]["active_review_id"] = None
        self.assertRaises(Invalid, marker, cyc)


DELETE = object()


# The staged load (§9): fixed paths first, then validation, then only the paths validation vouches for.
FIXED_FILES = ("state/tutorial/getting-started.v2.json", "state/tutorial/getting-started.v1.bak")
FIXED_DIRS = ("state/tutorial/getting-started.json", "state/tutorial/ops", "state/tutorial", "state", ".")


def staged_load(m, synced: list) -> None:
    synced += FIXED_FILES + FIXED_DIRS
    marker(m)
    for rm in m["rooms"].values():
        j = rm["journal"]
        for op in (j or {}).get("operations", []):
            synced.append("state/tutorial/" + op["base_ref"])
        if j and j.get("operations"):
            synced += [f"state/tutorial/ops/{j['op_id']}", "state/tutorial/ops"]


class StagedLoadTest(unittest.TestCase):
    def test_no_path_is_derived_from_a_field_before_the_whole_file_validates(self):
        m = m2(None)
        fill(m, ops=1)
        j2(m, R)
        synced = []
        staged_load(m, synced)
        op_id = m["rooms"][R]["journal"]["op_id"]
        self.assertIn(f"state/tutorial/ops/{op_id}/1.base", synced)
        self.assertEqual(synced[-2:], [f"state/tutorial/ops/{op_id}", "state/tutorial/ops"])
        m["rooms"][R]["journal"]["operations"][1]["base_ref"] = "../../../escape"
        synced = []
        self.assertRaises(Invalid, staged_load, m, synced)
        self.assertEqual(synced, list(FIXED_FILES + FIXED_DIRS), "only fixed paths before validation")


# A file and directory-entry model of one host. Each entry is present or not now, and in the durable
# image; each inode's data is durable or not. A power loss replaces the present state with the image.
class FS:
    def __init__(self, entries, inodes):
        self.entries, self.inodes = dict(entries), dict(inodes)

    def key(self):
        return tuple(sorted(self.entries.items())), tuple(sorted(self.inodes.items()))

    def copy(self):
        return FS(self.entries, self.inodes)

    def exists(self, p):
        return self.entries.get(p, (None, None))[0] is not None

    def create(self, p, inode, data_durable):
        self.entries[p] = (inode, self.entries.get(p, (None, None))[1])
        self.inodes.setdefault(inode, data_durable)

    def unlink(self, p):
        self.entries[p] = (None, self.entries[p][1])

    def fsync_file(self, p):
        self.inodes[self.entries[p][0]] = True

    def fsync_dir(self, d):
        for p, (now, _) in list(self.entries.items()):
            if p.rsplit("/", 1)[0] == d:
                self.entries[p] = (now, now)

    def power_loss(self):
        out = FS({}, self.inodes)
        for p, (_, image) in self.entries.items():
            parent = p.rsplit("/", 1)[0]
            alive = all(self.entries.get(a, ("root", "root"))[1] is not None for a in ancestors(parent))
            out.entries[p] = (image if alive else None, image if alive else None)
        return out

    def intact(self, p):
        inode = self.entries.get(p, (None, None))[0]
        return inode is not None and self.inodes[inode]


def ancestors(d):
    parts = d.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


class Fault(Exception):
    pass


def explore(start: FS, process, starts=3):
    """Every durable image a power loss can leave, over up to `starts` processes, each of which may fail
    at any sync (and stop) or crash between any two operations."""
    images, seen, todo = [], set(), [(start, 0)]
    while todo:
        fs, n = todo.pop()
        if (fs.key(), n) in seen:
            continue
        seen.add((fs.key(), n))
        images.append(fs.power_loss())
        if n == starts:
            continue
        todo.append((fs.power_loss(), n + 1))
        for stop in itertools.count():
            run, ops = fs.copy(), []
            try:
                process(run, Budget(stop, ops))
                todo.append((run, n + 1))
                break
            except Fault:
                todo.append((run, n + 1))
    return images


class Budget:
    """Allows `stop` operations, then faults: the crash or failed sync the exploration injects there."""
    def __init__(self, stop, log):
        self.left, self.log = stop, log

    def __call__(self, name):
        if self.left == 0:
            raise Fault(name)
        self.left -= 1


ST = "state/tutorial"
V2, BAK, V1 = ST + "/getting-started.v2.json", ST + "/getting-started.v1.bak", ST + "/getting-started.json"
OPS, OPD = ST + "/ops", ST + "/ops/op1"
SIDE = OPD + "/0.base"


def stage_one(fs, step, sync_ops=True, sync_backup=True):
    for f in (V2, BAK):
        if fs.exists(f) and (sync_backup or f != BAK):
            step("fsync " + f)
            fs.fsync_file(f)
    for d in (OPS, ST, "state"):
        if fs.exists(d) and (sync_ops or d != OPS):
            step("fsync " + d)
            fs.fsync_dir(d)


def mkdir(fs, step, d):
    """A directory is created, and its parent synced, before anything is written into it."""
    if not fs.exists(d):
        step("mkdir " + d)
        fs.create(d, "dir:" + d, True)
        step("fsync " + d.rsplit("/", 1)[0])
        fs.fsync_dir(d.rsplit("/", 1)[0])


def save(fs, step, path, inode):
    step("rename " + path)
    fs.create(path, inode, True)
    step("fsync " + path.rsplit("/", 1)[0])
    fs.fsync_dir(path.rsplit("/", 1)[0])


def publish(fs, step, sync_ops=True):
    """From a durable J1: prepare operation 0's base sidecar, then J2."""
    stage_one(fs, step, sync_ops=sync_ops)
    if fs.entries[V2][0] != "J1":
        return
    mkdir(fs, step, OPS)
    mkdir(fs, step, OPD)
    save(fs, step, SIDE, "base0")
    save(fs, step, V2, "J2")


def migrate(fs, step, m1_sync=True, sync_backup=True):
    """M1 (link, then file-sync the shared inode, then the directory), M2, M3's unlink."""
    stage_one(fs, step, sync_backup=sync_backup)
    if not fs.exists(V2) and not fs.exists(BAK):
        step("link")
        fs.create(BAK, fs.entries[V1][0], None)
        if m1_sync:
            step("fsync backup")
            fs.fsync_file(BAK)
        step("fsync " + ST)
        fs.fsync_dir(ST)
    if not fs.exists(V2):
        save(fs, step, V2, "converted")
    if fs.exists(V1):
        step("unlink")
        fs.unlink(V1)
        step("fsync " + ST)
        fs.fsync_dir(ST)


def publish_start():
    return FS({"state": ("dir:state",) * 2, ST: ("dir:st",) * 2, V2: ("J1", "J1")},
              {"dir:state": True, "dir:st": True, "J1": True})


def migrate_start():
    # The deployed writer never syncs: v1's entry may have reached the disk, its data not.
    return FS({"state": ("dir:state",) * 2, ST: ("dir:st",) * 2, V1: ("v1", "v1")},
              {"dir:state": True, "dir:st": True, "v1": False})


def j2_without_sidecar(images):
    return [i for i in images if i.entries.get(V2, (None,))[0] == "J2" and not i.intact(SIDE)]


def v2_without_backup(images):
    return [i for i in images if i.exists(V2) and not i.intact(BAK)]


class FileModelTest(unittest.TestCase):
    def test_no_power_loss_leaves_j2_without_its_sidecar(self):
        self.assertEqual(j2_without_sidecar(explore(publish_start(), publish)), [])

    def test_without_syncing_ops_an_unsynced_op_directory_loses_the_sidecar_under_a_durable_j2(self):
        images = explore(publish_start(), lambda fs, step: publish(fs, step, sync_ops=False))
        self.assertTrue(j2_without_sidecar(images))

    def test_no_power_loss_leaves_a_v2_file_without_an_intact_backup(self):
        self.assertEqual(v2_without_backup(explore(migrate_start(), migrate)), [])

    def test_m1_must_file_sync_the_backup_inode_and_the_load_must_sync_it_too(self):
        for variant in ({"m1_sync": False}, {"sync_backup": False}):
            with self.subTest(**variant):
                images = explore(migrate_start(), lambda fs, step, v=variant: migrate(fs, step, **v))
                self.assertTrue(v2_without_backup(images))


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
