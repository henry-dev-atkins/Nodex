# Codex UI Wrapper

Local-only wrapper around `codex app-server` with:

- FastAPI REST API plus WebSocket replay/live stream
- SQLite persistence for threads, turns, events, approvals, and import previews
- Browser UI for thread selection, branch graph, transcript streaming, composer, approvals, and copied-context import
- Explicit approval flow only, never auto-approved

## Status

The backend and UI are implemented and have been validated against a real local Codex install for:

- startup
- thread creation
- turn execution
- fork
- import preview and commit
- approval approve and deny flows

The frontend is a static FastAPI-served UI built with HTML, CSS, and ES modules. The original React/Vite plan remains in `source_plan.md` as a historical design reference.

## Key Docs

- `CONTEXT_DUMP.md`: current project state, resume guidance, and latest session notes
- `source_plan.md`: original design plan and reference contract
- `SECURITY.md`: local-only and token-auth rules

## Run

```powershell
.\run.cmd
```

Canonical CLI:

```powershell
python -m codex_ui dev
```

The app binds to `127.0.0.1:8787` by default and opens a browser window unless `--no-browser` is passed. Any extra args can be forwarded through `run.cmd`, for example `.\run.cmd --no-browser --port 8788`.

Runtime note:

- normal terminal execution is the supported path on this machine
- sandboxed subprocess startup on Windows can still hit `WinError 5` when spawning `codex app-server`

## Tests

```powershell
python -m pytest backend/tests -q
```

The tests use a fake Codex harness and do not require network access or a live Codex session.
