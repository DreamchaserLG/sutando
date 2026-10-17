#!/usr/bin/env python3
"""runtime_compat: discovery, classification, repair planning, assisted repair, auth.

Every binary here is a real executable shell stub on a temp PATH, so the probe
launches it exactly as it launches the user's CLI. Test names match the
"Test" column of docs/runtime-compat-repair-matrix.md.

Run: python3 tests/runtime-compat.test.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import runtime_compat as rc  # noqa: E402

CLAUDE_FLAGS = " ".join(rc.RUNTIMES["claude"].required_flags)
CODEX_FLAGS = " ".join(rc.RUNTIMES["codex"].required_flags)
Disk = namedtuple("Disk", "total used free")
PLENTY = Disk(10 ** 12, 0, 10 ** 12)


def write_exe(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def fake_cli(path: Path, version: str = "2.1.0", flags: str = CLAUDE_FLAGS,
             version_rc: int = 0, login_rc: int = 0, label: str = "(Claude Code)") -> str:
    return write_exe(path, f"""case "$1" in
  --version) echo "{version} {label}"; exit {version_rc} ;;
  --help) echo "Usage: x [options] {flags}"; exit 0 ;;
  login) exit {login_rc} ;;
esac
exit 0
""")


def fake_npm(path: Path, script: str) -> str:
    """An npm stub; $PREFIX is the --prefix argument it was given."""
    return write_exe(path, f"""PREFIX=""
