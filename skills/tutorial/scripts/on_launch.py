#!/usr/bin/env python3
"""The tutorial's launch hook (manifest `on_launch`): rebuild, in every room with an entry, the
Getting started page a newer feature list has outdated. It never creates a page."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import getting_started  # noqa: E402

if __name__ == "__main__":
    sys.exit(getting_started.main(["publish", "--all-rooms", "--if-newer"]))
