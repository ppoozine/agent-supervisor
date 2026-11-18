"""假的 `codex` 執行檔：依劇本印出預錄的事件，不啟動任何 agent。

用法：`python fake_codex.py <劇本.json> <codex 的參數...>`。劇本的欄位：

- `record`：把收到的參數與 stdin 寫到這個檔案
- `lines`：依序印到 stdout 的每一行
- `stderr`、`exit_code`：結束前印到 stderr 的內容與結束代碼
- `hang`：印完之後不結束，並啟動一個子程序（模擬 agent 正在執行指令）
- `ignore_interrupt`：自己與子程序都不理會 SIGINT
- `pids`：把自己與子程序的程序編號寫到這個檔案
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
    # 和真的 Codex 一樣，子程序在自己的程序群組裡。
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
