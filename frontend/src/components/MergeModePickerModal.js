import { escapeHtml, threadLabel } from "../rendering.js";
import { getTurn } from "../selectors.js";

const MODE_OPTIONS = [
  {
    value: "verbose",
    label: "Verbose",
    description: "Full copied branch context.",
  },
  {
    value: "summary",
    label: "Summary",
    description: "Four-sentence condensed branch summary.",
  },
  {
    value: "decision",
    label: "Decision",
    description: "Two-sentence branch decision and rationale.",
  },
  {
    value: "analysis",
    label: "Analysis",
    description: "Short analytical paragraph.",
  },
];

export function renderMergeModePickerModal(container, state, handlers) {
  const picker = state.mergeModePicker;
  if (!picker.open) {
    container.innerHTML = "";
    return;
  }

  const sourceThread = picker.sourceThreadId ? state.threads[picker.sourceThreadId] : null;
  const targetThread = picker.targetThreadId ? state.threads[picker.targetThreadId] : null;
  const sourceTurn = picker.sourceThreadId && picker.sourceTurnId ? getTurn(state, picker.sourceThreadId, picker.sourceTurnId) : null;
  const targetTurn = picker.targetThreadId && picker.targetTurnId ? getTurn(state, picker.targetThreadId, picker.targetTurnId) : null;

  container.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal modal-compact">
        <div class="modal-header">
          <div>
            <h3>Choose Merge Mode</h3>
            <p class="modal-subtle">${escapeHtml(sourceThread ? threadLabel(sourceThread) : "Unknown source")} to ${escapeHtml(targetThread ? threadLabel(targetThread) : "Unknown target")}</p>
          </div>
          <button id="close-merge-mode-picker" class="ghost-button">Close</button>
        </div>
        <div class="modal-parent-list">
          <div class="modal-parent-row">
            <span class="modal-parent-mark">Source</span>
            <span>${sourceTurn ? `T${sourceTurn.idx}` : "Unknown"}</span>
          </div>
          <div class="modal-parent-row">
            <span class="modal-parent-mark">Target</span>
            <span>${targetTurn ? `T${targetTurn.idx}` : "Unknown"}</span>
          </div>
        </div>
        <div class="merge-mode-grid">
          ${MODE_OPTIONS.map((option) => `
            <button type="button" class="merge-mode-card" data-merge-mode="${option.value}">
              <strong>${escapeHtml(option.label)}</strong>
              <span>${escapeHtml(option.description)}</span>
            </button>
          `).join("")}
        </div>
      </div>
    </div>
  `;

  container.querySelector("#close-merge-mode-picker")?.addEventListener("click", handlers.onClose);
  container.querySelectorAll("[data-merge-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      handlers.onSelectMode?.(button.dataset.mergeMode);
    });
  });
}
