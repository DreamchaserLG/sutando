#!/usr/bin/env python3
"""start-cli.sh loads the repo .env for policy switches but exports no credentials.

Every process the core session starts inherits its environment, so an exported
relay token lets any child poll the gateway as the real agent. The test runs the
launcher's own .env load block (cut from the script, so it tests what ships)
against a fake .env and reads back the exported environment.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "start-cli.sh"
LANES = REPO / "scripts" / "restart-gateway-lanes.sh"

FAKE_ENV = """\
SUTANDO_SELF_DEVELOPMENT_ENABLED=false
SUTANDO_CODEX_AUTO_RESET_ENABLED=true
AG2_AGENT_NAME="name with spaces 'and' quotes"
AG2_REMOTE_TOKEN="https://gw.invalid|prd-secret"
AG2_REMOTE_TOKEN_DEV="https://gw-dev.invalid|dev-secret"
AG2_REMOTE_TIER=owner
REMOTE_TASK_URL=https://gw.invalid
GEMINI_API_KEY=gemini-secret
TWILIO_AUTH_TOKEN=twilio-secret
STRIPE_SECRET_KEY=stripe-secret
ZOOM_PERSONAL_PASSCODE=123456
"""


def _load_block() -> str:
    lines = SCRIPT.read_text().splitlines()
    start = lines.index("unset _self_dir") + 1
    opener = lines.index('if [ -f "$REPO/.env" ]; then', start)
    end = lines.index("fi", opener)
    return "\n".join(lines[start:end + 1])


def _run(ambient: dict[str, str], dotenv: str = FAKE_ENV) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text(dotenv)
        script = f'set -euo pipefail\nREPO={tmp!r}\n{_load_block()}\nenv -0\n'
        env = {"PATH": "/usr/bin:/bin", **ambient}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, timeout=30)


def _exported(ambient: dict[str, str]) -> dict[str, str]:
    out = _run(ambient)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode())
    pairs = [p.split("=", 1) for p in out.stdout.decode().split("\0") if "=" in p]
    return {k: v for k, v in pairs}


class EnvLoadTest(unittest.TestCase):
    def test_policy_switches_are_exported(self):
        env = _exported({})
        self.assertEqual(env.get("SUTANDO_SELF_DEVELOPMENT_ENABLED"), "false")
        self.assertEqual(env.get("SUTANDO_CODEX_AUTO_RESET_ENABLED"), "true")
        self.assertEqual(env.get("AG2_AGENT_NAME"), "name with spaces 'and' quotes")

    def test_credentials_and_relay_identity_are_not_exported(self):
        env = _exported({})
        for name in ("AG2_REMOTE_TOKEN", "AG2_REMOTE_TOKEN_DEV", "AG2_REMOTE_TIER",
                     "REMOTE_TASK_URL", "GEMINI_API_KEY", "TWILIO_AUTH_TOKEN",
                     "STRIPE_SECRET_KEY", "ZOOM_PERSONAL_PASSCODE"):
            self.assertNotIn(name, env, name)
        self.assertFalse(any("secret" in v for v in env.values()))

    def test_ambient_values_are_kept(self):
        env = _exported({"SUTANDO_SELF_DEVELOPMENT_ENABLED": "",
                         "AG2_REMOTE_TOKEN": "ambient-value"})
        self.assertEqual(env.get("SUTANDO_SELF_DEVELOPMENT_ENABLED"), "")
        self.assertEqual(env.get("AG2_REMOTE_TOKEN"), "ambient-value")

    def test_a_broken_env_file_still_stops_the_launcher(self):
        out = _run({}, "SUTANDO_X=1\nfalse\n")
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn(b"SUTANDO_X=1", out.stdout)


class GatewayLanesLoadTest(unittest.TestCase):
    """restart-gateway-lanes.sh runs from the core session, which no longer carries the
    relay token, so it must read the repo .env itself before starting the lanes."""

    def test_lanes_script_sees_the_repo_env_token_without_inheriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "src").mkdir()
            shutil.copy(LANES, root / "scripts" / LANES.name)
            (root / "scripts" / "python-binary.sh").write_text(
                'resolve_python() { echo /usr/bin/true; }\n')
            (root / "scripts" / "sutando-config.sh").write_text(f'echo {str(root / "ws")!r}\n')
            (root / "src" / "startup-runtime.sh").write_text(
                'start_gateway_lanes() { [ -n "${AG2_REMOTE_TOKEN:-}" ] && echo LANE_TOKEN_SET'
                ' || echo LANE_TOKEN_MISSING; }\n')
            (root / ".env").write_text('AG2_REMOTE_TOKEN="https://gw.invalid|prd-secret"\n')
            out = subprocess.run(["bash", str(root / "scripts" / LANES.name)],
                                 env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                                 text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("LANE_TOKEN_SET", out.stdout)


if __name__ == "__main__":
    unittest.main()
