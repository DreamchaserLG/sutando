#!/usr/bin/env python3
"""Find, classify and repair the agent CLI (Claude Code or Codex) the core runs on.

One owner for "is this install's core CLI usable, and if not, what fixes it".
Both runtime launchers call `ensure` before they replace a core; health-check
and `doctor` call `discover` to report the same verdict without acting.

Every candidate binary (PATH order, then the Sutando-managed install, then the
known install locations) is launched with `--version` and `--help`, and gets
exactly one class:

  ok              usable, within the tested major version
  newer-untested  usable, but a newer major than this engine was tested with
  too-old         below the minimum version
  known-bad       a version listed as broken for the core
  incompatible    reads a version, but lacks a flag the launcher passes
  broken          does not launch (not executable, dangling link, exec error)
  missing         no candidate at all

The first usable candidate in that order wins, so a host whose first `claude`
on PATH works keeps using exactly that binary.

Repairs never touch the user's own installs or config. The assisted repair
installs a compatible CLI into a Sutando-owned directory, only with explicit
consent, with retry and backoff, a disk-space check before the download, and
an atomic switch that keeps the previous version when anything fails. Success
is reported only after the new binary has actually been launched.

CLI:
  python3 src/runtime_compat.py doctor [--runtime claude|codex|all] [--json]
  python3 src/runtime_compat.py ensure --runtime R [--wait S] [--repair] [--yes]
  python3 src/runtime_compat.py repair --runtime R --yes
Exit codes for ensure: 0 usable (prints the binary path), 3 binary unusable,
4 login required, 2 usage error. The repair matrix:
docs/runtime-compat-repair-matrix.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

OK = "ok"
NEWER_UNTESTED = "newer-untested"
TOO_OLD = "too-old"
KNOWN_BAD = "known-bad"
INCOMPATIBLE = "incompatible"
BROKEN = "broken"
MISSING = "missing"
USABLE = (OK, NEWER_UNTESTED)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNUSABLE = 3
EXIT_LOGIN = 4

PROBE_TIMEOUT_S = 30
INSTALL_TIMEOUT_S = 900
MIN_FREE_BYTES = 512 * 1024 * 1024
WAIT_POLL_S = 5.0

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_DISK_FULL_RE = re.compile(r"ENOSPC|no space left on device", re.I)
_NETWORK_RE = re.compile(
    r"ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|ECONNRESET|E403|E407|EPROTO|"
    r"network|proxy|certificate|SELF_SIGNED|UNABLE_TO_GET_ISSUER|getaddrinfo|"
    r"resolve host|Failed to connect|curl: \(\d+\)", re.I)
_NODE_MISSING_RE = re.compile(r"node: No such file|env: node|node: not found", re.I)

Version = Tuple[int, int, int]
Runner = Callable[..., "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class RuntimeSpec:
    name: str
    binary: str
    package: str
    min_version: Version
    tested_major: int
    required_flags: Tuple[str, ...]
    known_bad: Tuple[Version, ...] = ()
    manual_install: str = ""


RUNTIMES: Dict[str, RuntimeSpec] = {
    "claude": RuntimeSpec(
        name="claude", binary="claude", package="@anthropic-ai/claude-code",
        min_version=(2, 0, 0), tested_major=2,
        required_flags=("--name", "--dangerously-skip-permissions", "--add-dir",
                        "--settings", "--remote-control", "--chrome", "--plugin-dir",
                        "--resume", "--session-id"),
        manual_install="curl -fsSL https://claude.ai/install.sh | bash",
    ),
    "codex": RuntimeSpec(
        name="codex", binary="codex", package="@openai/codex",
        min_version=(0, 40, 0), tested_major=0,
        required_flags=("--cd", "--add-dir", "--sandbox", "--ask-for-approval",
                        "--search", "--no-alt-screen", "--config", "--model"),
        manual_install="npm install -g --prefix ~/.local @openai/codex",
    ),
}


@dataclass
class Candidate:
    path: str
    source: str
    status: str = MISSING
    version: Optional[str] = None
    missing_flags: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status in USABLE


@dataclass
class Report:
    runtime: str
    candidates: List[Candidate]
    selected: Optional[Candidate]
    interrupted: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.selected is not None:
            return self.selected.status
        return self.candidates[0].status if self.candidates else MISSING

    @property
    def problem(self) -> Optional[Candidate]:
        """The candidate whose failure the user should fix first."""
        if self.selected is not None or not self.candidates:
            return None
        return self.candidates[0]

    def to_dict(self) -> dict:
        return {"runtime": self.runtime, "status": self.status,
                "selected": asdict(self.selected) if self.selected else None,
                "candidates": [asdict(c) for c in self.candidates],
                "interrupted_updates": list(self.interrupted)}


@dataclass
class RepairStep:
    status: str
    reason: str
    command: str
    consent_required: bool
    touches: str
    verify: str
    assisted: Optional[str] = None
    assisted_touches: str = ""


@dataclass
class RepairResult:
    ok: bool
    reason: str
    detail: str = ""
    user_action: str = ""
    attempts: int = 0
    path: Optional[str] = None


def parse_version(text: str) -> Optional[Version]:
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _fmt(v: Version) -> str:
    return ".".join(str(p) for p in v)


def _run(argv: Sequence[str], *, env: Optional[dict] = None, timeout: float = PROBE_TIMEOUT_S,
         cwd: Optional[str] = None) -> "subprocess.CompletedProcess":
    return subprocess.run(list(argv), env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def _tail(text: str) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else "no output"


def managed_root(env: Optional[dict] = None) -> Path:
    """Sutando-owned install root; never a user or global prefix."""
    env = os.environ if env is None else env
    if env.get("SUTANDO_RUNTIMES_DIR"):
        return Path(env["SUTANDO_RUNTIMES_DIR"])
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "sutando" / "runtimes"


SYSTEM_LOCATIONS: Dict[str, Tuple[str, ...]] = {
    "claude": ("/opt/homebrew/bin/claude", "/usr/local/bin/claude"),
    "codex": ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"),
}


def known_locations(spec: RuntimeSpec, env: dict) -> List[str]:
    home = Path(env.get("HOME") or str(Path.home()))
    if spec.name == "claude":
        dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".npm-global" / "bin"]
    else:
        dirs = [Path(env.get("CODEX_INSTALL_BIN") or str(home / ".npm-global" / "bin")),
                home / ".local" / "bin"]
    if "SUTANDO_RUNTIME_SYSTEM_DIRS" in env:
        system = [os.path.join(d, spec.binary)
                  for d in env["SUTANDO_RUNTIME_SYSTEM_DIRS"].split(os.pathsep) if d]
    else:
        system = list(SYSTEM_LOCATIONS.get(spec.name, ()))
    return [str(d / spec.binary) for d in dirs] + system


def candidate_paths(spec: RuntimeSpec, env: dict, root: Path) -> List[Tuple[str, str]]:
    """Every distinct install, in selection order: PATH, managed, then known locations."""
    out: List[Tuple[str, str]] = []
    seen = set()

    def add(path: str, source: str) -> None:
        if not os.path.lexists(path) or os.path.isdir(path):
            return
        key = os.path.realpath(path)
        if key in seen:
            return
        seen.add(key)
        out.append((path, source))

    for d in (env.get("PATH") or "").split(os.pathsep):
        if d:
            add(os.path.join(d, spec.binary), "PATH")
    add(str(root / "bin" / spec.binary), "managed")
    for p in known_locations(spec, env):
        add(p, "known")
    return out


def probe(path: str, spec: RuntimeSpec, *, source: str = "PATH", env: Optional[dict] = None,
          run: Runner = _run) -> Candidate:
    """Launch one binary and classify it. Never raises."""
    c = Candidate(path=path, source=source)
    if not os.path.exists(path):
        c.status, c.detail = BROKEN, "points to a missing file (an update may have been interrupted)"
        return c
    if not os.access(path, os.X_OK):
        c.status, c.detail = BROKEN, "is not executable"
        return c
    try:
        r = run([path, "--version"], env=env)
    except subprocess.TimeoutExpired:
        c.status, c.detail = BROKEN, f"--version did not answer within {PROBE_TIMEOUT_S}s"
        return c
    except OSError as exc:
        c.status, c.detail = BROKEN, f"could not be launched ({exc.strerror or exc})"
        return c
    if r.returncode != 0:
        c.status = BROKEN
        c.detail = f"--version exited {r.returncode}: {_tail(r.stderr or r.stdout)}"
        return c
    version = parse_version(r.stdout or r.stderr)
    if version is None:
        c.status, c.detail = BROKEN, f"--version printed no version: {_tail(r.stdout)}"
        return c
    c.version = _fmt(version)
    if version in spec.known_bad:
        c.status, c.detail = KNOWN_BAD, f"{c.version} is known to break the core"
        return c
    if version < spec.min_version:
        c.status = TOO_OLD
        c.detail = f"{c.version} is older than the minimum {_fmt(spec.min_version)}"
        return c
    try:
        h = run([path, "--help"], env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        c.status, c.detail = BROKEN, f"--help could not run ({exc})"
        return c
    text = (h.stdout or "") + (h.stderr or "")
    c.missing_flags = [f for f in spec.required_flags if not re.search(re.escape(f) + r"\b", text)]
    if h.returncode != 0 or c.missing_flags:
        c.status = INCOMPATIBLE
        c.detail = (f"{c.version} lacks {', '.join(c.missing_flags)}" if c.missing_flags
                    else f"--help exited {h.returncode}")
        return c
    if version[0] > spec.tested_major:
        c.status = NEWER_UNTESTED
        c.detail = f"{c.version} is newer than the tested {spec.tested_major}.x"
        return c
    c.status, c.detail = OK, c.version
    return c


def interrupted_updates(root: Path, runtime: str, pid_alive: Callable[[int], bool]) -> List[str]:
    """Staging dirs a killed repair left behind; their owner is no longer running."""
    base = root / runtime
    if not base.is_dir():
        return []
    out = []
    for d in sorted(base.glob(".staging-*")):
        parts = d.name.split("-")
        pid = int(parts[1]) if len(parts) > 2 and parts[1].isdigit() else 0
        if not pid or not pid_alive(pid):
            out.append(str(d))
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover(runtime: str, *, env: Optional[dict] = None, run: Runner = _run,
             root: Optional[Path] = None, spec: Optional[RuntimeSpec] = None,
             pid_alive: Callable[[int], bool] = _pid_alive) -> Report:
    env = dict(os.environ if env is None else env)
    spec = spec or RUNTIMES[runtime]
    root = root or managed_root(env)
    cands: List[Candidate] = []
    for path, source in candidate_paths(spec, env, root):
        # Known locations stand in only when no PATH or managed install works.
        if source == "known" and any(c.usable for c in cands):
            break
        cands.append(probe(path, spec, source=source, env=env, run=run))
    selected = next((c for c in cands if c.usable), None)
    return Report(runtime, cands, selected, interrupted_updates(root, runtime, pid_alive))


def _npm_prefix_of(path: str, spec: RuntimeSpec) -> Optional[str]:
    bin_dir = os.path.dirname(path)
    prefix = os.path.dirname(bin_dir)
    if os.path.basename(bin_dir) == "bin" and os.path.isdir(
            os.path.join(prefix, "lib", "node_modules", spec.package)):
        return prefix
    return None


def _assisted_cmd(runtime: str) -> str:
    return f"python3 src/runtime_compat.py repair --runtime {runtime} --yes"


CLAUDE_NATIVE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash -s stable"
CODEX_STANDALONE_INSTALL = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"


@dataclass(frozen=True)
class Installer:
    """One way to get a compatible CLI onto this host, and exactly what it changes."""
    kind: str
    argv: Tuple[str, ...]
    touches: str
    where: str


def choose_installer(spec: RuntimeSpec, env: dict, root: Path) -> Optional[Installer]:
    """npm into the managed root when npm exists; else the vendor's own Node-free path."""
    path = env.get("PATH")
    npm = shutil.which("npm", path=path)
    if npm:
        return Installer("npm", (npm,), f"only {root} (a Sutando-owned folder) and the npm cache",
                         str(root))
    home = Path(env.get("HOME") or str(Path.home()))
    if spec.name == "claude" and shutil.which("curl", path=path) and shutil.which("bash", path=path):
        bash = shutil.which("bash", path=path)
        return Installer("native", (bash, "-c", f"set -o pipefail; {CLAUDE_NATIVE_INSTALL}"),
                         "the official Claude Code installer's own folders, "
                         f"{home / '.local' / 'bin' / 'claude'} and {home / '.local' / 'share' / 'claude'}, "
                         f"plus one link in {root}", str(home / ".local" / "bin" / spec.binary))
    brew = shutil.which("brew", path=path)
    if spec.name == "codex" and brew:
        return Installer("brew", (brew, "install", "--cask", "codex"),
                         f"Homebrew's codex cask under {os.path.dirname(os.path.dirname(brew))}, "
                         f"plus one link in {root}", os.path.join(os.path.dirname(brew), "codex"))
    return None


