#!/usr/bin/env python3
"""The gateway heartbeat has its own clock: a timer thread posts every
HEARTBEAT_INTERVAL whatever the poll loop is doing, one post at a time."""
import inspect
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
_SANDBOX = tempfile.TemporaryDirectory()
os.environ['AGENT_CONNECT_STATE_DIR'] = str(Path(_SANDBOX.name) / 'state')
os.environ['AGENT_CONNECT_TASK_DIR'] = str(Path(_SANDBOX.name) / 'tasks')
os.environ['AGENT_CONNECT_RESULT_DIR'] = str(Path(_SANDBOX.name) / 'results')
os.environ['SUTANDO_HOST_LABEL'] = 'testhost'
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
from ag2_sparrow import remote_gateway_bridge as bridge  # noqa: E402


def _http_error(code):
    return urllib.error.HTTPError('http://gw/v1/heartbeat', code, 'x', {}, None)


class IntervalFromEnv(unittest.TestCase):
    def test_default_floor_and_bad_values(self):
        f = bridge._heartbeat_interval_from_env
        self.assertEqual(f(None), 5.0)
        self.assertEqual(f(''), 5.0)
        self.assertEqual(f('2.5'), 2.5)
        self.assertEqual(f('30'), 30.0)
        self.assertEqual(f('0.2'), 1.0, 'floored: a typo must not flood the relay')
        self.assertEqual(f('-3'), 1.0)
        self.assertEqual(f('abc'), 5.0)
        self.assertEqual(f('nan'), 5.0)

    def test_module_constant_reads_the_env_name_the_docstring_names(self):
        src = Path(bridge.__file__).read_text()
        self.assertIn('REMOTE_HEARTBEAT_INTERVAL_S', src.split('"""', 2)[1], 'documented in the config block')
        self.assertIn('HEARTBEAT_INTERVAL = _heartbeat_interval_from_env(os.environ.get("REMOTE_HEARTBEAT_INTERVAL_S"))', src)


class TimerThread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (('_STATE', Path(self.tmp.name)), ('_heartbeat_retry_at', 0.0),
                            ('_heartbeat_backoff', 0.0), ('_last_heartbeat_at', 0.0),
                            ('_last_core_health', None), ('_last_inflight_count', None),
                            ('_heartbeat_failures', 0), ('_heartbeat_last_error', None), ('_log', lambda m: None),
                            ('HEARTBEAT_INTERVAL', 0.1)):
            p = patch.object(bridge, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        bridge._OUTBOUND_STOP.clear()
        bridge._LOCK_LOST.clear()
        bridge._AUTH_RECOVERING.clear()
        self.addCleanup(bridge._OUTBOUND_STOP.set)
        self.posts = []
        self.lock = threading.Lock()

    def _req_ok(self, method, path, payload=None, timeout=None, **kw):
        with self.lock:
            self.posts.append((path, dict(payload or {})))
        return {}

    def _start(self, req):
        p = patch.object(bridge, '_req', side_effect=req)
        p.start()
        self.addCleanup(p.stop)
        t = bridge._start_heartbeat_timer({'t1', 't2'})
        self.addCleanup(lambda: (bridge._OUTBOUND_STOP.set(), t.join(timeout=2)))
        return t

    def test_posts_on_its_own_clock_with_no_poll_loop_at_all(self):
        # No poll loop runs in this test: the posts below come from the timer alone.
        self._start(self._req_ok)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.posts) >= 4:
                    break
            time.sleep(0.02)
        with self.lock:
            n = len(self.posts)
            paths = {p for p, _ in self.posts}
            inflight = {b['inflight'] for _, b in self.posts}
        self.assertGreaterEqual(n, 4, f'0.1 s interval, 3 s budget: {n} heartbeats')
        self.assertEqual(paths, {'/v1/heartbeat'})
        self.assertEqual(inflight, {2}, 'the heartbeat reports the size of the poll loop\'s inflight set')

    def test_stops_on_the_outbound_stop_event(self):
        t = self._start(self._req_ok)
        self.assertTrue(t.is_alive())
        bridge._OUTBOUND_STOP.set()
        t.join(timeout=1)
        self.assertFalse(t.is_alive(), 'one stop signal ends the timer as it ends the outbound worker')

    def test_a_401_does_not_kill_the_timer_and_the_loop_keeps_auth_recovery(self):
        calls = []

        def req(method, path, payload=None, timeout=None, **kw):
            calls.append(path)
            if len(calls) == 1:
                raise _http_error(401)
            return {}
        t = self._start(req)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(calls) < 3:
            time.sleep(0.02)
        self.assertGreaterEqual(len(calls), 3, 'the timer posted again after the 401')
        self.assertTrue(t.is_alive())
        # The timer logged and moved on; it did not run auth recovery itself.
        self.assertNotIn('_recover_auth', inspect.getsource(bridge._heartbeat_timer))

    def test_holds_still_while_the_poller_lock_is_lost(self):
        bridge._LOCK_LOST.set()
        self.addCleanup(bridge._LOCK_LOST.clear)
        self._start(self._req_ok)
        time.sleep(0.5)
        with self.lock:
            self.assertEqual(self.posts, [], 'a reaped poller must not keep announcing itself')


