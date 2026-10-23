#!/usr/bin/env python3
"""Draw a picture and send it to an AG2 Space room in one call, and write the task's result.

One command for the whole errand, so the session that runs it makes one tool call instead of
four (generate, inspect, upload, publish) with a think between each: on 2026-10-22 the long-lane
worker spent 2 min 15 s on a Stanley-bottle picture, 13 s of it generating. Steps:

  1. skills/image-generation/scripts/generate.py --prompt ...        -> the file
  2. skills/ag2space-voice/scripts/upload-media.ts --room ... --post -> posted in the room
  3. results/<task id>.txt via src/result_publish.py                  -> the voice says it

Prints ONE JSON line: {"ok": true, "path", "name", "room", "event_id", "result"} or
{"ok": false, "step": "generate|upload|result", "error", "detail"}. On a failure nothing is
published: the caller decides whether to retry or to answer with the failure.

Run:
  python3 skills/image-generation/scripts/draw_and_send.py --prompt "a golden retriever" \
      --room '!room:ag2.space' --task-id task-1792729855955 [--name dog.jpg] [--say "..."]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GENERATE = REPO / "skills" / "image-generation" / "scripts" / "generate.py"
UPLOAD = REPO / "skills" / "ag2space-voice" / "scripts" / "upload-media.ts"
sys.path.insert(0, str(REPO / "src"))

TIMEOUT_S = 240


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def fail(step: str, error: str, detail: str) -> int:
    return emit({"ok": False, "step": step, "error": error, "detail": detail[:600]})


def run_json(argv: list, step: str) -> "tuple[dict | None, int]":
    """Run a command that prints one JSON line last; (parsed, exit code) or (None, code) when it did not."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, fail(step, "no_run", f"{argv[0]}: {e}")
    line = ""
    for ln in reversed(p.stdout.splitlines()):
        if ln.strip().startswith("{"):
            line = ln.strip()
            break
    if not line:
        return None, fail(step, "no_json", (p.stderr or p.stdout or f"exit {p.returncode}").strip())
    try:
        return json.loads(line), 0
    except ValueError:
        return None, fail(step, "bad_json", line)


def safe_name(prompt: str, path: Path) -> str:
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:4]
    stem = "-".join(words) or "picture"
    return f"{stem}{path.suffix.lower() or '.png'}"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="draw a picture, post it in a room, write the result")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--room", required=True, help="the AG2 Space room or DM id the picture goes to")
    ap.add_argument("--task-id", default="", help="write results/<task id>.txt when given")
    ap.add_argument("--name", default="", help="file name in the room (default: from the prompt)")
    ap.add_argument("--say", default="", help="the result's first line (default: 'Drew <prompt> and sent it to the room.')")
    ap.add_argument("--workspace", default="", help="override the workspace (default: the configured one)")
    a = ap.parse_args(argv)

    generate = Path(os.environ.get("DRAW_AND_SEND_GENERATE") or GENERATE)
    upload = Path(os.environ.get("DRAW_AND_SEND_UPLOAD") or UPLOAD)
    if not upload.is_file():
        return fail("upload", "no_upload_script",
                    f"{upload} is missing: the ag2space-voice upload script (sonichi/sutando#5773) is needed to post the picture")

    gen, rc = run_json([sys.executable, str(generate), "--prompt", a.prompt], "generate")
    if gen is None:
        return rc
    if not gen.get("ok") or not gen.get("path"):
        return fail("generate", str(gen.get("error") or "failed"), str(gen.get("message") or gen.get("remedy") or ""))
    path = Path(str(gen["path"]))
    name = a.name or safe_name(a.prompt, path)

    up_argv = ["npx", "tsx", str(upload), "--room", a.room, "--file", str(path), "--name", name, "--post"]
    if upload.suffix == ".py":  # a test double
        up_argv = [sys.executable, str(upload), "--room", a.room, "--file", str(path), "--name", name, "--post"]
    up, rc = run_json(up_argv, "upload")
    if up is None:
        return rc
    if not up.get("ok"):
        return fail("upload", str(up.get("error") or "failed"), str(up.get("detail") or ""))
    if not up.get("posted"):
        return fail("upload", "not_posted", "uploaded but not posted in the room; nothing said")

    say = a.say or f"Drew {a.prompt.strip().rstrip('.')} and sent it to the room."
    result_text = f"{say}\n\nPosted as {up.get('name') or name}. Local copy: {path}\n"
    result_path = None
    if a.task_id:
        from workspace_default import resolve_workspace  # noqa: E402  (src/ on sys.path above)
        import result_publish  # noqa: E402
        ws = Path(a.workspace) if a.workspace else Path(resolve_workspace())
        result_path = ws / "results" / f"{a.task_id}.txt"
        try:
            result_publish.publish_text(result_path, result_text)
        except OSError as e:
            return fail("result", "not_published", f"{result_path}: {e}")
    return emit({"ok": True, "path": str(path), "name": up.get("name") or name, "room": a.room,
                 "event_id": up.get("event_id"), "result": str(result_path) if result_path else None})


if __name__ == "__main__":
    sys.exit(main())
