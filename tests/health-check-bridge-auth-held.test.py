#!/usr/bin/env python3
"""health-check reports a bridge the wrapper holds on a rejected token as held, not down.

The stamp counts only while the wrapper pid it names is alive, so a stale stamp
cannot mask a genuinely dead bridge from fix_down_bridges.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import os
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-hc-auth-held-")

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS += 1


with tempfile.TemporaryDirectory() as td, mock.patch.object(hc, "WORKSPACE_DIR", Path(td)):
    sup = Path(td) / "state" / "channel-bridge-supervisor"
    sup.mkdir(parents=True)
    stamp = sup / "discord.auth-held"
    check(not hc._bridge_auth_held("discord"), "no stamp -> not held")
    stamp.write_text(f"1700000000\n{os.getpid()}\n")
    check(hc._bridge_auth_held("discord"), "stamp with a live wrapper pid -> held")
    check(not hc._bridge_auth_held("slack"), "the stamp is per channel")
    stamp.write_text("1700000000\n999999\n")
    with mock.patch.object(hc, "_pid_alive", return_value=False):
        check(not hc._bridge_auth_held("discord"), "stamp whose wrapper is gone -> not held")
    stamp.write_text("1700000000\n")
    check(not hc._bridge_auth_held("discord"), "stamp without a pid -> not held")

held_row = {"name": "discord-bridge", "status": "fail",
            "detail": "held: credential rejected — vault set the token, then bash src/restart.sh"}
check(not str(held_row["detail"]).startswith(hc.DOWN_BRIDGE_DETAILS["discord-bridge"]),
      "held detail is not a fix_down_bridges restart candidate")
src = (REPO / "src" / "health-check.py").read_text()
check(held_row["detail"] in src, "the probe emits exactly this held detail")

print("PASS" if FAILS == 0 else f"{FAILS} FAILED")
raise SystemExit(1 if FAILS else 0)
