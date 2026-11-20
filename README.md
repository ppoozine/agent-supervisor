# agent-supervisor

[![CI](https://github.com/ppoozine/agent-supervisor/actions/workflows/ci.yml/badge.svg)](https://github.com/ppoozine/agent-supervisor/actions/workflows/ci.yml)

Orchestration and audit layer for AI coding agents: isolated worktrees, CI feedback loops, cost limits, full event log.

## Platforms

Supported on macOS and Linux. Windows is not supported; use WSL instead.

Besides Python and Node.js, the daemon calls these commands:

- `git` and `gh` (logged in to GitHub)
- `pgrep` and `ps` (on Linux they come from `procps`, which minimal images may lack)
- Optional: `notify-send` on Linux for desktop notifications; macOS uses the built-in `osascript`
