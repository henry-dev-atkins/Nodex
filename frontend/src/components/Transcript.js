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

function formatText(value) {
  return escapeHtml(normalizeText(value)).replaceAll("\n", "<br />");
}

function humanizeStatus(status) {
  if (status === "inProgress" || status === "running") {
    return "Running";
  }
  if (status === "completed") {
    return "Completed";
  }
  if (status === "denied") {
    return "Denied";
  }
  if (status === "error" || status === "failed") {
    return "Error";
  }
  if (status === "interrupted") {
    return "Interrupted";
  }
  return status || "Pending";
}

function shortenPath(path) {
  if (!path) {
    return "";
  }
  const normalized = normalizeText(path);
  const parts = normalized.split(/[\\/]/);
  return parts[parts.length - 1] || normalized;
}

function extractItemText(item) {
  if (!item || typeof item !== "object") {
    return "";
  }
  if (item.text) {
    return normalizeText(item.text);
  }
  if (Array.isArray(item.content)) {
    return normalizeText(
      item.content
        .map((part) => part?.text || "")
        .filter(Boolean)
        .join("\n"),
    );
  }
  return "";
}

function formatCommand(item, stage) {
  const command = normalizeText(Array.isArray(item.command) ? item.command.join(" ") : item.command || "Command");
  const lines = [
    `<div class="event-summary">${stage === "started" ? "Running" : humanizeStatus(item.status)} <code>${escapeHtml(command)}</code></div>`,
  ];
  if (item.cwd) {
    lines.push(`<div class="event-meta">in ${escapeHtml(normalizeText(item.cwd))}</div>`);
  }
  if (item.exitCode !== undefined && item.exitCode !== null) {
    lines.push(`<div class="event-meta">exit code ${escapeHtml(item.exitCode)}</div>`);
  }
  if (item.aggregatedOutput) {
    lines.push(`<pre class="event-output">${escapeHtml(normalizeText(item.aggregatedOutput))}</pre>`);
  }
  return {
    kind: item.status === "failed" ? "error" : "tool",
    title: "Command",
    bodyHtml: lines.join(""),
    plainText: command,
  };
}

function formatFileChange(item, stage) {
  const changes = Array.isArray(item.changes) ? item.changes : [];
  const title = item.status === "denied" ? "File change denied" : stage === "started" ? "File change proposed" : "File change applied";
  const list = changes.length
    ? `<ul class="event-list">${changes
        .slice(0, 6)
        .map((change) => {
          const kind = change.kind?.type || change.type || "update";
          return `<li><span>${escapeHtml(shortenPath(change.path))}</span><span>${escapeHtml(kind)}</span></li>`;
        })
        .join("")}${changes.length > 6 ? `<li><span>${changes.length - 6} more files</span><span></span></li>` : ""}</ul>`
    : '<div class="event-meta">No file details available.</div>';
  return {
    kind: item.status === "denied" ? "warning" : "tool",
    title,
    bodyHtml: list,
    plainText: title,
  };
}

function formatReasoning(item) {
  const summary = normalizeText(Array.isArray(item.summary) ? item.summary.join("\n") : item.text || "");
  if (!summary.trim()) {
    return null;
  }
  return {
    kind: "note",
    title: "Reasoning",
    bodyHtml: formatText(summary),
    plainText: summary,
  };
}

function formatWebSearch(item) {
  const query = normalizeText(item.query || item.action?.query || "Search");
  return {
    kind: "tool",
    title: "Web search",
    bodyHtml: `<div class="event-summary">${escapeHtml(query)}</div>`,
    plainText: query,
  };
}

function buildItemBlock(item, stage) {
  if (!item || typeof item !== "object") {
    return null;
  }
  if (item.type === "agentMessage") {
    const text = extractItemText(item);
    if (!text) {
      return null;
    }
    const commentary = item.phase === "commentary";
    return {
      kind: commentary ? "note" : "assistant",
      title: commentary ? "Commentary" : "Assistant",
      bodyHtml: formatText(text),
      plainText: text,
    };
  }
  if (item.type === "commandExecution") {
    return formatCommand(item, stage);
  }
  if (item.type === "fileChange") {
    return formatFileChange(item, stage);
  }
  if (item.type === "reasoning") {
    return formatReasoning(item);
  }
  if (item.type === "webSearch") {
    return formatWebSearch(item);
  }
  return null;
}

function buildApprovalBlocks(approvals) {
  return approvals
    .slice()
    .sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)))
    .map((approval) => {
      const details = approval.details || {};
      const isCommand = approval.requestMethod === "item/commandExecution/requestApproval";
      const title =
        approval.status === "pending"
          ? "Approval requested"
          : approval.status === "approve"
            ? "Approval approved"
            : "Approval denied";
      const lines = [];
      if (isCommand && details.command) {
        lines.push(`<div class="event-summary"><code>${escapeHtml(normalizeText(details.command))}</code></div>`);
      }
      if (isCommand && details.cwd) {
        lines.push(`<div class="event-meta">in ${escapeHtml(normalizeText(details.cwd))}</div>`);
      }
      if (!isCommand) {
        lines.push(`<div class="event-summary">${escapeHtml(normalizeText(details.reason || "Codex requested permission to change files in this workspace."))}</div>`);
      }
      if (details.grantRoot) {
        lines.push(`<div class="event-meta">scope ${escapeHtml(normalizeText(details.grantRoot))}</div>`);
      }
      return {
        kind: approval.status === "deny" ? "warning" : "note",
        title,
        bodyHtml: lines.join(""),
        plainText: `${title} ${approval.requestMethod}`,
      };
    });
}