def _manual_without_installer(spec: RuntimeSpec) -> str:
    if spec.name == "claude":
        return f"Install curl, then run: {CLAUDE_NATIVE_INSTALL}"
    return (f"Run: {CODEX_STANDALONE_INSTALL}  (or install Homebrew and run: brew install --cask codex, "
            "or install Node.js 22+ and run: npm install -g @openai/codex)")


def plan(report: Report, spec: Optional[RuntimeSpec] = None,
         root: Optional[Path] = None, env: Optional[dict] = None) -> RepairStep:
    """The safe next action for this report's state."""
    spec = spec or RUNTIMES[report.runtime]
    env = dict(os.environ if env is None else env)
    root = root or managed_root(env)
    verify = f"python3 src/runtime_compat.py doctor --runtime {report.runtime}"
    if report.selected is not None and report.selected.status == NEWER_UNTESTED:
        return RepairStep(NEWER_UNTESTED,
                          f"{spec.binary} {report.selected.detail}; the core will try it.",
                          "No action needed. If the core misbehaves, install the tested "
                          f"version: npm install -g --prefix ~/.local {spec.package}@{spec.tested_major}",
                          False, "nothing", verify)
    if report.selected is not None:
        return RepairStep(OK, f"{spec.binary} {report.selected.version} at "
                          f"{report.selected.path} is compatible.", "No action needed.",
                          False, "nothing", verify)
    installer = choose_installer(spec, env, root)
    assisted = _assisted_cmd(report.runtime) if installer else None
    assisted_touches = installer.touches if installer else ""
    bad = report.problem
    if bad is None:
        command = spec.manual_install if installer else _manual_without_installer(spec)
        return RepairStep(MISSING, f"{spec.binary} is not installed.", command, True,
                          f"the install location the {spec.binary} installer chooses",
                          verify, assisted, assisted_touches)
    where = f"{spec.binary} at {bad.path} {bad.detail}."
    if bad.status == BROKEN and _NODE_MISSING_RE.search(bad.detail):
        return RepairStep(BROKEN, where + " Its Node.js runtime was not found.",
                          "Install Node.js 22 or newer (brew install node), or reinstall "
                          "the desktop app, which bundles it.", True,
                          "your Node.js install", verify)
    if bad.status == BROKEN:
        command = spec.manual_install if installer else _manual_without_installer(spec)
        return RepairStep(BROKEN, where + " Sutando will not use it.", command, True,
                          f"the install at {bad.path}", verify, assisted, assisted_touches)
    prefix = _npm_prefix_of(bad.path, spec)
    if prefix:
        command = f"npm install -g --prefix {shlex.quote(prefix)} {spec.package}@{spec.tested_major}"
    elif spec.name == "claude":
        command = f"{shlex.quote(bad.path)} update"
    elif installer:
        command = f"{spec.manual_install}@{spec.tested_major}"
    else:
        command = _manual_without_installer(spec)
    return RepairStep(bad.status, where, command, True, f"the install at {bad.path}",
                      verify, assisted, assisted_touches)


