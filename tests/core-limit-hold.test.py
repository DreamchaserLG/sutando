#!/usr/bin/env python3
"""A core refused for a provider limit is held, not relaunched in a loop (#5633).

Pinned here:
  * the classifier: 429, rate_limit_error and the CLI's usage-limit banners are a
    limit; a plain crash or a 500 is not;
  * `decide`: a limit holds with a recorded reason, repeated revives stay held,
    and the core launches by itself once the reset time passes; with no reset
    time a bounded backoff applies and grows only on fresh evidence;
  * the real launcher (start-cli.sh, PATH-shimmed tmux): a limit leaves no core
    window and exits 75, a plain crash is healed as before, and the next revive
    after the hold ends launches the core;
  * the status surfaces: runtime-health reports `paused` with the reason, and the
    health-check recovery does not relaunch or DM while the hold is in force.

Run: python3 tests/core-limit-hold.test.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import core_limit_hold as clh  # noqa: E402

SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def limited_record(now: float, *, reset_in: float = 3600, rejected: bool = True,
                   model: str = "claude-opus-5-5", ago: float = 30) -> dict:
    """The proxy's record after the CLI's request was refused."""
    status = "rejected" if rejected else "allowed"
    headers = {
        "anthropic-ratelimit-unified-status": status,
        "anthropic-ratelimit-unified-7d-status": status,
        "anthropic-ratelimit-unified-7d-utilization": "1.0",
        "anthropic-ratelimit-unified-7d-reset": str(int(now + reset_in)),
    }
    return {
        "available": not rejected,
        "last_checked": iso(now - ago),
        "headers": headers,
        "recent_rejections": [{
            "ts": iso(now - ago), "status": 429, "path": "/v1/messages", "model": model,
            "snippet": '{"type":"error","error":{"type":"rate_limit_error","message":"usage limit"}}',
        }],
    }


class Classifier(unittest.TestCase):
    def test_limits(self):
        now = time.time()
        for text in ('API Error: 429 {"error":{"type":"rate_limit_error"}}',
                     "Claude usage limit reached. Your limit will reset at 3pm (America/Los_Angeles).",
                     "5-hour limit reached ∙ resets 3pm",
                     "Opus weekly limit reached ∙ resets Oct 20, 9am",
                     "You've hit your limit · resets 11am",
                     "Claude AI usage limit reached|1760040000"):
            self.assertIsNotNone(clh.classify_text(text, now), text)

    def test_not_limits(self):
        now = time.time()
        for text in ("", "API Error: 500 internal server error", "Segmentation fault",
                     "fetch failed", "The core exited with status 1"):
            self.assertIsNone(clh.classify_text(text, now), text)

    def test_reset_from_cli_text(self):
        at = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc).timestamp()
        lim = clh.classify_text("5-hour limit reached ∙ resets 3pm (UTC)", at)
        self.assertEqual(lim.reset_at, datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc).timestamp())
        lim = clh.classify_text("usage limit reached ∙ resets 9am (UTC)", at)
        self.assertEqual(lim.reset_at, datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc).timestamp())


class Decide(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_limit_holds_until_reset_then_resumes(self):
        now = 1_800_000_000.0
        rec = limited_record(now, reset_in=3600)
        d = clh.decide(self.ws, now, quota=rec)
        self.assertFalse(d.launch)
        hold = json.loads((self.ws / "state" / clh.HOLD_FILE).read_text())
        self.assertEqual(hold["status"], "held")
        self.assertEqual(hold["kind"], "usage-limit")
        self.assertEqual(hold["model"], "claude-opus-5-5")
        self.assertEqual(hold["reset_at"], now + 3600)
        self.assertIn("core paused: usage limit on claude-opus-5-5, resets", hold["reason"])
        status = json.loads((self.ws / "state" / "core-status.json").read_text())
        self.assertEqual((status["status"], status["step"]), ("paused", hold["reason"]))
        # Revives every two minutes until the reset: none launches.
        for minute in range(2, 60, 2):
            self.assertFalse(clh.decide(self.ws, now + minute * 60, quota=rec).launch, minute)
        # The reset passed and the same record is all there is: the core comes back.
        after = now + 3600 + clh.RESET_GRACE_S + 1
        d = clh.decide(self.ws, after, quota=rec)
        self.assertTrue(d.launch)
        self.assertEqual(clh.read_hold(self.ws)["status"], "released")
        self.assertIsNone(clh.active_hold(self.ws, after))

    def test_no_reset_backs_off_and_grows_only_on_new_evidence(self):
        now = 1_800_000_000.0
        rec = limited_record(now, rejected=False, ago=1)  # a 429 with no rejected window
        rec["last_checked"] = iso(now - 1)
        d = clh.decide(self.ws, now, quota=rec)
        self.assertFalse(d.launch)
        self.assertEqual(d.hold["until"], now + clh.BACKOFF_BASE_S)
        self.assertIn("retrying", d.line)
        t = now + clh.BACKOFF_BASE_S + 1
        self.assertTrue(clh.decide(self.ws, t, quota=rec).launch)
        # The relaunched core is refused again: a fresh 429, a longer hold.
        rec2 = limited_record(t + 30, rejected=False, ago=1)
        d = clh.decide(self.ws, t + 30, quota=rec2)
        self.assertFalse(d.launch)
        self.assertEqual(d.hold["strikes"], 2)
        self.assertEqual(d.hold["until"], t + 30 + 2 * clh.BACKOFF_BASE_S)

    def test_backoff_is_bounded(self):
        lim = clh.Limit("rate-limit", 0, None, None, "provider-record")
        self.assertEqual(clh._hold_until(lim, 20, 0), clh.BACKOFF_CAP_S)

    def test_plain_crash_launches(self):
        now = 1_800_000_000.0
        self.assertTrue(clh.decide(self.ws, now, quota=None).launch)
        self.assertTrue(clh.decide(self.ws, now, quota={"headers": {}}).launch)
        self.assertIsNone(clh.read_hold(self.ws))

    def test_old_evidence_does_not_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, ago=clh.EVIDENCE_WINDOW_S + 60)
        self.assertTrue(clh.decide(self.ws, now, quota=rec).launch)

    def test_served_since_the_429_does_not_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, rejected=False, ago=120)
        rec["last_checked"] = iso(now - 10)
        self.assertTrue(clh.decide(self.ws, now, quota=rec).launch)

    def test_other_model_family_does_not_hold(self):
        now = 1_800_000_000.0
        rec = limited_record(now, model="claude-opus-5-5")
        self.assertTrue(clh.decide(self.ws, now, quota=rec, launch_model="sonnet").launch)
        self.assertFalse(clh.decide(self.ws, now, quota=rec, launch_model="claude-opus-5-5").launch)

    def test_clear_lifts_and_same_evidence_does_not_rearm(self):
        now = 1_800_000_000.0
        rec = limited_record(now)
        self.assertFalse(clh.decide(self.ws, now, quota=rec).launch)
        clh.clear(self.ws, now + 1)
        self.assertTrue(clh.decide(self.ws, now + 2, quota=rec).launch)

    def test_cli_transcript_evidence(self):
        now = time.time()
        proj = self.ws / "projects"
        proj.mkdir()
        entry = {"isApiErrorMessage": True, "timestamp": iso(now - 20), "type": "assistant",
                 "message": {"model": "<synthetic>", "content": [
                     {"type": "text", "text": "Claude usage limit reached. Your limit will reset at 3pm (UTC)."}]}}
        (proj / "s.jsonl").write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(entry) + "\n")
        d = clh.decide(self.ws, now, quota=None, projects_dir=proj)
        self.assertFalse(d.launch)
        self.assertEqual(d.hold["source"], "cli-transcript")
        self.assertIsNotNone(d.hold["reset_at"])
        (proj / "s.jsonl").write_text(json.dumps(dict(entry, message={"content": "API Error: 500"})) + "\n")
        clh.clear(self.ws)
        self.assertTrue(clh.decide(self.ws, now + 1, quota=None, projects_dir=proj).launch)


# ---- the real launcher, PATH-shimmed (harness from start-cli-private-socket-foreign-core) ----
TMUX_STUB = r"""#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
echo "$sub $*" >> "$TMUX_LOG"
case "$sub" in
  has-session) case "$*" in *watcher*) exit 1 ;; esac; [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  new-session) touch "$SESS_MARK"; exit 0 ;;
  new-window) echo 0; exit 0 ;;
  list-panes) [ -f "$SESS_MARK" ] || exit 1; exit 0 ;;
  *) exit 0 ;;
