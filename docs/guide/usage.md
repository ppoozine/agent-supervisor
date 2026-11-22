# Using agent-supervisor

This page explains what you see on the board, what the config file controls, and every limit that
stops the agents. For installing and the first task, see the [README](../../README.md).

## Tasks, runs, and the event log

- A **task** is one piece of work: one branch, one worktree, at most one pull request, and one
  agent session that is kept across everything below.
- A **run** is one turn of the agent inside that session. The first run does the task; later runs
  fix CI, address PR comments, resolve a merge conflict, or follow a message you sent. A task has at
  most one active run at a time.
- Every run, agent message, tool call, permission denial, push, and PR comment is written to an
  **event log** that is never edited. The task's page shows it in the **Chat** and **Events** tabs.

Worktrees live in `~/.agent-supervisor/worktrees/<project>/task-<id>`, on a branch named
`asv/task-<id>-<title>`. They branch from the remote default branch (`origin/<branch>`) after a
fetch, so your own checkout is never touched.

After each run that ends normally, the supervisor (not the agent) does the publishing:

1. Commits anything the agent left uncommitted, as
   `chore(agent): save uncommitted changes from run <id>`.
2. Pushes the branch. If the agent rewrote commits it had already pushed, the supervisor
   force-pushes only when the remote still points at its own last push; if someone else pushed to
   the branch, it stops with an error instead of overwriting their work.
3. Opens a pull request if there is none yet. Its title is the task title.

If the branch has no commits, no pull request is opened and the card goes to **Needs you** with
**No changes**.

## The board

Columns are never stored; they are worked out from the run, pull request, CI, and review state each
time anything changes. A task is in exactly one place.

| Column | Meaning |
| --- | --- |
| **Working** | A run is active, or one is about to start automatically. |
| **Needs you** | Nothing more will happen without you. The label says why. |
| **In review** | The pull request is open and waiting on CI, on GitHub, or on a review. |
| **Ready to merge** | CI passed, no conflicts, and (if the project requires it) a review approved. Merge on GitHub. |
| **Archive** | Below the board: the pull request was merged or closed, or you archived the task. |

### Labels

**Working**

| Label | Meaning |
| --- | --- |
| Working | The agent is doing the task or following your message. |
| Fixing CI | CI failed; the agent has the failing log. |
| Addressing review | New comments on the pull request were sent to the agent. |
| Resolving conflict | The pull request conflicts with the base branch; the supervisor started a merge and the agent is fixing the conflicted files. |

**Needs you**

| Label | Meaning | What to do |
| --- | --- | --- |
| Agent error | The run failed: the agent crashed, could not start, or a git or GitHub step after it failed. The task's events say which. | Fix the cause, then send a message to continue. |
| Stopped | You pressed Stop. | Send a message to continue, or archive. |
| Limit reached | The run hit its turn, budget, or time limit. | Send a message to continue with a fresh budget. |
| Interrupted | The daemon stopped or crashed during the run. | Send a message to continue. |
| No changes | The agent finished without changing anything, so there is no pull request. | Send a message, or archive. |
| Changes requested | PR comments are waiting but will not be sent automatically: the 5 automatic rounds are used up, or the budget is. Also shown when a reviewer requested changes without a comment that could be sent. | Send a message. |
| Merge conflict | The conflict will not be resolved automatically: the 3 attempts are used up, the budget is, or the agent already tried on this commit without fixing it. | Send a message, or resolve it yourself. |
| CI failed | CI failed and will not be fixed automatically: the 3 attempts are used up, or the agent already tried on this commit without producing a new one. | Send a message, or fix it yourself. |
| Over budget | CI failed and the task has used its cost or token budget. | Send a message; it starts a new budget. |

The first four labels describe how the last run ended and take precedence: while the last run did
not end normally, the card shows one of them whatever else is pending, and nothing runs
automatically until you send a message.

**In review**

| Label | Meaning |
| --- | --- |
| CI running | Checks are still running on the latest commit. |
| No checks | The pull request has no CI checks at all. Nothing will be fixed automatically. |
| Checking mergeability | CI passed, but GitHub has not yet said whether the branch conflicts. Usually a few seconds. |
| Review pending | CI passed, and the project requires an approving review that has not happened yet. |