def _existing_ancestor(path: Path) -> Path:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _classify_failure(text: str) -> str:
    if _DISK_FULL_RE.search(text):
        return "disk-full"
    if _NETWORK_RE.search(text):
        return "blocked-download"
    return "install-failed"


def _swap_link(link: Path, target: Path) -> None:
    """Point `link` at `target` atomically; a crash leaves the old link intact."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))


def assisted_install(runtime: str, *, env: Optional[dict] = None, run: Runner = _run,
                     root: Optional[Path] = None, spec: Optional[RuntimeSpec] = None,
                     attempts: int = 3, backoff_s: float = 2.0,
                     sleep: Callable[[float], None] = time.sleep,
                     disk_usage: Callable[[str], "shutil._ntuple_diskusage"] = shutil.disk_usage,
                     pid_alive: Callable[[int], bool] = _pid_alive,
                     min_free: int = MIN_FREE_BYTES, now: Callable[[], float] = time.time,
                     log: Callable[[str], None] = lambda _m: None) -> RepairResult:
    """Install a compatible CLI. Callers hold the user's consent. Concurrent repairs of one
    runtime run one at a time; a waiter that finds a working result reuses it."""
    env = dict(os.environ if env is None else env)
    spec = spec or RUNTIMES[runtime]
    root = root or managed_root(env)
    retry = f"Then retry: {_assisted_cmd(runtime)}"
    installer = choose_installer(spec, env, root)
    if installer is None:
        return RepairResult(False, "no-installer", "neither npm nor the vendor installer can run here",
                            _manual_without_installer(spec))
    free = disk_usage(str(_existing_ancestor(root))).free
    if free < min_free:
        need = (min_free - free) // (1024 * 1024) + 1
        return RepairResult(False, "disk-full", f"{free // (1024 * 1024)} MB free under {root}",
                            f"Free at least {need} MB on that disk. {retry}")
    base = root / runtime
    base.mkdir(parents=True, exist_ok=True)
    link = root / "bin" / spec.binary
    before = os.readlink(str(link)) if os.path.islink(str(link)) else None
    from file_lock import locked_file
    with locked_file(base / ".lock"):
        for stale in interrupted_updates(root, runtime, pid_alive):
            log(f"removing an interrupted update at {stale}; the previous version was kept")
            shutil.rmtree(stale, ignore_errors=True)
        now_points = os.readlink(str(link)) if os.path.islink(str(link)) else None
        if now_points is not None and now_points != before:
            ready = probe(str(link), spec, source="managed", env=env, run=run)
            if ready.usable:
                log(f"another repair already installed {spec.binary} {ready.version}; using it")
                return RepairResult(True, "installed", f"{spec.binary} {ready.version} at {link}",
                                    "", 0, str(link))
        ctx = _Ctx(spec, base, root, env, run, attempts, backoff_s, sleep, now, log, retry, installer)
        if installer.kind == "npm":
            return _install_npm(ctx)
        return _install_external(ctx)


@dataclass
class _Ctx:
    spec: RuntimeSpec
    base: Path
    root: Path
    env: dict
    run: Runner
    attempts: int
    backoff_s: float
    sleep: Callable[[float], None]
    now: Callable[[], float]
    log: Callable[[str], None]
    retry: str
    installer: Installer


def _run_with_retry(ctx: _Ctx, argv: Sequence[str], env: dict, before=None):
    """(None, attempts) on success, else (failed RepairResult, attempts)."""
    for n in range(1, ctx.attempts + 1):
        if before:
            before()
        try:
            r = ctx.run(list(argv), env=env, timeout=INSTALL_TIMEOUT_S)
            rc, text = r.returncode, (r.stderr or "") + (r.stdout or "")
        except subprocess.TimeoutExpired:
            rc, text = -1, "the install timed out"
        except OSError as exc:
            rc, text = -1, f"the installer could not run: {exc}"
        if rc == 0:
            return None, n
        reason, detail = _classify_failure(text), _tail(text)
        ctx.log(f"attempt {n}/{ctx.attempts} failed ({reason}): {detail}")
        if reason == "disk-full" or n == ctx.attempts:
            return _failed(reason, detail, n, ctx.root, ctx.retry, ctx.installer), n
        ctx.sleep(ctx.backoff_s * (2 ** (n - 1)))
    raise AssertionError("unreachable")  # pragma: no cover


def _install_npm(ctx: _Ctx) -> RepairResult:
    spec, base, env = ctx.spec, ctx.base, ctx.env
    staging = base / f".staging-{os.getpid()}-{int(ctx.now())}"
    npm_env = dict(env, npm_config_update_notifier="false", npm_config_fund="false",
                   npm_config_audit="false")
    argv = [ctx.installer.argv[0], "install", "-g", "--prefix", str(staging),
            f"{spec.package}@{spec.tested_major}"]
    aside = None
    try:
        failed, n = _run_with_retry(ctx, argv, npm_env,
                                    before=lambda: shutil.rmtree(staging, ignore_errors=True))
        if failed:
            return failed
        staged = probe(str(staging / "bin" / spec.binary), spec, source="staged", env=env, run=ctx.run)
        if not staged.usable:
            return RepairResult(False, "verify-failed", f"the new {spec.binary} {staged.detail}",
                                f"The previous version was kept. Install it yourself: "
                                f"{spec.manual_install}", n)
        final = base / "versions" / staged.version
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists() and probe(str(final / "bin" / spec.binary), spec, env=env, run=ctx.run).usable:
            ctx.log(f"{spec.binary} {staged.version} is already installed and works; reusing it")
        else:
            if final.exists():
                aside = final.with_name(f".replaced-{staged.version}-{os.getpid()}-{int(ctx.now())}")
                os.rename(str(final), str(aside))
            os.rename(str(staging), str(final))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if aside is not None:
            shutil.rmtree(aside, ignore_errors=True)
    return _link_and_verify(ctx, final / "bin" / spec.binary, n)


def _install_external(ctx: _Ctx) -> RepairResult:
    """The vendor's own installer, which picks its own folder; Sutando then links to it."""
    failed, n = _run_with_retry(ctx, ctx.installer.argv, ctx.env)
    if failed:
        return failed
    target = Path(ctx.installer.where)
    got = probe(str(target), ctx.spec, source="installed", env=ctx.env, run=ctx.run)
    if not got.usable:
        return RepairResult(False, "verify-failed", f"the installed {ctx.spec.binary} {got.detail}",
                            f"Install it yourself: {_manual_without_installer(ctx.spec)}", n)
    return _link_and_verify(ctx, target, n)


