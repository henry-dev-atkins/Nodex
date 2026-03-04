# Codex UI Wrapper Context Dump

Last updated: 2026-03-03

## Purpose

This file is a direct handover note for the current repo state. It records:

- the stable baseline
- the UI experiment branches
- the current uncommitted trial
- the most relevant files and behaviors
- what has and has not been verified

## Repo Snapshot

- Active branch during handover: `ui-trial-studio-theme`
- Current `HEAD`: `ac38a3a`
- Branch purpose: second large UI experiment branch
- Worktree status at handover: modified, not committed

Current modified files:

- `frontend/src/components/ActionBar.js`
- `frontend/src/components/AppShell.js`
- `frontend/src/styles.css`

## Important Branches

- `master` at `f1e65a5`
  - stable branch/transcript product before the large shell rewrites
  - commit message: `Show imported turns in transcript history`

- `ui-trial-shell-rewrite` at `8c39eea`
  - first major UI refactor/reorg
  - commit message: `Create alternate UI shell rewrite`

- `ui-trial-studio-theme` at `ac38a3a`
  - second visual trial checkpoint
  - commit message: `Create studio theme UI trial`

## Current Uncommitted Trial

This is the work sitting on top of `ui-trial-studio-theme`.

Intent:

- stop doing color-only variations
- actually rearrange the same feature set into a different information hierarchy

Current layout experiment:

- left sidebar remains conversation selection
- new left command dock inside the main shell
  - actions live here
  - compare lives here
- top context ribbon spans the active work area
- transcript and map live underneath the ribbon

The idea is to change the interaction order from:

- header
- actions/context/compare near transcript
- transcript or graph

to:

- choose actions from a dock
- see active context as a ribbon
- work in transcript or graph below

## Files That Matter Most

### Frontend shell and layout

- `frontend/src/components/AppShell.js`
  - current HTML structure for the entire app shell
  - latest uncommitted trial changed this substantially
  - now uses:
    - `workspace-deck`
    - `command-column`
    - `content-stage`
    - `context-ribbon`

- `frontend/src/layout.js`
  - owns persistent layout state
  - sidebar width and map graph width resizers
  - still compatible with the current shell

- `frontend/src/main.js`
  - main orchestration only
  - bootstraps shell, store, layout controller, UI actions
  - renders:
    - thread list
    - action bar
    - compare panel
    - context panel
    - graph
    - transcript(s)
    - import modal

### Frontend behavior surfaces

- `frontend/src/components/Transcript.js`
  - branch transcript
  - inherited lineage rows
  - imported rows inline in history
  - inline approvals
  - expanded detail blocks
  - bounded scroll for long response bodies

- `frontend/src/components/GraphView.js`
  - vertical branch-lane DAG
  - prompt-labeled nodes
  - zoom and pan
  - lane reordering
  - drag handle linking for child-turn creation
  - solid lineage edges, dashed imported-context edges

- `frontend/src/components/ActionBar.js`
  - explicit structural actions:
    - `Continue`
    - `Branch`
    - `Merge Into...`
    - `Compare`
  - current uncommitted trial changes only layout/presentation, not semantics

- `frontend/src/components/ContextPanel.js`
  - active context stack
  - lineage and imports
  - uses `getSelectedContextStack`
  - current uncommitted trial places this into a top ribbon container

- `frontend/src/components/ComparePanel.js`
  - side-by-side compare summary panel

- `frontend/src/components/ThreadList.js`
  - conversation list in sidebar

- `frontend/src/styles.css`
  - the main file for all visible UI experimentation
  - this is where most of the latest trial lives

### Backend state that the UI depends on

- `backend/app/codex_manager.py`
  - import preview and commit flow
  - persisted context-link metadata
  - branch-vs-continue behavior for imported context

- `backend/app/api.py`
  - bootstrap and approval endpoints

- `backend/app/db.py`
  - persistence for threads, turns, events, approvals

### Selectors/store

- `frontend/src/store.js`
  - selected node
  - compare state
  - forced branch state
  - pending merge state
  - expanded turn key

- `frontend/src/selectors.js`
  - branch labels
  - context stack
  - compare snapshots
  - conversation/root helpers

## What Works

- local app boot flow
- conversation selection
- branch naming:
  - `Main`
  - `Branch 1`
  - `Branch 2`
  - etc.
- branch transcript with inherited lineage rows
- imported turns shown in transcript history
- explicit branch/merge/compare actions
- graph node dragging for child-turn creation
- merge-back into another branch via graph action model
- persisted imported-context provenance edges
- zoom and pan in the DAG
- lane ordering persistence
- inline approvals
- import preview with secret detection/edit gate
- runtime caps:
  - 4 active sessions
  - 10 minute idle eviction
  - LRU reclamation
  - single restart attempt

## What Is Still Weak

- no browser/manual verification was run from this environment
- current UI experimentation is faster than the documentation cadence, so docs can drift without a pass like this one
- the repo now has multiple UI identities across branches and the current worktree is yet another one
- there is still no frontend regression harness
- the user is explicitly in trial-and-error mode, so visual cohesion is intentionally unstable right now

## Verification

Latest backend verification run during handover:

```powershell
python -m pytest backend/tests -q
```

Result:

- `11 passed, 1 warning`

Warning:

- existing Starlette multipart pending-deprecation warning

Not verified:

- browser rendering
- browser interaction feel
- frontend build/lint with `node`

## Current Docs State

Updated in this handover:

- `README.md`
- `docs/UI_REDESIGN_SPEC.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `CONTEXT_DUMP.md` (this file)

Security doc:

- `SECURITY.md` was reviewed
- no content change was required for this handover

## Recommended Next Move

Before doing more UI changes:

1. Review the current uncommitted dock/ribbon trial in a browser.
2. Decide whether to:
   - commit it on `ui-trial-studio-theme`
   - branch it into a fourth experiment branch
   - discard it and continue from `ac38a3a`
3. Only then begin the next visual experiment.

Reason:

- there are already two committed UI experiment branches plus one live uncommitted trial
- without a checkpoint decision, future comparison will get muddy fast

## Minimal Mental Model For The Next Person

- `master` is the safest baseline.
- `ui-trial-shell-rewrite` is the first major shell split.
- `ui-trial-studio-theme` is the second committed visual trial.
- the current worktree on `ui-trial-studio-theme` is a third, uncommitted structural shuffle.
- if you want to continue experimentation cleanly, branch or commit first.