**Ready to merge**: the label is **Ready**.

**Archive**: **Merged**, **Closed** (the pull request was closed without merging), or **Archived**
(you archived the task while its pull request was still open, or it never had one).

When several automatic actions could happen at once, the order is: PR comments, then the merge
conflict, then the CI failure.

### The Chat tab

The task's page opens on **Chat**: the task as a conversation, split into runs. Each run starts
with a line saying its number, why it started, how it ended, how long it took and what it cost,
followed by what it was given and what the agent did. Every entry is marked with where it came
from:

| Entry | What it is |
| --- | --- |
| **You** (right, blue) | The task description or a message you sent |
| **Automatic** (dashed, yellow) | Instructions agent-supervisor sent on its own: failing CI logs, a merge conflict, or PR comments. Folded; open it to see exactly what the agent was given |
| **Reviewer** (purple edge) | A comment on the pull request, with the file and line for inline comments. Faded, with the reason, when it was not passed to the agent (a bot, an author who is not a collaborator, an approval, no text) |
| **Agent** | What the agent said, and one line per command, tool call or thought; open a line to see its output. Failed commands are red; actions blocked by the policy are orange |
| System lines (centered) | Pushes, the pull request, CI results, and runs that ended badly |

The header shows the automatic rounds used since you last stepped in, against their limits:
`fixes 1/3` (failed CI and merge conflicts together) and `comments 0/5`.

### Sending a message, Stop, and Archive

