# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub:
**Security → Report a vulnerability** on this repository
([direct link](https://github.com/ppoozine/agent-supervisor/security/advisories/new)).
Do not open a public issue for them.

Include what you found, how to reproduce it, and what an attacker could do with it. The project is
maintained by one person, so a reply may take a few days.

## Supported versions

Only the latest release gets fixes. agent-supervisor is at 0.x; there are no backports.

## What counts

agent-supervisor deliberately runs coding agents with your user's access, and its permission policy
is not a security boundary. The [trust model](docs/guide/trust-model.md) lists what is and is not
enforced. Things it already says are not protected (for example, an agent getting around a denied
command prefix, or a local program calling the API) are known limitations, not vulnerabilities.

Please do report, for example:

- A way for a web page, or anything that is not a local program, to call the daemon's API.
- A way to make the supervisor itself (not the agent) push, delete, or run something it should not.
- Credentials leaking into the database, the logs, the board, or API responses through the
  supervisor's own code.
- A limit that the trust model or the [usage guide](docs/guide/usage.md) says is enforced but is not.