def _link_and_verify(ctx: _Ctx, target: Path, n: int) -> RepairResult:
    """Switch the managed link, launch through it, and switch back if it does not run."""
    spec = ctx.spec
    link = ctx.root / "bin" / spec.binary
    previous = os.readlink(str(link)) if os.path.islink(str(link)) else None
    _swap_link(link, target)
    live = probe(str(link), spec, source="managed", env=ctx.env, run=ctx.run)
    if not live.usable:
        if previous is not None:
            _swap_link(link, Path(previous))
        else:
            os.unlink(str(link))
        return RepairResult(False, "verify-failed",
                            f"the installed {spec.binary} {live.detail}; the previous version was restored",
                            f"Install it yourself: {spec.manual_install}", n)
    return RepairResult(True, "installed", f"{spec.binary} {live.version} at {link}", "", n,
                        str(link))


def _failed(reason: str, detail: str, n: int, root: Path, retry: str,
            installer: Optional[Installer] = None) -> RepairResult:
    source = "registry.npmjs.org" if installer is None or installer.kind == "npm" else "the vendor's download site"
    actions = {
        "disk-full": f"Free at least {MIN_FREE_BYTES // (1024 * 1024)} MB on the disk "
                     f"holding {root}. {retry}",
        "blocked-download": f"Check your network, proxy or firewall; the installer must reach "
                            f"{source}. {retry}",
        "install-failed": f"Read the installer error above. {retry}",
    }
    return RepairResult(False, reason, detail, actions[reason], n)


