#!/usr/bin/env python3
"""draw_and_send.py: one call draws, posts and publishes the result; a failed step publishes nothing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills" / "image-generation" / "scripts" / "draw_and_send.py"

FAKE_GENERATE = '''#!/usr/bin/env python3
import json, os, sys
out = os.environ["FAKE_OUT"]
open(out, "wb").write(b"\\xff\\xd8\\xff\\xe0" + b"\\x07" * 50)
mode = os.environ.get("FAKE_GENERATE_MODE", "ok")
if mode == "refused":
    print(json.dumps({"ok": False, "error": "refused", "message": "the model declined", "remedy": "reword"})); sys.exit(1)
print("noise line first")
print(json.dumps({"ok": True, "path": out, "model": "fake"}))
'''

FAKE_UPLOAD = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_UPLOAD_ARGS"], "w").write(json.dumps(args))
mode = os.environ.get("FAKE_UPLOAD_MODE", "ok")
if mode == "forbidden":
    print(json.dumps({"ok": False, "error": "FORBIDDEN", "detail": "not allowed here"})); sys.exit(1)
if mode == "unposted":
    print(json.dumps({"ok": True, "name": "x.jpg", "posted": False, "event_id": None})); sys.exit(0)
name = args[args.index("--name") + 1]
print(json.dumps({"ok": True, "name": name, "file_id": "f1", "mxc": "mxc://ag2.space/x", "posted": True, "event_id": "$ev1"}))
'''


class DrawAndSend(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="draw-and-send-"))
        self.ws = self.tmp / "ws"
        (self.ws / "results").mkdir(parents=True)
        self.gen = self.tmp / "gen.py"
        self.gen.write_text(FAKE_GENERATE)
        self.up = self.tmp / "up.py"
        self.up.write_text(FAKE_UPLOAD)
        self.env = {
            **os.environ,
            "DRAW_AND_SEND_GENERATE": str(self.gen),
            "DRAW_AND_SEND_UPLOAD": str(self.up),
            "FAKE_OUT": str(self.tmp / "generated-1.png"),
            "FAKE_UPLOAD_ARGS": str(self.tmp / "upload-args.json"),
        }

    def run_it(self, *extra, **env):
        e = {**self.env, **env}
        argv = [sys.executable, str(SCRIPT), "--prompt", "A golden retriever, watercolor.", "--room", "!r:ag2.space",
                "--workspace", str(self.ws), *extra]
        p = subprocess.run(argv, capture_output=True, text=True, env=e, timeout=60)
        last = [ln for ln in p.stdout.splitlines() if ln.strip()][-1]
        return p.returncode, json.loads(last)

    def test_one_call_draws_posts_and_publishes_the_result(self):
        rc, out = self.run_it("--task-id", "task-123")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(out["name"], "a-golden-retriever-watercolor.png")
        self.assertEqual(out["event_id"], "$ev1")
        args = json.loads((self.tmp / "upload-args.json").read_text())
        self.assertIn("--post", args)
        self.assertEqual(args[args.index("--room") + 1], "!r:ag2.space")
        self.assertEqual(args[args.index("--file") + 1], str(self.tmp / "generated-1.png"))
        result = (self.ws / "results" / "task-123.txt").read_text()
        self.assertTrue(result.startswith("Drew A golden retriever, watercolor and sent it to the room.\n"), result)
        self.assertIn("Posted as a-golden-retriever-watercolor.png", result)
        self.assertNotIn("[file:", result, "posted already: a [file:] marker would upload it a second time")
        self.assertEqual([p.name for p in (self.ws / "results").iterdir()], ["task-123.txt"], "no staged leftovers")

    def test_own_words_and_name(self):
        rc, out = self.run_it("--task-id", "task-7", "--name", "dog.jpg", "--say", "The dog is in your DM.")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["name"], "dog.jpg")
        self.assertTrue((self.ws / "results" / "task-7.txt").read_text().startswith("The dog is in your DM.\n"))

    def test_no_task_id_publishes_nothing(self):
        rc, out = self.run_it()
        self.assertEqual(rc, 0, out)
        self.assertIsNone(out["result"])
        self.assertEqual(list((self.ws / "results").iterdir()), [])

    def test_a_refused_generation_publishes_nothing(self):
        rc, out = self.run_it("--task-id", "task-9", FAKE_GENERATE_MODE="refused")
        self.assertEqual(rc, 1)
        self.assertEqual((out["ok"], out["step"], out["error"]), (False, "generate", "refused"))
        self.assertEqual(list((self.ws / "results").iterdir()), [])
        self.assertFalse((self.tmp / "upload-args.json").exists(), "nothing uploaded")

    def test_a_refused_upload_publishes_nothing(self):
        rc, out = self.run_it("--task-id", "task-9", FAKE_UPLOAD_MODE="forbidden")
        self.assertEqual(rc, 1)
        self.assertEqual((out["step"], out["error"]), ("upload", "FORBIDDEN"))
        self.assertEqual(list((self.ws / "results").iterdir()), [])

    def test_uploaded_but_not_posted_is_a_failure(self):
        rc, out = self.run_it("--task-id", "task-9", FAKE_UPLOAD_MODE="unposted")
        self.assertEqual(rc, 1)
        self.assertEqual((out["step"], out["error"]), ("upload", "not_posted"))
        self.assertEqual(list((self.ws / "results").iterdir()), [])

    def test_missing_upload_script_names_the_pr(self):
        rc, out = self.run_it("--task-id", "task-9", DRAW_AND_SEND_UPLOAD=str(self.tmp / "absent.ts"))
        self.assertEqual(rc, 1)
        self.assertEqual((out["step"], out["error"]), ("upload", "no_upload_script"))
        self.assertIn("5773", out["detail"])


if __name__ == "__main__":
    unittest.main()