esac
"""
PY_STUB = r"""#!/bin/bash
for a in "$@"; do
  case "$a" in
    *core_heartbeat.py|*core-input-watch.py|*core-supervisor-relay.py|*notifier*) exit 0 ;;
  esac
done
exec "$REAL_PY" "$@"
"""


class Launcher(unittest.TestCase):
    """The desktop's revive is `start-cli.sh` with the session up and the core gone."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        td = Path(self.td.name)
        self.ws = td / "workspace"
        (self.ws / "state").mkdir(parents=True)
        self.bin = td / "bin"
        self.bin.mkdir()
        self.sess = td / "sess"
        self.sess.write_text("")
        self.log = td / "tmux.log"
        for name, body in (("tmux", TMUX_STUB), ("pgrep", "#!/bin/bash\nexit 1\n"),
                           ("ps", "#!/bin/bash\nexit 0\n"), ("claude", "#!/bin/bash\nexit 0\n"),
                           ("py", PY_STUB)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(td),
            "SESS_MARK": str(self.sess), "TMUX_LOG": str(self.log),
            "SUTANDO_PY": str(self.bin / "py"), "REAL_PY": sys.executable,
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "SUTANDO_TEST_MODE": "1",
            "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_RESTART_GRACE_S": "1",
            "SUTANDO_TMUX_SOCKET": str(td / "run" / "tmux.sock"),
        }

    def tearDown(self):
        self.td.cleanup()

    def revive(self):
        self.log.write_text("")
        r = subprocess.run(["/bin/bash", str(SCRIPT)], env=self.env, capture_output=True,
                           text=True, timeout=120)
        healed = any(c.startswith("new-window") for c in self.log.read_text().splitlines())
        return r.returncode, r.stdout + r.stderr, healed

    def write_quota(self, rec):
        (self.ws / "state" / "quota-state.json").write_text(json.dumps(rec))

    def test_plain_crash_is_revived(self):
        rc, out, healed = self.revive()
        self.assertTrue(healed, out[-600:])
        self.assertNotIn("not launched", out)

    def test_limit_holds_then_resumes_after_reset(self):
        self.write_quota(limited_record(time.time()))
        for _ in range(3):
            rc, out, healed = self.revive()
            self.assertEqual(rc, 75, out[-600:])
            self.assertFalse(healed, "a held core was relaunched")
            self.assertIn("core paused: usage limit on claude-opus-5-5, resets", out)
        hold = json.loads((self.ws / "state" / clh.HOLD_FILE).read_text())
        # The reset passes: move the hold's end into the past, nothing else.
        hold["until"] = time.time() - 1
        (self.ws / "state" / clh.HOLD_FILE).write_text(json.dumps(hold))
        rc, out, healed = self.revive()
        self.assertTrue(healed, out[-600:])
        self.assertEqual(json.loads((self.ws / "state" / clh.HOLD_FILE).read_text())["status"], "released")


