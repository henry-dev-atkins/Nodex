# Security

This project is local-only by design.

- Default bind address: `127.0.0.1`
- REST authentication: random bearer token generated on first run
- WebSocket authentication: token query parameter
- Approval policy: explicit user approval only
- Import flow: preview and edit gate before copied context is submitted to another thread

Report vulnerabilities privately before any public disclosure:

- GitHub Security Advisories: https://github.com/henry-dev-atkins/codex-wrapper/security/advisories/new

We aim to acknowledge reports within 72 hours.
