# Trust model and known limitations

agent-supervisor runs coding agents unattended, on your machine, as your user. This page says
plainly what protects you and what does not. The short version:

- **Only use it on repos you trust, with collaborators you trust.**
- **The permission policy is a basic safeguard, not a security boundary.**
- **Nothing is sandboxed by agent-supervisor itself.** What isolation there is comes from the agent
  (Codex has a real sandbox; Claude Code's is not in effect here).
- **The API has no login.** Any program on your machine can use it.

Real isolation (containers, per-project policies, interactive approval) is planned but not built.

## What an agent can reach

An agent runs as a child process of the daemon, as your user, in the task's worktree. Unless the
agent's own sandbox stops it, it can reach whatever you can:

- Every file your user can read, including SSH keys, cloud credentials, and other repos.
- The environment the daemon was started with, including `ANTHROPIC_API_KEY`, `CODEX_API_KEY`, and
  anything else you exported in that shell.
- The network.
- Your GitHub access through `gh` and git's credentials, which are the same ones the supervisor uses
  to push and open pull requests.

A worktree only gives each task its own working directory. All worktrees of a repo share the same
`.git` data, so a task can see and change other tasks' branches.

## What the permission policy does

The `[permission]` section of the config file (see [usage](usage.md#the-config-file)) has three
parts. Each agent enforces what it can, and what it cannot enforce is declared in its
`capabilities` rather than pretended.

| Setting | Claude Code | Codex |
| --- | --- | --- |
| `allowed_tools` | Enforced: every tool call is checked before it runs; anything not on the list is denied and recorded as a `permission_denied` event. | Not enforced. `codex exec` cannot approve calls one by one. |
| `denied_commands` | Enforced, but only as a prefix match on each part of a shell command (split on `;`, `&&`, `\|`, and so on). | Not enforced. Codex cannot block individual commands here. |
| `sandbox_level` | `read-only` denies the file-writing tools and shell commands altogether. `workspace-write` limits only the file-writing tools to the worktree (see below). | Enforced by Codex's own sandbox. |
| `allow_network` | **Not enforced.** Claude Code's sandbox settings had no effect in testing and the cause is not known, so shell commands have full network access. | Enforced by Codex's own sandbox. |

Claude Code's file-writing tools may only write inside the worktree. Shell commands are not limited
that way. If you override `[permission.tool_rules]` in the config file, the whole table is replaced:
leaving out `allowed_tools` allows every tool.

Why this is not a boundary, even where it is enforced:

- **Prefix matching is easy to get around.** `git push` is denied, but a script, a test, a Makefile
  target, or `bash -c "..."` that pushes is not. Any agent that can run the project's tests can run
  arbitrary code through them.
- **The repo itself can change what the agent does.** Claude Code loads the repo's project settings
  (`.claude/settings.json`, `CLAUDE.md`), which can include hooks that run commands. Codex loads your
  own `~/.codex` configuration, including MCP servers; whether those are bound by its sandbox has not
  been checked.
- **Codex can run any command its sandbox allows.** With network access allowed, that includes
  commands that use your credentials.

## Untrusted text reaches the agent

Several kinds of text that other people can write are sent to the agent as instructions:

- **Comments on the pull request.** Every comment that is not from a bot is sent back to the agent:
  plain comments, review summaries, and line comments, **from any author**. On a repo where people
  you do not trust can comment on pull requests, for example a public one, anyone can tell your agent
  what to do.
- **CI logs.** The failing part of the log is sent when CI fails. Its content comes from the code
  and the tests in the repo.
- **The repo's content**, as with any coding agent: files, instructions in `CLAUDE.md` or
  `AGENTS.md`, and the output of commands.

Combined with the section above, a prompt injection in any of these can run commands as you.

## The HTTP API and the board

- The daemon listens only on `127.0.0.1`.
- There is no login. It checks the `Host` and `Origin` headers, so other websites open in your
  browser cannot call it, including through DNS rebinding.
- **Any program running on your machine can call it**, and through it start agents in any registered
  repo. On a shared machine, other users can too.

## What is stored

- Everything lives in `~/.agent-supervisor` (or your `data_dir`): a SQLite database and the
  worktrees. Nothing is encrypted.
- The database keeps every agent message and tool call, including command output. If an agent prints
  a secret, it is in the database and visible on the board.
- agent-supervisor never stores API keys or tokens. The agent checks read whether a credential is set,
  not its value, and do not record the tools' raw output.

## Using a Claude subscription

The settings page shows when Claude Code is signed in with a Claude subscription (claude.ai) instead
of an API key, together with the passage of the Agent SDK documentation about it. Whether using your
own subscription this way is acceptable is your call; the documentation does not say clearly. Setting
`ANTHROPIC_API_KEY` makes Claude Code use the key instead.

## Other known limitations

- **One user, one machine.** No accounts or roles.
- **No merge button.** You merge on GitHub.
- **No limit on parallel runs and no queue.** Every task that qualifies for an automatic fix starts
  at once.
- **Polling only.** GitHub is checked every 30 seconds; there are no webhooks.
- **Edited comments are not sent again.** Comments that were already on a pull request when the
  supervisor first saw it are sent.
- **Cancelling a CI run on GitHub counts as a failure** and uses one of the three automatic fixes.
- **Codex tasks show no cost**, and only the token budget and the time limit stop them.
- **After the daemon crashes**, the next start stops the agent processes left behind, but does not
  check that every process they started is gone.

## Reporting a security issue

See [SECURITY.md](../../SECURITY.md).
