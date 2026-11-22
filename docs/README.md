# Documentation

## For users and adapter authors (English)

| Document | What it covers |
| --- | --- |
| [guide/usage.md](guide/usage.md) | The board's columns and labels, the config file, every limit |
| [guide/trust-model.md](guide/trust-model.md) | What is and is not protected, and the known limitations |
| [guide/adapters.md](guide/adapters.md) | Writing an adapter for another coding agent |
| [guide/http-api.md](guide/http-api.md) | The HTTP API the board uses |

## Design documents (Traditional Chinese)

Everything else in this directory is in Traditional Chinese. You do not need it to use
agent-supervisor or to write an adapter; the English guides above are complete on their own. A
machine translation is usually enough to follow it.

### `design/`: how it works now

Start here. These describe the current version and are kept up to date.

| Document | What it is |
| --- | --- |
| [design/vision.md](design/vision.md) | What the project is for, what it will not do, and how it differs from similar tools |
| [design/roadmap.md](design/roadmap.md) | The phases: what is done, what comes next, and what each one must achieve |
| [design/spec.md](design/spec.md) | The specification: database tables, the run lifecycle, the board, automatic fixes and their limits, review comments, merge conflicts, recovery, the API, the adapter interface |
| [design/decisions.md](design/decisions.md) | Every decision still in effect, grouped by topic, plus open questions and known issues |
| [design/diagrams.md](design/diagrams.md) | Architecture and flow diagrams (Mermaid) |
| [design/claude-sdk-notes.md](design/claude-sdk-notes.md) | Research and tests of the Claude Agent SDK: streaming, sessions, limits, permissions |
| [design/codex-notes.md](design/codex-notes.md) | Research and tests of the Codex CLI: `codex exec`, its JSON events, sandbox, resuming |

### `history/`: how it got here

| Document | What it is |
| --- | --- |
| [history/decision-log.md](history/decision-log.md) | The full decision log: each decision, the options not taken, and why. Append-only, so it also holds decisions that were later overturned; `design/decisions.md` lists the ones in effect |
| [history/plans/](history/plans/) | Records of planning discussions: making the project public (`09`), and the plan after `0.3` (`11`), including a look at how Agent Orchestrator handles the same problems |

### Task sheets

The project is developed with an AI coding agent (Claude Code). Each phase is split into task
sheets that are handed to the agent one task at a time, with a plan agreed before any code. The
sheets are deleted once their phase is done; the ones for phases 1 to 4 are in the
[`v0.3.0` tag](https://github.com/ppoozine/agent-supervisor/tree/v0.3.0/docs). The current one is
[design/phase5-tasks.md](design/phase5-tasks.md).
