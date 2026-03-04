import { escapeHtml } from "../rendering.js";
import { getSelectedContextStack } from "../selectors.js";

function renderContextGroup(title, entries) {
  if (!entries.length) {
    return `
      <section class="context-panel-group">
        <h3>${escapeHtml(title)}</h3>
        <div class="context-empty">None</div>
      </section>
    `;
  }
  return `
    <section class="context-panel-group">
      <h3>${escapeHtml(title)}</h3>
      <div class="context-stack">
        ${entries
          .map((entry) => {
            const turn = entry.snapshot.turn;
            const responsePreview = entry.snapshot.summary?.previewShort || "No response yet.";
            const importMeta = entry.kind === "import" && entry.mergeMode
              ? ` · ${entry.mergeMode} · ${entry.sourceNodeCount || 1} turn${entry.sourceNodeCount === 1 ? "" : "s"}`
              : "";
            return `
              <button type="button" class="context-chip ${entry.isSelected ? "is-selected" : ""}" data-context-node="${entry.nodeId}">
                <span class="context-chip-meta">${escapeHtml(entry.snapshot.branchLabel)} | T${turn.idx}${escapeHtml(importMeta)}</span>
                <span class="context-chip-title">${escapeHtml(entry.snapshot.promptSummary)}</span>
                <span class="context-chip-preview">${escapeHtml(responsePreview)}</span>
              </button>
            `;
          })
          .join("")}
      </div>
    </section>
  `;
}

export function renderContextPanel(container, state, handlers) {
  const entries = getSelectedContextStack(state);
  if (!entries.length) {
    container.innerHTML = '<div class="empty-state">Select a turn to inspect its active context.</div>';
    return;
  }

  const selectedNodeId = state.selectedNodeId;
  const lineage = entries
    .filter((entry) => entry.kind === "lineage")
    .map((entry) => ({ ...entry, isSelected: entry.nodeId === selectedNodeId }));
  const imports = entries
    .filter((entry) => entry.kind === "import")
    .map((entry) => ({ ...entry, isSelected: entry.nodeId === selectedNodeId }));

  container.innerHTML = `
    <div class="context-panel">
      <div class="context-panel-header">
        <h2>Current Context</h2>
        <span>${lineage.length} path | ${imports.length} import${imports.length === 1 ? "" : "s"}</span>
      </div>
      ${renderContextGroup("Path", lineage)}
      ${renderContextGroup("Imports", imports)}
    </div>
  `;

  container.querySelectorAll("[data-context-node]").forEach((button) => {
    button.addEventListener("click", () => {
      handlers.onSelectNode?.(button.dataset.contextNode);
    });
  });
}
