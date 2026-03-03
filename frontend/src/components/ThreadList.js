import { escapeHtml, threadLabel } from "../rendering.js";
import { countConversationBranches, countConversationTurns, getConversationRoots } from "../selectors.js";

function statusClass(status) {
  if (status === "running" || status === "inProgress") {
    return "is-running";
  }
  if (status === "error" || status === "dead") {
    return "is-error";
  }
  return "is-idle";
}

export function renderThreadList(container, state, onSelect) {
  const roots = getConversationRoots(state);

  if (!roots.length) {
    container.innerHTML = '<div class="empty-state">Create a conversation to begin.</div>';
    return;
  }

  container.innerHTML = roots
    .map((thread) => {
      const selected = thread.threadId === state.selectedConversationId ? "selected" : "";
      return `
        <article class="thread-row ${selected}" data-thread-id="${thread.threadId}">
          <div class="thread-row-title">${escapeHtml(threadLabel(thread))}</div>
          <div class="thread-row-meta">
            <span>${countConversationBranches(state, thread.threadId)} branches | ${countConversationTurns(state, thread.threadId)} turns</span>
            <span class="status-dot ${statusClass(thread.status)}" title="${escapeHtml(thread.status || "idle")}" aria-hidden="true"></span>
          </div>
        </article>
      `;
    })
    .join("");

  container.querySelectorAll("[data-thread-id]").forEach((element) => {
    element.addEventListener("click", () => onSelect(element.dataset.threadId));
  });
}
