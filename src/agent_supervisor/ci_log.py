"""Turn failed CI logs into content for the agent. All functions here are pure.

CI logs often run to thousands of lines and the real error is usually at the end, so only the tail
of each failed check is kept.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

TRUNCATION_NOTICE = "[... earlier output omitted ...]\n"

# Each line of `gh run view --log` has the format "job name<TAB>step name<TAB>timestamp content".
_PREFIX = re.compile(r"^[^\t]*\t[^\t]*\t")
_TIMESTAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
# In testing, `gh` prints the ESC control character as the two characters "^[", so both forms must
# be stripped.
_ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*[A-Za-z]")


@dataclass(frozen=True)
class CheckLog:
    """The name of one failed check and its raw log."""

    name: str
    text: str


def clean(raw: str) -> str:
    """Strip the job and step prefix, timestamp, and color codes from each line."""
    lines = []
    for line in raw.splitlines():
        line = _TIMESTAMP.sub("", _PREFIX.sub("", line))
        lines.append(_ANSI.sub("", line).rstrip())
    return "\n".join(lines).strip()


def tail(text: str, max_chars: int) -> str:
    """Keep the last `max_chars` characters, starting at a full line; note any cut."""
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
    """Build the fix instructions for the agent: the failed checks and the tail of each log."""
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
