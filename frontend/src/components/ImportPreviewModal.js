import { escapeHtml, threadLabel } from "../rendering.js";
import { getTurn } from "../selectors.js";

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
  const sourceTurn = modal.sourceThreadId && modal.sourceTurnIds?.[0]
    ? getTurn(state, modal.sourceThreadId, modal.sourceTurnIds[0])
    : null;
  const targetTurn = modal.targetThreadId && modal.targetTurnId ? getTurn(state, modal.targetThreadId, modal.targetTurnId) : null;

  container.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal">
        <div class="modal-header">
          <div>
            <h3>Create Child Turn</h3>
            <p class="modal-subtle">${escapeHtml(sourceThread ? threadLabel(sourceThread) : "Unknown source")} -> ${escapeHtml(targetThread ? threadLabel(targetThread) : "Unknown target")}</p>
          </div>
          <button id="close-import-modal" class="ghost-button">Close</button>
        </div>

        <div class="modal-parent-list">
          <label class="modal-parent-row">
            <input type="checkbox" checked disabled />
            <span>Primary parent: ${targetTurn ? `T${targetTurn.idx}` : "Unknown"}</span>
          </label>
          <label class="modal-parent-row">
            <input type="checkbox" checked disabled />
            <span>Imported context: ${sourceTurn ? `T${sourceTurn.idx}` : "Unknown"}</span>
          </label>
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
                <button id="commit-import" class="primary-button">Create</button>
              </div>
            `
            : ""
        }
      </div>
    </div>
  `;

  container.querySelector("#close-import-modal")?.addEventListener("click", handlers.onClose);
  container.querySelector("#cancel-import")?.addEventListener("click", handlers.onClose);
  container.querySelector("#commit-import")?.addEventListener("click", () => {
    const edited = container.querySelector("#import-preview-text")?.value || "";
    handlers.onCommit(edited);
  });
}
