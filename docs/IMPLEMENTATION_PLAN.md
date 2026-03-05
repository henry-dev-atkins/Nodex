# Codex UI Wrapper Implementation Plan

This plan translates the v2 UI simplification spec into concrete workstreams.

Source spec: [UI_REDESIGN_SPEC.md](/C:/Users/Henry/PersonalProjects/codex-wrapper/docs/UI_REDESIGN_SPEC.md)

## Objectives

- Reduce UI noise and visual bulk.
- Replace text-driven context blending with direct DAG manipulation.
- Make the active path and active context obvious without requiring graph interpretation.
- Split day-to-day reading from structural DAG editing.
- Add backend/runtime limits required for scaling.
- Keep the documentation aligned with current state and target state.

## Current Status Summary

- `python -m codex_ui dev` already exists and remains the canonical dev runner.
- Context-import preview with secret detection already exists in the backend/frontend flow.
- The simplified v2 interaction model is now the active UI.
- The shipped shell now defaults to `Focus` mode, with `Map` mode for structural DAG work.
- The shipped action model is now explicit: `Continue`, `Branch`, `Merge Into...`, and `Compare`.
- The shipped transcript is paired with a `Current Context` stack so inherited and imported context are visible without reading the DAG first.
- The shipped graph uses vertical branch lanes with prompt-labeled node boxes, `Main / Branch n` naming, and persisted manual lane ordering because that proved clearer than number-only nodes.
- The shipped graph interaction now uses a click-vs-drag threshold so node repositioning and node selection do not interfere with each other.
- The shipped map context menu now includes direct empty-start-node deletion for empty branches and empty root conversations.
- The shipped branch flow now reconstructs replayable lineage history from stored turns/events and rejects empty resume snapshots to prevent ghost branches.
- The shipped transcript now uses chat-style alignment (user right, assistant left) while still including inherited and imported rows in-order with subtle differentiated shading.
- The shipped transcript folds commentary into reasoning by default and keeps reasoning/command panels collapsed unless explicitly expanded.
- The shipped transcript clamps message bodies to two lines by default and exposes compact per-message `more/less` controls.
- The shipped transcript allows only one auxiliary panel (`Reasoning` or `Commands`) open at a time per turn.
- The main remaining purpose of this document is traceability from implementation back to design intent.

## Phase 1: Visual System Compression

Status: `implemented`

Scope:

- Reduce panel radius to `6px`
- Reduce button radius to `4px`
- Remove gradients and heavy shadows
- Move to flat neutral backgrounds and thin borders
- Normalize spacing to the `8px` grid

Acceptance criteria:

- No heavy shadow treatment remains in the main app shell
- The app uses flat surfaces with subtle borders
- Panel, button, and spacing values match the v2 spec

## Phase 2: Header, Sidebar, and Transcript Density Pass

Status: `implemented`

Scope:

- Replace the current multi-line header with a one-line thread/turn/status row
- Remove the transcript focus explanation block
- Replace sidebar cards with compact list rows
- Shift transcript presentation to chat-style left/right message alignment
- Retain branch lineage/import visibility with compact inline provenance markers
- Clamp message previews and surface compact per-message expand/collapse controls

Acceptance criteria:

- Header fits on one line with thread name, active turn, and status dot
- Sidebar entries render as dense rows, not cards
- Transcript reads like a chat flow while preserving branch provenance context
- Default message rendering is dense, with two-line previews and explicit expand controls

## Phase 3: DAG Interaction Redesign

Status: `implemented`

Scope:

- Remove transcript checkboxes and the `Blend Context` button
- Add graph connector handles on hover
- Support drag from one turn node to another
- Show temporary drag line
- Open a create-child modal on valid drop
- Render solid primary-parent edges and dashed imported-context edges
- Keep node selection and node drag behavior disambiguated with a movement threshold
- Support deleting empty branches/conversations directly from map start nodes
- Enforce downward-only linking and cycle prevention

Acceptance criteria:

- Users can create a linked child turn entirely from the DAG
- Imported context links render distinctly from primary lineage
- Empty branch roots can be cleaned up directly from the DAG without leaving map mode
- Invalid links are blocked before commit

## Phase 4: Transcript and Approval Simplification

Status: `implemented`

Scope:

- Render approvals attached to assistant message bubbles inside transcript content
- Keep approval presentation compact so it reads as part of the assistant output
- Keep approval actions minimal and inline
- Batch agent streaming deltas at `75ms`
- Collapse streamed output into a single message block
- Keep reasoning and command details collapsed by default behind explicit toggles
- Ensure at most one auxiliary detail panel is open at a time per turn

Acceptance criteria:

- Approval UI no longer dominates transcript width and remains visually tied to related assistant output
- Streaming produces one coherent assistant block per turn
- Reasoning/command details are opt-in and do not expand automatically with the main message
- No auto-approval behavior is introduced

## Phase 5: Graph Layout and Styling Rules

Status: `implemented`

Scope:

- Align graph layout to `X = branch lane`, `Y = turn index`
- Remove visual effects such as glow and shadows
- Use white nodes with thin borders by default
- Use stronger border for active state and accent ring for running state
- Show prompt previews inside node boxes instead of number-only nodes

Acceptance criteria:

- Node shapes, borders, and connector thickness match the simplified spec
- Imported links are clearly dashed and primary edges are solid
- Active and running states are visually distinguishable without extra labels
- Nodes remain readable without needing to decode turn numbers alone

## Phase 6: Backend Runtime Limits

Status: `implemented`

Scope:

- Cap active thread processes at `4`
- Add `10` minute idle eviction
- Apply LRU shutdown policy
- Auto-restart once after a crash
- Resume with `thread/resume` when a thread is reopened

Acceptance criteria:

- Runtime enforces the process cap
- Idle processes are reclaimed predictably
- Reopening a previously evicted thread resumes cleanly
- Branch-from-turn reconstruction fails fast when replayable history is unavailable and rejects empty resumed snapshots

## Phase 7: Documentation and Tooling Alignment

Status: `implemented`

Scope:

- Keep `python -m codex_ui dev` as the canonical runner
- Retain `run.cmd` only as a thin convenience wrapper
- Keep README aligned with the current shipped state
- Keep the redesign spec and implementation plan as the canonical forward-looking docs

Acceptance criteria:

- README clearly distinguishes current product behavior from planned v2 behavior
- Spec and plan are easy to find from the repo root

## Suggested Implementation Order

1. Phase 1: visual system compression
2. Phase 2: header, sidebar, and transcript density
3. Phase 5: graph layout/style alignment
4. Phase 3: direct DAG interaction redesign
5. Phase 4: transcript and approval simplification
6. Phase 6: backend runtime limits
7. Phase 7: final documentation pass

## Risks

- Multi-parent child creation is not just a UI change; it requires a clear backend lineage model.
- Reworking the graph interaction without simplifying the data model first can create partial or misleading provenance.
- Runtime process caps and auto-resume behavior need tests, not just documentation.

## Done Criteria

- The UI visually matches the v2 simplification spec.
- Context merging/importing is DAG-native instead of checkbox-driven.
- Transcript and approvals are dense and low-noise.
- Runtime process management is bounded and tested.
- README, spec, and plan stay aligned.
- Branch creation, DAG node interaction, and empty-branch cleanup behavior are explicitly documented as core features.
- Chat-style transcript alignment, line-clamped message previews, and compact auxiliary detail toggles are documented as core behavior.