def auth_status(runtime: str, binary: str, *, env: Optional[dict] = None, run: Runner = _run,
                claude_config_dir: Optional[str] = None,
                keychain_check: Optional[Callable[[], bool]] = None) -> dict:
    """{login: ok|logged-out|unknown, remedy}. Claude's verdict is auth_preflight's."""
    env = dict(os.environ if env is None else env)
    if runtime == "claude":
        if not claude_config_dir:
            return {"login": "unknown", "remedy": "", "detail": "no config dir to check"}
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import auth_preflight
        kwargs = {"env": env}
        if keychain_check is not None:
            kwargs["keychain_check"] = keychain_check
        r = auth_preflight.check_auth_state(claude_config_dir, **kwargs)
        if r["verdict"] == "ok":
            return {"login": "ok", "remedy": "", "detail": "logged in"}
        return {"login": "logged-out", "remedy": r["remedy"], "detail": "; ".join(r["reasons"])}
    home = env.get("CODEX_HOME")
    login = (f"CODEX_HOME={shlex.quote(home)} " if home else "") + f"{shlex.quote(binary)} login"
    try:
        r = run([binary, "login", "status"], env=env)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"login": "unknown", "remedy": login, "detail": f"login status failed: {exc}"}
    if r.returncode == 0:
        return {"login": "ok", "remedy": "", "detail": "logged in"}
    return {"login": "logged-out", "remedy": f"run: {login}",
            "detail": f"Codex CLI is not authenticated for {home or 'the default Codex home'}"}


