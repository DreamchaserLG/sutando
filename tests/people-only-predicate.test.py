#!/usr/bin/env python3
"""The people-only predicate: `space.ag2.no_agent` is a literal `true`, nothing else.

One owner (src/people_only.py) for the flag every reader and reactor consults.
The package copy under ag2-sparrow must be byte-identical (the drift test pins it).
"""
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from people_only import (  # noqa: E402
    NO_AGENT_KEY, is_people_only, omitted_note, split_people_only,
)


class LiteralTrueOnly(unittest.TestCase):
    def test_the_key_is_the_wire_spelling(self):
        self.assertEqual(NO_AGENT_KEY, "space.ag2.no_agent")

    def test_a_flagged_content_dict_is_people_only(self):
        self.assertTrue(is_people_only({"body": "lunch?", "space.ag2.no_agent": True}))

    def test_a_flagged_event_envelope_is_people_only(self):
        self.assertTrue(is_people_only({"type": "message.created",
                                        "content": {"body": "x", "space.ag2.no_agent": True}}))

    def test_only_a_literal_true_counts(self):
        for bad in ("true", "True", 1, 0, False, None, {}, [], {"on": True}, "yes"):
            self.assertFalse(is_people_only({"body": "x", NO_AGENT_KEY: bad}),
                             f"{bad!r} must not read as the flag")
            self.assertFalse(is_people_only({"content": {"body": "x", NO_AGENT_KEY: bad}}),
                             f"{bad!r} under content must not read as the flag")

    def test_an_unflagged_message_is_not_people_only(self):
        self.assertFalse(is_people_only({"body": "hello"}))
        self.assertFalse(is_people_only({"content": {"body": "hello"}}))

    def test_non_dicts_are_never_people_only(self):
        for obj in (None, "", "space.ag2.no_agent", 3, [{"space.ag2.no_agent": True}]):
            self.assertFalse(is_people_only(obj))

    def test_a_non_dict_content_does_not_raise(self):
        self.assertFalse(is_people_only({"content": "space.ag2.no_agent"}))
        self.assertFalse(is_people_only({"content": None}))


class SplitAndNote(unittest.TestCase):
    def test_split_keeps_order_and_counts_the_omitted(self):
        items = [{"event_id": "a"}, {"event_id": "b", NO_AGENT_KEY: True},
                 {"event_id": "c", "content": {NO_AGENT_KEY: True}}, {"event_id": "d"}]
        kept, omitted = split_people_only(items)
        self.assertEqual([m["event_id"] for m in kept], ["a", "d"])
        self.assertEqual(omitted, 2)

    def test_split_tolerates_none_and_non_dict_items(self):
        self.assertEqual(split_people_only(None), ([], 0))
        self.assertEqual(split_people_only(["x", 1]), (["x", 1], 0))

    def test_the_note_wording(self):
        self.assertEqual(omitted_note(1), "1 people-only message omitted")
        self.assertEqual(omitted_note(3), "3 people-only messages omitted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
