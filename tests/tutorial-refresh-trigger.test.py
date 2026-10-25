#!/usr/bin/env python3
"""The Getting started refresh after an engine update: src/startup.sh starts
`getting_started.py publish --all-rooms --if-newer`, and that run rebuilds the page in every room
the marker has an entry for, each room on its own."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
base_spec = importlib.util.spec_from_file_location("tutorial_base", HERE / "tutorial-getting-started.test.py")
base = importlib.util.module_from_spec(base_spec)
base_spec.loader.exec_module(base)  # isolates the environment and loads getting_started once
gs, tp = base.gs, base.gs.tour_page

ME = "@kai.agent:example.test"
ROOMS = ("!a:example.test", "!b:example.test", "!c:example.test")
OLD = "tutorial.2026-10-01.1"
# One store per room, picked by the room argument; importing it (as the script does for credential
# names) refuses, as the real room_commons.py does nothing at import.
ROOMED_STUB = (
    "if __name__ != '__main__':\n    raise ImportError('a CLI stub, not importable')\n"
    "import os, re, sys\n"
    "_a = [a for a in sys.argv[1:] if a != '--json']\n"
    "_a = _a[2:] if _a[0] == '--kind' else _a\n"
    "os.environ['STUB_STATE'] = os.path.join(os.environ['STUB_ROOMS'], re.sub(r'\\W', '_', _a[1]) + '.json')\n"
) + base.STUB


def store(rooms_dir: Path, room: str) -> Path:
    return rooms_dir / (re.sub(r"\W", "_", room) + ".json")


class RoomedActions(base.FakeActions):
    def __init__(self, rooms_dir: Path):
        super().__init__(rooms_dir / "unused.json", by=ME)
        self.rooms_dir = rooms_dir

    def execute(self, room, action, arguments, operation_id=None):
        self.state = store(self.rooms_dir, room)
        return super().execute(room, action, arguments, operation_id)


class _Rooms(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tutorial-refresh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws, self.rooms_dir = self.tmp / "ws", self.tmp / "rooms"
        self.rooms_dir.mkdir()
        self.stub = self.tmp / "room_commons.py"
        self.stub.write_text(ROOMED_STUB)
        self.old_list = self.tmp / "old.json"
        self.old_list.write_text(json.dumps({**json.loads(tp.LIST_PATH.read_text(encoding="utf-8")),
                                             "revision": OLD}))
        env = {k: v for k, v in base.isolated_child_env().items()
               if not k.startswith(("SUTANDO_", "AG2", "REMOTE_TASK", "AGENT_", "ROOM_C", "MATRIX_"))
               and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME")}
        env.update({"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "STUB_ROOMS": str(self.rooms_dir)})
        self.env = env
        for patcher in (mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(gs, "_workspace", return_value=self.ws),
                        mock.patch.object(gs, "connect_actions", return_value=RoomedActions(self.rooms_dir))):
            patcher.start()
            self.addCleanup(patcher.stop)
        base.linux_exec_limit(self)

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gs.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def seed(self, rooms=ROOMS):
        for room in rooms:
            code, out, err = self.main("publish", "--room", room, "--name", "Blue", "--core", "codex",
                                       "--room-commons", str(self.stub), "--agent-mxid", ME,
                                       "--list", str(self.old_list))
            self.assertEqual((code, json.loads(out)["action"]), (0, "created"), err)
            self.assertEqual(self.revision(room), OLD)

    def page(self, room):
        return json.loads(store(self.rooms_dir, room).read_text())["pages"]["html-p1"]

    def revision(self, room):
        return tp.read_marker(self.page(room))[1]


class AllRoomsTest(_Rooms):
    def refresh(self, *extra):
        return self.main("publish", "--all-rooms", "--if-newer", "--name", "Blue", "--core", "codex",
                         "--room-commons", str(self.stub), "--agent-mxid", ME, *extra)

    def test_every_room_with_an_entry_is_rebuilt_and_an_edited_one_is_kept(self):
        self.seed()
        db = json.loads(store(self.rooms_dir, ROOMS[1]).read_text())
        db["pages"]["html-p1"] += "<p>OWNER NOTES</p>"
        store(self.rooms_dir, ROOMS[1]).write_text(json.dumps(db))
        code, out, err = self.refresh()
        got = json.loads(out)
        self.assertEqual((code, got["ok"], got["action"], sorted(got["rooms"])), (0, True, "refreshed", sorted(ROOMS)))
        self.assertEqual({r: got["rooms"][r]["action"] for r in ROOMS},
                         {ROOMS[0]: "updated", ROOMS[1]: "kept", ROOMS[2]: "updated"}, err)
        self.assertEqual([self.revision(r) for r in ROOMS], [tp.FEATURES.revision, OLD, tp.FEATURES.revision])
        code, out, _ = self.refresh()
        self.assertEqual({r: v["action"] for r, v in json.loads(out)["rooms"].items()},
                         {ROOMS[0]: "unchanged", ROOMS[1]: "kept", ROOMS[2]: "unchanged"})

    def test_a_failing_room_leaves_the_others_refreshed_and_sets_the_exit_code(self):
        self.seed()
        store(self.rooms_dir, ROOMS[0]).write_text("{")  # its stub store is unreadable: that room's run fails
        code, out, _ = self.refresh()
        got = json.loads(out)
        self.assertNotEqual(code, 0)
        self.assertEqual((got["ok"], got["rooms"][ROOMS[0]]["code"]), (False, code))
        self.assertEqual([got["rooms"][r]["action"] for r in ROOMS[1:]], ["updated", "updated"])

    def test_no_rooms_is_a_clean_no_op_and_the_flags_are_checked(self):
        code, out, _ = self.refresh()
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "action": "refreshed", "rooms": {}}))
        self.assertEqual(self.main("publish", "--all-rooms", "--agent-mxid", ME)[0], 2)
        self.assertEqual(self.refresh("--room", ROOMS[0])[0], 2)


TRIGGER = re.compile(r'^if \[ -n "\$PY" \] && \[ -f "\$REPO/skills/tutorial/scripts/getting_started\.py" \]; then\n'
                     r'(?:.*\n)*?fi\n', re.M)


class StartupTriggerTest(_Rooms):
    def test_startup_sh_starts_the_refresh_and_it_rebuilds_every_room(self):
        startup = (REPO / "src" / "startup.sh").read_text()
        blocks = TRIGGER.findall(startup)
        self.assertEqual(len(blocks), 1, "startup.sh starts the tutorial refresh exactly once")
        self.assertIn("publish --all-rooms --if-newer", blocks[0])
        self.assertLess(startup.index('LOGS_DIR="$WORKSPACE/logs"'), startup.index(blocks[0]))
        self.seed(ROOMS[:2])
        # A copy of the engine, so the run finds the stub room_commons beside the skill and this workspace.
        repo = self.tmp / "repo"
        ignore = shutil.ignore_patterns("__pycache__", "node_modules")
        for tree in ("src", "shared"):
            shutil.copytree(REPO / tree, repo / tree, ignore=ignore)
        shutil.copytree(REPO / "skills" / "tutorial", repo / "skills" / "tutorial", ignore=ignore)
        (repo / "skills" / "room-commons" / "scripts").mkdir(parents=True)
        shutil.copy(self.stub, repo / "skills" / "room-commons" / "scripts" / "room_commons.py")
        (repo / "sutando.config.json").write_text(json.dumps({"workspace": {"path": str(self.ws)}}))
        logs = self.tmp / "logs"
        logs.mkdir()
        env = {**self.env, "AGENT_MXID": ME, "PY": sys.executable, "REPO": str(repo), "LOGS_DIR": str(logs)}
        run = subprocess.run(["bash", "-c", "set -e\n" + blocks[0] + "wait\n"], env=env,
                             capture_output=True, text=True, timeout=300)
        log = (logs / "tutorial-refresh.log").read_text()
        self.assertEqual(run.returncode, 0, run.stderr + log)
        got = json.loads(log.strip().splitlines()[-1])
        self.assertEqual({r: v["action"] for r, v in got["rooms"].items()},
                         {ROOMS[0]: "updated", ROOMS[1]: "updated"}, log)
        self.assertEqual([self.revision(r) for r in ROOMS[:2]], [tp.FEATURES.revision] * 2)


if __name__ == "__main__":
    unittest.main()
