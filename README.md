# Codex UI Wrapper

Local-only wrapper around `codex app-server` with:

- FastAPI REST API plus WebSocket replay/live stream
- SQLite persistence for threads, turns, events, approvals, and import previews
- Browser UI for conversation selection, direct DAG-driven child-turn creation, compact transcripts, and inline approvals
- Explicit approval flow only, never auto-approved

## Design Docs

- Current redesign target: [docs/UI_REDESIGN_SPEC.md](/C:/Users/Henry/PersonalProjects/codex-wrapper/docs/UI_REDESIGN_SPEC.md)
- Execution plan: [docs/IMPLEMENTATION_PLAN.md](/C:/Users/Henry/PersonalProjects/codex-wrapper/docs/IMPLEMENTATION_PLAN.md)

The README describes the current shipped behavior. The spec and implementation plan record the design constraints and execution structure behind the current UI.

## Terms

- Conversation: the full tree rooted at the first branch.
- Main: the root branch of a conversation.
- Branch: any continuation line within the conversation DAG, labeled `Main`, `Branch 1`, `Branch 2`, and so on.
- Turn: one user prompt plus the assistant work and resulting response for that step.

## Current UI

- Left sidebar: compact conversation rows with branch/turn counts and status dots
- Header: one-line conversation title plus current `Main / Branch n` label, turn, and connection-status dot
- Graph: zoomable and pannable vertical DAG with prompt-summary boxes, draggable branch lanes, solid lineage edges, dashed imported-context edges, and drag handles for child-turn creation or merge-back into another branch head
- Transcript: compact `Tn` rows with summarized prompt previews, response previews, inherited parent-context rows, and scrollable expanded responses
- Approvals: inline inside the transcript, never modal auto-approval
- Child-turn creation: drag from one turn node onto another, inspect the generated transfer blob, then create the linked child turn

Context imports are copied into the created child turn as prompt text, but the new turn is also linked back to its source turn(s) so the DAG can show provenance.

## Runtime Limits

- Maximum active Codex sessions: `4`
- Idle session eviction: `10` minutes
- Eviction policy: least recently used idle session
- Crash handling: one automatic resume attempt
- Resume path: `thread/resume` when a thread is reopened

## Architecture

- `backend/`: FastAPI app, SQLite persistence, Codex process/session management, REST API, WebSocket hub, tests
- `frontend/`: static HTML/CSS/ES module UI served by the backend
- `backend/tests/fixtures/schema/`: test-only Codex schema fixture used by the fake CLI harness
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

## Supported Flows

- Create a conversation
- Start or continue a turn on the selected branch
- Branch from an earlier turn by focusing that node and replying
- Create a linked child turn by dragging one DAG node onto another
- Merge a side branch back into another branch head by dragging across branches
- Approve or deny Codex file-change / command requests
