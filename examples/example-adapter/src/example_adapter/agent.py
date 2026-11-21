"""A fake coding agent for the example adapter: no model, no account, predictable output.

It stands in for a real agent CLI so the adapter around it can show every part of the adapter
interface: starting a process, reading its events, resuming a session, and stopping it.

Usage::

    python -m example_adapter.agent --marker MARKER [--resume SESSION_ID] [--read-only]

It reads the instructions from stdin, works in the current directory, and prints one JSON object
per line on stdout::

    {"type": "session", "id": "..."}
    {"type": "message", "text": "..."}
    {"type": "command", "id": "...", "command": "ls", "status": "started"}
    {"type": "command", "id": "...", "command": "ls", "status": "finished", "output": "...",
     "exit_code": 0}
    {"type": "file", "id": "...", "path": "...", "status": "started"}
    {"type": "file", "id": "...", "path": "...", "status": "finished"}
    {"type": "done", "input_tokens": 12, "output_tokens": 34}

On SIGINT it prints {"type": "interrupted"} and exits with 130, as many agent CLIs do.

`--marker` is ignored; it is there so the adapter's process marker is on the command line, where
agent-supervisor looks for leftover processes after a restart. The pause between steps comes from
the EXAMPLE_AGENT_STEP_SECONDS environment variable (default 1), so a run lasts long enough to be
watched and stopped.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

NOTES_FILE = "EXAMPLE_AGENT_NOTES.md"
SESSIONS_DIR = Path(tempfile.gettempdir()) / "example-agent-sessions"


def main() -> int:
    """Run one turn of the fake agent."""
    arguments = _parse_arguments()
    try:
        _run_turn(arguments)
    except KeyboardInterrupt:
        _emit({"type": "interrupted"})
        return 130
    return 0


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="example-agent")
    parser.add_argument("--marker", required=True)
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--read-only", action="store_true")
    return parser.parse_args()


def _run_turn(arguments: argparse.Namespace) -> None:
    prompt = sys.stdin.read()
    session_id = arguments.resume or uuid.uuid4().hex
    turn = _next_turn(session_id)
    _emit({"type": "session", "id": session_id})

    _step()
    first_line = (prompt.strip().splitlines() or ["(empty)"])[0][:100]
    _emit({"type": "message", "text": f"Turn {turn} of this session. Asked: {first_line}"})

    _step()
    command_id = uuid.uuid4().hex
    _emit({"type": "command", "id": command_id, "command": "ls", "status": "started"})
    listing = "\n".join(sorted(name for name in os.listdir(".") if name != ".git"))
    _emit(
        {
            "type": "command",
            "id": command_id,
            "command": "ls",
            "status": "finished",
            "output": listing,
            "exit_code": 0,
        }
    )

    _step()
    if arguments.read_only:
        _emit({"type": "message", "text": "Read-only: not writing anything."})
    else:
        file_id = uuid.uuid4().hex
        _emit({"type": "file", "id": file_id, "path": NOTES_FILE, "status": "started"})
        with Path(NOTES_FILE).open("a") as notes:
            notes.write(f"- turn {turn}: {first_line}\n")
        _emit({"type": "file", "id": file_id, "path": NOTES_FILE, "status": "finished"})

    _step()
    _emit({"type": "message", "text": "Done."})
    _emit({"type": "done", "input_tokens": len(prompt) // 4 + 1, "output_tokens": 40})


def _next_turn(session_id: str) -> int:
    """Count the turns of a session, so a resumed session knows it has run before."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    path = SESSIONS_DIR / f"{session_id}.json"
    turns = json.loads(path.read_text())["turns"] + 1 if path.exists() else 1
    path.write_text(json.dumps({"turns": turns}))
    return turns


def _step() -> None:
    time.sleep(float(os.environ.get("EXAMPLE_AGENT_STEP_SECONDS", "1")))


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


if __name__ == "__main__":
    sys.exit(main())