class TickAndLoopOnTheMainThread(unittest.TestCase):
    """The same body, run synchronously: the coverage gate does not trace threads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logged = []
        for name, value in (('_STATE', Path(self.tmp.name)), ('_heartbeat_retry_at', 0.0),
                            ('_heartbeat_backoff', 0.0), ('_last_heartbeat_at', 0.0),
                            ('_last_core_health', None), ('_last_inflight_count', None),
                            ('_heartbeat_failures', 0), ('_heartbeat_last_error', None), ('_log', self.logged.append),
                            ('HEARTBEAT_INTERVAL', 0.01)):
            p = patch.object(bridge, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        bridge._LOCK_LOST.clear()
        bridge._AUTH_RECOVERING.clear()

    def test_tick_posts_and_reports_it(self):
        with patch.object(bridge, '_req', return_value={}) as req:
            self.assertTrue(bridge._heartbeat_tick({'a'}))
        self.assertEqual(req.call_args.args[:2], ('POST', '/v1/heartbeat'))
        self.assertEqual(req.call_args.args[2]['inflight'], 1)

    def test_tick_holds_still_on_a_lost_lock(self):
        bridge._LOCK_LOST.set()
        self.addCleanup(bridge._LOCK_LOST.clear)
        with patch.object(bridge, '_req', return_value={}) as req:
            self.assertFalse(bridge._heartbeat_tick(set()))
        self.assertEqual(req.call_count, 0)

    def test_tick_swallows_a_401_with_a_log_line_and_no_recovery(self):
        with patch.object(bridge, '_req', side_effect=_http_error(401)), \
                patch.object(bridge, '_recover_auth', create=True) as recover:
            self.assertFalse(bridge._heartbeat_tick(set()))
        self.assertEqual(recover.call_count, 0)
        self.assertEqual([m for m in self.logged if 'HTTP 401' in m and 'heartbeat failed' in m].__len__(), 1, self.logged)

    def test_tick_swallows_any_other_error(self):
        with patch.object(bridge, '_req', side_effect=RuntimeError('boom')):
            self.assertFalse(bridge._heartbeat_tick(set()))
        self.assertTrue(any('RuntimeError: boom' in m for m in self.logged), self.logged)

    def test_loop_ticks_until_the_stop_event_is_set(self):
        waits = []

        class StopAfterTwo:
            def wait(self, timeout):
                waits.append(timeout)
                return len(waits) > 2  # False, False, then True: stop
        with patch.object(bridge, '_OUTBOUND_STOP', StopAfterTwo()), \
                patch.object(bridge, '_req', return_value={}) as req:
            bridge._heartbeat_timer({'a', 'b'})  # returns: the loop ended on the stop
        self.assertEqual(waits, [0.01, 0.01, 0.01], 'one wait of HEARTBEAT_INTERVAL per beat')
        self.assertEqual(req.call_count, 2, 'a post per completed wait, none after the stop')


class SingleFlight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (('_STATE', Path(self.tmp.name)), ('_heartbeat_retry_at', 0.0),
                            ('_heartbeat_backoff', 0.0), ('_last_heartbeat_at', 0.0),
                            ('_last_core_health', None), ('_last_inflight_count', None),
                            ('_heartbeat_failures', 0), ('_heartbeat_last_error', None), ('_log', lambda m: None)):
            p = patch.object(bridge, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def test_a_post_in_flight_makes_the_other_caller_skip(self):
        with patch.object(bridge, '_req', return_value={}) as req:
            self.assertTrue(bridge._HEARTBEAT_MUTEX.acquire(blocking=False))
            try:
                self.assertFalse(bridge._post_heartbeat(set(), force=True), 'skipped, not queued behind the other post')
                self.assertEqual(req.call_count, 0)
            finally:
                bridge._HEARTBEAT_MUTEX.release()
            self.assertTrue(bridge._post_heartbeat(set(), force=True))
            self.assertEqual(req.call_count, 1)

    def test_the_lock_is_released_after_an_error(self):
        with patch.object(bridge, '_req', side_effect=_http_error(401)):
            with self.assertRaises(urllib.error.HTTPError):
                bridge._post_heartbeat(set(), force=True)
        self.assertTrue(bridge._HEARTBEAT_MUTEX.acquire(blocking=False), 'released on the raise')
        bridge._HEARTBEAT_MUTEX.release()


class MainWiring(unittest.TestCase):
    def test_main_starts_the_timer_beside_the_outbound_worker_and_joins_it_on_exit(self):
        src = inspect.getsource(bridge.main)
        self.assertIn('_heartbeat_thread = _start_heartbeat_timer(inflight)', src)
        self.assertLess(src.index('_start_outbound_worker(inflight)'), src.index('_start_heartbeat_timer(inflight)'))
        self.assertIn('_heartbeat_thread.join(', src)
        self.assertIn('_post_heartbeat(inflight)', src, 'the gated poll-boundary posts stay')


class ReviewFollowUps(unittest.TestCase):
    """#5534 review: inflight changes go out at once; errors back off and log once;
    the timer is quiet while the poll loop waits in auth recovery."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logged = []
        for name, value in (('_STATE', Path(self.tmp.name)), ('_heartbeat_retry_at', 0.0),
                            ('_heartbeat_backoff', 0.0), ('_last_heartbeat_at', 0.0),
                            ('_last_core_health', None), ('_last_inflight_count', None),
                            ('_heartbeat_failures', 0), ('_heartbeat_last_error', None),
                            ('_log', self.logged.append), ('HEARTBEAT_INTERVAL', 5.0)):
            p = patch.object(bridge, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        bridge._LOCK_LOST.clear()
        bridge._AUTH_RECOVERING.clear()

    def test_a_task_taken_right_after_a_timer_post_goes_out_at_the_poll_boundary(self):
        # The reviewer's probe: timer post, a task lands in inflight, the gated
        # poll-boundary post one second later must send it (it used to return False).
        now = [1_000_000.0]
        inflight = set()
        with patch.object(bridge.time, 'time', side_effect=lambda: now[0]), \
                patch.object(bridge, '_req', return_value={}) as req:
            self.assertTrue(bridge._heartbeat_tick(inflight))
            inflight.add('task-1')
            now[0] += 1
            self.assertTrue(bridge._post_heartbeat(inflight))
            self.assertEqual([c.args[2]['inflight'] for c in req.call_args_list], [0, 1])
            now[0] += 1
            self.assertFalse(bridge._post_heartbeat(inflight), 'nothing changed: still gated')
            inflight.discard('task-1')
            now[0] += 1
            self.assertTrue(bridge._post_heartbeat(inflight), 'a finished task goes out at once too')
            self.assertEqual(req.call_args_list[-1].args[2]['inflight'], 0)

    def test_a_repeated_network_error_is_logged_once_and_the_recovery_once(self):
        err = bridge._NETWORK_ERRORS[0]('relay down')
        with patch.object(bridge, '_req', side_effect=err):
            for _ in range(5):
                self.assertFalse(bridge._heartbeat_tick(set()))
        self.assertEqual(bridge._heartbeat_failures, 5)
        self.assertEqual(len([m for m in self.logged if 'heartbeat network error' in m]), 1, self.logged)
        with patch.object(bridge, '_req', return_value={}):
            self.assertTrue(bridge._heartbeat_tick(set()))
        self.assertEqual(bridge._heartbeat_failures, 0)
        self.assertTrue(any('recovered after 5' in m for m in self.logged), self.logged)

    def test_a_different_error_is_logged_again(self):
        with patch.object(bridge, '_req', side_effect=_http_error(500)):
            bridge._heartbeat_tick(set())
            bridge._heartbeat_tick(set())
        with patch.object(bridge, '_req', side_effect=_http_error(502)):
            bridge._heartbeat_tick(set())
        self.assertEqual([m.split(' — ')[0] for m in self.logged if 'heartbeat failed' in m],
                         ['heartbeat failed: HTTP 500', 'heartbeat failed: HTTP 502'])

    def test_the_timer_backs_off_on_consecutive_failures_up_to_60_s(self):
        waits = []
        for n in (0, 1, 2, 3, 4, 50):
            with patch.object(bridge, '_heartbeat_failures', n):
                waits.append(bridge._heartbeat_timer_wait())
        self.assertEqual(waits, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0])
        with patch.object(bridge, 'HEARTBEAT_INTERVAL', 120.0), patch.object(bridge, '_heartbeat_failures', 3):
            self.assertEqual(bridge._heartbeat_timer_wait(), 120.0, 'never shorter than the interval')

    def test_a_401_storm_costs_one_log_line_and_backs_off(self):
        with patch.object(bridge, '_req', side_effect=_http_error(401)):
            for _ in range(10):
                self.assertFalse(bridge._heartbeat_tick(set()))
        self.assertEqual(len([m for m in self.logged if 'HTTP 401' in m]), 1, self.logged)
        self.assertEqual(bridge._heartbeat_timer_wait(), 60.0)

    def test_the_timer_is_quiet_while_the_poll_loop_waits_in_auth_recovery(self):
        bridge._AUTH_RECOVERING.set()
        self.addCleanup(bridge._AUTH_RECOVERING.clear)
        with patch.object(bridge, '_req', return_value={}) as req:
            for _ in range(3):
                self.assertFalse(bridge._heartbeat_tick(set()))
        self.assertEqual(req.call_count, 0, 'the rejected bearer is not presented again')
        bridge._AUTH_RECOVERING.clear()
        with patch.object(bridge, '_req', return_value={}) as req:
            self.assertTrue(bridge._heartbeat_tick(set()))

    def test_main_marks_auth_recovery_around_recover_auth(self):
        src = inspect.getsource(bridge.main)
        i = src.index('recovered = _recover_auth(e.code)')
        self.assertLess(src.rindex('_AUTH_RECOVERING.set()', 0, i), i)
        self.assertIn('_AUTH_RECOVERING.clear()', src[i:i + 200], 'cleared in a finally right after')


if __name__ == '__main__':
    unittest.main()