function buildBlocks(turn, events, approvals) {
  const blocks = [];
  let delta = "";
  let reasoningDelta = "";

  const pushBlock = (block) => {
    if (!block) {
      return;
    }
    const last = blocks[blocks.length - 1];
    if (last && last.kind === block.kind && last.title === block.title && last.plainText === block.plainText) {
      return;
    }
    blocks.push(block);
  };

  const flushDelta = (phase = "assistant") => {
    if (!delta.trim()) {
      return;
    }
    pushBlock({
      kind: phase === "commentary" ? "note" : "assistant",
      title: phase === "commentary" ? "Commentary" : "Assistant",
      bodyHtml: formatText(delta.trim()),
      plainText: delta.trim(),
    });
    delta = "";
  };

  const flushReasoning = () => {
    const text = normalizeText(reasoningDelta).trim();
    if (!text) {
      return;
    }
    pushBlock({
      kind: "note",
      title: "Reasoning",
      bodyHtml: formatText(text),
      plainText: text,
    });
    reasoningDelta = "";
  };

  for (const event of events) {
    if (String(event.type).startsWith("codex/event/")) {
      continue;
    }
    if (event.type === "item/agentMessage/delta") {
      delta += event.payload.delta || "";
      continue;
    }
    if (event.type === "item/reasoning/summaryTextDelta") {
      reasoningDelta += event.payload.delta || "";
      continue;
    }
    if (event.type === "item/completed") {
      const item = event.payload.item || {};
      if (item.type === "agentMessage") {
        const commentary = item.phase === "commentary";
        const text = extractItemText(item).trim();
        if (delta.trim() === text) {
          delta = "";
          flushReasoning();
          pushBlock({
            kind: commentary ? "note" : "assistant",
            title: commentary ? "Commentary" : "Assistant",
            bodyHtml: formatText(text),
            plainText: text,
          });
          continue;
        }
      }
      flushDelta();
      flushReasoning();
      pushBlock(buildItemBlock(item, "completed"));
      continue;
    }
    if (event.type === "item/started") {
      flushDelta();
      flushReasoning();
      pushBlock(buildItemBlock(event.payload.item || {}, "started"));
      continue;
    }
    if (event.type === "error") {
      flushDelta();
      flushReasoning();
      pushBlock({
        kind: "error",
        title: "Error",
        bodyHtml: formatText(event.payload.error?.message || "The turn failed."),
        plainText: String(event.payload.error?.message || "The turn failed."),
      });
    }
  }
  flushDelta();
  flushReasoning();

  for (const approvalBlock of buildApprovalBlocks(approvals)) {
    pushBlock(approvalBlock);
  }
  if (blocks.length) {
    return blocks;
  }
  return (turn.metadata?.items || []).map((item) => buildItemBlock(item, "completed")).filter(Boolean);
}

export function renderTranscript(container, state, handlers) {
  const threadId = state.selectedThreadId;
  if (!threadId) {
    container.innerHTML = '<div class="empty-state">Select a thread to inspect its turns.</div>';
    return;
  }
  const turns = (state.turnsByThread[threadId] || []).slice().sort((a, b) => a.idx - b.idx);
  if (!turns.length) {
    container.innerHTML = '<div class="empty-state">This thread has no turns yet.</div>';
    return;
  }

  container.innerHTML = turns
    .map((turn) => {
      const key = `${threadId}:${turn.turnId}`;
      const events = state.eventsByTurn[key] || [];
      const approvals = Object.values(state.approvals).filter((approval) => approval.threadId === threadId && approval.turnId === turn.turnId);
      const blocks = buildBlocks(turn, events, approvals);
      const checked = state.importSelection[`${threadId}:${turn.turnId}`] ? "checked" : "";
      return `
        <article class="turn-card">
          <div class="turn-header">
            <div>
              <strong>Turn ${turn.idx}</strong>
              <div class="thread-meta">
                <span class="turn-status turn-status-${escapeHtml(turn.status)}">${escapeHtml(humanizeStatus(turn.status))}</span>
                <span>${escapeHtml(turn.turnId.slice(0, 8))}</span>
              </div>
            </div>
            <label class="import-selection">
              <input type="checkbox" data-import-turn="${turn.turnId}" ${checked} />
              Select for import
            </label>
          </div>
          <div class="turn-prompt">
            <span class="turn-role">Prompt</span>
            <p class="turn-user">${formatText(turn.userText || "No prompt captured.")}</p>
          </div>
          <div class="turn-events">
            ${blocks
              .map(
                (block) => `
                  <div class="event-block ${block.kind}">
                    <span class="event-label">${block.title}</span>
                    <div class="event-body">${block.bodyHtml}</div>
                  </div>
                `,
              )
              .join("")}
          </div>
        </article>
      `;
    })
    .join("");

  container.querySelectorAll("[data-import-turn]").forEach((input) => {
    input.addEventListener("change", () => handlers.onToggleImportTurn(threadId, input.dataset.importTurn));
  });
}
