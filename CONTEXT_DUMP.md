# Context Dump

Date: 2026-03-03
Workspace: `C:\Users\Henry\PersonalProjects\codex-wrapper`

## Session state

The project is now a working local-only Codex wrapper with:

- FastAPI backend
- SQLite persistence
- token-protected REST and WebSocket access
- multi-thread Codex session management
- approval handling
- fork and copied-context import
- static browser UI served by the backend

This is no longer a planning-only repo. The main end-to-end flow has been implemented and exercised against a real local Codex install.

## Where we came from

The repo originally started from `source_plan.md`.

That plan assumed:

- Python backend
- React + Vite frontend
- Codex integration through `codex app-server`

During implementation, the environment changed the frontend choice:

- Python tooling was available
- `codex` was installed
- Node and NPM were not installed

So the frontend was implemented as plain HTML, CSS, and ES modules served directly by FastAPI instead of React/Vite.

The live Codex protocol was also generated and inspected using:

- `.codex_schema/`
- `.codex_ts/`

That replaced earlier guesses with the installed CLI's actual request, response, notification, and approval shapes.

## What is implemented

### Backend

Key backend files:

- `backend/app/main.py`
- `backend/app/api.py`
- `backend/app/codex_manager.py`
- `backend/app/codex_rpc.py`
- `backend/app/db.py`
- `backend/app/ws.py`
- `backend/app/settings.py`

Implemented behavior:

- local-only FastAPI app
- bearer-token REST auth
- token-protected WebSocket replay/live stream
- SQLite tables for threads, turns, events, approvals, and import previews
- Codex CLI version/schema validation on startup
- thread start, resume, fork, and turn start
- approval capture and approval response
- import preview and import commit
- replay cursor support with persisted global `eventId`
- per-turn `seq` ordering
- session eviction and resume behavior

### Frontend

Key frontend files:

- `frontend/index.html`
- `frontend/src/main.js`
- `frontend/src/store.js`
- `frontend/src/styles.css`
- `frontend/src/components/ThreadList.js`
- `frontend/src/components/GraphView.js`
- `frontend/src/components/Transcript.js`
- `frontend/src/components/Composer.js`
- `frontend/src/components/ApprovalModal.js`
- `frontend/src/components/ImportPreviewModal.js`

The current UI now provides:

- thread list with branch indentation
- larger branch map
- readable conversation transcript
- internal transcript scrolling
- composer
- approval modal
- import preview modal

The transcript no longer dumps raw JSON. It renders readable assistant output, commentary, reasoning, web searches, commands, file changes, and approvals.

## What was validated live

The wrapper has been run against the installed Codex runtime and validated for:

- server startup
- `/health`
- `/api/bootstrap`
- thread creation
- real turn submission
- real fork
- import preview
- import commit
- real approval `approve`
- real approval `deny`

Approval validation matters because that was the last major live-runtime blocker earlier in the session. The approval response path is now confirmed to round-trip correctly against real Codex.

## Test status

Implemented tests:

- `backend/tests/test_db_and_manager.py`
- `backend/tests/test_integration_harness.py`
- `backend/tests/fake_codex_cli.py`

Validated commands this session:

- `python -m pytest backend/tests/test_integration_harness.py backend/tests/test_db_and_manager.py -q -p no:cacheprovider`
- `python -m compileall backend\app backend\tests codex_ui`

Those passed during the implementation session before this cleanup pass.

## Latest session work

This closing session focused on frontend cleanup and repo housekeeping.

### UI cleanup completed

- Simplified transcript rendering so live messages are readable
- Enlarged the graph area and made branch lanes clearer
- Moved scrolling into the transcript panel
- Simplified thread cards
- Simplified approval modal copy
- Fixed composer busy-state handling for live `inProgress` turns
- Added display-side text normalization for stored mojibake from session history

Relevant files updated in this session:

- `frontend/src/components/Transcript.js`
- `frontend/src/components/GraphView.js`
- `frontend/src/components/ThreadList.js`
- `frontend/src/components/ApprovalModal.js`
- `frontend/src/components/Composer.js`
- `frontend/src/styles.css`
- `frontend/index.html`

### Documentation cleanup completed

- Replaced the temporary `tmp_CONTEXT_DUMP.md` with this `CONTEXT_DUMP.md`
- Updated `README.md` to point at the current runtime and docs
- Added repo hygiene rules in `.gitignore`
- Added a current-status note to `source_plan.md`

### Workspace cleanup completed

- Removed the temporary context-dump file and transient log files
- Stopped the local wrapper servers started during validation
- Removed most live-validation and screenshot artifacts
- Left `.codex_ui_data/` intact as the local app data directory

Cleanup limitation:

- a small set of pytest scratch directories created by the surrounding sandbox tooling could not be removed even with escalated ownership and ACL attempts
- these remaining directories are ignored by `.gitignore`:
  - `.tmp/pytest`
  - `.tmp/pytest-of-Henry`
  - `.tmp_test_artifacts/tmpjy_7d53a`
  - `.tmp_test_artifacts/tmpvpuhejin`
  - `.tmp_test_artifacts/tmpyne0emn2`
  - `backend/pytest-cache-files-*`

## Known caveats

- The frontend is plain JS, not React/Vite.
- The repo is not currently a git repository in this workspace snapshot.
- Sandboxed Windows subprocess creation can still hit `WinError 5` for Codex child processes. Normal terminal execution works.
- Chrome headless visual verification required escalation outside the sandbox.

## Rules and operating assumptions

- Local only by default
- Default bind address is `127.0.0.1`
- REST uses bearer token auth
- WebSocket uses token query auth
- Approvals are explicit only
- Import is preview/edit/confirm, not an automatic merge
- Thread IDs and turn IDs are opaque
- `eventId` is the global replay cursor
- `seq` is ordering inside a turn
- The backend is defensive about Codex protocol drift and validates required contract pieces on startup

## Recommended resume point

If work resumes next session, start from:

1. `README.md`
2. `CONTEXT_DUMP.md`
3. `backend/app/codex_manager.py`
4. `frontend/src/components/Transcript.js`
5. `frontend/src/components/GraphView.js`

## Immediate next candidates

If development continues later, the highest-value next steps are:

1. Add markdown rendering for assistant messages
2. Add transcript collapse/expand per turn
3. Add more deterministic UI-level regression coverage if a browser test harness is introduced
