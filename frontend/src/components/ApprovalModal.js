function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function normalizeText(value) {
  const text = String(value ?? "");
  const hasUtf8Mojibake =
    text.includes(String.fromCharCode(195)) ||
    text.includes(String.fromCharCode(194)) ||
    text.includes(String.fromCharCode(226));
  if (!hasUtf8Mojibake) {
    return text;
  }
  try {
    const bytes = Uint8Array.from(Array.from(text), (char) => char.codePointAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return text;
  }
}

function buildApprovalSummary(approval) {
  const details = approval.details || {};
  if (approval.requestMethod === "item/commandExecution/requestApproval") {
    return `
      <div class="approval-summary">
        <p>Codex wants to run a command.</p>
        ${details.command ? `<pre class="approval-detail"><code>${escapeHtml(normalizeText(details.command))}</code></pre>` : ""}
        ${details.cwd ? `<p class="subdued">Working directory: ${escapeHtml(normalizeText(details.cwd))}</p>` : ""}
      </div>
    `;
  }
  return `
    <div class="approval-summary">
      <p>${escapeHtml(normalizeText(details.reason || "Codex wants to make a file change in this workspace."))}</p>
      ${details.grantRoot ? `<p class="subdued">Scope: ${escapeHtml(normalizeText(details.grantRoot))}</p>` : ""}
    </div>
  `;
}

export function renderApprovalModal(container, state, handlers) {
  const pending = Object.values(state.approvals).find((approval) => approval.status === "pending");
  if (!pending) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = `
    <div class="modal-backdrop">
      <div class="modal">
        <div class="modal-header">
          <div>
            <p class="eyebrow">Approval Required</p>
            <h3>${pending.requestMethod === "item/commandExecution/requestApproval" ? "Run command?" : "Apply file change?"}</h3>
          </div>
          <span class="status-chip">Thread ${pending.threadId.slice(0, 8)}</span>
        </div>
        ${buildApprovalSummary(pending)}
        <div class="modal-actions">
          <button id="deny-approval" class="danger-button">Deny</button>
          <button id="approve-approval" class="primary-button">Approve</button>
        </div>
      </div>
    </div>
  `;

  container.querySelector("#approve-approval").addEventListener("click", () => handlers.onDecision(pending.approvalId, "approve"));
  container.querySelector("#deny-approval").addEventListener("click", () => handlers.onDecision(pending.approvalId, "deny"));
}
