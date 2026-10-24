#!/usr/bin/env python3
"""The tutorial's feature list (skills/tutorial/feature-list.json): the page renders from it with
the same HTML the hard-coded sections gave (fallback mode: every screenshot inlined), a malformed
list is refused by field, a screenshot is an asset reference that points at the room's media when
the room has it, the page records the list's revision and `--if-newer` rebuilds only a page whose
list is older, `--list` swaps the list without any network call by default, and the size caps that
keep the page publishable hold."""
from __future__ import annotations

import contextlib
import email.message
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
base_spec = importlib.util.spec_from_file_location("tutorial_base", HERE / "tutorial-getting-started.test.py")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)  # isolates the environment and loads getting_started once
gs, tp = base.gs, base.gs.tour_page
Section = tp.Section

# --- fixture: the sections as tour_page.py hard-coded them before the list existed ---------------
OLD_SECTIONS = [
    Section("dm", "Your DM",
            "Your DM with {name} is where you ask for things for yourself. What you ask for "
            "yourself (research, mail, calendar) comes back in your DM, never in a room.",
            "Ask {name} in your DM:",
            "What can you do for me today?", "dm"),
    Section("rooms", "Rooms",
            "Humans and agents talk in the same chat.",
            "Press + at the top of the room list and choose Room. Then open the room menu "
            "(&#8942;), choose Invite, and add {name}.",
            images=(("room-list-add.jpg", "The + button at the top of the room list"),
                    ("new-room-dialog.jpg", "The New Chat Room dialog"),
                    ("room-menu-invite.jpg", "The room menu, with Invite"))),
    Section("commons", "Commons",
            "Each room's shared Docs, HTML pages, PDFs, whiteboards, sheets and databases. Open them "
            "with the Commons icon in the room header.",
            "Ask {name} in a room:",
            "Make a plan for our team offsite in Commons.", "room",
            (("room-header-commons.jpg", "The room header; Commons is the second icon"),
             ("commons-surfaces.jpg", "Commons: Doc, Whiteboard, HTML, PDF, Sheet and Database"))),
    Section("agents", "Connect other agents",
            "Bring agents from the apps you already use into your rooms.",
            "Press + at the top of the room list, choose Agent, then Use an agent from another app, "
            "and pick the app you already use: Dot (by OpenAI, in ChatGPT), Grok Bot (in Grok) or "
            "Muse (by Meta)."),
    Section("teamwork", "Teamwork",
            "@-mention any agent in a room to hand it work; it replies in the thread.",
            "In a room with another agent, type @ and ask {name}:",
            "Research the three best note-taking apps for a small team, then ask another agent "
            "in this room to review your answer.", "room",
            (("composer-mention.jpg", "The @ button in the message composer"),)),
    Section("calls", "Calls &amp; voice",
            "Talk live with {name} in your DM, or with people in any room.",
            "Press the headphone icon in the room header (Start call). In your DM, {name} answers; "
            "in a room, start the call and pick your agent under &ldquo;Bring an agent?&rdquo;.",
            images=(("room-header-call.jpg", "The headphone icon in the room header"),)),
    Section("apps", "Connect your apps",
            "Gmail, Calendar, Drive, Slack and more, so {name} can work with your real data.",
            "Press the plug icon in your user card (bottom left) to open Settings &rarr; "
            "Integrations, or ask {name} and a Connect button appears:",
            tp.CONNECT_PROMPT, "dm",
            (("user-card-plug.jpg", "The plug icon in your user card"),
             ("settings-integrations.jpg", "Settings → Apps → Integrations"))),
    Section("activity", "Activity &amp; notices",
            "The bell at the top of the room list and each room's Activity tab show what needs "
            "you and what your agents are working on. The unread badge follows each room's "
            "notification setting.",
            "Try it: open a room's Activity tab while an agent works on something you asked for, "
            "then open the room menu (&#8942;), choose Notifications, and pick Mention &amp; Keywords.",
            images=(("room-list-top.jpg", "The bell at the top of the room list"),)),
    Section("mobile", "Mobile",
            "The same rooms and agents on your phone.",
            "Pair it in Settings &rarr; Mobile pairing.",
            images=(("mobile-pairing.jpg", "Settings: Mobile pairing"),)),
]
OLD_IMPORT_SECTION = Section(
    "import", "Import your Claude Code history",
    "If you use Claude Code, {name} can read your local Claude Code history and bring what you "
    "worked on into its memory. You see a digest first; nothing is kept until you say so.",
    "Ask {name}:",
    tp.IMPORT_PROMPT, "dm",
)


def as_shots(sec):
    """The old (file, alt) pairs as asset references: the asset name is the file's stem, the file
    its fallback. This adapter is the only difference between the fixture and the list."""
    return sec._replace(images=tuple(tp.Shot(f[:-4], f, alt) for f, alt in sec.images))


# The old module appended the import section on a Claude core; the list says so with `runtime`.
OLD_LIST = tp.FeatureList(tp.FEATURES.revision,
                          tuple(as_shots(s) for s in OLD_SECTIONS) + (OLD_IMPORT_SECTION._replace(runtime="claude"),))
MXC_OF = {name: f"mxc://ag2.example/{i:02d}{name.replace('-', '')}" for i, name in enumerate(tp.asset_names(tp.FEATURES))}

STEP_IDS = ["dm", "rooms", "commons", "agents", "teamwork", "calls", "apps", "activity", "mobile", "import"]
ASSET_CAP = 60_000
PAGE_CAP = 300_000


def good_list(**over):
    data = json.loads(tp.LIST_PATH.read_text(encoding="utf-8"))
    data.update(over)
    return data


def with_step(**fields):
    """A list whose first step takes `fields` (a None value removes the key)."""
    data = good_list()
    step = data["steps"][0]
    for k, v in fields.items():
        if v is None:
            step.pop(k, None)
        else:
            step[k] = v
    return data


