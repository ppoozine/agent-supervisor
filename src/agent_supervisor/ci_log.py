"""把 CI 的失敗 log 整理成給 agent 的內容。全部是純函式。

CI log 動輒數千行，真正的錯誤通常在最後，所以每個失敗的檢查只保留尾段。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

TRUNCATION_NOTICE = "[... earlier output omitted ...]\n"

# `gh run view --log` 每一行的格式是「job 名稱<TAB>步驟名稱<TAB>時間戳 內容」。
_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t")
_TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
# 實測 `gh` 會把控制字元 ESC 印成「^[」兩個字元，所以兩種寫法都要去掉。
_ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class CheckLog:
    """一個失敗檢查的名稱與它的原始 log。"""

    name: str
    text: str


def clean(raw: str) -> str:
    """去掉每一行的 job 與步驟前綴、時間戳、顏色碼。"""
    lines = []
    for line in raw.splitlines():
        line = _TIMESTAMP.sub("", _PREFIX.sub("", line))
        lines.append(_ANSI.sub("", line).rstrip())
    return "\n".join(lines).strip()


def tail(text: str, max_chars: int) -> str:
    """保留最後 `max_chars` 個字元，從完整的一行開始；有截掉就加上說明。"""
    if len(text) <= max_chars:
        return text
    kept = text[-max_chars:]
    first_newline = kept.find("\n")
    if first_newline != -1:
        kept = kept[first_newline + 1 :]
    return TRUNCATION_NOTICE + kept


def build_fix_prompt(
    head_sha: str, logs: Sequence[CheckLog], without_log: Sequence[str], *, max_chars: int
) -> str:
    """組出交給 agent 的修復指示：失敗的檢查與各自的 log 尾段。"""
    per_check = max(max_chars // max(len(logs), 1), 1)
    parts = [
        f"CI failed on commit {head_sha}. Fix the cause of the failures below."
        " Do not push or open a pull request; that is done for you."
    ]
    for log in logs:
        parts.append(f"## Failed check: {log.name}\n\n```\n{tail(clean(log.text), per_check)}\n```")
    if without_log:
        parts.append("Failed checks without an available log: " + ", ".join(without_log))
    return "\n\n".join(parts)
