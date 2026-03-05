# Codex UI Wrapper UI Redesign Spec

Version: `2.0 UI Simplification Pass`

Goal: sharper, denser, lower-noise interface with direct DAG manipulation.

Current shipped refinements on top of this spec:

- The app now defaults to a transcript-first `Focus` mode and keeps DAG editing in a separate `Map` mode.
- Structural actions are explicit in the main shell: `Continue`, `Branch`, `Merge Into...`, and `Compare`.
- The selected turn now exposes a `Current Context` stack so inherited and imported context can be inspected directly.
- Branches are labeled `Main`, `Branch 1`, `Branch 2`, and so on.
- Branch transcripts include inherited parent-lineage rows.
- Branch lanes can be manually reordered in the DAG and that order persists locally.
- Branch creation now requires replayable lineage history and validates resumed branch snapshots to prevent empty ghost branches.
- DAG node selection now explicitly distinguishes click from drag so positioning nodes does not cause accidental reselection.
- Empty start nodes now expose direct delete actions for empty branches and empty root conversations.

## 1. Global Visual System Changes

### 1.1 Border Radius

- Panels: `16px -> 6px`
- Buttons: `12px -> 4px`
- Remove exaggerated rounding everywhere.

### 1.2 Shadows

- Remove heavy shadows.
- Use subtle `1px` neutral borders instead of elevation.

### 1.3 Background

- Remove gradients.
- Use a flat neutral application background.
- Keep only a slight tint behind the graph canvas.

### 1.4 Spacing System

Adopt a strict `8px` grid.

- `8px` base unit
- `16px` standard padding
- `24px` section separation
- `40px` maximum vertical spacing between major panels

## 2. Header Simplification

Remove:

- `Conversation Graph`
- Multi-line thread metadata block
- Separate status section

Replace with a single-line header:

```text
Untitled Thread - T3                    Live *
```

Rules:

- Thread name left-aligned
- Current turn inline
- Live status shown as a small dot only
- No extra explanatory text

## 3. Remove Redundant Status Blocks

Remove the transcript focus/status block:

- `Focus: Turn 3`
- `0 included context links`
- `Blue rows include imported context...`

Replace with:

- `Branch 2 | T3 | 0 imports`
- Or just `T3` with a small link icon if imports exist

Any explanation moves to tooltip-on-hover, not persistent text.

## 4. Sidebar Density Improvements

Change conversation entries from tall cards to compact rows.

Old:

```text
Thread Title
Conversation root
3 branches 10 turns
Idle
```

New:

```text
Thread Title
3 branches | 10 turns        *
```

Rules:

- Counts should be compact but still explicit
- Status shown as a small dot
- No rounded card containers
- Hover highlight only

## 5. Graph Panel Simplification

Remove:

- `Branch DAG` title
- Heavy graph container frame
- Excess padding

New graph panel rules:

- Graph spans full width
- `16px` horizontal padding
- Very light `1px` border at the top only
- Slightly tinted canvas background such as `#FAFAFA`

## 6. Direct DAG Interaction

Remove:

- Context-selection checkboxes
- `Blend Context` button

Add interactive edge linking.

Behavior:

1. Hover a turn node to reveal a small connector handle (`4px` circle).
2. Drag from turn `A` to turn `B`.
3. Show a temporary link while dragging.
4. On drop, open a modal.

Modal:

```text
Create Child Turn
Parents:
[x] T2
[x] T5
[ Create ]
```

On create:

- A new child turn is created
- Solid lines represent primary parents
- Dashed lines represent imported context

Rules:

- Only allow linking downward in time
- Prevent cycles
- If two parents are selected, the child inherits from both

Graph styling:

- Thin `1.5px` connectors
- No glow
- Active node highlighted only by a stronger border

## 7. Transcript Compaction

Change turn headers from:

```text
Turn 1 [Completed] [Completed]
```

To:

```text
T1  done
```

Rules:

- Hover reveals extra actions via `...`
- No persistent action buttons
- Remove the `Extra Info` button
- Expand inline on click
- Each row should show a short prompt preview and short response preview even when collapsed

Agent streaming:

- Batch deltas every `75ms`
- Combine into a single message block

## 8. Approval UI Simplification

Approvals should render inline within the transcript instead of as large full-width blocks.

Rules:

- Subtle colored left border
- Minimal inline `Approve` / `Deny` actions
- Never auto-approve

## 9. Graph Layout Rules

Layout:

- `X` axis = branch lane
- `Y` axis = turn index
- Parent-child links are solid
- Imported links are dashed
- No drop shadows
- Nodes are perfect circles or `4px` rounded squares

Node styling:

- Default: white fill, `1px` border
- Active: darker `2px` border
- Running: accent-color ring
- Nodes should display a truncated prompt preview so the graph is readable without relying on turn numbers alone

## 10. Process Limits for Scaling

Add explicit runtime limits:

- Maximum active thread processes: `4`
- Idle eviction after `10` minutes
- LRU shutdown policy
- Auto-restart once on crash
- Resume via `thread/resume` when the user reopens a thread

## 11. Cross-Platform Dev Runner

Remove:

- `scripts/dev.sh`

Use the Python entrypoint:

```powershell
python -m codex_ui dev
```

Responsibilities:

- Start backend
- Start optional frontend dev server if needed
- Open browser
- Work on macOS, Linux, and Windows

## 12. Import Context Redesign

Before imported context is submitted:

1. Backend builds the transfer blob.
2. Run detection:
   - regex token patterns
   - entropy check
3. Send preview to the frontend modal.
4. User can edit before final submission.
5. Highlight suspected secrets.

No silent redaction.

## 13. Overall Aesthetic Goals

- Fewer shapes
- Fewer labels
- More whitespace efficiency
- Monochrome base palette
- Accent color used only for:
  - active node
  - running state
  - imported link indicator

Design principle:

If a label only explains something already visually obvious, remove it.