def many_steps(n, shots=0):
    """A list of `n` text-only steps; with `shots`, each step inlines the biggest fallback file that
    many times under distinct asset names (the duplicate-asset rule is per name, not per file)."""
    biggest = max(tp.ASSETS.glob("*.jpg"), key=lambda p: p.stat().st_size).name
    data = good_list()
    data["steps"] = [{"id": f"step-{i}", "title": f"Step {i}", "surface": "dm", "since_release": "2026-10-23",
                      "body": [f"What step {i} is for {{name}}.", "Ask {name}:"],
                      "screenshot": [{"asset": f"shot-{i}-{j}", "fallback_file": f"assets/{biggest}", "alt": "a"}
                                     for j in range(shots)]}
                     for i in range(n)]
    return data


def served_by(opened):
    """fetch_list's opener replaced by one whose open() is `opened`."""
    return mock.patch.object(gs, "list_opener", return_value=types.SimpleNamespace(open=opened))


class FakeTransport(urllib.request.BaseHandler):
    """Answers https, http and ftp requests from `routes`: {url: (status, body or Location)}."""
    handler_order = 100

    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def _serve(self, req):
        self.seen.append(req.full_url)
        status, payload = self.routes[req.full_url]
        headers = email.message.Message()
        if status == 302:
            headers["Location"] = payload
        resp = urllib.response.addinfourl(io.BytesIO(b"" if status == 302 else payload), headers, req.full_url, status)
        resp.msg = "Found" if status == 302 else "OK"
        return resp

    https_open = http_open = ftp_open = _serve


class LosslessTest(unittest.TestCase):
    def test_the_list_renders_the_same_html_as_the_hard_coded_sections(self):
        cases = 0
        for name in ("Blue", "<script>x</script>", "  Kai  "):
            for core in tp.CORES:
                for audience in tp.AUDIENCES:
                    for agent in (None, "@kai.agent:example.test"):
                        for prompts in (None, {"dm": "Draft my update.", "finale": "Plan my week.", "bad": "x"}):
                            for roles in (None, "I picked: Founder, Sales."):
                                old = tp.render_page(name, core, audience, agent, prompts, roles, OLD_LIST)
                                new = tp.render_page(name, core, audience, agent, prompts, roles)
                                self.assertEqual(old, new, (name, core, audience, agent, prompts, roles))
                                cases += 1
        self.assertEqual(cases, 3 * 3 * 2 * 2 * 2 * 2)

    def test_the_bundled_list_carries_todays_steps(self):
        self.assertEqual([s.id for s in tp.FEATURES.steps], STEP_IDS)
        self.assertEqual([s.id for s in tp.SECTIONS], STEP_IDS[:-1])
        by = {s.id: s for s in tp.FEATURES.steps}
        self.assertEqual(by["import"].runtime, "claude")
        self.assertTrue(all(s.runtime is None for s in tp.FEATURES.steps if s.id != "import"))
        self.assertEqual({s.since_release for s in tp.FEATURES.steps}, {"2026-10-15"})
        self.assertEqual(tp.PROMPT_SLOTS, ("dm", "commons", "teamwork", "apps", "import", "finale"))
        self.assertEqual([s.id for s in tp.sections_for("codex")], STEP_IDS[:-1])
        self.assertEqual([s.id for s in tp.sections_for("claude")], STEP_IDS)
        raw = json.loads(tp.LIST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], tp.SCHEMA_VERSION)
        self.assertEqual(raw["revision"], tp.FEATURES.revision)
        self.assertEqual(set(raw["steps"][0]), {"id", "title", "body", "surface", "screenshot",
                                                 "since_release", "paste"})
        shots = [shot for s in raw["steps"] if s["screenshot"] for shot in s["screenshot"]]
        self.assertEqual(len(shots), 11)
        for shot in shots:
            self.assertEqual(set(shot), {"asset", "fallback_file", "alt"})
            self.assertEqual(shot["fallback_file"], f"assets/{shot['asset']}.jpg")  # today's names: the stems

    def test_sections_match_field_for_field(self):
        for old, new in zip(OLD_LIST.steps, tp.FEATURES.steps):
            self.assertEqual((old.id, old.title, old.what, old.how, old.prompt, old.images),
                             (new.id, new.title, new.what, new.how, new.prompt, new.images))
            if old.prompt:
                self.assertEqual(old.where, new.where)  # the paste hint's surface
        for sec in OLD_SECTIONS:
            self.assertEqual([(f, alt) for f, alt in sec.images], [(s.file, s.alt) for s in as_shots(sec).images])


