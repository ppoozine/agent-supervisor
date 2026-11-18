"""ci_log.py 的測試：CI log 的清理、截取與修復指示。"""

from agent_supervisor.ci_log import TRUNCATION_NOTICE, CheckLog, build_fix_prompt, clean, tail


def test_clean_removes_prefix_timestamp_and_colour_codes() -> None:
    raw = (
        "pytest\tRun tests\t﻿2026-11-16T16:22:48.9587401Z ##[group]Run pytest\n"
        "pytest\tRun tests\t2026-11-16T16:22:48.9588140Z \x1b[36;1mpytest -q\x1b[0m\n"
        "pytest\tRun tests\t2026-11-16T16:22:48.9588729Z ^[[36;1mecho done^[[0m\n"
        "pytest\tRun tests\t2026-11-16T16:22:49.0680600Z ASSERTION FAILED\n"
    )

    assert clean(raw) == "##[group]Run pytest\npytest -q\necho done\nASSERTION FAILED"


def test_tail_keeps_short_text_as_is() -> None:
    assert tail("line 1\nline 2", 100) == "line 1\nline 2"


def test_tail_keeps_the_end_and_starts_on_a_whole_line() -> None:
    text = "\n".join(f"noise line {number}" for number in range(1, 3001)) + "\nASSERTION FAILED"

    kept = tail(text, 60)

    assert kept.startswith(TRUNCATION_NOTICE + "noise line ")
    assert kept.endswith("noise line 3000\nASSERTION FAILED")
    assert len(kept) <= 60 + len(TRUNCATION_NOTICE)


def test_fix_prompt_shares_the_limit_between_failed_checks() -> None:
    long_log = "\n".join(f"line {number}" for number in range(5000))
    logs = [CheckLog("unit", long_log + "\nunit boom"), CheckLog("lint", long_log + "\nlint boom")]

    prompt = build_fix_prompt("abc123", logs, ["external/scan"], max_chars=2000)

    assert "commit abc123" in prompt
    assert "## Failed check: unit" in prompt and "unit boom" in prompt
    assert "## Failed check: lint" in prompt and "lint boom" in prompt
    assert "external/scan" in prompt
    assert len(prompt) < 2000 + 600
