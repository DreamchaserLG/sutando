#!/usr/bin/env python3
"""A relay that holds every task poll while answering everything else (#5678).

Drives the real poll loop against a stub relay on loopback: `GET /v1/tasks`
is held past the client's read timeout, every other request answers 200. The
bridge used to read each timeout as an empty poll (stamping `connected: true`
and a fresh `last_ok_ts`), then back off forever while heartbeats kept the
agent looking alive. Here it must not claim success, must name the stuck
session after STUCK_POLL_TIMEOUTS, and must exit for a relaunch at most once
per STUCK_POLL_RECOVERY_INTERVAL_S without losing a claimed task. The last
case drives the real launchd wrapper to show that exit code is relaunched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time as _real_time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402
_GW, _ = isolate_then_import()

# The real read timeout is POLL_WAIT + 10 s; the stub holds longer than this one.
_SHORT_READ_TIMEOUT_S = 0.3


class _StubRelay:
    """Holds every task poll (after `first_tasks`, served once) and answers the rest."""

    def __init__(self, first_tasks=None, fail_acks=False):
        self.release = threading.Event()
        self.hits: list[str] = []
        stub = self
        pending = [list(first_tasks)] if first_tasks else []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                except OSError:
                    pass

            def do_GET(self):
                stub.hits.append(f"GET {self.path}")
                if self.path.startswith("/v1/tasks?"):
                    if pending:
                        self._send(200, {"tasks": pending.pop(0)})
                        return
                    stub.release.wait(30)
                    self._send(200, {"tasks": []})
                    return
                self._send(200, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                stub.hits.append(f"POST {self.path}")
                if fail_acks and self.path.endswith("/ack"):
                    self._send(503, {"error": "unavailable"})
                    return
                self._send(200, {})

            do_PUT = do_POST

        ThreadingHTTPServer.daemon_threads = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class _Clock:
    """The module's `time`: a held poll costs its full read timeout and a back-off
    costs its delay on the virtual clock, without the test waiting either out."""

    def __init__(self, gw):
        self.gw, self.offset = gw, 0.0

    def time(self):
        return _real_time.time() + self.offset

    def monotonic(self):
        return _real_time.monotonic() + self.offset

    def sleep(self, s):
        if threading.current_thread() is threading.main_thread():
            self.offset += s
        else:
            _real_time.sleep(s)

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _Bridge:
    """The real `main()` against the stub; `passes` loop iterations, then the
    singleton check ends it like a lost lock would."""

    _NOOP = ("_recover_orphan_proactive", "_maybe_start_event_channel",
             "_push_pool_advertisement", "_retry_withheld_reviews", "refresh_routing")

    def __init__(self, gw, relay, root: Path):
        self.gw, self.relay, self.root = gw, relay, root
        self.logs: list[str] = []
        self.writes: list[dict] = []
        self._saved: dict = {}

    def __enter__(self):
        gw = self.gw
        names = ("TOKEN", "URL", "time", "_acquire_singleton", "_heartbeat_singleton", "_req",
                 "_log", "_emit_gateway_status", "GATEWAY_STATUS_FILE", "STUCK_POLL_RECOVERY_FILE",
                 "_last_heartbeat_at", "_heartbeat_retry_at", "_ack_disabled_until") + self._NOOP
        for n in names:
            self._saved[n] = getattr(gw, n, None)
        # Absent before the fix: the harness must run there so the asserts can fail.
        self._streak = dict(getattr(gw, "_POLL_STREAK", {}))
        self._req_ok = dict(getattr(gw, "_LAST_REQ_OK", {}))
        gw._OUTBOUND_STOP.clear()
        gw._OUTBOUND_WAKE.clear()
        gw.TOKEN, gw.URL = "secret", self.relay.url
        self.clock = gw.time = _Clock(gw)
        gw._acquire_singleton = lambda *a, **k: True
        gw._last_heartbeat_at = gw._heartbeat_retry_at = gw._ack_disabled_until = 0.0
        for n in self._NOOP:
            setattr(gw, n, lambda *a, **k: None)
        real_req = self._saved["_req"]

        def req(method, path, payload=None, timeout=gw._REQ_TIMEOUT_S):
            if path.startswith("/v1/tasks?wait="):
                try:
                    return real_req(method, path, payload, timeout=_SHORT_READ_TIMEOUT_S)
                except TimeoutError:
                    self.clock.offset += gw.POLL_WAIT + 10   # what the real hold costs
                    raise
            return real_req(method, path, payload, timeout=timeout)

        gw._req = req
        gw._log = lambda m: self.logs.append(str(m))
        gw.GATEWAY_STATUS_FILE = self.root / "state" / "gateway-status.json"
        gw.STUCK_POLL_RECOVERY_FILE = self.root / "state" / "gateway-stuck-poll.json"
        real_emit = self._saved["_emit_gateway_status"]

        def emit(connected, **k):
            real_emit(connected, **k)
            self.writes.append(json.loads(gw.GATEWAY_STATUS_FILE.read_text()))

        gw._emit_gateway_status = emit
        return self

    def __exit__(self, *exc):
        gw = self.gw
        for n, v in self._saved.items():
            setattr(gw, n, v)
        for name, saved in (("_POLL_STREAK", self._streak), ("_LAST_REQ_OK", self._req_ok)):
            if hasattr(gw, name):
                getattr(gw, name).clear(); getattr(gw, name).update(saved)
        gw._OUTBOUND_STOP.set(); gw._OUTBOUND_WAKE.set()
        return False

    def run(self, passes: int):
        alive = [True] * passes + [False]
        self.gw._heartbeat_singleton = lambda *a, **k: alive.pop(0) if alive else False
        try:
            self.gw.main()
        except SystemExit as e:
            return e.code
        return None


class StuckRelayPollTest(unittest.TestCase):
    def setUp(self):
        self.gw = _GW
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for d in ("state", "tasks", "results"):
            (self.root / d).mkdir()
        self.relay = _StubRelay()

    def tearDown(self):
        self.relay.close()
        self.tmp.cleanup()

    def n(self):
        return getattr(self.gw, "STUCK_POLL_TIMEOUTS", 5)

    def code(self):
        return getattr(self.gw, "STUCK_POLL_EXIT_CODE", 69)

    def seed_status(self, last_ok):
        (self.root / "state" / "gateway-status.json").write_text(
            json.dumps({"connected": True, "ts": last_ok, "last_ok_ts": last_ok}))

    # (a) a timed-out poll claims no success
    def test_a_timed_out_poll_neither_advances_last_ok_ts_nor_reports_connected(self):
        self.seed_status(1_700_000_000)
        with _Bridge(self.gw, self.relay, self.root) as b:
            rc = b.run(passes=2)          # both inside the grace: the benign-empty path
        self.assertIsNone(rc)
        polls = [h for h in self.relay.hits if h.startswith("GET /v1/tasks?")]
        self.assertEqual(len(polls), 2, "both passes reached the held poll")
        after_poll = b.writes[1:]         # the first write is the startup "not yet connected"
        self.assertTrue(after_poll, "each timed-out pass still writes the status record")
        for w in after_poll:
            self.assertEqual(w["last_ok_ts"], 1_700_000_000, "a timeout stamped last_ok_ts")
            self.assertIs(w["connected"], False, "a timeout reported connected:true")
        self.assertIn("timed out", after_poll[-1]["error"])
        self.assertEqual(b.clock.offset, 2 * (self.gw.POLL_WAIT + 10), "no back-off inside the grace")

    # (b) the stuck session is named after N timeouts, with heartbeats answering
    def test_n_timeouts_with_heartbeats_answering_is_a_stuck_poll(self):
        n = self.n()
        with _Bridge(self.gw, self.relay, self.root) as b:
            rc = b.run(passes=n - 1)
        self.assertIsNone(rc, "fewer than N timeouts is not stuck")
        self.assertNotIn("stuck_poll", b.writes[-1])
        self.assertEqual(b.writes[-1].get("consecutive_poll_timeouts"), n - 1)
        with _Bridge(self.gw, self.relay, self.root) as b:
            rc = b.run(passes=n)
        self.assertEqual(rc, self.code(), "the Nth timeout ends the process for a relaunch")
        self.assertTrue(any(h == "POST /v1/heartbeat" for h in self.relay.hits),
                        "heartbeats kept answering through the streak")
        last = b.writes[-1]
        self.assertIs(last.get("stuck_poll"), True)
        self.assertEqual(last.get("consecutive_poll_timeouts"), n)
        self.assertIsInstance(last.get("stuck_since"), int)
        self.assertIs(last["connected"], False)
        stuck_lines = [m for m in b.logs if m.startswith("stuck relay poll: ") and "consecutive" in m]
        self.assertEqual(len(stuck_lines), 1, "logged once per episode")

    def test_timeouts_with_nothing_else_answering_are_an_outage_not_a_stuck_poll(self):
        real = self.gw._post_heartbeat
        with _Bridge(self.gw, self.relay, self.root) as b:
            self.gw._post_heartbeat = lambda *a, **k: False   # no other request reaches the relay
            try:
                rc = b.run(passes=self.n() + 2)
            finally:
                self.gw._post_heartbeat = real
        self.assertIsNone(rc)
        self.assertNotIn("stuck_poll", b.writes[-1])

    # (c) at most one recovery per interval, across the relaunch
    def test_recovery_runs_at_most_once_per_interval(self):
        n = self.n()
        interval = getattr(self.gw, "STUCK_POLL_RECOVERY_INTERVAL_S", 600)
        with _Bridge(self.gw, self.relay, self.root) as b:
            self.assertEqual(b.run(passes=n), self.code())
        stamp = json.loads((self.root / "state" / "gateway-stuck-poll.json").read_text())["at"]
        # The relaunched process meets the same stuck relay inside the interval.
        with _Bridge(self.gw, self.relay, self.root) as b:
            rc = b.run(passes=n + 3)
            offset = b.clock.offset
        self.assertIsNone(rc, "a second restart inside the interval")
        self.assertIs(b.writes[-1].get("stuck_poll"), True, "still surfaced while held back")
        held = [m for m in b.logs if "next one allowed in" in m]
        self.assertEqual(len(held), 1, "the hold-back is logged once")
        self.assertLess(offset, interval, "the case stayed inside the interval")
        # Once the interval has passed, the next timeout of the episode restarts again.
        with _Bridge(self.gw, self.relay, self.root) as b:
            b.clock.offset = interval + 1
            self.assertEqual(b.run(passes=n), self.code())
        stamp2 = json.loads((self.root / "state" / "gateway-stuck-poll.json").read_text())["at"]
        self.assertGreaterEqual(stamp2 - stamp, interval)

    # (d) a claimed task survives the recovery exit
    def test_a_claimed_unacked_task_survives_the_recovery_exit(self):
        self.relay.close()
        tid = "task-1760000000123"
        self.relay = _StubRelay(first_tasks=[{"id": tid, "task": "summarise the doc"}], fail_acks=True)
        with _Bridge(self.gw, self.relay, self.root) as b:
            rc = b.run(passes=self.n() + 1)
        self.assertEqual(rc, self.code())
        self.assertIn(f"POST /v1/tasks/{tid}/ack", self.relay.hits, "the claim was acked and refused")
        self.assertTrue(list(self.gw.TASKS_DIR.glob(f"{tid}*")), "the task file is gone")
        self.assertIn(tid, self.gw._load_inflight(), "the relaunch would not reload it in flight")
        self.assertIn(tid, self.gw._load_pending_acks(), "the relaunch would not retry its ack")


class SupervisorRelaunchTest(unittest.TestCase):
    """The real launchd wrapper relaunches the recovery exit (it retires on 75)."""

    def test_the_wrapper_relaunches_the_stuck_poll_exit(self):
        code = getattr(_GW, "STUCK_POLL_EXIT_CODE", None)
        self.assertIsNotNone(code, "no stuck-poll exit code")
        self.assertNotIn(code, (0, 75), "0 and 75 are stand-downs no supervisor relaunches")
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            for sub in ("src/launchd", "scripts", "ws/state/channel-bridge-supervisor", "ws/results", "shims"):
                (d / sub).mkdir(parents=True)
            (d / "scripts" / "sutando-config.sh").write_text(
                f'#!/bin/bash\ncase "${{1:-}}" in workspace) echo "{d}/ws" ;; *) echo "{d}/none.env" ;; esac\n')
            # First run exits like a stuck-poll recovery; the relaunch stands down with 75.
            (d / "src" / "remote-gateway-bridge.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                f"p = Path({str(d / 'runs')!r}); n = int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n + 1))\n"
                f"sys.exit({code} if n == 0 else 75)\n")
            wrapper = d / "src" / "launchd" / "gateway-bridge-wrapper.sh"
            wrapper.write_text((_REPO / "src" / "launchd" / "gateway-bridge-wrapper.sh").read_text())
            (d / "shims" / "python3").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
            (d / "shims" / "osascript").write_text("#!/bin/bash\nexit 0\n")
            for f in ("scripts/sutando-config.sh", "shims/python3", "shims/osascript"):
                os.chmod(d / f, 0o755)
            env = {k: v for k, v in os.environ.items() if k != "AG2_REMOTE_TOKEN"}
            env.update(PATH=f"{d / 'shims'}:{os.environ['PATH']}", REMOTE_TASK_TOKEN="t",
                       SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY="0")
            r = subprocess.run(["bash", str(wrapper)], cwd=d, env=env, capture_output=True,
                               text=True, timeout=30)
            runs = int((d / "runs").read_text())
        self.assertEqual(runs, 2, f"the wrapper did not relaunch exit {code}:\n{r.stderr}")
        self.assertIn("automatically restarting", r.stderr)
        self.assertEqual(r.returncode, 0, "the stand-down after the relaunch ends the wrapper")


if __name__ == "__main__":
    unittest.main(verbosity=2)