class StatusSurfaces(unittest.TestCase):
    def _load(self, name, rel):
        spec = importlib.util.spec_from_file_location(name, str(REPO / rel))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_runtime_health_reports_paused(self):
        rh = self._load("rh_limit", "src/runtime-health.py")
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "state").mkdir()
            clh.decide(ws, time.time(), quota=limited_record(time.time()))
            rh._core_running = lambda: True
            rh._pane_text = lambda: ""
            rh._core_status = lambda w: (None, None)
            rh._gateway_running = lambda: True
            rh._resolve_workspace = lambda repo: str(ws)
            d = rh.derive()
        self.assertEqual(d["health"], "paused")
        self.assertEqual(rh.severity_of("paused"), "warn")
        self.assertEqual(rh.severity_gate({"health": "paused", "confirm": 9}), "report")
        self.assertIn("core paused: usage limit on claude-opus-5-5", d["detail"])

    def test_health_check_recovery_holds(self):
        hc = self._load("hc_limit", "src/health-check.py")
        with tempfile.TemporaryDirectory() as td:
            calls = []
            r = hc.recover_core_if_wedged(
                state_file=Path(td) / "core-recovery.json", now=1_800_000_000.0,
                alive_fn=lambda: False, oldest_task_fn=lambda: None, status_ts_fn=lambda: None,
                just_booted_fn=lambda: False, stopped_fn=lambda: False,
                restart_fn=lambda: calls.append("restart") or True,
                sender=lambda m: calls.append("dm") or True,
                limit_hold_fn=lambda: {"until": 1_800_003_600.0, "reason": "core paused: x"})
        self.assertEqual(r["action"], "limit-hold")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