- **Message** (on the task's page): starts a new run in the same session with your text. It is how
  you continue after anything in **Needs you**. A message is never refused because of the budget:
  it resets the automatic attempt counters and starts a new budget. It is refused while a run is
  active, after archiving, and once the pull request is merged or closed.
  Press **Cmd+Enter** (or **Ctrl+Enter**) to send; Enter alone adds a new line. The same shortcut
  starts a task from the **New task** form's description. While a run is active you can type a
  draft, but Send stays disabled until the run ends; press Stop first to send at once.
- **Stop**: ends the active run. The agent's process and everything it started are killed, and
  nothing is published; changes stay in the worktree. Stopped tasks are never retried automatically.
- **Archive**: removes the worktree. Stop the active run first; archiving is refused while one is
  running. Uncommitted changes are committed to the task's branch locally (not pushed) before the
  worktree goes, so nothing is lost. The branch and the pull request stay; archiving does not close
  the pull request, and its label still follows it on GitHub. There is no unarchive.

## Comments on the pull request

New comments on the pull request are sent back to the agent automatically: plain comments, review
summaries, and line comments, from the repo's owner, members of its organization, and collaborators.
Comments from anyone else, from bots, empty review shells, and approvals are recorded in the task's
events but not sent; see the [trust model](trust-model.md#untrusted-text-reaches-the-agent). Several comments in a row are batched: the supervisor waits one polling interval after the
last new one.

The supervisor does not reply to comments or resolve them. If the agent disagrees with a comment,
its reasons are in its last message on the task's page.

Comments do not count as you stepping in, because the agent uses the same GitHub account as you:
the supervisor cannot tell your comments from comments the agent might post itself. Only a message
from the board or the CLI resets the counters.

## Limits

Every automatic loop is bounded, and the bounds are enforced by the supervisor, not left to the
agent.

| Limit | Default | Where it is set | When it is reached |
| --- | --- | --- | --- |
| Automatic CI fixes and conflict resolutions | 3 per task, shared | Fixed | No more automatic runs; the card goes to Needs you. |
| Automatic rounds of PR comments | 5 per task | Fixed | Same. |
| Cost per task | $5.00 | `task_cost_limit_usd` | No more automatic runs; an active run is stopped by the agent if it can (see below). |
| Tokens per task | 20,000,000 | `task_token_limit` | Same. |
| Turns per run | 50 | `max_turns_per_run` | The run ends as Limit reached. |
| Time per run | 30 minutes | `run_timeout_seconds` | The supervisor stops the run; Limit reached. |

- A commit's CI failure, a commit's conflict, and a comment are each handled once, no matter how
  often they are seen.
- Cost and tokens count from the latest time you stepped in (opening the task or sending a message),
  not over the task's whole life. The card still shows the task's total.
- The counters reset only when you send a message. After Stop, an error, an interruption, or a limit,
  nothing restarts automatically until you send one and that run ends normally.
- Limits only decide whether to keep retrying. They never block a result: if the third fix makes CI
  pass, the card goes to Ready to merge as usual, even over budget.
- Cost is the agent's own estimate from list prices, not your bill. With a subscription it is only a
  measure of relative use.
- Codex does not report cost, so the cost limit does not apply to Codex tasks; the token limit does.
  Cards show tokens only.
- The turn and in-run budget limits are enforced by the agent when it can (Claude Code can; Codex
  cannot, so for Codex only the time limit stops a run early). The time limit always works.

## The config file

The daemon reads `~/.agent-supervisor/config.toml` if it exists; every key is optional. Use another
file with `agsv --config <path> serve`. Unknown keys are an error, so a typo does not go unnoticed.
Restart the daemon after editing it.

```toml
# Where the database, worktrees, and lock file go.
data_dir = "~/.agent-supervisor"

# Budget per task, counted from the latest time you stepped in.
task_cost_limit_usd = 5.0
task_token_limit = 20_000_000

# Limits per run.
max_turns_per_run = 50
run_timeout_seconds = 1800

# How often GitHub is polled, in seconds.
poll_interval_seconds = 30

# The port of the board and the API. The daemon only listens on 127.0.0.1.
api_port = 8765

# How much of the failing CI logs is sent to the agent, in characters, shared by all failed checks.
ci_log_max_chars = 20_000

# Desktop notification when a card enters Needs you.
notify_needs_you = true

[permission]
# "workspace-write" or "read-only".
sandbox_level = "workspace-write"
allow_network = true

[permission.tool_rules]
# Claude Code only. Tools the agent may use; anything else is denied.
allowed_tools = ["Read", "Glob", "Grep", "Write", "Edit", "MultiEdit", "Bash", "TodoWrite"]
# Claude Code only. Shell commands starting with these are denied.
denied_commands = ["git push", "gh", "sudo", "rm -rf /"]
```

The defaults are the values shown. `[permission.tool_rules]` replaces the default table as a whole:
if you set only `denied_commands`, `allowed_tools` is no longer limited and every tool is allowed.
Copy both keys when you change either. What each agent actually does with `[permission]` differs; see
the [trust model](trust-model.md#what-the-permission-policy-does) before relying on it.

## Projects

A project is a local clone registered with the supervisor. The board's **Add project** asks for the
path and whether an approving review is required before **Ready to merge**. The GitHub repo and the
base branch are read from `origin`.

To choose a different base branch or GitHub repo, register it from the command line instead. Running
it again for the same path updates the settings:

```sh
agsv project add ~/code/my-app --default-branch develop --require-review
```

Requiring a review only helps if someone else reviews: GitHub does not let you approve your own pull
request, and the pull requests are opened with your account.

## Notifications

When a card enters **Needs you**, the daemon shows a desktop notification: through `osascript` on
macOS, `notify-send` on Linux (skipped if it is missing). It fires once on entering, not again while
the card stays there, and not for cards already there when the daemon starts. Turn it off with
`notify_needs_you = false`.

## The command line

`agsv serve` is the main command. The board and the [HTTP API](http-api.md) cover everything else.
The other commands come from before the daemon existed. `start`, `message`, `watch`, and `archive`
do the work in the foreground themselves, so they refuse to run while a daemon is running:

| Command | What it does |
| --- | --- |
| `agsv serve` | Start the daemon: the board, the API, polling, and automatic fixes. |
| `agsv project add <path>` | Register a repo, or update its settings. |
| `agsv list` | List tasks with their column and label. |
| `agsv show <task>` | Show a task's runs and events. |
| `agsv stop` | Stop every active run (not one task; use Stop on the board for that). |
| `agsv start`, `agsv message`, `agsv watch`, `agsv archive` | Foreground versions of opening a task, sending a message, polling, and archiving. |

Run any of them with `--help` for the options.
