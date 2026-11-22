# Writing an adapter

An adapter lets agent-supervisor drive one kind of coding agent. agent-supervisor creates the
worktree, decides when the agent runs and with what instructions, then commits, pushes, and opens
the pull request. The adapter only runs the agent for one turn and reports what happened.

The built-in adapters (Claude Code and Codex) and any adapter you write are loaded the same way,
so an adapter is an ordinary Python package; nothing in this repository has to change.

> **Not stable yet.** Until 1.0, the adapter interface described here may change in any minor
> release (0.3 to 0.4, and so on). Changes are listed in the release notes. Pin the
> agent-supervisor version your adapter was written against.

A complete, runnable example is in [`examples/example-adapter/`](../../examples/example-adapter/):
a fake agent CLI and the adapter around it. It needs no account, so you can run a whole task with
it. Read it next to this guide.

## Contents

- [How a turn works](#how-a-turn-works)
- [The interface](#the-interface)
- [Events](#events)
- [The result of a turn](#the-result-of-a-turn)
- [Capabilities](#capabilities)
- [Stopping: what must happen when the stream is closed](#stopping-what-must-happen-when-the-stream-is-closed)
- [Reporting usage](#reporting-usage)
- [Checking whether the agent can be used](#checking-whether-the-agent-can-be-used)
- [Packaging and loading](#packaging-and-loading)
- [Testing](#testing)
- [Common mistakes](#common-mistakes)

## How a turn works

Every time agent-supervisor wants the agent to do something (the first instructions of a task, a
message from the user, a failed CI log, review comments, a merge conflict), it starts a **run**. A
run calls your adapter's `run_turn` once:

1. agent-supervisor prepares the worktree and calls `run_turn(prompt=..., cwd=worktree, ...)`.
2. Your adapter starts the agent and yields an `AgentEvent` for each thing the agent does. Each one
   is written to the task's audit log and shown in the board's Log tab as it happens.
3. When the agent finishes, your adapter yields one `TurnResult`, as the last item.
4. If the result says the turn succeeded, agent-supervisor commits whatever the agent left
   uncommitted in the worktree, pushes, and opens the pull request if there is none yet.

The agent edits files in `cwd` and may commit them, but must not push, open pull requests, or
switch branches; agent-supervisor does that.

Later runs of the same task pass the `session_id` your adapter reported, so the agent can continue
the same conversation, if it supports that.

A run can also end early: the user presses Stop, the run's time limit passes, or the daemon shuts
down. agent-supervisor then closes your stream, and your adapter must stop the agent (see
[Stopping](#stopping-what-must-happen-when-the-stream-is-closed)).

## The interface

Everything an adapter needs is in `agent_supervisor.adapters.base`, `agent_supervisor.enums`,
`agent_supervisor.tool_status`, and `agent_supervisor.process`; the imports below are all of it. An adapter is any object with these four members; it does not
inherit from anything.

```python
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agent_supervisor.adapters.base import (
    AdapterCapabilities,
    AgentEvent,
    PermissionPolicy,
    RunLimits,
    SandboxLevel,
    TurnResult,
)
from agent_supervisor.enums import EventType, ExitStatus
from agent_supervisor.process import CommandTimeoutError, run_command
from agent_supervisor.tool_status import (
    AuthKind,
    ToolMissingError,
    ToolStatus,
    read_version,
    run_check,
)


class MyAdapter:
    display_name = "My agent"
    capabilities = AdapterCapabilities(
        can_resume_session=True,
        can_limit_turns=False,
        can_limit_budget=False,
        can_gate_tool_calls=False,
        can_restrict_network=False,
        reports_cost=False,
    )

    async def check(self) -> ToolStatus: ...

    async def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_id: str | None,
        model: str | None,
        limits: RunLimits,
        policy: PermissionPolicy,
        previous_usage: dict[str, Any] | None,
    ) -> AsyncIterator[AgentEvent | TurnResult]:
        ...
        yield ...
```

`run_turn` is an **async generator**: define it with `async def` and use `yield` in its body.

One adapter object is created when the daemon starts, by calling your entry point with no
arguments, and it is shared: several tasks can be inside `run_turn` at the same time. Keep all
per-turn state in local variables, not on `self`.

### `display_name: str`

The name people see on the board, e.g. `"Claude Code"`. The task form, the cards, and the settings
page use it.

### `capabilities: AdapterCapabilities`

What your adapter can actually do. See [Capabilities](#capabilities).

### `check()`

Whether the agent can be used on this machine. See
[Checking whether the agent can be used](#checking-whether-the-agent-can-be-used).

### `run_turn(...)`

Run the agent once. All arguments are keyword-only:

| Argument | Meaning |
| --- | --- |
| `prompt` | The instructions for this turn, as plain text. For the first run it is the task description; later runs carry a message from the user, a CI failure log, review comments, or a request to resolve a merge conflict, already written as instructions. Pass it to the agent as is. |
| `cwd` | The task's worktree. Run the agent there. It is a git checkout on the task's branch; the agent may commit, but must stay on this branch. |
| `session_id` | `None` to start a new session; otherwise the session id your adapter reported in an earlier turn of the same task, to continue that session. Always `None` when `can_resume_session` is false. |
| `model` | The model the user typed when opening the task, or `None` for the agent's default. Pass it through; it is free text and agent-supervisor does not check it. |
| `limits` | `RunLimits(max_turns, max_budget_usd)`: limits to enforce while the agent runs. Each is `None` unless you declared the matching capability. See [Capabilities](#capabilities). |
| `policy` | `PermissionPolicy`: what the agent is allowed to do. See [The permission policy](#the-permission-policy). |
| `previous_usage` | The `usage_raw` your adapter returned for this task's latest turn that had one, or `None`. Only needed when the agent reports running totals. See [Reporting usage](#reporting-usage). |

What `run_turn` yields:

1. A `session_started` event, **first, before starting the agent process**. See
   [`session_started`](#session_started).
2. Any number of events, in the order things happened.
3. Exactly one `TurnResult`, last. agent-supervisor stops reading once it has the result and
   closes your generator right away; anything yielded after it is ignored. So wait for the agent
   process to exit (with a timeout) and collect its exit code and stderr **before** yielding the
   result.

If the generator ends without yielding a `TurnResult`, the run ends as an error ("agent stream
ended without a result"). If it raises an exception, the run ends as an error and the exception's
text is shown as the reason. Both are fine for real failures; just make sure the agent process is
stopped in either case (use `try`/`finally`).

### The permission policy

`policy` comes from the user's config file and is the same for every agent:

| Field | Meaning |
| --- | --- |
| `sandbox_level` | `SandboxLevel.WORKSPACE_WRITE` (default): the agent may write inside `cwd`. `SandboxLevel.READ_ONLY`: it must not write at all. |
| `allow_network` | Whether the agent may use the network. |
| `tool_rules` | A free-form mapping. Each adapter decides what it understands. The default config holds `allowed_tools` (tool names) and `denied_commands` (command prefixes such as `git push` and `gh`). Ignore keys you do not understand. |

Enforce what the agent lets you enforce, and when you block an action, yield a
`permission_denied` event (see [Events](#events)). What you cannot enforce, ignore, and say so in
`capabilities` (`can_gate_tool_calls`, `can_restrict_network`); the user sees that, and the
policy is not a reason to refuse the run. Never pretend to enforce something.

This policy is basic protection, not a security boundary. agent-supervisor is meant for trusted
repositories.

## Events

An event is `AgentEvent(type, payload)`: `type` is an `EventType` and `payload` is a dict that can
be serialized to JSON. Events are appended to the audit log and never changed afterwards.

Adapters may yield these five types. The others (`pushed`, `pr_opened`, `ci_status`,
`review_comment`, `state_change`) are written by agent-supervisor itself.

| Type | When | Payload |
| --- | --- | --- |
| `session_started` | First thing in every turn; again once the session id is known | `session_id`, `process_marker` |
| `agent_message` | The agent says something, reasons, or updates its plan | `kind`, `text` |
| `command` | The agent runs a shell command | `phase`, `tool_use_id`, `tool`, `command`; on finish also `output`, `exit_code`, `is_error` |
| `tool_call` | The agent uses any other tool (edits a file, searches, ...) | `phase`, `tool_use_id`, `tool`, `input`; on finish also `is_error` and any result |
| `permission_denied` | Your adapter blocked an action because of the policy | `tool`, `input`, `reason` |

The Log tab of the board reads the fields named above. The audit log (the Events tab) shows the
whole payload, so you can add more fields, e.g. the agent's raw message, to help with debugging.

### `session_started`

```python
AgentEvent(EventType.SESSION_STARTED, {"session_id": None, "process_marker": marker})
```

- `process_marker`: a string that appears on the **command line** of the agent process and of no
  other process, e.g. `f"my-agent-{uuid.uuid4().hex}"`. If the daemon is killed while the agent
  runs, the next start finds the leftover process with `pgrep -f <marker>` and stops it before
  the task can run again. Yield this event before you start the process: if the daemon dies right
  after starting it, the marker must already be on record. How to get it onto the command line
  depends on the agent; an unused argument, an environment variable that is shown on the command
  line, or a wrapper's argument all work. If your agent runs in-process (no child process), use
  `None`.
- `session_id`: the session being continued, or `None` for a new session whose id is not known yet.
  When the agent reports the new id, yield another `session_started` with that id and the same
  marker. agent-supervisor saves the latest non-empty `session_id` and passes it to the next turn.
  When resuming, the first event already carries the id; yield another one only if the agent
  reports a different id (some agents start a new session when the old one cannot be resumed).

### `agent_message`

```python
AgentEvent(EventType.AGENT_MESSAGE, {"kind": "message", "text": "I'll add the greeting."})
```

`kind` is `"message"` for what the agent says, `"reasoning"` for its reasoning (shown dimmed), or
`"plan"` for plan or to-do updates (shown like a message). Messages with empty `text` are not
shown.

### `command` and `tool_call`

Each action is **two events** with the same `tool_use_id`: one with `"phase": "started"` when it
begins, and one with `"phase": "finished"` when it ends. The log is append-only, so the result
cannot be added to the first event later. If the agent only tells you about an action after it is
done, yield both events one after the other.

```python
# A shell command
AgentEvent(
    EventType.COMMAND,
    {
        "phase": "started",
        "tool_use_id": "c1",
        "tool": "shell",
        "command": "pytest -q",
    },
)
AgentEvent(
    EventType.COMMAND,
    {
        "phase": "finished",
        "tool_use_id": "c1",
        "tool": "shell",
        "command": "pytest -q",
        "output": "3 passed",
        "exit_code": 0,
        "is_error": False,
    },
)

# Any other tool
AgentEvent(
    EventType.TOOL_CALL,
    {
        "phase": "started",
        "tool_use_id": "t1",
        "tool": "edit_file",
        "input": {"path": "app.py"},
    },
)
AgentEvent(
    EventType.TOOL_CALL,
    {
        "phase": "finished",
        "tool_use_id": "t1",
        "tool": "edit_file",
        "is_error": False,
    },
)
```

The Log tab shows `$ <command>` for a started command, its `output` when it finishes (in red when
`is_error` is true), and `<tool> <input>` for a started tool call. `input` can be a string or a
dict. Use the agent's own id for `tool_use_id` if it has one; otherwise make one up. `tool` is
free text: use the agent's own tool names. Put `command` on the finished event too, even if the
agent only reports it at the start; keep a map from `tool_use_id` to the command while the turn
runs.

Use `command` only for shell commands and `tool_call` for everything else.

### `permission_denied`

```python
AgentEvent(
    EventType.PERMISSION_DENIED,
    {
        "tool": "shell",
        "input": "git push --force",
        "reason": "command starts with 'git push'",
    },
)
```

Yield it each time your adapter refuses an action because of `policy`, with the full input that
was refused.

## The result of a turn

```python
TurnResult(
    session_id="abc123",
    exit_status=ExitStatus.SUCCESS,
    tokens_in=1200,
    tokens_out=340,
    cost_usd=None,
    usage_raw={"session_id": "abc123", "input_tokens": 1200, "output_tokens": 340},
    detail=None,
)
```

| Field | Meaning |
| --- | --- |
| `session_id` | The session this turn used; `None` if there is none. |
| `exit_status` | How the turn ended; see below. |
| `tokens_in`, `tokens_out` | Tokens used by **this turn**. `None` when unknown. |
| `cost_usd` | Money spent by **this turn**, in US dollars. `None` when unknown or when the agent does not report it. |
| `usage_raw` | Whatever usage data you want to keep, as a JSON-serializable dict, or `None`. It is stored with the run and handed back as `previous_usage` next time. |
| `detail` | A short reason shown on the board when the turn did not succeed, e.g. the last lines of the agent's error output. |

`exit_status` is one of:

| Value | Use it when | What happens next |
| --- | --- | --- |
| `ExitStatus.SUCCESS` | The agent finished its turn normally | agent-supervisor commits, pushes, and opens or updates the pull request |
| `ExitStatus.LIMIT_REACHED` | The agent stopped because of `limits.max_turns` or `limits.max_budget_usd` | Nothing is published; the card waits for the user |
| `ExitStatus.ERROR` | Anything else went wrong: the agent crashed, exited non-zero, or produced no result | Nothing is published; the card waits for the user |

Do not use `STOPPED` or `INTERRUPTED`. agent-supervisor sets those itself when it closes your
stream, and does not read your result in that case.

`SUCCESS` means "the agent says it is done", not "the work is right": CI decides that afterwards.
Report `SUCCESS` even when the agent changed nothing; agent-supervisor then has nothing to push
and does not open a pull request.

## Capabilities

```python
AdapterCapabilities(
    can_resume_session=...,
    can_limit_turns=...,
    can_limit_budget=...,
    can_gate_tool_calls=...,
    can_restrict_network=...,
    reports_cost=...,
)
```

Fill these in truthfully. agent-supervisor uses some of them to decide what to pass you and what
to show; if you claim something the agent cannot do, limits silently stop working.

| Field | Set it to true when | Effect |
| --- | --- | --- |
| `can_resume_session` | The agent can continue an earlier session by id | When false, `session_id` is always `None`: every turn starts fresh, and follow-up instructions arrive without the earlier conversation |
| `can_limit_turns` | You stop the agent after `limits.max_turns` agent turns (model round trips) | When false, `limits.max_turns` is always `None` |
| `can_limit_budget` | You stop the agent once it has spent `limits.max_budget_usd` | When false, `limits.max_budget_usd` is always `None` |
| `can_gate_tool_calls` | You can see each tool call before it runs and refuse it | Shown to the user; does not change what is passed |
| `can_restrict_network` | You can actually keep the agent off the network when `policy.allow_network` is false | Shown to the user; does not change what is passed |
| `reports_cost` | `TurnResult.cost_usd` is filled in | When false, the board shows token counts instead of money, and the task's cost limit cannot apply (the token limit still does) |

Whatever you declare, agent-supervisor still enforces its own limits: the run's time limit, and
the task's total cost and token limits between runs.

## Stopping: what must happen when the stream is closed

When the user presses Stop, the run's time limit passes, or the daemon shuts down,
agent-supervisor closes your generator (it uses `contextlib.aclosing`). Python then raises
`GeneratorExit` at the `yield` where your generator is paused, and your `finally` blocks run.

**By the time your generator returns, the agent and all of its child processes must be gone.**
agent-supervisor waits for that before it marks the run as stopped, so a new run never works in
the same worktree while the old agent is still editing it.

A pattern that works for a CLI agent (see `examples/example-adapter/src/example_adapter/adapter.py`):

```python
marker = f"my-agent-{uuid.uuid4().hex}"
yield AgentEvent(EventType.SESSION_STARTED, {"session_id": session_id, "process_marker": marker})

process = await asyncio.create_subprocess_exec(
    "my-agent",
    "--tag",
    marker,
    "--",
    prompt,
    cwd=cwd,
    stdin=asyncio.subprocess.DEVNULL,  # or PIPE: then write the prompt and close it
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    start_new_session=True,  # its own process group
)
try:
    ...  # read output and yield events; wait for the process; then yield the TurnResult
finally:
    await stop(process)


async def stop(process: asyncio.subprocess.Process, grace_seconds: float = 5.0) -> None:
    """Ask the agent to stop, then kill whatever is left of its process group."""
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGINT)
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), grace_seconds)
    # Even once the agent itself has exited, children it started may still be running in its
    # group: kill the whole group every time.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), grace_seconds)
```

Things to get right:

- **Start the agent in its own process group** (`start_new_session=True`). Otherwise Ctrl-C in the
  daemon's terminal reaches the agent directly, and you cannot kill the agent's own children
  together with it. The group id is then the agent's pid, which is what `os.killpg` takes.
- **Ask first, then force the whole group.** Many agent CLIs clean up on SIGINT; give them a few
  seconds. Use the signal your agent actually handles: if it ignores SIGINT, send SIGTERM instead,
  or every Stop waits out the whole grace period. Then SIGKILL the group, even if the agent has
  already exited, since its children may not have.
- **Do not `await` anything that can hang forever in `finally`.** Bound every wait with a timeout.
- **Read stderr while the agent runs** (e.g. in a separate `asyncio` task), or the agent blocks
  once the pipe buffer fills up.
- **Do not leave stdin open and unused.** An agent that reads stdin waits for it forever. Use
  `DEVNULL`, or write the prompt and close it.
- **Do not catch `asyncio.CancelledError` or `GeneratorExit` and carry on.** Clean up and let them
  propagate.

After a daemon crash, agent-supervisor finds leftover processes whose command line contains
`process_marker` and sends them SIGINT, then SIGKILL. That is a fallback for when your `finally`
never ran, and it reaches only processes with the marker on their command line: children without
it are stopped only if the agent stops them itself on SIGINT. The normal path is your own cleanup.

## Reporting usage

agent-supervisor adds up each run's usage to enforce the per-task cost and token limits, and shows
it on the cards and the Usage page. So `TurnResult` must hold **this turn's** usage only.

- **The agent reports this turn's usage.** Put it in `tokens_in`, `tokens_out`, and `cost_usd`.
- **The agent reports running totals for the session.** Subtract the totals at the end of the
  previous turn. Keep the totals and the session id in `usage_raw`; agent-supervisor passes the
  latest `usage_raw` of the task back as `previous_usage`:

  ```python
  def this_turn(totals: dict, session_id: str, previous_usage: dict | None) -> dict:
      """Subtract each running total from its value at the end of the previous turn."""
      if previous_usage is None or previous_usage.get("session_id") != session_id:
          return totals  # a new session: everything so far is this turn
      return {name: max(value - previous_usage.get(name, 0), 0) for name, value in totals.items()}


  totals = {"input_tokens": 1200, "output_tokens": 340, "cost_usd": 0.05}  # as the agent reports
  turn = this_turn(totals, session_id, previous_usage)
  usage_raw = {"session_id": session_id, **totals}
  ```

  Subtract each total separately. Floating-point leftovers in `cost_usd` (e.g. `0.0100000002`) are
  fine; there is no need to round.

  Compare the session id: `previous_usage` is the task's latest `usage_raw`, which may come from
  an earlier session (for example, when a session could not be resumed and a new one started).
  A turn that was stopped has no result, so its `usage_raw` is not kept; if the agent had already
  counted it, the next turn's difference includes it. That is correct: the usage is counted once,
  one turn late.
- **The agent reports nothing, or the turn ended before it did.** Use `None`. Never put `0` for
  "unknown": `0` means "free", and the task's limits would treat it as such. The board marks
  numbers that include unknown runs as incomplete.

`tokens_in` counts all input tokens, including cached ones if the agent reports them separately;
keep the breakdown in `usage_raw` if you have it.

## Checking whether the agent can be used

`check()` tells the settings page, the new-task form, and the startup log whether the agent is
installed and signed in. It runs once when the daemon starts and again when the user presses
"Check again".

```python
async def check(self) -> ToolStatus:
    version, problem = await read_version(run_command, ["my-agent"], fix=("pipx install my-agent",))
    if problem is not None:
        return problem  # not installed, or `--version` failed or timed out
    if os.environ.get("MY_AGENT_API_KEY"):
        return ToolStatus(
            installed=True,
            version=version,
            auth=AuthKind.API_KEY,
            auth_label="MY_AGENT_API_KEY",
        )
    return ToolStatus(  # `auth` defaults to AuthKind.NONE: not usable
        installed=True,
        version=version,
        detail="not signed in",
        fix=("my-agent login",),
    )
```

`ToolStatus` fields (all but `installed` are optional):

| Field | Meaning |
| --- | --- |
| `installed` | Whether the agent can be run at all |
| `version` | Its version, if known |
| `auth` | How it is signed in, an `AuthKind`; defaults to `NONE`. The agent counts as usable when it is installed and `auth` is not `NONE`. See below. |
| `auth_label` | How it is signed in, for people, e.g. `"ChatGPT (plus)"` or `"MY_AGENT_API_KEY"` |
| `detail` | Why it cannot be used, when it cannot |
| `fix` | Commands the user can run in a terminal to fix it |
| `notice` | Something worth knowing even though it is usable |

`AuthKind` values for agents:

| Value | Meaning |
| --- | --- |
| `API_KEY` | A key or token billed by usage, e.g. from an environment variable; also gateways |
| `SUBSCRIPTION` | A personal plan's allowance, e.g. signed in with a ChatGPT or claude.ai account |
| `CLOUD_PROVIDER` | A cloud provider's credentials, e.g. Amazon Bedrock or Google Vertex AI |
| `OTHER` | Signed in some other way, or nothing is needed |
| `NONE` | Not signed in |

(`ACCOUNT` is for tools that are not agents, such as `gh`; adapters do not use it.)

Rules:

- **Do not spend anything.** No model calls; reading files, environment variables, and `--version`
  or `status` subcommands is fine.
- **Never return a credential**, not even part of one, and never return a tool's raw output, which
  can contain one. Name where the credential comes from instead ("the `MY_AGENT_API_KEY`
  environment variable").
- **Be quick and do not raise.** The daemon waits for every check before it starts serving. A
  check that raises, or takes longer than 30 seconds, marks your agent as unusable with a vague
  "the check failed" (the traceback goes to the daemon's log). Catch your errors and report them
  in `detail`, where you can say what is wrong; give commands a timeout.
- Configured is not the same as valid: you usually cannot tell whether a key works without using
  it. That is fine; a bad key shows up as an error in the first run.

Helpers, so you do not have to handle timeouts and missing executables yourself:

- `read_version(run, executable, fix=...)` in `agent_supervisor.tool_status`: `executable` is the
  list of arguments that starts the agent, e.g. `["my-agent"]` or `[sys.executable, "agent.py"]`;
  `--version` is appended. Returns `(version, None)`, or `(None, status)` with a ready-made
  "not installed" or "did not answer" status to return as is.
- `run_check(run, args)` in `agent_supervisor.tool_status`: runs any other check command (e.g.
  `["my-agent", "whoami"]`) with a 15-second timeout and returns a `CommandResult`
  (`returncode`, `stdout`, `stderr`). Raises `ToolMissingError` when the executable cannot be run
  and `CommandTimeoutError` (from `agent_supervisor.process`) when it times out; catch both.
- `parse_version(text)`: the first version number in a text.
- `run` is `agent_supervisor.process.run_command`, or any async function with the same
  signature (handy in tests).

```python
try:
    result = await run_check(run_command, ["my-agent", "whoami"])
except (ToolMissingError, CommandTimeoutError):
    return ToolStatus(installed=True, version=version, detail="`my-agent whoami` failed")
if result.returncode != 0:
    return ToolStatus(
        installed=True, version=version, detail="not signed in", fix=("my-agent login",)
    )
```

## Packaging and loading

agent-supervisor loads adapters through the `agent_supervisor.adapters`
[entry point](https://packaging.python.org/en/latest/specifications/entry-points/) group. Declare
one in your package's `pyproject.toml`. A complete one, for a package laid out as
`src/my_adapter/__init__.py`:

```toml
[project]
name = "my-agent-adapter"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = []  # not agent-supervisor; see below

[project.entry-points."agent_supervisor.adapters"]
my_agent = "my_adapter:MyAdapter"

[build-system]
requires = ["uv_build>=0.12.19,<0.14.0"]
build-backend = "uv_build"

[tool.uv.build-backend]
# Needed because the import name differs from the package name.
module-name = "my_adapter"
```

Any build backend works; this one is what `examples/example-adapter` uses.

- The name (`my_agent`) is the agent type: it is stored with each task and used in the API. It
  must be lowercase letters, digits, and underscores, starting with a letter. Do not change it
  once people use it, or their existing tasks can no longer run.
- The value points to anything that returns an adapter when called with no arguments: usually
  the class itself, or a function.
- `claude_code` and `codex` are taken by the built-in adapters.

Installing your package next to agent-supervisor is all it takes:

```sh
# When agent-supervisor is installed as a tool
uv tool install --force <agent-supervisor .whl file or URL> --with my-adapter

# While developing, from a clone of agent-supervisor
uv run --with-editable ../my-adapter agsv serve
```

Do **not** list `agent-supervisor` as a dependency of your package: it is not on PyPI, and an
unrelated package uses that name there. Install yours next to it as shown above.

When the daemon starts, it logs every adapter it could not load, with the reason, and carries on
without it. Your adapter is skipped when:

- importing it fails, or calling the entry point raises;
- it lacks `display_name` (a string), `capabilities` (an `AdapterCapabilities`), `run_turn`, or
  `check`;
- the name breaks the rule above, or another adapter already uses it.

Once loaded, it appears on the settings page and in the new-task form, with the result of
`check()`.

## Testing

Test `run_turn` directly; you do not need the daemon. The tests below are `async def` functions
and need [pytest-asyncio](https://pytest-asyncio.readthedocs.io/) with `asyncio_mode = "auto"`
under `[tool.pytest.ini_options]` (or mark each test with `@pytest.mark.asyncio`).

```python
from contextlib import aclosing

from agent_supervisor.adapters.base import PermissionPolicy, RunLimits, TurnResult
from agent_supervisor.enums import EventType, ExitStatus


async def test_one_turn(tmp_path):
    stream = MyAdapter().run_turn(
        prompt="add a greeting",
        cwd=tmp_path,
        session_id=None,
        model=None,
        limits=RunLimits(max_turns=None, max_budget_usd=None),
        policy=PermissionPolicy(),
        previous_usage=None,
    )
    async with aclosing(stream):
        items = [item async for item in stream]

    assert items[0].type is EventType.SESSION_STARTED
    assert items[0].payload["process_marker"]
    assert isinstance(items[-1], TurnResult)
    assert items[-1].exit_status is ExitStatus.SUCCESS
```

Worth testing:

- The first item is `session_started` with a marker, and the last is a `TurnResult`.
- Resuming: run two turns, passing the first result's `session_id` to the second.
- Stopping: make the agent slow (or give it a long command), read items until the agent is in the
  middle of something (not just the first `session_started`, which comes before the process
  starts), note its pid (`pgrep -f <marker>`), then leave the `async with aclosing(...)` block.
  Afterwards nothing may be left in its process group: `pgrep -g <pid>` finds nothing. Checking
  only `pgrep -f <marker>` misses children without the marker on their command line.
- Usage: a second turn of the same session reports only its own usage.
- Limits, if you declared them: running out gives `LIMIT_REACHED`.
- `check()` with the agent signed in, not signed in, and not installed.

`tests/test_example_adapter.py` in this repository does all of these for the example adapter,
including running a whole task through agent-supervisor with a fake GitHub.

To try it on the board, start the daemon with your adapter installed, open a task on a throwaway
repository, and watch the Log tab. Press Stop during a run and check that the agent process is
gone (`pgrep -f <marker>`, using the marker from the Events tab).

## Common mistakes

- **Yielding `session_started` after starting the process**, or with a marker that is not on the
  process's command line. A crash in between leaves a process the daemon cannot find.
- **A marker that is not unique**, such as the agent's name. Restart recovery would stop every
  matching process, including ones that belong to other tasks or are not agent-supervisor's.
- **Not stopping the agent when the stream is closed**, or stopping only the top process and leaving
  its children. Stop then leaves an agent editing the worktree in the background. Signal the whole
  process group, and do it even when the agent itself exited quickly.
- **Reporting session totals as this turn's usage.** Costs add up twice from the second turn on.
- **Reporting `0` instead of `None`** for unknown usage. Limits stop working.
- **Claiming a capability you cannot enforce**, e.g. `can_limit_budget=True` without actually
  stopping the agent at the budget.
- **Keeping per-turn state on `self`.** Several tasks share one adapter object.
- **Letting the agent push, open pull requests, or switch branches.** agent-supervisor does those;
  if the agent does them too, the task's branch and pull request get out of step. Deny `git push`
  and `gh` if you can (the default `tool_rules["denied_commands"]` lists them).
- **Blocking the event loop.** `run_turn` and `check` run inside the daemon's event loop; use
  `asyncio` subprocesses, or `asyncio.to_thread` for blocking calls. A blocking call freezes the
  board and every other task.
- **Raising from `check()`.** Your agent shows as unusable with no useful reason.
- **Not reading stderr.** The agent hangs once the pipe is full, and the run only ends at its time
  limit.
- **Yielding the result before the agent has exited.** The stream is closed right after the
  result, so the exit code and the end of stderr are lost, and a still-running agent gets killed.
