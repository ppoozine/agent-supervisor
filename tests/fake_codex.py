"""Fake `codex` executable: prints prerecorded events from a script; never starts an agent.

Usage: `python fake_codex.py <script.json> <codex arguments...>`. Script fields:

- `record`: write the received arguments and stdin to this file
- `lines`: lines printed to stdout in order
- `stderr`, `exit_code`: what to print to stderr before exiting, and the exit code
- `hang`: do not exit after printing; start a child process (as if the agent ran a command)
- `ignore_interrupt`: both this process and the child ignore SIGINT
- `pids`: write the PIDs of this process and the child to this file
"""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

CHILD = """
import signal, sys, time
if sys.argv[1] == "ignore":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
time.sleep(600)
"""


def main() -> None:
    script = json.loads(Path(sys.argv[1]).read_text())
    if "record" in script:
        Path(script["record"]).write_text(
            json.dumps({"arguments": sys.argv[2:], "stdin": sys.stdin.read()})
        )
    for line in script.get("lines", []):
        print(line, flush=True)
    if script.get("hang"):
        hang(script)
    sys.stderr.write(script.get("stderr", ""))
    sys.exit(script.get("exit_code", 0))


def hang(script: dict[str, object]) -> None:
    ignore = bool(script.get("ignore_interrupt"))
    # Like the real Codex, the child runs in its own process group.
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, "ignore" if ignore else "obey"], start_new_session=True
    )

    def on_interrupt(signum: int, frame: object) -> None:
        child.kill()
        child.wait()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal.SIG_IGN if ignore else on_interrupt)
    Path(str(script["pids"])).write_text(json.dumps([child.pid]))
    print(
        json.dumps(
            {
                "type": "item.started",
                "item": {"id": "hang", "type": "command_execution", "command": "sleep"},
            }
        ),
        flush=True,
    )
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
