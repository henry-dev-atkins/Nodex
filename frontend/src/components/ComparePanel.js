import { escapeHtml } from "../rendering.js";
import { getCompareSnapshots } from "../selectors.js";

function renderCard(label, snapshot, currentSelectedNodeId) {
  if (!snapshot?.turn) {
    return `
      <article class="compare-card is-empty">
        <div class="compare-card-label">${escapeHtml(label)}</div>
        <div class="compare-empty">Choose a turn to compare.</div>
      </article>
    `;
  }
  const summary = snapshot.summary || {};
  return `
    <article class="compare-card ${currentSelectedNodeId === snapshot.nodeId ? "is-current" : ""}">
      <div class="compare-card-label">${escapeHtml(label)}</div>
      <div class="compare-card-meta">${escapeHtml(snapshot.branchLabel)} | T${snapshot.turn.idx}</div>
      <div class="compare-card-title">${escapeHtml(snapshot.promptSummary)}</div>
      <div class="compare-card-section">
        <strong>Prompt</strong>
        <p>${escapeHtml(summary.promptShort || snapshot.promptSummary)}</p>
      </div>
      <div class="compare-card-section">
        <strong>Response</strong>
        <p>${escapeHtml(summary.previewShort || "No response yet.")}</p>
      </div>
    </article>
  `;
}

export function renderComparePanel(container, state, handlers) {
  if (!state.compare.open) {
    container.innerHTML = "";
    return;
  }

  const { left, right } = getCompareSnapshots(state);
  const waitingForRight = Boolean(left && !right);
  container.innerHTML = `
    <section class="compare-panel">
      <div class="compare-panel-header">
        <div>
          <h2>Compare Turns</h2>
          <span>${waitingForRight ? "Select another turn, then press Compare again." : "Side-by-side prompt and response summaries."}</span>
        </div>
        <div class="compare-panel-actions">
          <button type="button" class="ghost-button" data-compare-action="use-current" ${left ? "" : "disabled"}>Use Current As Right</button>
          <button type="button" class="ghost-button" data-compare-action="swap" ${left && right ? "" : "disabled"}>Swap</button>
          <button type="button" class="ghost-button" data-compare-action="close">Close</button>
        </div>
      </div>
      <div class="compare-grid">
        ${renderCard("Left", left, state.selectedNodeId)}
        ${renderCard("Right", right, state.selectedNodeId)}
      </div>
    </section>
  `;

  container.querySelectorAll("[data-compare-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.dataset.compareAction;
      if (action === "use-current") {
        handlers.onUseCurrent?.();
      } else if (action === "swap") {
        handlers.onSwap?.();
      } else if (action === "close") {
        handlers.onClose?.();
      }
    });
  });
}