class ListTest(unittest.TestCase):
    def refuse(self, data, *words):
        with self.assertRaises(ValueError) as caught:
            tp.parse_list(data)
        for w in words:
            self.assertIn(w, str(caught.exception))

    def test_the_bundled_list_validates_and_extra_envelope_keys_are_allowed(self):
        served = good_list(kind="tutorial", vendor=None, etag="x")
        self.assertEqual(tp.parse_list(served), tp.FEATURES)
        self.assertEqual(tp.load_list(), tp.FEATURES)

    def test_each_malformed_shape_is_refused_by_name(self):
        self.refuse([], "JSON object")
        self.refuse(good_list(schema_version=2), "schema_version")
        self.refuse(good_list(revision="v2"), "revision")
        self.refuse(good_list(revision="tutorial.2026-10-23"), "revision")
        self.refuse(good_list(steps=[]), "steps")
        self.refuse(with_step(id="Marketplace!"), "steps[0]", "id")
        self.refuse(with_step(id="rooms"), "duplicate", "rooms")
        self.refuse(with_step(id="finale"), "finale")
        self.refuse(with_step(title=""), "step dm", "title")
        self.refuse(with_step(body=["only one part"]), "body", "2 parts")
        self.refuse(with_step(body=["what", "how", "a third part"]), "body", "2 parts")
        self.refuse(with_step(body=[]), "body", "2 parts")
        self.refuse(with_step(body="what, how"), "body", "2 parts")
        self.assertEqual(tp.BODY_PARTS, 2)
        self.refuse(good_list(revision="other.2026-10-24.1"), "revision", "tutorial.YYYY-MM-DD.N")
        self.refuse(with_step(body=["a", {"text": "b", "href": "https://x.test"}]), "link")
        self.refuse(with_step(body=["a", {"label": "b"}]), "body part")
        self.refuse(with_step(paste=""), "paste")
        self.refuse(with_step(surface="mobile"), "surface", "dm, room")  # a paste needs a chat surface
        self.refuse(with_step(paste=None, surface="desk"), "surface", "dm, room, mobile")
        shot = {"asset": "x", "fallback_file": "assets/room-list-add.jpg", "alt": "a"}
        self.refuse(with_step(screenshot=[{**shot, "fallback_file": "../x.jpg"}]), "screenshot[0]", "fallback_file")
        self.refuse(with_step(screenshot=[{**shot, "fallback_file": "room-list-add.jpg"}]), "assets/<name>.jpg")
        self.refuse(with_step(screenshot=[{**shot, "fallback_file": "assets/nope.jpg"}]), "nope.jpg")
        self.refuse(with_step(screenshot=[{"asset": "x", "fallback_file": "assets/room-list-add.jpg"}]), "alt")
        self.refuse(with_step(screenshot=[{**shot, "asset": "Room List"}]), "asset must be")
        self.refuse(with_step(screenshot=[{**shot, "asset": "room-list-add"}]), "duplicate asset", "room-list-add")
        self.refuse(with_step(screenshot=["room-list-add.jpg"]), "object with asset")
        self.refuse(with_step(screenshot="room-list-add.jpg"), "object with asset")
        self.refuse(with_step(since_release="Oct 2026"), "since_release")
        self.refuse(with_step(runtime="gemini"), "runtime")
        data = good_list()
        for s in data["steps"]:
            s["runtime"] = "claude"
        self.refuse(data, "every core")
        self.assertLessEqual(len(good_list()["steps"]), tp.STEPS_MAX // 2)
        self.refuse(many_steps(40), "steps", "at most 24", "got 40")
        self.assertEqual(len(tp.parse_list(many_steps(tp.STEPS_MAX)).steps), tp.STEPS_MAX)

    def test_markup_and_stray_placeholders_are_refused_in_the_title_and_both_body_parts(self):
        tag = "<script>fetch('https://x.test')</script>"
        self.refuse(with_step(title=f"Your DM {tag}"), "step dm title", "'<'")
        self.refuse(with_step(body=[tag, "Ask {name}:"]), "body[0]", "'<'")
        self.refuse(with_step(body=["What.", {"text": "a > b"}]), "body[1]", "'>'")
        self.refuse(with_step(body=["Costs {0} dollars", "How."]), "body[0]", "{name}", "{0}")
        self.refuse(with_step(body=["What.", "Ask {x}:"]), "body[1]", "{name}", "{x}")
        self.refuse(with_step(body=["What {}.", "How."]), "body[0]", "{name}")
        self.refuse(with_step(body=["{name!r}", "How."]), "body[0]", "{name}")
        self.refuse(with_step(body=["Unbalanced {", "How."]), "body[0]", "literal brace")
        self.refuse(with_step(title="Step {0}"), "step dm title", "{name}", "{0}")
        self.refuse(with_step(title="Hello {other}"), "step dm title", "{name}", "{other}")
        self.refuse(with_step(title="Un{matched"), "step dm title", "literal brace")
        page = tp.render_page("Blue", "codex", features=tp.parse_list(with_step(title="{{Your}} DM with {name}")))
        self.assertEqual(page.count("{Your} DM with Blue"), 2)  # the step heading and the tour's step title
        self.assertNotIn("{name}", page)
        data = with_step(body=["Braces {{like these}} stay literal for {name}.", "Ask {name}:"])
        page = tp.render_page("Blue", "codex", features=tp.parse_list(data))
        self.assertIn("Braces {like these} stay literal for Blue.", page)
        self.assertNotIn("{name}", page)

    def test_body_parts_may_be_text_objects_and_the_revision_orders_by_date_then_number(self):
        data = with_step(body=[{"text": "What."}, {"text": "How."}])
        self.assertEqual(tp.parse_list(data).steps[0][2:4], ("What.", "How."))
        self.assertEqual(tp.revision_key("tutorial.2026-10-23.10"), ("2026-10-23", 10))
        self.assertIsNone(tp.revision_key("tutorial.2026-10-23"))
        self.assertIsNone(tp.revision_key(None))
        self.assertTrue(tp.newer("tutorial.2026-10-23.10", "tutorial.2026-10-23.9"))
        self.assertTrue(tp.newer("tutorial.2026-11-01.1", "tutorial.2026-10-23.9"))
        self.assertTrue(tp.newer("tutorial.2026-10-23.1", None))  # a page from before lists had one
        self.assertFalse(tp.newer("tutorial.2026-10-23.1", "tutorial.2026-10-23.1"))
        self.assertFalse(tp.newer("tutorial.2026-10-23.1", "tutorial.2026-10-23.2"))
        self.assertFalse(tp.newer("junk", None))  # an unreadable list revision never forces a rebuild
        self.assertIsNone(tp.revision_key("other.2026-10-24.1"))  # the namespace is part of the format
        self.assertFalse(tp.newer("other.2026-10-24.1", "tutorial.2026-10-23.9"))

    def test_a_new_step_renders_with_the_default_art_and_opens_its_prompt_slot(self):
        data = good_list()
        data["steps"].insert(1, {"id": "marketplace", "title": "Marketplace", "surface": "dm",
                                 "body": ["Cloud tools for {name}.", "Ask {name}:"], "screenshot": None,
                                 "since_release": "2026-10-23", "paste": "Install the Notion tool."})
        features = tp.parse_list(data)
        self.assertEqual(tp.prompt_slots(features)[:2], ("dm", "marketplace"))
        page = tp.render_page("Blue", "codex", prompts={"marketplace": "Show me the Marketplace."},
                              features=features)
        self.assertIn('id="s-marketplace"', page)
        self.assertIn(tp.DEFAULT_ICON, page)
        self.assertIn(f"--a-marketplace:{tp.DEFAULT_ACCENT[0]};", page)
        self.assertIn(f"--a-marketplace:{tp.DEFAULT_ACCENT[1]};", page)
        self.assertIn("Show me the Marketplace.", page)
        self.assertEqual(base.steps(page)[1], "Marketplace")
        self.assertGreaterEqual(tp.contrast(tp.DEFAULT_ACCENT[0], tp.CARD[0]), 4.5)
        self.assertGreaterEqual(tp.contrast(tp.DEFAULT_ACCENT[1], tp.CARD[1]), 4.5)
        self.assertNotIn("--a-marketplace", tp.render_page("Blue", "codex"))

    def test_an_unreadable_file_is_a_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "list.json"
            bad.write_text("{nope")
            with self.assertRaisesRegex(ValueError, "not JSON"):
                tp.load_list(bad)
            with self.assertRaisesRegex(ValueError, "cannot read"):
                tp.load_list(Path(tmp) / "missing.json")
            big = Path(tmp) / "big.json"
            big.write_bytes(json.dumps(good_list(pad="x" * tp.LIST_MAX_BYTES)).encode())
            with self.assertRaisesRegex(ValueError, f"over the {tp.LIST_MAX_BYTES}-byte cap"):
                tp.load_list(big)
            self.assertEqual(gs.LIST_MAX_BYTES, tp.LIST_MAX_BYTES)  # one cap for a file and a URL


class MediaTest(unittest.TestCase):
    """A screenshot is an asset reference: the room's media when the room has it, the fallback
    file inlined otherwise, which is today's page byte for byte."""

    def test_clean_media_keeps_only_the_lists_assets_with_well_formed_mxc_uris(self):
        raw = {**MXC_OF, "unknown": "mxc://ag2.example/zz", "room-list-top": "https://ag2.example/x.jpg",
               "mobile-pairing": "mxc://no-id", "commons-surfaces": 7}
        kept = tp.clean_media(raw)
        self.assertEqual(set(kept), set(tp.asset_names(tp.FEATURES)) - {"room-list-top", "mobile-pairing",
                                                                         "commons-surfaces"})
        self.assertEqual(tp.clean_media(["x"]), {})
        self.assertEqual(tp.clean_media(None), {})
        self.assertEqual(len(tp.asset_names(tp.FEATURES)), 11)

    def test_a_mapped_asset_points_at_the_rooms_media_and_the_rest_stay_inlined(self):
        page = tp.render_page("Blue", "codex", media={"room-list-add": MXC_OF["room-list-add"]})
        self.assertIn(f'<img src="{MXC_OF["room-list-add"]}" alt="The + button at the top of the room list" '
                      'loading="lazy">', page)
        self.assertEqual(page.count('<img src="data:image/jpeg;base64,'), 10)
        self.assertEqual(page.count('<img src="mxc://'), 1)

    def test_a_fully_mapped_page_shrinks_to_text(self):
        for core in tp.CORES:
            page = tp.render_page("Blue", core, "dm", "@kai.agent:example.test", media=MXC_OF)
            self.assertNotIn("data:image", page)
            self.assertEqual(page.count('<img src="mxc://'), 11)
            self.assertLess(len(page.encode("utf-8")), 40_000, core)

    def test_without_media_the_page_is_the_fallback_page(self):
        self.assertEqual(tp.render_page("Blue", "claude", media={}), tp.render_page("Blue", "claude"))
        self.assertEqual(tp.render_page("Blue", "claude", media={"junk": "mxc://ag2.example/x"}),
                         tp.render_page("Blue", "claude"))


class MarkerTest(unittest.TestCase):
    ME = "@kai.agent:example.test"

    def test_the_marker_records_the_revision_and_reads_back_from_old_and_new_pages(self):
        new = tp.owner_marker(self.ME, tp.FEATURES.revision)
        self.assertEqual(new, f'<meta name="sutando-tutorial" content="{self.ME}" '
                              f'data-revision="{tp.FEATURES.revision}">')
        self.assertEqual(tp.read_marker("<html>" + new), (self.ME, tp.FEATURES.revision))
        self.assertEqual(tp.read_marker(tp.owner_marker(self.ME)), (self.ME, None))  # a pre-list page
        self.assertEqual(tp.read_marker(tp.owner_marker('@a"b:x', 'r"1')), ('@a"b:x', 'r"1'))
        self.assertIsNone(tp.read_marker("<html>no tag</html>"))
        self.assertIsNone(tp.read_marker(""))

    def test_the_page_carries_the_list_revision_only_with_an_agent(self):
        page = tp.render_page("Blue", "codex", "dm", self.ME)
        self.assertEqual(tp.read_marker(page), (self.ME, tp.FEATURES.revision))
        self.assertIsNone(tp.read_marker(tp.render_page("Blue", "codex")))
        other = tp.FeatureList("tutorial.2026-11-01.1", tp.FEATURES.steps)
        self.assertEqual(tp.read_marker(tp.render_page("Blue", "codex", "dm", self.ME, features=other))[1],
                         "tutorial.2026-11-01.1")

    def test_ownership_reads_both_marker_forms(self):
        page = {"kind": "html-g", "title": "Getting started", "by": self.ME}
        self.assertTrue(gs.owned(page, tp.owner_marker(self.ME), self.ME))
        self.assertTrue(gs.owned(page, tp.owner_marker(self.ME, "tutorial.2026-10-23.1"), self.ME))
        self.assertFalse(gs.owned(page, tp.owner_marker("@other:x", "tutorial.2026-10-23.1"), self.ME))
        self.assertFalse(gs.owned(page, 'x <meta name="sutando-tutorial" content="@kai.agent:example.test', self.ME))


class SizeTest(unittest.TestCase):
    def test_every_fallback_file_is_under_the_per_asset_cap(self):
        files = {p.name: p.stat().st_size for p in tp.ASSETS.glob("*.jpg")}
        self.assertTrue(files)
        over = {f: n for f, n in files.items() if n > ASSET_CAP}
        self.assertEqual(over, {})
        used = {shot.file for s in tp.FEATURES.steps for shot in s.images}
        self.assertEqual(used, set(files))

    def test_every_page_variant_is_under_the_page_cap(self):
        long_prompts = {slot: "x" * tp.PROMPT_MAX for slot in tp.PROMPT_SLOTS}
        roles = ", ".join("r" * 40 for _ in range(tp.ROLES_MAX))
        for core in tp.CORES:
            for audience in tp.AUDIENCES:
                page = tp.render_page("x" * tp.NAME_MAX, core, audience, "@" + "a" * 200 + ":example.test",
                                      long_prompts, roles)
                self.assertLessEqual(len(page.encode("utf-8")), PAGE_CAP, (core, audience))


    def test_render_refuses_a_page_over_the_cap_however_the_list_reaches_it(self):
        self.assertEqual(tp.PAGE_MAX_BYTES, PAGE_CAP)
        heavy = tp.parse_list(many_steps(tp.STEPS_MAX, shots=3))  # within the step bound, over the cap
        with self.assertRaisesRegex(ValueError, f"over the {PAGE_CAP}-byte cap") as caught:
            tp.render_page("Blue", "codex", features=heavy)
        self.assertIn("room media", str(caught.exception))
        light = tp.parse_list(many_steps(tp.STEPS_MAX))
        self.assertLess(len(tp.render_page("Blue", "codex", features=light).encode("utf-8")), PAGE_CAP)


class _Cli(unittest.TestCase):
    """main() in-process against a stub room_commons.py, like the base MainTest."""

    ROOM = "!dm:example.test"
    ME = "@kai.agent:example.test"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-list-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stub = self.tmp / "room_commons.py"
        self.stub.write_text(base.STUB)
        env = {k: v for k, v in base.isolated_child_env().items()
               if not k.startswith(("SUTANDO_", "AG2", "REMOTE_TASK", "AGENT_", "ROOM_C", "MATRIX_"))
               and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME")}
        env.update({"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "STUB_STATE": str(self.tmp / "room.json"),
                    "SUTANDO_WORKSPACE_DIR": str(self.tmp / "ws")})
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(gs, "_workspace", return_value=self.tmp / "ws")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.door = base.FakeActions(self.tmp / "room.json", by=self.ME)
        door = mock.patch.object(gs, "connect_actions", return_value=self.door)
        door.start()
        self.addCleanup(door.stop)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gs.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write_list(self, revision, name="list.json", **over):
        path = self.tmp / name
        path.write_text(json.dumps(good_list(revision=revision, **over)))
        return path

    def page_text(self, kind="html-p1"):
        return json.loads((self.tmp / "room.json").read_text())["pages"][kind]

    def record(self, **fields):
        marker = gs.marker_path(self.tmp / "ws")
        rooms = gs.load_marker(marker)
        rooms[self.ROOM] = {**rooms[self.ROOM], **fields}
        gs.save_marker(marker, rooms)

    def publish(self, *extra):
        return self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                         "--room-commons", str(self.stub), "--agent-mxid", self.ME, *extra)


class ListOverrideTest(_Cli):
    def test_render_from_a_list_file(self):
        other = self.write_list("tutorial.2026-11-01.1")
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(other))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, tp.render_page("Blue", "codex", features=tp.load_list(other)))

    def test_a_bad_list_falls_back_to_the_file_and_says_so(self):
        broken = self.tmp / "broken.json"
        broken.write_text(json.dumps(good_list(schema_version=9)))
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(broken))
        self.assertEqual(code, 0)
        self.assertEqual(out, tp.render_page("Blue", "codex"))
        self.assertIn("--list", err)
        self.assertIn("schema_version", err)
        self.assertIn("skill's own list", err)
        code, out, err = self.main("render", "--name", "Blue", "--list", str(self.tmp / "missing.json"))
        self.assertEqual((code, out), (0, tp.render_page("Blue")))
        self.assertIn("cannot read", err)

    def test_a_list_with_markup_or_a_stray_placeholder_falls_back_to_the_file_and_says_so(self):
        for field, bad, said in (("title", "Your DM <script>x</script>", "title"),
                                 ("body", ["<a href='https://x.test'>x</a>", "How."], "body[0]"),
                                 ("body", ["Costs {0} dollars", "How."], "{0}"),
                                 ("body", ["What.", "Ask {x}:"], "{x}")):
            broken = self.tmp / "broken.json"
            broken.write_text(json.dumps(with_step(**{field: bad})))
            code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(broken))
            self.assertEqual(code, 0, (bad, err))
            self.assertEqual(out, tp.render_page("Blue", "codex"))
            self.assertNotIn("x.test", out)
            for w in ("--list", said, "skill's own list"):
                self.assertIn(w, err, bad)

    def test_a_page_over_the_cap_is_refused_by_render_and_publish(self):
        heavy = self.tmp / "heavy.json"
        heavy.write_text(json.dumps(many_steps(tp.STEPS_MAX, shots=3)))
        code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", str(heavy))
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"over the {PAGE_CAP}-byte cap", err)
        code, out, _ = self.publish("--list", str(heavy))
        self.assertEqual((code, json.loads(out)["ok"]), (4, False))
        self.assertIn(f"over the {PAGE_CAP}-byte cap", json.loads(out)["reason"])
        state = self.tmp / "room.json"
        self.assertFalse(state.exists() and json.loads(state.read_text()).get("pages"))  # nothing was written

    def test_no_network_call_without_a_list_url(self):
        with mock.patch.object(gs.urllib.request, "urlopen", side_effect=AssertionError("network")) as net, \
                mock.patch.object(gs, "list_opener", side_effect=AssertionError("network")) as opener:
            code, out, _ = self.main("render", "--name", "Blue", "--core", "codex")
            self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")))
            other = self.write_list("tutorial.2026-11-01.1")
            code, _, _ = self.main("render", "--name", "Blue", "--list", str(other))
            self.assertEqual(code, 0)
            code, _, _ = self.publish()
            self.assertEqual(code, 0)
        net.assert_not_called()
        opener.assert_not_called()

    def test_a_served_list_replaces_the_file(self):
        served = json.dumps(good_list(revision="tutorial.2026-11-01.1", kind="tutorial", etag="x")).encode()
        seen = {}

        @contextlib.contextmanager
        def opened(req, timeout=None):
            seen["url"], seen["ua"], seen["timeout"] = req.full_url, req.get_header("User-agent"), timeout
            yield io.BytesIO(served)

        with served_by(opened):
            code, out, err = self.main("render", "--name", "Blue", "--core", "codex",
                                       "--list", "https://api.example.test/setup-copy?scope=tutorial")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, {"url": "https://api.example.test/setup-copy?scope=tutorial",
                                "ua": gs.USER_AGENT, "timeout": gs.LIST_TIMEOUT})
        self.assertEqual(out, tp.render_page("Blue", "codex", features=tp.parse_list(json.loads(served))))
        with served_by(opened):
            code, out, _ = self.publish("--list", "https://u:secret@api.example.test:8443/setup-copy?token=t#x")
        self.assertEqual((code, json.loads(out)["list"]), (0, "https://api.example.test:8443/setup-copy"))

    def test_a_redirect_is_followed_only_to_https(self):
        served = json.dumps(good_list(revision="tutorial.2026-11-01.1")).encode()
        routes = {"https://trusted.example/list": (302, "http://127.0.0.1:8080/cleartext-list"),
                  "http://127.0.0.1:8080/cleartext-list": (200, served),
                  "https://trusted.example/ftp": (302, "ftp://files.example/list"),
                  "ftp://files.example/list": (200, served),
                  "https://trusted.example/moved": (302, "https://cdn.example/list?sig=s"),
                  "https://cdn.example/list?sig=s": (200, served)}
        for url, target in (("https://trusted.example/list", "http://127.0.0.1:8080/cleartext-list"),
                            ("https://trusted.example/ftp", "ftp://files.example/list")):
            transport = FakeTransport(routes)
            with self.assertRaises(ValueError) as caught:
                gs.fetch_list(url, gs.list_opener(transport))
            self.assertIn("only https", str(caught.exception))
            self.assertEqual(transport.seen, [url])  # the cleartext target is never requested
            self.assertNotIn("secret", str(caught.exception))
        transport = FakeTransport(routes)
        self.assertEqual(gs.fetch_list("https://trusted.example/moved", gs.list_opener(transport)).revision,
                         "tutorial.2026-11-01.1")
        self.assertEqual(transport.seen, ["https://trusted.example/moved", "https://cdn.example/list?sig=s"])
        # Through the CLI the refusal falls back to the file; the note shows no userinfo or query.
        with mock.patch.object(gs, "list_opener", lambda *h: urllib.request.build_opener(
                gs._HttpsRedirects(), FakeTransport({"https://me:secret@trusted.example/list?token=abc": (302, "http://x.test/?k=v")}))):
            code, out, err = self.main("render", "--name", "Blue", "--core", "codex",
                                       "--list", "https://me:secret@trusted.example/list?token=abc")
        self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")))
        self.assertIn("--list https://trusted.example/list not used", err)
        self.assertIn("redirect to http://x.test/ refused", err)
        for leak in ("secret", "token=abc", "k=v", "me:"):
            self.assertNotIn(leak, err)
        self.assertEqual(gs.safe_source("https://u:p@h.test:8443/x/y?token=1#f"), "https://h.test:8443/x/y")
        self.assertEqual(gs.safe_source("/tmp/list.json"), "/tmp/list.json")

    def test_a_served_list_that_fails_falls_back(self):
        for url, payload, word in (("http://api.example.test/x", b"{}", "https"),
                                   ("https://api.example.test/x", b"not json", "not JSON"),
                                   ("https://api.example.test/x", b"x" * (gs.LIST_MAX_BYTES + 1), "over"),
                                   ("https://api.example.test/x", json.dumps(good_list(steps=[])).encode(), "steps")):
            @contextlib.contextmanager
            def opened(req, timeout=None, payload=payload):
                yield io.BytesIO(payload)

            with served_by(opened):
                code, out, err = self.main("render", "--name", "Blue", "--core", "codex", "--list", url)
            self.assertEqual((code, out), (0, tp.render_page("Blue", "codex")), url)
            self.assertIn(word, err, url)
        with served_by(mock.Mock(side_effect=OSError("refused"))):
            code, out, err = self.main("render", "--name", "Blue", "--list", "https://api.example.test/x")
        self.assertEqual((code, out), (0, tp.render_page("Blue")))
        self.assertIn("cannot fetch", err)
        self.assertNotIn("refused", err)  # the transport's own words stay out of the note

    def test_media_points_the_page_at_room_media_and_is_remembered_for_the_room(self):
        media = self.tmp / "media.json"
        media.write_text(json.dumps({"room-list-add": MXC_OF["room-list-add"], "junk": "mxc://ag2.example/j",
                                     "mobile-pairing": "https://ag2.example/x.jpg"}))
        code, out, _ = self.publish("--media", str(media))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["media"]), (0, "created", ["room-list-add"]))
        self.assertIn(f'<img src="{MXC_OF["room-list-add"]}"', self.page_text())
        self.assertEqual(self.page_text().count("data:image/jpeg"), 10)
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual(entry["media"], {"room-list-add": MXC_OF["room-list-add"]})
        code, out, _ = self.publish()  # the room keeps its media map
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["media"]),
                         (0, "unchanged", ["room-list-add"]))
        more = self.tmp / "more.json"
        more.write_text(json.dumps({"new-room-dialog": MXC_OF["new-room-dialog"]}))
        code, out, _ = self.publish("--media", str(more))
        self.assertEqual((code, json.loads(out)["action"], json.loads(out)["media"]),
                         (0, "updated", ["new-room-dialog", "room-list-add"]))
        self.assertEqual(self.page_text().count('<img src="mxc://'), 2)
        broken = self.tmp / "broken.json"
        broken.write_text("{nope")
        code, out, _ = self.main("render", "--name", "Blue", "--media", str(broken))
        self.assertEqual((code, out), (0, tp.render_page("Blue")))

    def test_prompts_for_a_slot_only_the_served_list_has_are_used_and_kept(self):
        data = good_list(revision="tutorial.2026-11-01.1")
        data["steps"].insert(1, {"id": "marketplace", "title": "Marketplace", "surface": "dm",
                                 "body": ["Cloud tools for {name}.", "Ask {name}:"], "screenshot": None,
                                 "since_release": "2026-11-01", "paste": "Install the Notion tool."})
        other = self.tmp / "m.json"
        other.write_text(json.dumps(data))
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"marketplace": "Show me the Marketplace."}))
        code, out, _ = self.publish("--if-newer", "--list", str(other), "--prompts", str(prompts))
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        self.assertIn("Show me the Marketplace.", self.page_text())
        self.assertNotIn("Install the Notion tool.", self.page_text())
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual(entry["prompts"], {"marketplace": "Show me the Marketplace."})
        data["revision"] = "tutorial.2026-11-02.1"
        other.write_text(json.dumps(data))
        code, out, _ = self.publish("--if-newer", "--list", str(other))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn("Show me the Marketplace.", self.page_text())

    def test_publish_reports_which_list_it_used(self):
        other = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--list", str(other))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["list"], got["revision"]),
                         (0, "created", str(other), "tutorial.2026-11-01.1"))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual((entry["state"], entry["revision"]), ("complete", "tutorial.2026-11-01.1"))


