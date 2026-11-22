# Contributing

Thanks for your interest. agent-supervisor is maintained by one person, so the most useful
contributions are small and focused: a bug report with steps to reproduce, a fix with a test, a
new [adapter](docs/guide/adapters.md) published as your own package.

## Before you start

- **Bugs**: open an issue with the version (`uv tool list` shows it), your OS, the agent, and what
  happened. The task's **Events** tab and the daemon's log usually show where it went wrong.
- **Features**: open an issue first and describe the problem, before writing code. The
  [roadmap](docs/01-roadmap.md) (in Chinese) has the planned order; some things are left out on
  purpose, and the [decision log](docs/03-decisions.md) says why.
- **A new agent** does not need to be in this repo: adapters are ordinary Python packages found
  through an entry point. See [Writing an adapter](docs/guide/adapters.md).
- **Security issues**: do not open an issue; see [SECURITY.md](SECURITY.md).

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Node.js 22 (only for the board).

```sh
git clone https://github.com/ppoozine/agent-supervisor
cd agent-supervisor
uv sync
(cd web && npm install && npm run build)   # builds the board into src/agent_supervisor/web_dist
uv run agsv serve                         # http://127.0.0.1:8765
```

To work on the board with live reload, keep the daemon running and start Vite; it serves the board
at <http://localhost:5173> and forwards `/api` to the daemon on port 8765:

```sh
cd web && npm run dev
```

To try changes without touching your real data, point the daemon at another directory and port with
a config file:

```toml
# dev.toml
data_dir = "~/agsv-dev"
api_port = 8766
```

```sh
uv run agsv --config dev.toml serve
```

## Checks

CI runs these on macOS and Linux; run them before opening a pull request:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest

cd web
npm test
npm run build
```

Tests never start a real agent. They use fake adapters that replay recorded events, and fake `git`
and `gh` runners where needed. New behavior needs a test.

## Conventions

- **Language**: code, comments, docstrings, the board's text, and everything under `docs/guide/` are
  in English. The design documents in `docs/` are in Chinese ([what they are](docs/README.md));
  you do not need to read or update them to contribute.
- **Python**: 3.13, full type hints, a one-line docstring on public functions, asyncio throughout.
  No ORM; SQL lives in `db.py`.
- **Calls to outside programs** (`git`, `gh`, the agents) stay in their own modules (`worktree.py`,
  `github.py`, the adapters) so tests can replace them.
- **The runner talks to agents only through the adapter interface.** Nothing specific to one agent
  belongs in `runner.py`.
- **The event log is append-only.** Never update or delete rows in `events`.
- **Board columns are derived, not stored.** Change `status.py`, not the database.
- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/):
  `fix(runner): stop the run before archiving`. Imperative, lower case, no period.
- **One change per pull request.** Say what it changes and why, and how you tested it.

If you change the adapter interface or the HTTP API, update
[`docs/guide/adapters.md`](docs/guide/adapters.md) or [`docs/guide/http-api.md`](docs/guide/http-api.md)
in the same pull request.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the same as the project.
