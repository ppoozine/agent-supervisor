"""Finding adapters through the `agent_supervisor.adapters` entry point group."""

import logging
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

import pytest

from agent_supervisor.adapters.claude_code import ClaudeCodeAdapter
from agent_supervisor.adapters.codex import CodexAdapter
from agent_supervisor.adapters.loading import GROUP, load_adapters
from tests.fake_adapter import FakeAdapter

EXAMPLE_SOURCE = Path(__file__).parents[1] / "examples" / "example-adapter" / "src"


def external(name: str, value: str) -> EntryPoint:
    """An entry point as another installed package would declare it."""
    return EntryPoint(name=name, value=value, group=GROUP)


def make_fake() -> FakeAdapter:
    return FakeAdapter([])


class NotAnAdapter:
    display_name = "Half done"


def test_built_in_adapters_are_registered_as_entry_points() -> None:
    adapters = load_adapters()

    assert isinstance(adapters["claude_code"], ClaudeCodeAdapter)
    assert isinstance(adapters["codex"], CodexAdapter)


def test_an_external_adapter_is_loaded_next_to_the_built_in_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(EXAMPLE_SOURCE))
    installed = [*entry_points(group=GROUP), external("example", "example_adapter:ExampleAdapter")]

    adapters = load_adapters(installed)

    assert list(adapters) == ["claude_code", "codex", "example"]
    assert adapters["example"].display_name == "Example agent"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (external("broken", "no_such_module:Adapter"), "No module named 'no_such_module'"),
        (external("half", f"{__name__}:NotAnAdapter"), "lacks run_turn, check, capabilities"),
        (external("Bad-Name", f"{__name__}:make_fake"), "lowercase letters"),
        (external("codex", f"{__name__}:make_fake"), "another adapter already uses that name"),
    ],
    ids=["import-fails", "not-an-adapter", "bad-name", "takes-a-built-in-name"],
)
def test_a_bad_external_adapter_is_skipped_and_logged(
    entry: EntryPoint, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="agent_supervisor.adapters")
    installed = [*entry_points(group=GROUP), entry, external("fine", f"{__name__}:make_fake")]

    adapters = load_adapters(installed)

    assert isinstance(adapters["codex"], CodexAdapter)
    assert isinstance(adapters["fine"], FakeAdapter)
    assert entry.name not in adapters or entry.name == "codex"
    assert reason in caplog.text


def test_the_example_package_declares_its_entry_point() -> None:
    text = (EXAMPLE_SOURCE.parent / "pyproject.toml").read_text()

    assert '[project.entry-points."agent_supervisor.adapters"]' in text
    assert 'example = "example_adapter:ExampleAdapter"' in text
