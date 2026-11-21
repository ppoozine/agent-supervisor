# Example adapter

A minimal [agent-supervisor](../../README.md) adapter. It needs no account and spends nothing:
the "agent" is a small fake CLI (`example_adapter/agent.py`) that reads the instructions, lists the
files, appends a line to `EXAMPLE_AGENT_NOTES.md`, and reports token counts. That is enough for a
task to go all the way through: worktree, commit, push, pull request.

Use it to see how a third-party adapter plugs in, or as a starting point for your own. The adapter
(`example_adapter/adapter.py`) is written the way one for a real agent CLI would be: one process
per turn, a marker on its command line, its output turned into events, and the process stopped
whenever a turn ends early.

## How agent-supervisor finds it

`pyproject.toml` declares an entry point; its name, `example`, becomes the agent type:

```toml
[project.entry-points."agent_supervisor.adapters"]
example = "example_adapter:ExampleAdapter"
```

Installing the package next to agent-supervisor is all it takes. It is not loaded by default.

## Try it

From a clone of agent-supervisor:

```sh
uv run --with-editable examples/example-adapter agsv serve
```

Or add it to an installed agent-supervisor:

```sh
uv tool install --force <agent-supervisor .whl file or URL> --with ./examples/example-adapter
```

The settings page then lists "Example agent", and the new task form offers it. Each step of the
fake agent waits one second so you can watch the log and try Stop; set
`EXAMPLE_AGENT_STEP_SECONDS` before starting the daemon to change that.

agent-supervisor is not listed as a dependency: it is not on PyPI, and a different package uses
that name there. Always install this package next to it as shown above.