def _say(msg: str) -> None:
    print(f"runtime-compat: {msg}", file=sys.stderr)


def _ask_consent(question: str) -> bool:  # pragma: no cover - needs a real terminal
    if not sys.stdin.isatty():
        return False
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"{question} [y/N] ")
            tty.flush()
            return tty.readline().strip().lower() in ("y", "yes")
    except OSError:
        return False


RETRY_PHRASE = "retry now"


def install_phrase(runtime: str) -> str:
    return f"install compatible {RUNTIMES[runtime].binary}"


class Incident:
    """One owner DM per incident: a fixed name, persisted, queued once, closed on success."""

    def __init__(self, workspace: Path, runtime: str, clock: Callable[[], float] = time.time):
        self.workspace, self.runtime, self.clock = Path(workspace), runtime, clock
        self.path = self.workspace / "state" / f"runtime-compat-{runtime}.json"
        try:
            self.rec = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.rec = {}

    @property
    def open(self) -> bool:
        return self.rec.get("status") == "open"

    @property
    def actions(self) -> List[str]:
        return list(self.rec.get("actions") or [])

    def _save(self) -> None:
        from local_record import write_whole
        write_whole(self.path, self.rec)

    def notify(self, body: str) -> bool:
        """Queue the incident's DM if it is not queued yet. True when this call queued it."""
        from owner_reply import queue_notice
        if not self.open:
            t = int(self.clock())
            self.rec = {"status": "open", "opened_at": t, "actions": [], "notice_queued": False,
                        "notice": f"proactive-runtime-compat-{self.runtime}-{t}.txt"}
            self._save()
        if self.rec.get("notice_queued") or not queue_notice(self.workspace, self.rec["notice"], body):
            return False
        self.rec["notice_queued"] = True
        self._save()
        return True

    def take_replies(self) -> List[Tuple[Path, str]]:
        """Owner replies to this incident not yet acted on; each is recorded before it runs."""
        from owner_reply import matching_replies
        if not self.open:
            return []
        got = list(matching_replies(self.workspace, (install_phrase(self.runtime), RETRY_PHRASE),
                                    skip=self.actions))
        if got:
            self.rec["actions"] = self.actions + [p.stem for p, _ in got]
            self._save()
        return got

    def close(self) -> None:
        if self.open:
            self.rec.update(status="resolved", resolved_at=int(self.clock()))
            self._save()


def notice_body(runtime: str, step: RepairStep) -> str:
    binary = RUNTIMES[runtime].binary
    lines = [f"Your Sutando core could not start: {step.reason}", ""]
    if step.assisted:
        lines += [f'Reply "{install_phrase(runtime)}" and Sutando installs a compatible {binary}. '
                  f"It changes only {step.assisted_touches}, never your settings. "
                  "The core then starts by itself.", ""]
    lines += [f"Or fix it yourself: {step.command}",
              f'then reply "{RETRY_PHRASE}" and the core starts.']
    return "\n".join(lines) + "\n"


def login_notice_body(runtime: str, auth: dict) -> str:
    return (f"Your Sutando core could not start: {RUNTIMES[runtime].binary} is not logged in.\n\n"
            f"Log in: {auth['remedy']}\nthen reply \"{RETRY_PHRASE}\" and the core starts.\n")


