export function renderImportPreviewModal(container, state, handlers) {
  const modal = state.importModal;
  if (!modal.open) {
    container.innerHTML = "";
    return;
  }

  const otherThreads = Object.values(state.threads).filter((thread) => thread.threadId !== modal.sourceThreadId);
  const secrets = modal.preview?.suspectedSecrets || [];
  const previewText = modal.preview?.transferBlob || "";

  container.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal">
        <div class="modal-header">
          <div>
            <p class="eyebrow">Import From Branch</p>
            <h3>Copied context, not a merge</h3>
          </div>
          <button id="close-import-modal" class="ghost-button">Close</button>
        </div>

        <label class="subdued" for="import-target">Destination thread</label>
        <select id="import-target">
          <option value="">Select destination</option>
          ${otherThreads
            .map(
              (thread) => `
                <option value="${thread.threadId}" ${modal.targetThreadId === thread.threadId ? "selected" : ""}>
                  ${thread.title || thread.threadId}
                </option>
              `,
            )
            .join("")}
        </select>

        ${modal.error ? `<p class="subdued" style="color:#8b2d2d">${modal.error}</p>` : ""}

        <div class="modal-actions">
          <button id="preview-import" class="ghost-button" ${modal.loading ? "disabled" : ""}>Preview Import</button>
        </div>

        ${
          modal.preview
            ? `
              <div class="secret-list">${
                secrets.length
                  ? secrets.map((secret) => `${secret.label} at ${secret.start}-${secret.end}`).join("\n")
                  : "No obvious secrets detected."
              }</div>
              <textarea id="import-preview-text">${previewText}</textarea>
              <div class="modal-actions">
                <button id="cancel-import" class="ghost-button">Cancel</button>
                <button id="commit-import" class="primary-button">Commit Import</button>
              </div>
            `
            : ""
        }
      </div>
    </div>
  `;

  container.querySelector("#close-import-modal").addEventListener("click", handlers.onClose);
  container.querySelector("#preview-import").addEventListener("click", () => {
    const target = container.querySelector("#import-target").value;
    handlers.onPreview(target);
  });
  if (modal.preview) {
    container.querySelector("#cancel-import").addEventListener("click", handlers.onClose);
    container.querySelector("#commit-import").addEventListener("click", () => {
      const edited = container.querySelector("#import-preview-text").value;
      handlers.onCommit(edited);
    });
  }
}
