#!/usr/bin/env python3
"""The seed never destroys a .claude.json it cannot read (resume plan F3, 2026-10-20).

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
    def test_a_truncated_file_is_copied_aside_and_a_fresh_config_is_seeded(self):
        # Review on #5762: skipping every seed left the detached core at the onboarding,
        # trust and bypass prompts. The bytes live on in the copy; the live file is seeded.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            truncated = '{"oauthAccount": {"emailAddress": "me@example.com"}, "projects": {"/x": {'
            (ccd / ".claude.json").write_text(truncated)
            rc, out = _run(ccd, td)
            self.assertEqual(rc, 0, out)
            copies = list(ccd.glob(".claude.json.unreadable-*"))
            self.assertEqual(len(copies), 1, "one copy set aside")
            self.assertEqual(copies[0].read_text(), truncated, "the copy keeps the bytes")
            cfg = json.loads((ccd / ".claude.json").read_text())
            self.assertTrue(cfg["hasCompletedOnboarding"], "the live file is seeded from {}")
            self.assertNotIn("oauthAccount", cfg)
            self.assertIn("unreadable", out)
            self.assertIn("onboarding-seed", out)
            self.assertTrue((ccd / "settings.json").exists(), "the dangerous-mode seed runs too")

    def test_only_the_newest_copies_are_kept(self):
        # Review on #5762: a launch crash loop set one copy aside per attempt with no cap.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            truncated = '{"oauthAccount": {"emailAddress": "me@example.com"}, "projects": {"/x": {'
            (ccd / ".claude.json").write_text(truncated)
            # Named by epoch; the mtimes are deliberately the wrong way round, as copy2
            # keeps the original's mtime: the cap must sort by the name, not the clock.
            for i in range(4):
                old = ccd / f".claude.json.unreadable-{1000 + i}"
                old.write_text(f"old copy {i}")
                os.utime(old, (5000 - i, 5000 - i))
            rc, out = _run(ccd, td)
            self.assertEqual(rc, 0, out)
            copies = sorted(ccd.glob(".claude.json.unreadable-*"), key=lambda q: int(q.name.rsplit("-", 1)[1]))
            self.assertEqual(len(copies), 3, "the cap holds, the new copy included")
            self.assertEqual([q.read_text() for q in copies[:2]], ["old copy 2", "old copy 3"], "the oldest epochs went first")
            self.assertEqual(copies[-1].read_text(), truncated, "the newest copy is this launch's")

    def test_no_copy_means_no_overwrite(self):
        # Review on #5762: when the copy aside fails, the seed must not rewrite the only
        # holder of those bytes. A read-only config dir makes the copy fail.
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            truncated = '{"oauthAccount": {"emailAddress": "me@example.com"}, "projects": {"/x": {'
            (ccd / ".claude.json").write_text(truncated)
            ccd.chmod(0o555)
            try:
                rc, out = _run(ccd, td)
            finally:
                ccd.chmod(0o755)
            self.assertEqual(rc, 0, out)
            self.assertEqual((ccd / ".claude.json").read_text(), truncated, "kept byte for byte")
            self.assertEqual(list(ccd.glob(".claude.json.unreadable-*")), [], "no copy")
            self.assertFalse((ccd / "settings.json").exists(), "nothing seeded")
            self.assertIn("could not be copied aside", out)

    def test_a_failed_copy_leaves_older_copies_alone(self):
        # When this launch's copy fails, the seed removes only the stub it may have left
        # (never an older copy) and still neither prunes nor overwrites anything.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            truncated = '{"oauthAccount": {"emailAddress": "me@example.com"}, "projects": {"/x": {'
            (ccd / ".claude.json").write_text(truncated)
            # An unwritable dir makes this launch's copy fail; an older stub copy is present.
            stub = ccd / ".claude.json.unreadable-1000"
            stub.write_text("")
            ccd.chmod(0o555)
            try:
                rc, out = _run(ccd, td)
            finally:
                ccd.chmod(0o755)
            self.assertEqual(rc, 0, out)
            self.assertEqual((ccd / ".claude.json").read_text(), truncated, "kept byte for byte")
            self.assertIn("could not be copied aside", out)
            self.assertTrue(stub.exists(), "an older stub is not this launch's to remove")

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
