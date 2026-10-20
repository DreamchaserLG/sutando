#!/usr/bin/env python3
"""The seed never rewrites a .claude.json it cannot read (resume plan F3, 2026-10-20).

A Claude Code write cut off by a reboot or a power loss leaves a truncated .claude.json.
The seed program in session-launch.sh used to treat that as an empty config and write
the onboarding keys over it, dropping everything else the file held, and the core came
up as a fresh install, signed out. Now the file is kept as it is, a copy is set aside,
and the seed is skipped for that launch. Same harness as the upsell seed test.

Run: python3 tests/start-cli-unreadable-claude-json-seed.test.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"


def _seed_program() -> str:
    m = re.search(r"<<'PY'.*?\n(.*?)\nPY\b", SCRIPT.read_text(), re.S)
    assert m, "seed PY heredoc not found in session-launch.sh"
    prog = m.group(1)
    assert "unreadable" in prog, "the seed program no longer guards an unreadable .claude.json"
    return prog


def _run(ccd: Path, home: Path) -> tuple[int, str]:
    env = dict(os.environ, _ccd=str(ccd), _cwd=str(ccd / "work"), _accept_bypass="1", HOME=str(home))
    r = subprocess.run([sys.executable, "-c", _seed_program()], env=env, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


class TestUnreadableClaudeJson(unittest.TestCase):
    def test_a_truncated_file_is_kept_and_the_seed_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            truncated = '{"oauthAccount": {"emailAddress": "me@example.com"}, "projects": {"/x": {'
            (ccd / ".claude.json").write_text(truncated)
            rc, out = _run(ccd, td)
            self.assertEqual(rc, 0, out)
            self.assertEqual((ccd / ".claude.json").read_text(), truncated, "kept byte for byte")
            copies = list(ccd.glob(".claude.json.unreadable-*"))
            self.assertEqual(len(copies), 1, "one copy set aside")
            self.assertEqual(copies[0].read_text(), truncated)
            self.assertIn("unreadable", out)
            self.assertNotIn("onboarding-seed: hasCompletedOnboarding", out)
            self.assertFalse((ccd / "settings.json").exists(), "the dangerous-mode seed is skipped too")

    def test_only_the_newest_copies_are_kept(self):
        # Review on #5762: a launch crash loop set one copy aside per attempt with no cap.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            truncated = '{"oauthAccount": {"emailAddress": "me@example.com"}, "projects": {"/x": {'
            (ccd / ".claude.json").write_text(truncated)
            for i in range(4):
                old = ccd / f".claude.json.unreadable-{1000 + i}"
                old.write_text(f"old copy {i}")
                os.utime(old, (1000 + i, 1000 + i))
            rc, out = _run(ccd, td)
            self.assertEqual(rc, 0, out)
            copies = sorted(ccd.glob(".claude.json.unreadable-*"), key=lambda q: q.stat().st_mtime)
            self.assertEqual(len(copies), 3, "the cap holds, the new copy included")
            self.assertEqual([q.read_text() for q in copies[:2]], ["old copy 2", "old copy 3"], "the oldest went first")
            self.assertEqual(copies[-1].read_text(), truncated, "the newest copy is this launch's")
            self.assertEqual((ccd / ".claude.json").read_text(), truncated, "the live file is untouched")

    def test_an_empty_file_is_still_seeded(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            (ccd / ".claude.json").write_text("")
            rc, out = _run(ccd, td)
            self.assertEqual(rc, 0, out)
            cfg = json.loads((ccd / ".claude.json").read_text())
            self.assertTrue(cfg["hasCompletedOnboarding"])
            self.assertEqual(list(ccd.glob(".claude.json.unreadable-*")), [])

    def test_a_readable_file_is_seeded_as_before(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            (ccd / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}))
            rc, out = _run(ccd, td)
            self.assertEqual(rc, 0, out)
            cfg = json.loads((ccd / ".claude.json").read_text())
            self.assertTrue(cfg["hasCompletedOnboarding"])
            self.assertEqual(cfg["oauthAccount"]["emailAddress"], "me@example.com", "other keys kept")
            self.assertIn("onboarding-seed", out)


if __name__ == "__main__":
    unittest.main()
