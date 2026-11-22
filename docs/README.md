# Documentation

## For users and adapter authors (English)

| Document | What it covers |
| --- | --- |
| [guide/usage.md](guide/usage.md) | The board's columns and labels, the config file, every limit |
| [guide/trust-model.md](guide/trust-model.md) | What is and is not protected, and the known limitations |
| [guide/adapters.md](guide/adapters.md) | Writing an adapter for another coding agent |
| [guide/http-api.md](guide/http-api.md) | The HTTP API the board uses |

## Design documents (Traditional Chinese)

Everything else in this directory is in Traditional Chinese. These are the project's working
documents: the specification the code follows, every design decision with the alternatives that
were rejected, and the research done before each agent was integrated. They are kept as a record of
how and why the tool was built this way, and are still written in Chinese as the project goes on.
You do not need them to use agent-supervisor or to write an adapter; the English guides above are
complete on their own. A machine translation is usually enough to follow them.

| Document | What it is |
| --- | --- |
| [00-vision.md](00-vision.md) | What the project is for, and how it relates to similar tools |
| [01-roadmap.md](01-roadmap.md) | The phases, what each one delivers, and what is left for later |
| [02-mvp-spec.md](02-mvp-spec.md) | The specification: database tables, the run lifecycle, the board, automatic fixes and their limits, review comments, merge conflicts, recovery, the API, the adapter interface |
| [03-decisions.md](03-decisions.md) | The decision log: each decision, the options not taken, and why; plus open questions |
| [05-ui-reference.md](05-ui-reference.md) | What the board shows and the reference it was modelled on |
| [06-diagrams.md](06-diagrams.md) | Architecture and flow diagrams (Mermaid) |
| [09-open-source-plan.md](09-open-source-plan.md) | The plan for making the project public: an inventory, positioning, language, platforms, naming, scope |
| [claude-sdk-notes.md](claude-sdk-notes.md) | Research and tests of the Claude Agent SDK: streaming, sessions, limits, permissions |
| [codex-notes.md](codex-notes.md) | Research and tests of the Codex CLI: `codex exec`, its JSON events, sandbox, resuming |

### The task sheets

[04-phase1-tasks.md](04-phase1-tasks.md), [07-phase2-tasks.md](07-phase2-tasks.md),
[08-phase3-tasks.md](08-phase3-tasks.md) and [10-phase4-tasks.md](10-phase4-tasks.md) are the
instructions each phase was built from. The project was developed with an AI coding agent
(Claude Code): each task in these sheets was handed to it as written, one at a time, with a plan
agreed before any code and a list of what the author verified by hand afterwards. They are kept to
show how the work was split up and checked, not as current documentation; where they disagree with
the specification, the specification wins. Many of their "what I will verify" checkboxes were never
ticked. The boxes were not kept up to date, so they do not show what was checked at the time; the
features have been in use and reworked since, and the automated tests are the current record.
