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

function humanizeStatus(status) {
  if (status === "inProgress" || status === "running") {
    return "running";
  }
  if (status === "completed") {
    return "completed";
  }
  return status || "idle";
}

function buildDepthMap(threads) {
  const byId = Object.fromEntries(threads.map((thread) => [thread.threadId, thread]));
  const depthMap = {};
  for (const thread of threads) {
    let depth = 0;
    let current = thread;
    while (current.parentThreadId && byId[current.parentThreadId]) {
      depth += 1;
      current = byId[current.parentThreadId];
    }
    depthMap[thread.threadId] = depth;
  }
  return depthMap;
}

export function renderThreadList(container, state, onSelect) {
  const threads = Object.values(state.threads).sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
  const depthMap = buildDepthMap(threads);

  if (!threads.length) {
    container.innerHTML = '<div class="empty-state">Create a thread to begin.</div>';
    return;
  }

  container.innerHTML = threads
    .map((thread) => {
      const selected = thread.threadId === state.selectedThreadId ? "selected" : "";
      const depth = depthMap[thread.threadId] || 0;
      const preview = normalizeText(
        thread.metadata?.preview || (thread.parentThreadId ? `Fork of ${thread.parentThreadId.slice(0, 8)}` : thread.threadId.slice(0, 8)),
      );
      return `
        <article class="thread-card ${selected}" data-thread-id="${thread.threadId}" style="margin-left:${depth * 16}px">
          <div class="thread-title-row">
            <strong>${escapeHtml(normalizeText(thread.title || "Untitled thread"))}</strong>
            <span class="mini-status">${escapeHtml(humanizeStatus(thread.status))}</span>
          </div>
          <div class="thread-preview">${escapeHtml(preview)}</div>
          <div class="thread-meta">
            <span>${escapeHtml(thread.threadId.slice(0, 8))}</span>
          </div>
        </article>
      `;
    })
    .join("");

  container.querySelectorAll("[data-thread-id]").forEach((element) => {
    element.addEventListener("click", () => onSelect(element.dataset.threadId));
  });
}
