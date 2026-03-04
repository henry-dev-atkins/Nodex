import { escapeHtml, threadLabel } from "../rendering.js";
import { getTurn } from "../selectors.js";

const MODE_OPTIONS = [
  { value: "verbose", label: "Verbose" },
  { value: "summary", label: "Summary" },
  { value: "decision", label: "Decision" },
  { value: "analysis", label: "Analysis" },
];

export function renderImportPreviewModal(container, state, handlers) {
  const modal = state.importModal;
  if (!modal.open) {
    container.innerHTML = "";
    return;
  }

  const previewText = modal.preview?.transferBlob || "";
  const secrets = modal.preview?.suspectedSecrets || [];
  const sourceThread = modal.sourceThreadId ? state.threads[modal.sourceThreadId] : null;
  const targetThread = modal.targetThreadId ? state.threads[modal.targetThreadId] : null;
  const sourceTurn = modal.sourceThreadId && modal.sourceAnchorTurnId
    ? getTurn(state, modal.sourceThreadId, modal.sourceAnchorTurnId)
    : null;
  const targetTurn = modal.targetThreadId && modal.targetTurnId ? getTurn(state, modal.targetThreadId, modal.targetTurnId) : null;

  container.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal">
        <div class="modal-header">
          <div>
            <h3>Create Merged Child Turn</h3>
            <p class="modal-subtle">${escapeHtml(sourceThread ? threadLabel(sourceThread) : "Unknown source")} to ${escapeHtml(targetThread ? threadLabel(targetThread) : "Unknown target")}</p>
          </div>
          <button id="close-import-modal" class="ghost-button">Close</button>
        </div>

        <div class="modal-parent-list">
          <div class="modal-parent-row">
            <span class="modal-parent-mark">Parent</span>
            <span>${targetTurn ? `T${targetTurn.idx}` : "Unknown"}</span>
          </div>
          <div class="modal-parent-row">
            <span class="modal-parent-mark">Import</span>
            <span>${sourceTurn ? `T${sourceTurn.idx}` : "Unknown"}</span>
          </div>
        </div>
        <div class="merge-mode-toggle">
          ${MODE_OPTIONS.map((option) => `
            <button
              type="button"
              class="ghost-button ${modal.mergeMode === option.value ? "is-active" : ""}"
              data-import-merge-mode="${option.value}"
            >
              ${escapeHtml(option.label)}
            </button>
          `).join("")}
        </div>

        ${
          modal.loading
            ? '<div class="modal-loading">Building transfer preview...</div>'
            : ""
        }
        ${
          modal.error
            ? `<p class="import-error">${escapeHtml(modal.error)}</p>`
            : ""
        }
        ${
          !modal.loading && !modal.preview && !modal.error
            ? '<div class="modal-loading">Preparing preview...</div>'
            : ""
        }
        ${
          modal.preview
            ? `
              <div class="context-preview-meta">
                <div class="secret-list">${
                  secrets.length
                    ? secrets.map((secret) => `${secret.label} at ${secret.start}-${secret.end}`).join("\n")
                    : "No obvious secrets detected."
                }</div>
              </div>
              <textarea id="import-preview-text">${previewText}</textarea>
              <div class="modal-actions">
                <button id="cancel-import" class="ghost-button">Cancel</button>
                <button id="commit-import" class="primary-button">Create Child</button>
              </div>
            `
            : ""
        }
      </div>
    </div>
  `;

  container.querySelector("#close-import-modal")?.addEventListener("click", handlers.onClose);
  container.querySelector("#cancel-import")?.addEventListener("click", handlers.onClose);
  container.querySelectorAll("[data-import-merge-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextMode = button.dataset.importMergeMode;
      if (nextMode && nextMode !== modal.mergeMode) {
        handlers.onSelectMode?.(nextMode);
      }
    });
  });
  container.querySelector("#commit-import")?.addEventListener("click", () => {
    const edited = container.querySelector("#import-preview-text")?.value || "";
    handlers.onCommit(edited);
  });
}