def ensure(runtime: str, *, env: Optional[dict] = None, wait_s: float = 0.0,
           repair: bool = False, consent: Optional[Callable[[str], bool]] = None,
           enforce_auth: bool = False, claude_config_dir: Optional[str] = None,
           resume_hint: str = "", workspace: Optional[Path] = None, notify_owner: bool = False,
           say: Callable[[str], None] = _say,
           discover_fn: Callable[..., Report] = discover,
           repair_fn: Callable[..., RepairResult] = assisted_install,
           auth_fn: Callable[..., dict] = auth_status,
           sleep: Callable[[float], None] = time.sleep,
           now: Callable[[], float] = time.monotonic,
           clock: Callable[[], float] = time.time) -> Tuple[int, Optional[str]]:
    """Make the core CLI usable, then hand back its path. An install changes only with
    `consent`, or the owner's "install compatible <cli>" reply to this incident's DM."""
    env = dict(os.environ if env is None else env)
    inc = Incident(workspace, runtime, clock) if workspace else None
    acted: List[Tuple[Path, str]] = []
    outcome = {"rc": EXIT_UNUSABLE, "repair": None, "why": ""}
    try:
        rc, path = _ensure(runtime, env, wait_s, repair, consent, enforce_auth, claude_config_dir,
                           resume_hint, inc, notify_owner, acted, outcome, say, discover_fn,
                           repair_fn, auth_fn, sleep, now)
        outcome["rc"] = rc
        if rc == EXIT_OK and inc is not None:
            inc.close()
        return rc, path
    finally:
        for task, phrase in acted:
            _answer_reply(workspace, task, phrase, runtime, outcome)


def _answer_reply(workspace, task, phrase, runtime, outcome) -> None:
    from owner_reply import answer
    repaired = outcome["repair"]
    if outcome["rc"] == EXIT_OK:
        text = (f"Installed {repaired.detail} in Sutando's own folder. The core is starting now."
                if phrase != RETRY_PHRASE and repaired is not None and repaired.ok
                else f"{RUNTIMES[runtime].binary} works now. The core is starting.")
    elif repaired is not None and not repaired.ok:
        text = f"That did not fix it: {repaired.detail}. Next: {repaired.user_action}"
    else:
        text = f"The core still cannot start: {outcome['why']}"
    answer(workspace, task, text)


def _ensure(runtime, env, wait_s, repair, consent, enforce_auth, claude_config_dir, resume_hint,
            inc, notify_owner, acted, outcome, say, discover_fn, repair_fn, auth_fn, sleep, now):
    spec = RUNTIMES[runtime]
    again = f" Then run again: {resume_hint}" if resume_hint else ""

    def replies() -> set:
        got = inc.take_replies() if inc is not None else []
        acted.extend(got)
        return {phrase for _, phrase in got}

    def repair_now() -> None:
        result = repair_fn(runtime, env=env, log=say)
        outcome["repair"] = result
        say(f"repair {'succeeded' if result.ok else 'failed'}: {result.detail}")
        if not result.ok:
            say(f"next: {result.user_action}")

    owner_said = replies()
    report = discover_fn(runtime, env=env)
    for stale in report.interrupted:
        say(f"an interrupted update left {stale}; the previous version is still in use.")
    if report.selected is None:
        step = plan(report, env=env)
        outcome["why"] = step.reason
        say(f"cannot start the core: {step.reason}")
        say(f"fix: {step.command}")
        granted = install_phrase(runtime) in owner_said
        if step.assisted and not granted and repair and consent is not None:
            granted = consent(f"Install a compatible {spec.binary}? This changes "
                              f"{step.assisted_touches}. Nothing else.")
        if step.assisted and granted:
            repair_now()
            report = discover_fn(runtime, env=env)
        elif step.assisted:
            say(f"or let Sutando install one for you: {step.assisted}")
        if report.selected is None and notify_owner and inc is not None:
            if inc.notify(notice_body(runtime, step)):
                say("asked the owner in their DM how to fix it.")

        def recheck() -> Report:
            said = replies()
            r = discover_fn(runtime, env=env)
            if r.selected is None and install_phrase(runtime) in said and plan(r, env=env).assisted:
                repair_now()
                r = discover_fn(runtime, env=env)
            return r
        report = _wait(report, wait_s, recheck, lambda r: r.selected is not None, say, sleep, now,
                       f"waiting up to {int(wait_s)}s for the fix; the launch continues on its own.")
        if report.selected is None:
            say(f"the core was not started.{again}")
            return EXIT_UNUSABLE, None
    sel = report.selected
    if sel.status == NEWER_UNTESTED:
        say(plan(report, env=env).reason)
    say(f"using {sel.path} ({sel.version})")
    auth = auth_fn(runtime, sel.path, env=env, claude_config_dir=claude_config_dir)
    if auth["login"] == "logged-out":
        outcome["why"] = f"{spec.binary} is not logged in"
        say(f"{spec.binary} is not logged in: {auth['detail']}")
        say(f"fix: {auth['remedy']}")
        if enforce_auth and notify_owner and inc is not None and inc.notify(login_notice_body(runtime, auth)):
            say("asked the owner in their DM to log in.")
        holder = {"auth": auth}

        def recheck_auth() -> dict:
            replies()
            holder["auth"] = auth_fn(runtime, sel.path, env=env, claude_config_dir=claude_config_dir)
            return holder["auth"]
        _wait(auth, wait_s, recheck_auth, lambda a: a["login"] != "logged-out", say, sleep, now,
              f"waiting up to {int(wait_s)}s for the login; the launch continues on its own.")
        if holder["auth"]["login"] == "logged-out" and enforce_auth:
            say(f"{spec.binary} is not authenticated; the core was not started.{again}")
            return EXIT_LOGIN, None
    return EXIT_OK, sel.path


