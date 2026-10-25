#!/usr/bin/env python3
"""outbox.receipt_state: a status only from a record that reads cleanly.

A caller deciding whether a claimed item may be sent again needs "absent" to mean
no record exists, never "the record could not be read". Pinned on the src copy and
the vendored copy the gateway imports.

Run: python3 tests/outbox-receipt-state.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import outbox as src_outbox  # noqa: E402
from ag2_sparrow import outbox as vendored_outbox  # noqa: E402

MODULES = (src_outbox, vendored_outbox)


class ReceiptState(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def record(self, mod) -> Path:
        mod.record_delivered(self.root, "proactive-1", provider="p")
        (path,) = (self.root / mod.ITEMS_DIR).iterdir()
        return path

    def test_absent_only_when_no_record_exists(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                self.assertEqual(mod.receipt_state(self.root / mod.__name__, "proactive-1"), "absent")

    def test_a_clean_record_reads_as_its_status(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                self.record(mod)
                self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "DELIVERED")

    def test_torn_malformed_or_statusless_records_are_unknown(self):
        for mod in MODULES:
            root = self.root / f"torn-{mod.__name__}"
            mod.record_delivered(root, "proactive-1", provider="p")
            (path,) = (root / mod.ITEMS_DIR).iterdir()
            for body in ('{"status": "DELIV', "[1, 2]", '{"status": ""}', "{}", '{"status": "absent"}',
                         '{"status": "delivered"}', '{"status": 1}'):
                with self.subTest(mod=mod.__name__, body=body):
                    path.write_text(body)
                    self.assertEqual(mod.receipt_state(root, "proactive-1"), "unknown")

    def test_a_dangling_or_non_regular_entry_is_unknown_not_absent(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                path = self.record(mod)
                path.unlink()
                path.symlink_to(self.root / "nowhere")
                self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")
                path.unlink()
                path.mkdir()
                self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")
                path.rmdir()

    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_an_unreadable_record_is_unknown_not_absent(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                path = self.record(mod)
                path.chmod(0)
                try:
                    self.assertEqual(mod.receipt_state(self.root, "proactive-1"), "unknown")
                finally:
                    path.chmod(0o644)


if __name__ == "__main__":
    unittest.main(verbosity=1)
