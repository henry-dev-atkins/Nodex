import { escapeHtml } from "../rendering.js";
import { getBranchLabel, getHeadTurn, getSelectedNode } from "../selectors.js";

export function renderActionBar(container, state, handlers) {
  const selectedNode = getSelectedNode(state);
  const selectedThread = selectedNode?.thread || null;
  const headTurn = selectedThread ? getHeadTurn(state, selectedThread.threadId) : null;
  const hasTurn = Boolean(selectedNode?.turn);
  const canBranch = Boolean(selectedThread && (selectedNode?.turn || headTurn));
  const canCompare = Boolean(hasTurn);
  const canMerge = Boolean(hasTurn);
  const forcedBranchActive = state.forcedBranchNodeId && state.forcedBranchNodeId === selectedNode?.nodeId;
  const pendingMergeActive = state.pendingMergeSourceNodeId && state.pendingMergeSourceNodeId === selectedNode?.nodeId;
  const branchLabel = selectedThread ? getBranchLabel(state, selectedThread.threadId) : "Branch";
  const summary = pendingMergeActive
    ? "Map mode is armed. Pick a destination turn."
    : forcedBranchActive
      ? `Next send branches from ${branchLabel} ${selectedNode?.turn ? `T${selectedNode.turn.idx}` : "Start"}.`
      : selectedNode?.turn
      ? `${branchLabel} / ${selectedNode.turn.idx === headTurn?.idx ? `T${selectedNode.turn.idx} is current.` : `T${selectedNode.turn.idx} is earlier.`}`
        : "Select a turn to branch, merge, or compare it.";

  container.innerHTML = `
    <section class="action-bar">
      <div class="action-bar-copy">
        <span class="action-bar-summary">${escapeHtml(summary)}</span>
      </div>
      <div class="action-bar-buttons">
        <button type="button" class="primary-button" data-action="continue" ${selectedThread ? "" : "disabled"}>Continue</button>
        <button type="button" class="ghost-button ${forcedBranchActive ? "is-active" : ""}" data-action="branch" ${canBranch ? "" : "disabled"}>Branch</button>
        <button type="button" class="ghost-button ${pendingMergeActive ? "is-active" : ""}" data-action="merge" ${canMerge ? "" : "disabled"}>${pendingMergeActive ? "Cancel Merge" : "Merge Into..."}</button>
        <button type="button" class="ghost-button ${state.compare.open ? "is-active" : ""}" data-action="compare" ${canCompare ? "" : "disabled"}>${state.compare.open && state.compare.leftNodeId && !state.compare.rightNodeId ? "Set Compare" : "Compare"}</button>
      </div>
    </section>
  `;

  container.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.action;
      if (action === "continue") {
        handlers.onContinue?.();
      } else if (action === "branch") {
        handlers.onBranch?.();
      } else if (action === "merge") {
        handlers.onMerge?.();
      } else if (action === "compare") {
        handlers.onCompare?.();
      }
    });
  });
}