class IfNewerTest(_Cli):
    def test_a_newer_list_rebuilds_the_page_in_place(self):
        code, out, _ = self.publish()
        self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer))
        got = json.loads(out)
        self.assertEqual((code, got["action"], got["kind"], got["summoned"]), (0, "updated", "html-p1", False))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["revision"],
                         "tutorial.2026-11-01.1")

    def test_a_current_page_is_left_alone_even_when_the_text_would_differ(self):
        self.publish()
        before = self.page_text()
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Kai", "--core", "claude",
                                 "--room-commons", str(self.stub), "--agent-mxid", self.ME, "--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(self.page_text(), before)
        older = self.write_list("tutorial.2026-10-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(older))
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(self.page_text(), before)
        # Without --if-newer, a differing text still rewrites, as before.
        code, out, _ = self.main("publish", "--room", self.ROOM, "--name", "Kai", "--core", "codex",
                                 "--room-commons", str(self.stub), "--agent-mxid", self.ME)
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn("Getting started with Kai", self.page_text())

    def test_a_page_from_before_lists_had_a_revision_is_rebuilt(self):
        self.publish()
        db = json.loads((self.tmp / "room.json").read_text())
        db["pages"]["html-p1"] = db["pages"]["html-p1"].replace(
            tp.owner_marker(self.ME, tp.FEATURES.revision), tp.owner_marker(self.ME))
        (self.tmp / "room.json").write_text(json.dumps(db))
        self.record(hash=gs.content_hash(db["pages"]["html-p1"]))  # the old script wrote that page
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, None))
        code, out, _ = self.publish("--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, tp.FEATURES.revision))

    def test_the_refresh_keeps_the_prompts_the_page_was_built_with(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my weekly update.", "finale": "Plan my sprint."}))
        self.publish("--prompts", str(prompts), "--roles", "Founder")
        entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual(entry["prompts"], {"dm": "Draft my weekly update.", "finale": "Plan my sprint."})
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer), "--roles", "Founder")
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        page = self.page_text()
        self.assertIn("Draft my weekly update.", page)
        self.assertIn("Plan my sprint.", page)
        self.assertIn("Picked for: Founder", page)
        # A plain publish without --prompts resets to the generic prompts, as before.
        code, out, _ = self.publish("--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertNotIn("Draft my weekly update.", self.page_text())
        self.assertNotIn("prompts", gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM])

    def test_the_refresh_keeps_the_roles_line_without_being_told_again(self):
        self.publish("--roles", "I picked: Founder, Sales.")
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["roles"], "Founder, Sales")
        newer = self.write_list("tutorial.2026-11-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn('<p class="roles">Picked for: Founder, Sales</p>', self.page_text())
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))
        code, out, _ = self.publish("--if-newer", "--list", str(self.write_list("tutorial.2026-11-02.1")),
                                    "--roles", "Engineer")  # roles given again win
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertIn("Picked for: Engineer</p>", self.page_text())
        code, out, _ = self.publish("--list", str(newer))  # a plain publish resets, like the prompts
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertNotIn('class="roles"', self.page_text())
        self.assertNotIn("roles", gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM])

    def test_a_page_someone_edited_is_kept_until_it_hashes_to_the_entry_again(self):
        self.publish()
        written = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["hash"]
        db = json.loads((self.tmp / "room.json").read_text())
        mine, theirs = db["pages"]["html-p1"], db["pages"]["html-p1"].replace("</main>", "<p>Our notes.</p></main>")
        db["pages"]["html-p1"] = theirs
        (self.tmp / "room.json").write_text(json.dumps(db))
        newer = self.write_list("tutorial.2026-11-01.1")
        for extra in (("--if-newer", "--list", str(newer)), ("--name", "Kai")):
            code, out, err = self.main("publish", "--room", self.ROOM, "--name", "Blue", "--core", "codex",
                                       "--room-commons", str(self.stub), "--agent-mxid", self.ME, *extra)
            got = json.loads(out)
            self.assertEqual((code, got["action"], got.get("edited")), (0, "kept", True), extra)
            self.assertEqual(self.page_text(), theirs)
            self.assertIn("html-p1", err)
            self.assertIn("edited", err)
            entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
            self.assertEqual((entry["hash"], entry["revision"]), (written, tp.FEATURES.revision))
        db["pages"]["html-p1"] = mine  # put back as written: the next newer list rebuilds it
        (self.tmp / "room.json").write_text(json.dumps(db))
        code, out, err = self.publish("--if-newer", "--list", str(newer))
        self.assertEqual((code, json.loads(out)["action"], err), (0, "updated", ""))
        self.assertEqual(tp.read_marker(self.page_text()), (self.ME, "tutorial.2026-11-01.1"))

    def test_a_same_or_older_revision_pass_never_blesses_an_owner_edit(self):
        self.publish()
        written = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["hash"]
        db = json.loads((self.tmp / "room.json").read_text())
        theirs = db["pages"]["html-p1"].replace("</main>", "<p>Our notes.</p></main>")
        db["pages"]["html-p1"] = theirs
        (self.tmp / "room.json").write_text(json.dumps(db))
        older = self.write_list("tutorial.2026-10-01.1")
        for extra in (("--if-newer",), ("--if-newer", "--list", str(older))):
            code, out, err = self.publish(*extra)
            self.assertEqual((code, json.loads(out)["action"]), (0, "kept"), extra)
            self.assertIn("edited", err)
            self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]["hash"], written)
        code, out, _ = self.publish("--if-newer", "--list", str(self.write_list("tutorial.2026-11-01.1")))
        self.assertEqual((code, json.loads(out)["action"]), (0, "kept"))
        self.assertEqual(self.page_text(), theirs)  # the edit survives the next revision

    def test_an_unchanged_or_kept_run_leaves_the_entry_as_written(self):
        prompts, media = self.tmp / "p.json", self.tmp / "m.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        media.write_text(json.dumps({"room-list-add": MXC_OF["room-list-add"]}))
        self.publish("--prompts", str(prompts), "--roles", "Founder", "--media", str(media))
        before = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
        self.assertEqual((before["prompts"], before["roles"], before["media"]),
                         ({"dm": "Draft my update."}, "Founder", {"room-list-add": MXC_OF["room-list-add"]}))
        more = self.tmp / "more.json"
        more.write_text(json.dumps({"new-room-dialog": MXC_OF["new-room-dialog"]}))
        older = self.write_list("tutorial.2026-10-01.1")
        code, out, _ = self.publish("--if-newer", "--list", str(older), "--media", str(more))
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM], before)
        db = json.loads((self.tmp / "room.json").read_text())
        db["pages"]["html-p1"] += "<!-- theirs -->"
        (self.tmp / "room.json").write_text(json.dumps(db))
        code, out, _ = self.publish("--media", str(more), "--roles", "Sales")
        self.assertEqual((code, json.loads(out)["action"]), (0, "kept"))
        self.assertEqual(gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM], before)

    def test_a_rewrite_cut_short_is_finished_by_the_next_run_at_the_exact_target(self):
        prompts = self.tmp / "p.json"
        prompts.write_text(json.dumps({"dm": "Draft my update."}))
        newer = self.write_list("tutorial.2026-11-01.1")
        target = tp.render_page("Blue", "codex", "dm", self.ME, {"dm": "Draft my update."}, "Founder",
                                tp.load_list(newer))
        self.assertEqual(len(gs.chunks(target)), 2)  # the default-size page: two clears, two appends
        boundaries = {"replace:1": ["replace"], "replace:2": ["replace", "replace"],
                      "append:1": ["replace", "replace", "append"],
                      "append:2": ["replace", "replace", "append", "append"]}
        for at, calls in boundaries.items():
            with self.subTest(at=at):
                (self.tmp / "room.json").unlink(missing_ok=True)
                shutil.rmtree(self.tmp / "ws", ignore_errors=True)
                code, out, _ = self.publish()
                self.assertEqual((code, json.loads(out)["action"]), (0, "created"))
                db = json.loads((self.tmp / "room.json").read_text())
                mine = db["pages"]["html-p1"]
                db["calls"] = []
                (self.tmp / "room.json").write_text(json.dumps(db))
                os.environ["STUB_FAIL_AT"] = at
                code, out, _ = self.publish("--if-newer", "--list", str(newer), "--prompts", str(prompts),
                                            "--roles", "Founder")
                os.environ.pop("STUB_FAIL_AT")
                self.assertEqual((code, json.loads(out)["ok"]), (4, False))
                db = json.loads((self.tmp / "room.json").read_text())
                self.assertEqual([c[0] for c in db["calls"] if c[0] in ("replace", "append")], calls)
                left = self.page_text()
                self.assertNotEqual(left, target)
                if at == "append:1":
                    self.assertEqual(left, "")  # the blank page a retry must not read as an edit
                entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
                self.assertEqual((entry["state"], entry["hash"], entry["prompts"], entry["roles"]),
                                 ("updating", gs.content_hash(target), {"dm": "Draft my update."}, "Founder"))
                self.assertEqual(json.loads(self.main("plan", "--room", self.ROOM)[1])["pending"], True)
                # The retry passes no prompts or roles: the entry carries the target's.
                code, out, err = self.publish("--if-newer", "--list", str(newer), "--owner", "@owner:example.test")
                got = json.loads(out)
                self.assertEqual((code, got["action"], got["summoned"], got.get("edited"), err),
                                 (0, "updated", False, None, ""))
                self.assertEqual(self.page_text(), target)
                entry = gs.load_marker(gs.marker_path(self.tmp / "ws"))[self.ROOM]
                self.assertEqual((entry["state"], entry["hash"]), ("complete", gs.content_hash(target)))
                self.assertNotEqual(mine, target)

    def test_a_pending_page_is_finished_whatever_the_revision(self):
        self.door.fail = "after"
        code, out, _ = self.publish()
        self.assertEqual(code, 4)
        code, out, _ = self.publish("--if-newer")
        self.assertEqual((code, json.loads(out)["action"]), (0, "updated"))
        self.assertEqual(self.page_text(), tp.render_page("Blue", "codex", "dm", self.ME))

    def test_publish_api_takes_the_list_and_the_rule(self):
        marker = gs.marker_path(self.tmp / "ws")
        commons = gs.Commons(self.stub, self.ROOM, actions=lambda: self.door)
        out = gs.publish(commons, marker, "Blue", core="codex", agent=self.ME)
        self.assertEqual(out["revision"], tp.FEATURES.revision)
        other = tp.FeatureList("tutorial.2026-11-01.1", tp.FEATURES.steps)
        out = gs.publish(commons, marker, "Blue", core="codex", agent=self.ME, features=other, if_newer=True)
        self.assertEqual((out["action"], out["revision"]), ("updated", "tutorial.2026-11-01.1"))
        out = gs.publish(commons, marker, "Kai", core="codex", agent=self.ME, features=other, if_newer=True)
        self.assertEqual(out["action"], "unchanged")


class DocTest(unittest.TestCase):
    SKILL = tp.LIST_PATH.parent / "SKILL.md"

    def test_skill_md_describes_the_list_workflow_the_revision_rule_and_the_visuals_follow_up(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("feature-list.json", text)
        self.assertIn("--if-newer", text)
        self.assertIn("--list", text)
        self.assertIn("data-revision", text)
        self.assertIn("Visuals (follow-up)", text)
        self.assertIn("Commons asset references", text)
        self.assertIn("room.media.upload", text)
        self.assertIn("mxc://", text)
        self.assertIn("fallback_file", text)
        self.assertIn("--media", text)
        self.assertNotIn("reword them in `tour_page.py`", text)
        self.assertIn(str(ASSET_CAP // 1000) + " KB", text)
        self.assertIn(str(PAGE_CAP // 1000) + " KB", text)
        self.assertIn("`updating`", text)
        canon = (HERE.parent / "docs" / "tutorial-delivery.md").read_text(encoding="utf-8")
        for word in ("--if-newer", "kept", "updating", "mxc://", "1 MiB", "Last verified:"):
            self.assertIn(word, canon)


if __name__ == "__main__":
    unittest.main()
