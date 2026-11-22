# agent-supervisor

[![CI](https://github.com/ppoozine/agent-supervisor/actions/workflows/ci.yml/badge.svg)](https://github.com/ppoozine/agent-supervisor/actions/workflows/ci.yml)

Orchestration and audit layer for AI coding agents: isolated worktrees, CI feedback loops, cost limits, full event log.

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/); it fetches Python 3.13 if
you don't have it. Node.js is not needed.

**From a release** (recommended; includes the board). Pick a version on the
[Releases page](https://github.com/ppoozine/agent-supervisor/releases) and install its `.whl` file:

```sh
uv tool install https://github.com/ppoozine/agent-supervisor/releases/download/v0.3.0rc1/agent_supervisor-0.3.0rc1-py3-none-any.whl
```

To upgrade later, run the same command with the newer file and `--force`.

**From git** (follows `main`; the command line and the API only, **no board**, because the board
is built at release time and git does not carry it):

```sh
uv tool install git+https://github.com/ppoozine/agent-supervisor
```

Then start the daemon and open the board at <http://127.0.0.1:8765>. No config file is needed;
data goes to `~/.agent-supervisor`. The settings page lists which agents you can use and what is
missing.

```sh
agsv serve
```

**From a clone** (for development; needs Node.js to build the board):

```sh
uv sync
(cd web && npm install && npm run build)
uv run agsv serve
```

## Platforms

Supported on macOS and Linux. Windows is not supported; use WSL instead.

Besides Python, the daemon calls these commands:

- `git` and `gh` (logged in to GitHub)
- `pgrep` and `ps` (on Linux they come from `procps`, which minimal images may lack)
- Optional: `notify-send` on Linux for desktop notifications; macOS uses the built-in `osascript`

## Extending

- [Writing an adapter](docs/guide/adapters.md): connect another coding agent. Adapters are
  ordinary Python packages; [`examples/example-adapter`](examples/example-adapter/) is a complete
  one that needs no account.
- [HTTP API](docs/guide/http-api.md): everything the board does, scripts can do too.

## Stability

agent-supervisor is at 0.x. Until 1.0, the adapter interface, the HTTP API, the config file, and
the database schema may change in any minor release (0.3 to 0.4, and so on); the release notes list
what changed. Pin the version you build on.