def _wait(value, wait_s, recheck, done, say, sleep, now, banner):
    if done(value) or wait_s <= 0:
        return value
    say(banner)
    deadline = now() + wait_s
    while now() < deadline:
        sleep(min(WAIT_POLL_S, max(0.0, deadline - now())))
        value = recheck()
        if done(value):
            say("fixed; continuing the launch.")
            return value
    return value


def _doctor_lines(report: Report, auth: Optional[dict], env: Optional[dict] = None) -> List[str]:
    step = plan(report, env=env)
    lines = [f"{report.runtime}: {report.status}"]
    for c in report.candidates:
        mark = "*" if c is report.selected else " "
        lines.append(f"  {mark} [{c.status}] {c.path} ({c.source}) {c.detail}")
    for stale in report.interrupted:
        lines.append(f"  interrupted update left {stale}; the previous version is in use")
    lines.append(f"  reason: {step.reason}")
    lines.append(f"  fix: {step.command}")
    if step.assisted:
        lines.append(f"  assisted fix (asks first; changes {step.assisted_touches}): {step.assisted}")
    if auth and auth["login"] == "logged-out":
        lines.append(f"  login: {auth['remedy']}")
    return lines


def _workspace() -> Optional[Path]:
    """The workspace the owner's DM and replies live in; None when it cannot be resolved."""
    try:
        from workspace_default import resolve_workspace
        return Path(resolve_workspace())
    except Exception:  # noqa: BLE001 - the gate still works, only without the owner DM
        return None


def main(argv: Optional[Sequence[str]] = None, *, env: Optional[dict] = None,
         consent: Callable[[str], bool] = _ask_consent) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor")
    d.add_argument("--runtime", choices=("claude", "codex", "all"), default="all")
    d.add_argument("--json", action="store_true")
    e = sub.add_parser("ensure")
    e.add_argument("--runtime", choices=tuple(RUNTIMES), required=True)
    e.add_argument("--wait", type=float, default=0.0)
    e.add_argument("--repair", action="store_true")
    e.add_argument("--yes", action="store_true", help="consent to the assisted repair")
    e.add_argument("--enforce-auth", action="store_true")
    e.add_argument("--claude-config-dir", default="")
    e.add_argument("--resume-hint", default="")
    e.add_argument("--notify-owner", action="store_true",
                   help="off a terminal: DM the owner once and act on their reply")
    r = sub.add_parser("repair")
    r.add_argument("--runtime", choices=tuple(RUNTIMES), required=True)
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    env = dict(os.environ if env is None else env)
    if a.cmd == "doctor":
        names = list(RUNTIMES) if a.runtime == "all" else [a.runtime]
        reports = [discover(n, env=env) for n in names]
        if a.json:
            print(json.dumps([dict(r.to_dict(), plan=asdict(plan(r, env=env))) for r in reports], indent=2))
        else:
            for rep in reports:
                auth = (auth_status(rep.runtime, rep.selected.path, env=env,
                                    claude_config_dir=env.get("CLAUDE_CONFIG_DIR"))
                        if rep.selected else None)
                print("\n".join(_doctor_lines(rep, auth, env)))
        return 0 if all(rep.selected for rep in reports) else EXIT_UNUSABLE
    if a.cmd == "repair":
        if not a.yes:
            _say("the assisted repair changes files; pass --yes to consent.")
            return EXIT_USAGE
        result = assisted_install(a.runtime, env=env, log=_say)
        _say(f"{result.reason}: {result.detail}")
        if not result.ok:
            _say(f"next: {result.user_action}")
        return 0 if result.ok else EXIT_UNUSABLE
    give = (lambda _q: True) if a.yes or env.get("SUTANDO_RUNTIME_REPAIR_CONSENT") == "1" else consent
    rc, path = ensure(a.runtime, env=env, wait_s=a.wait, repair=a.repair, consent=give,
                      enforce_auth=a.enforce_auth, claude_config_dir=a.claude_config_dir or None,
                      resume_hint=a.resume_hint, workspace=_workspace(),
                      notify_owner=a.notify_owner)
    if path:
        print(path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
