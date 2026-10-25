#!/usr/bin/env python3
"""Launch hooks: the scripts installed skills declare with the manifest field `on_launch`, each
started once per core launch, detached, so no launch waits on or fails because of one.

Generic core helper: it names the field, never a skill; `src/skill_roots.py` resolves the
declarers across the sanctioned roots and `main()` (the launcher's edge, run by
src/agent/start-cli.sh) injects them into `start()`. The contract a hook relies on:

- run with no arguments: `<python> <script>` for a `.py`, `bash <script>` otherwise;
- in its own session, stdin from /dev/null, stdout and stderr appended to
  `<workspace>/logs/launch-hook-<skill>.log`;
- nobody waits for it, so it must finish on its own; one that cannot start is reported on stderr
  and the others still start.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

FIELD = "on_launch"


def start(hooks: Iterable[Tuple[str, Path]], log_dir: Path, python: str,
          env: Optional[dict] = None) -> List[Tuple[str, int]]:
    """Start each injected (skill name, script) detached; returns (name, pid) of those started."""
    started = []
    for name, script in hooks:
        argv = [python, str(script)] if Path(script).suffix == ".py" else ["bash", str(script)]
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / f"launch-hook-{name}.log", "ab") as log:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True, close_fds=True, env=env)
            started.append((name, proc.pid))
        except OSError as exc:
            print(f"launch-hooks: {name}: not started ({type(exc).__name__})", file=sys.stderr)
    return started


def main() -> int:
    """The edge: resolve the workspace and the declarers, start them, and always exit 0."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from skill_roots import declared_scripts, skill_roots
        from workspace_default import resolve_workspace
        workspace = resolve_workspace(migrate=False)
        hooks = declared_scripts(FIELD, skill_roots(workspace))
    except Exception as exc:  # noqa: BLE001 — a launch never fails on a hook
        print(f"launch-hooks: none started ({type(exc).__name__})", file=sys.stderr)
        return 0
    for name, pid in start(hooks, Path(workspace) / "logs", sys.executable):
        print(f"launch-hooks: started {name} (pid {pid})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