while [ $# -gt 0 ]; do [ "$1" = --prefix ] && PREFIX="$2"; shift; done
{script}
""")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "runtimes"
        self.bin1 = self.tmp / "bin1"
        self.bin2 = self.tmp / "bin2"
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin1}:{self.bin2}:/usr/bin:/bin",
                    "SUTANDO_RUNTIMES_DIR": str(self.root)}
        self.sleeps = []
        self.said = []
        self._system = dict(rc.SYSTEM_LOCATIONS)
        rc.SYSTEM_LOCATIONS.update({"claude": (), "codex": ()})

    def tearDown(self):
        rc.SYSTEM_LOCATIONS.update(self._system)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def discover(self, runtime="claude", **kw):
        return rc.discover(runtime, env=self.env, **kw)

    def install(self, runtime="claude", **kw):
        kw.setdefault("disk_usage", lambda _p: PLENTY)
        return rc.assisted_install(runtime, env=self.env, sleep=self.sleeps.append,
                                   log=self.said.append, **kw)

    def managed_version(self, version="2.1.0", flags=CLAUDE_FLAGS, name="claude"):
        target = self.root / name / "versions" / version / "bin" / name
        fake_cli(target, version, flags)
        rc._swap_link(self.root / "bin" / name, target)
        return target


class Discovery(Base):
    def test_discovery_reads_version_and_capabilities(self):
        p = fake_cli(self.bin1 / "claude", "2.1.286")
        r = self.discover()
        self.assertEqual(r.status, rc.OK)
        self.assertEqual(r.selected.path, p)
        self.assertEqual(r.selected.version, "2.1.286")
        self.assertEqual(r.selected.missing_flags, [])

    def test_missing_when_nothing_installed(self):
        r = self.discover()
        self.assertEqual(r.status, rc.MISSING)
        self.assertIsNone(r.selected)
        self.assertIsNone(r.problem)

    def test_known_location_found_off_path(self):
        p = fake_cli(self.home / ".npm-global" / "bin" / "codex", "0.160.1", CODEX_FLAGS,
                     label="")
        r = self.discover("codex")
        self.assertEqual(r.selected.path, p)
        self.assertEqual(r.selected.source, "known")

    def test_system_dirs_override(self):
        p = fake_cli(self.tmp / "sys" / "claude")
        self.env["PATH"] = "/usr/bin:/bin"
        self.env["SUTANDO_RUNTIME_SYSTEM_DIRS"] = str(self.tmp / "sys")
        self.assertEqual(self.discover().selected.path, p)

    def test_known_location_never_replaces_a_path_install(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        fake_cli(self.home / ".local" / "bin" / "claude", "2.1.0")
        r = self.discover()
        self.assertIsNone(r.selected)
        self.assertEqual([c.source for c in r.candidates], ["PATH"])

    def test_too_old(self):
        fake_cli(self.bin1 / "claude", "1.0.99")
        r = self.discover()
        self.assertEqual(r.status, rc.TOO_OLD)
        self.assertIn("older than the minimum 2.0.0", r.problem.detail)

    def test_known_bad(self):
        spec = rc.RuntimeSpec(**dict(rc.RUNTIMES["claude"].__dict__, known_bad=((2, 1, 5),)))
        fake_cli(self.bin1 / "claude", "2.1.5")
        self.assertEqual(self.discover(spec=spec).status, rc.KNOWN_BAD)

    def test_newer_untested_is_usable_with_warning(self):
        fake_cli(self.bin1 / "claude", "3.0.0")
        r = self.discover()
        self.assertEqual(r.status, rc.NEWER_UNTESTED)
        self.assertTrue(r.selected.usable)
        step = rc.plan(r, root=self.root)
        self.assertFalse(step.consent_required)
        self.assertIn("@2", step.command)

    def test_incompatible_when_flag_missing(self):
        fake_cli(self.bin1 / "claude", "2.1.0", CLAUDE_FLAGS.replace("--remote-control", ""))
        r = self.discover()
        self.assertEqual(r.status, rc.INCOMPATIBLE)
        self.assertEqual(r.problem.missing_flags, ["--remote-control"])

    def test_help_failure_is_incompatible(self):
        write_exe(self.bin1 / "claude", 'case "$1" in --version) echo 2.1.0;; *) exit 2;; esac\n')
        self.assertEqual(self.discover().status, rc.INCOMPATIBLE)

    def test_broken_when_exec_fails(self):
        fake_cli(self.bin1 / "claude", "2.1.0", version_rc=1)
        r = self.discover()
        self.assertEqual(r.status, rc.BROKEN)
        self.assertIn("--version exited 1", r.problem.detail)

    def test_broken_when_not_executable(self):
        (self.bin1).mkdir()
        (self.bin1 / "claude").write_text("x")
        self.assertIn("not executable", self.discover().problem.detail)

    def test_broken_partial_binary_from_interrupted_update(self):
        write_exe(self.bin1 / "claude", "")
        (self.bin1 / "claude").write_bytes(b"\x7fELF\x00partial")
        (self.bin1 / "claude").chmod(0o755)
        self.assertEqual(self.discover().status, rc.BROKEN)

    def test_broken_dangling_link_from_interrupted_update(self):
        self.bin1.mkdir()
        os.symlink(str(self.tmp / "gone" / "claude"), str(self.bin1 / "claude"))
        r = self.discover()
        self.assertEqual(r.status, rc.BROKEN)
        self.assertIn("missing file", r.problem.detail)

    def test_broken_when_no_version_printed(self):
        write_exe(self.bin1 / "claude", "echo hello\n")
        self.assertIn("no version", self.discover().problem.detail)

    def test_broken_when_version_hangs(self):
        def run(argv, **_kw):
            raise rc.subprocess.TimeoutExpired(argv, 1)
        fake_cli(self.bin1 / "claude")
        self.assertIn("did not answer", self.discover(run=run).problem.detail)

    def test_broken_when_help_cannot_run(self):
        real = rc._run

        def run(argv, **kw):
            if argv[-1] == "--help":
                raise OSError("boom")
            return real(argv, **kw)
        fake_cli(self.bin1 / "claude")
        self.assertIn("--help could not run", self.discover(run=run).problem.detail)

    def test_broken_when_launch_raises(self):
        def run(argv, **_kw):
            raise OSError(8, "Exec format error")
        fake_cli(self.bin1 / "claude")
        self.assertIn("Exec format error", self.discover(run=run).problem.detail)

    def test_multiple_path_installs_first_usable_wins(self):
        fake_cli(self.bin1 / "claude", "1.0.0")
        good = fake_cli(self.bin2 / "claude", "2.1.0")
        r = self.discover()
        self.assertEqual(r.selected.path, good)
        self.assertEqual([c.status for c in r.candidates], [rc.TOO_OLD, rc.OK])

    def test_multiple_path_installs_keep_path_order_when_both_work(self):
        first = fake_cli(self.bin1 / "claude", "3.0.0")
        fake_cli(self.bin2 / "claude", "2.1.0")
        self.assertEqual(self.discover().selected.path, first)

    def test_same_install_twice_on_path_is_one_candidate(self):
        p = fake_cli(self.bin1 / "claude")
        self.bin2.mkdir()
        os.symlink(p, str(self.bin2 / "claude"))
        self.assertEqual(len(self.discover().candidates), 1)

    def test_directory_named_like_the_cli_is_ignored(self):
        (self.bin1 / "claude").mkdir(parents=True)
        self.assertEqual(self.discover().candidates, [])

    def test_managed_install_used_after_broken_path_install(self):
        fake_cli(self.bin1 / "claude", "2.1.0", version_rc=3)
        self.managed_version("2.2.0")
        r = self.discover()
        self.assertEqual(r.selected.source, "managed")
        self.assertEqual(r.selected.version, "2.2.0")

    def test_report_to_dict_round_trips_as_json(self):
        fake_cli(self.bin1 / "claude")
        d = json.loads(json.dumps(self.discover().to_dict()))
        self.assertEqual(d["status"], rc.OK)
        self.assertEqual(rc.Report("claude", [], None).to_dict()["selected"], None)

    def test_managed_root_defaults(self):
        self.assertEqual(rc.managed_root({"XDG_DATA_HOME": "/nonexistent/x"}),
                         Path("/nonexistent/x/sutando/runtimes"))
        self.assertTrue(str(rc.managed_root({})).endswith("sutando/runtimes"))
        self.assertTrue(str(rc.managed_root(None)))

    def test_parse_version(self):
        self.assertEqual(rc.parse_version("codex-cli 0.160.1"), (0, 160, 1))
        self.assertIsNone(rc.parse_version(""))


class Planner(Base):
    def plan(self, runtime="claude"):
        return rc.plan(self.discover(runtime), root=self.root)

    def test_plan_missing(self):
        s = self.plan()
        self.assertEqual(s.status, rc.MISSING)
        self.assertIn("claude.ai/install.sh", s.command)
        self.assertTrue(s.consent_required)
        self.assertIn("--yes", s.assisted)
        self.assertIn("doctor --runtime claude", s.verify)

    def test_plan_ok(self):
        fake_cli(self.bin1 / "claude")
        s = self.plan()
        self.assertEqual((s.status, s.command, s.consent_required), (rc.OK, "No action needed.", False))

    def test_plan_too_old_claude_uses_its_own_updater(self):
        p = fake_cli(self.bin1 / "claude", "1.5.0")
        s = self.plan()
        self.assertEqual(s.command, f"{p} update")
        self.assertIn(str(self.root), s.touches)

    def test_plan_too_old_npm_install_updates_that_prefix(self):
        prefix = self.tmp / "npmprefix"
        (prefix / "lib" / "node_modules" / "@openai" / "codex").mkdir(parents=True)
        fake_cli(prefix / "bin" / "codex", "0.10.0", CODEX_FLAGS, label="")
        self.env["PATH"] = f"{prefix / 'bin'}:/usr/bin:/bin"
        s = self.plan("codex")
        self.assertEqual(s.command, f"npm install -g --prefix {prefix} @openai/codex@0")

    def test_plan_too_old_codex_elsewhere(self):
        fake_cli(self.bin1 / "codex", "0.1.0", CODEX_FLAGS, label="")
        self.assertIn("@openai/codex@0", self.plan("codex").command)

    def test_plan_broken_reinstalls(self):
        fake_cli(self.bin1 / "claude", version_rc=1)
        s = self.plan()
        self.assertEqual(s.status, rc.BROKEN)
        self.assertIn("will not use it", s.reason)
        self.assertIsNotNone(s.assisted)

    def test_plan_broken_node_missing_points_at_node(self):
        write_exe(self.bin1 / "codex", "echo 'env: node: No such file or directory' >&2; exit 127\n")
        s = self.plan("codex")
        self.assertIn("Node.js", s.command)
        self.assertIsNone(s.assisted)


class AssistedRepair(Base):
    def npm(self, script):
        fake_npm(self.bin1 / "npm", script)

    def good_npm(self, version="2.3.0"):
        self.npm(f"""mkdir -p "$PREFIX/bin"
cat > "$PREFIX/bin/claude" <<'EOS'
#!/bin/sh
case "$1" in --version) echo "{version} (Claude Code)";; --help) echo "{CLAUDE_FLAGS}";; esac
EOS
chmod +x "$PREFIX/bin/claude"
""")

    def test_assisted_install_launches_before_reporting_success(self):
        self.good_npm()
        r = self.install()
        self.assertTrue(r.ok, r)
        self.assertEqual(r.path, str(self.root / "bin" / "claude"))
        self.assertIn("2.3.0", r.detail)
        self.assertEqual(self.discover().selected.source, "managed")
        self.assertEqual(list((self.root / "claude").glob(".staging-*")), [])

    def test_reinstall_of_same_version_replaces_it(self):
        self.good_npm()
        self.assertTrue(self.install().ok)
        self.assertTrue(self.install().ok)

    def test_npm_missing(self):
        self.env["PATH"] = str(self.tmp / "empty")
        r = self.install()
        self.assertEqual(r.reason, "npm-missing")
        self.assertIn("Node.js", r.user_action)

    def test_disk_full_checked_before_download(self):
        marker = self.tmp / "npm-ran"
        self.npm(f'touch "{marker}"\n')
        r = self.install(disk_usage=lambda _p: Disk(10, 10, 5 * 1024 * 1024))
        self.assertEqual(r.reason, "disk-full")
        self.assertFalse(marker.exists())
        self.assertIn("Free at least", r.user_action)

    def test_disk_full_during_download_is_not_retried(self):
        self.npm('echo "npm ERR! code ENOSPC" >&2; exit 1\n')
        r = self.install()
        self.assertEqual((r.reason, r.attempts, self.sleeps), ("disk-full", 1, []))

    def test_blocked_download_retries_with_backoff_then_guides(self):
        old = self.managed_version("2.1.0")
        self.npm('echo "npm ERR! getaddrinfo ENOTFOUND registry.npmjs.org" >&2; exit 1\n')
        r = self.install()
        self.assertEqual(r.reason, "blocked-download")
        self.assertEqual(r.attempts, 3)
        self.assertEqual(self.sleeps, [2.0, 4.0])
        self.assertIn("registry.npmjs.org", r.user_action)
        self.assertEqual(os.path.realpath(self.root / "bin" / "claude"), os.path.realpath(old))

    def test_blocked_download_recovers_on_retry(self):
        count = self.tmp / "count"
        self.good_npm()
        body = (self.bin1 / "npm").read_text()
        (self.bin1 / "npm").write_text(body.replace(
            '\nmkdir -p "$PREFIX/bin"',
            f'\nif [ ! -f "{count}" ]; then touch "{count}"; echo ETIMEDOUT >&2; exit 1; fi\n'
            'mkdir -p "$PREFIX/bin"', 1))
        r = self.install()
        self.assertTrue(r.ok, r)
        self.assertEqual((r.attempts, self.sleeps), (2, [2.0]))

    def test_generic_failure_and_unlaunchable_npm(self):
        self.npm('echo "npm ERR! something odd" >&2; exit 1\n')
        self.assertEqual(self.install(attempts=1).reason, "install-failed")

        def run(argv, **_kw):
            raise OSError("cannot exec")
        self.assertEqual(self.install(attempts=1, run=run).reason, "install-failed")

    def test_install_timeout_is_retried(self):
        def run(argv, **_kw):
            raise rc.subprocess.TimeoutExpired(argv, 1)
        self.npm("exit 0\n")
        r = self.install(attempts=2, run=run)
        self.assertEqual((r.reason, r.attempts), ("install-failed", 2))

    def test_interrupted_update_keeps_old_version(self):
        old = self.managed_version("2.1.0")
        self.npm('mkdir -p "$PREFIX/bin"; printf "#!/bin/sh\\nech" > "$PREFIX/bin/claude"; '
                 'chmod +x "$PREFIX/bin/claude"; kill -9 $$\n')
        r = self.install(attempts=1)
        self.assertFalse(r.ok)
        self.assertEqual(list((self.root / "claude").glob(".staging-*")), [])
        r2 = self.discover()
        self.assertEqual(os.path.realpath(r2.selected.path), os.path.realpath(old))

    def test_leftover_staging_is_reported_then_cleaned(self):
        self.managed_version("2.1.0")
        stale = self.root / "claude" / ".staging-999999-1"
        fake_cli(stale / "bin" / "claude", "2.4.0")
        r = self.discover(pid_alive=lambda _pid: False)
        self.assertEqual(r.interrupted, [str(stale)])
        self.assertEqual(r.selected.version, "2.1.0")
        self.good_npm()
        self.assertTrue(self.install(pid_alive=lambda _pid: False).ok)
        self.assertFalse(stale.exists())

    def test_live_staging_is_not_treated_as_interrupted(self):
        live = self.root / "claude" / f".staging-{os.getpid()}-1"
        live.mkdir(parents=True)
        self.assertEqual(self.discover().interrupted, [])
        self.assertFalse(rc._pid_alive(0))
        self.assertTrue(rc._pid_alive(1))

    def test_unusable_download_keeps_old_version(self):
        old = self.managed_version("2.1.0")
        self.good_npm(version="1.0.0")
        r = self.install()
        self.assertEqual(r.reason, "verify-failed")
        self.assertEqual(os.path.realpath(self.root / "bin" / "claude"), os.path.realpath(old))

    def test_link_that_will_not_launch_is_not_success(self):
        self.good_npm()
        real = rc.probe

        def probe(path, spec, **kw):
            c = real(path, spec, **kw)
            if kw.get("source") == "managed":
                c.status, c.detail = rc.BROKEN, "would not start"
            return c
        orig, rc.probe = rc.probe, probe
        try:
            r = self.install()
        finally:
            rc.probe = orig
        self.assertEqual(r.reason, "verify-failed")
        self.assertIn("would not start", r.detail)

    def test_swap_link_replaces_stale_temp(self):
        target = self.tmp / "t"
        target.write_text("")
        tmp = self.root / "bin" / f".claude.tmp-{os.getpid()}"
        tmp.parent.mkdir(parents=True)
        os.symlink("nowhere", str(tmp))
        rc._swap_link(self.root / "bin" / "claude", target)
        self.assertEqual(os.readlink(self.root / "bin" / "claude"), str(target))

    def test_interrupted_updates_without_runtime_dir(self):
        self.assertEqual(rc.interrupted_updates(self.root, "claude", lambda _p: False), [])
        (self.root / "claude" / ".staging-x").mkdir(parents=True)
        self.assertEqual(len(rc.interrupted_updates(self.root, "claude", lambda _p: True)), 1)


class Auth(Base):
    def test_codex_logged_out_points_to_login(self):
        p = fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, login_rc=1, label="")
        self.env["CODEX_HOME"] = str(self.home / "codex home")
        a = rc.auth_status("codex", p, env=self.env)
        self.assertEqual(a["state"], "logged-out")
        self.assertIn("login", a["remedy"])
        self.assertIn("'", a["remedy"])

    def test_codex_logged_in_and_unknown(self):
        p = fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, label="")
        self.assertEqual(rc.auth_status("codex", p, env=self.env)["state"], "ok")

        def run(argv, **_kw):
            raise OSError("x")
        self.assertEqual(rc.auth_status("codex", p, env=self.env, run=run)["state"], "unknown")

    def test_claude_auth_uses_auth_preflight(self):
        cfg = self.tmp / "cfg"
        cfg.mkdir()
        a = rc.auth_status("claude", "claude", env=self.env, claude_config_dir=str(cfg),
                           keychain_check=lambda: False)
        self.assertEqual(a["state"], "logged-out")
        self.assertIn("/login", a["remedy"])
        (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"x": 1}}))
        (cfg / ".credentials.json").write_text("{}")
        self.assertEqual(rc.auth_status("claude", "claude", env=self.env,
                                        claude_config_dir=str(cfg))["state"], "ok")
        self.assertEqual(rc.auth_status("claude", "claude", env=self.env)["state"], "unknown")


class Ensure(Base):
    def report(self, usable=True, status=rc.OK):
        c = rc.Candidate("/nonexistent/claude", "PATH", status if usable else rc.TOO_OLD, "2.1.0")
        return rc.Report("claude", [c], c if usable else None, ["/nonexistent/.staging-1-1"])

    def ensure(self, reports, auths=None, **kw):
        reports = list(reports)
        auths = list(auths or [{"state": "ok"}])
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        kw.setdefault("say", self.said.append)
        return rc.ensure("claude", env=self.env,
                         discover_fn=lambda *_a, **_k: reports.pop(0) if len(reports) > 1 else reports[0],
                         auth_fn=lambda *_a, **_k: auths.pop(0) if len(auths) > 1 else auths[0],
                         sleep=sleep, now=lambda: clock[0], **kw)

    def test_usable_returns_path(self):
        self.assertEqual(self.ensure([self.report()]), (0, "/nonexistent/claude"))
        self.assertTrue(any("interrupted update" in m for m in self.said))

    def test_unusable_without_consent_stops_with_fix_and_resume_hint(self):
        calls = []
        code, path = self.ensure([self.report(False)], repair=True, consent=lambda _q: False,
                                 repair_fn=lambda *a, **k: calls.append(1),
                                 resume_hint="bash src/agent/start-cli.sh")
        self.assertEqual((code, path, calls), (rc.EXIT_UNUSABLE, None, []))
        text = "\n".join(self.said)
        self.assertIn("fix:", text)
        self.assertIn("Then run again: bash src/agent/start-cli.sh", text)

    def test_unusable_without_repair_offers_assisted_command(self):
        self.ensure([self.report(False)])
        self.assertTrue(any("let Sutando install" in m for m in self.said))

    def test_repair_with_consent_resumes_the_same_launch(self):
        code, path = self.ensure(
            [self.report(False), self.report()], repair=True, consent=lambda _q: True,
            repair_fn=lambda *a, **k: rc.RepairResult(True, "installed", "claude 2.3.0"))
        self.assertEqual((code, path), (0, "/nonexistent/claude"))
        self.assertTrue(any("repair succeeded" in m for m in self.said))

    def test_failed_repair_names_the_next_step(self):
        code, _ = self.ensure(
            [self.report(False)], repair=True, consent=lambda _q: True,
            repair_fn=lambda *a, **k: rc.RepairResult(False, "disk-full", "5 MB", "Free 600 MB"))
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertTrue(any("next: Free 600 MB" in m for m in self.said))

    def test_waits_for_the_user_fix_then_continues(self):
        code, _ = self.ensure([self.report(False), self.report(False), self.report()], wait_s=30)
        self.assertEqual(code, 0)
        self.assertTrue(any("continuing the launch" in m for m in self.said))

    def test_wait_times_out(self):
        self.assertEqual(self.ensure([self.report(False)], wait_s=12)[0], rc.EXIT_UNUSABLE)

    def test_newer_untested_warns_and_continues(self):
        self.assertEqual(self.ensure([self.report(status=rc.NEWER_UNTESTED)])[0], 0)

    def test_logged_out_enforced_stops(self):
        out = {"state": "logged-out", "remedy": "run: codex login", "detail": "not authenticated"}
        code, _ = self.ensure([self.report()], [out], enforce_auth=True)
        self.assertEqual(code, rc.EXIT_LOGIN)
        self.assertTrue(any("not authenticated" in m for m in self.said))

    def test_logged_out_advisory_continues(self):
        out = {"state": "logged-out", "remedy": "/login", "detail": "x"}
        self.assertEqual(self.ensure([self.report()], [out])[0], 0)

    def test_auth_recovery_waits_for_login_then_continues(self):
        out = {"state": "logged-out", "remedy": "run: codex login", "detail": "x"}
        code, _ = self.ensure([self.report()], [out, out, {"state": "ok"}], enforce_auth=True,
                              wait_s=60)
        self.assertEqual(code, 0)


class Cli(Base):
    def main(self, *argv, consent=lambda _q: False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main(list(argv), env=self.env, consent=consent)
        return code, out.getvalue(), err.getvalue()

    def test_doctor_text_and_json(self):
        fake_cli(self.bin1 / "claude", "1.0.0")
        fake_cli(self.bin2 / "claude", "2.1.0")
        fake_cli(self.bin1 / "codex", "0.160.0", CODEX_FLAGS, login_rc=1, label="")
        (self.root / "codex" / ".staging-999999-1").mkdir(parents=True)
        code, out, _ = self.main("doctor")
        self.assertEqual(code, 0)
        self.assertIn("* [ok]", out)
        self.assertIn("[too-old]", out)
        self.assertIn("login:", out)
        self.assertIn("interrupted update", out)
        code, out, _ = self.main("doctor", "--runtime", "claude", "--json")
        self.assertEqual(json.loads(out)[0]["plan"]["status"], rc.OK)

    def test_doctor_unusable_exits_3_with_assisted_fix(self):
        code, out, _ = self.main("doctor", "--runtime", "claude")
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertIn("assisted fix", out)

    def test_repair_requires_consent(self):
        self.assertEqual(self.main("repair", "--runtime", "claude")[0], rc.EXIT_USAGE)

    def test_repair_reports_failure(self):
        self.env["PATH"] = str(self.tmp / "empty")
        code, _, err = self.main("repair", "--runtime", "claude", "--yes")
        self.assertEqual(code, rc.EXIT_UNUSABLE)
        self.assertIn("npm-missing", err)

    def test_repair_success(self):
        AssistedRepair.good_npm(self)
        old = rc.shutil.disk_usage
        rc.shutil.disk_usage = lambda _p: PLENTY
        try:
            code, _, err = self.main("repair", "--runtime", "claude", "--yes")
        finally:
            rc.shutil.disk_usage = old
        self.assertEqual(code, 0, err)

    def test_ensure_prints_selected_path(self):
        p = fake_cli(self.bin1 / "claude")
        code, out, _ = self.main("ensure", "--runtime", "claude")
        self.assertEqual((code, out.strip()), (0, p))

    def test_ensure_consent_env_runs_the_repair(self):
        AssistedRepair.good_npm(self)
        self.env["SUTANDO_RUNTIME_REPAIR_CONSENT"] = "1"
        old = rc.shutil.disk_usage
        rc.shutil.disk_usage = lambda _p: PLENTY
        try:
            code, out, err = self.main("ensure", "--runtime", "claude", "--repair")
        finally:
            rc.shutil.disk_usage = old
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), str(self.root / "bin" / "claude"))

    def test_ensure_unusable_exit_code(self):
        self.assertEqual(self.main("ensure", "--runtime", "codex")[0], rc.EXIT_UNUSABLE)


class HealthCheck(unittest.TestCase):
    """health-check reports the same verdict and names the fix."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hc_compat", REPO / "src" / "health-check.py")
        cls.hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.hc)

    def check(self, report, runtime="claude"):
        return self.hc.check_core_runtime_compat(runtime, discover=lambda *_a, **_k: report)

    def test_ok_warn_down(self):
        ok = rc.Candidate("/nonexistent/claude", "PATH", rc.OK, "2.1.0")
        self.assertEqual(self.check(rc.Report("claude", [ok], ok))["status"], "ok")
        new = rc.Candidate("/nonexistent/claude", "PATH", rc.NEWER_UNTESTED, "3.0.0", detail="newer")
        r = self.check(rc.Report("claude", [new], new))
        self.assertEqual(r["status"], "warn")
        self.assertIn("Fix:", r["detail"])
        down = self.check(rc.Report("claude", [], None))
        self.assertEqual(down["status"], "down")
        self.assertIn("claude.ai/install.sh", down["detail"])
        self.assertIn("--yes", down["detail"])

    def test_unknown_and_unresolved_runtime(self):
        self.assertEqual(self.check(None, runtime="agy")["status"], "warn")

        def boom(*_a, **_k):
            raise ValueError("bad config")
        orig, self.hc.resolve_core_runtime = self.hc.resolve_core_runtime, boom
        try:
            r = self.hc.check_core_runtime_compat()
        finally:
            self.hc.resolve_core_runtime = orig
        self.assertIn("bad config", r["detail"])

    def test_registered_in_run_checks(self):
        self.assertIn("checks.append(check_core_runtime_compat())",
                      (REPO / "src" / "health-check.py").read_text())


AssistedRepair.good_npm.__doc__ = "Shared with Cli: an npm stub that installs a working CLI."
Cli.npm = AssistedRepair.npm


if __name__ == "__main__":
    unittest.main(verbosity=1)
