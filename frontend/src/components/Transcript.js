import { buildBlocks, escapeHtml, formatText, humanizeTurnStatus, normalizeText, summarizeText, summarizeTurn, truncateText } from "../rendering.js";
import { getApprovalsForTurn, getBranchLabel, getHeadTurn, getNodeId, getSelectedNode, getSelectedThread, getTurns } from "../selectors.js";

function getContextLinks(turn) {
  const links = turn?.metadata?.contextLinks;
  return Array.isArray(links) ? links.filter((link) => link?.sourceThreadId && link?.sourceTurnId) : [];
}

function getTurnResponse(blocks, summary) {
  const assistant = [...blocks].reverse().find((block) => block.kind === "assistant");
  const commentary = [...blocks].reverse().find((block) => block.title === "Commentary");
  return assistant?.plainText || commentary?.plainText || summary.preview || "No response captured yet.";
}

function getStatusMark(turn, approvals) {
  if (approvals.some((approval) => approval.status === "pending")) {
    return { label: "...", tone: "is-running", title: "Waiting on approval" };
  }
  const decided = approvals.filter((approval) => approval.status === "approve" || approval.status === "deny");
  const latest = decided[decided.length - 1];
  if (latest?.status === "approve") {
    return { label: "&#10003;", tone: "is-ok", title: "Approved" };
  }
  if (latest?.status === "deny") {
    return { label: "x", tone: "is-error", title: "Denied" };
  }
  if (turn.status === "running" || turn.status === "inProgress") {
    return { label: "...", tone: "is-running", title: "Running" };
  }
  if (turn.status === "error" || turn.status === "failed" || turn.status === "interrupted") {
    return { label: "!", tone: "is-error", title: humanizeTurnStatus(turn.status) };
  }
  return { label: "&#10003;", tone: "is-done", title: humanizeTurnStatus(turn.status) };
}

function getApprovalSummary(approval) {
  const details = approval.details || {};
  if (approval.requestMethod === "item/commandExecution/requestApproval") {
    return normalizeText(details.command || "Codex requested command approval.");
  }
  return normalizeText(details.reason || "Codex requested file-change approval.");
}

function renderApprovalStrip(approvals) {
  if (!approvals.length) {
    return "";
  }
  return approvals
    .map((approval) => {
      const pending = approval.status === "pending";
      const tone = pending ? "is-pending" : approval.status === "deny" ? "is-denied" : "is-approved";
      return `
        <div class="approval-inline ${tone}">
          <div class="approval-inline-copy">
            <strong>${pending ? "Approval" : approval.status === "approve" ? "Approved" : "Denied"}</strong>
            <span>${escapeHtml(getApprovalSummary(approval))}</span>
          </div>
          ${
            pending
              ? `
                <div class="approval-inline-actions">
                  <button type="button" class="ghost-button approval-inline-button" data-approval-decision="deny" data-approval-id="${approval.approvalId}">Deny</button>
                  <button type="button" class="primary-button approval-inline-button" data-approval-decision="approve" data-approval-id="${approval.approvalId}">Approve</button>
                </div>
              `
              : ""
          }
        </div>
      `;
    })
    .join("");
}

function renderContextSummary(state, contextLinks) {
  if (!contextLinks.length) {
    return "";
  }
  return `
    <div class="turn-detail-block">
      <span class="turn-detail-label">Imported context</span>
      <ul class="turn-detail-list">
        ${contextLinks
          .map((link) => {
            const sourceTurn = getTurns(state, link.sourceThreadId).find((item) => item.turnId === link.sourceTurnId);
            const label = sourceTurn ? `T${sourceTurn.idx}` : link.sourceTurnId.slice(0, 8);
            return `<li>${escapeHtml(getBranchLabel(state, link.sourceThreadId))} / ${escapeHtml(label)}</li>`;
          })
          .join("")}
      </ul>
    </div>
  `;
}

function renderTurnStream(summary, blocks) {
  const streamBlocks = blocks.filter((block) => block?.plainText?.trim() && !String(block.title || "").startsWith("Approval"));
  return `
    <div class="turn-stream">
      <section class="turn-stream-block is-user">
        <div class="turn-stream-label">You</div>
        <div class="turn-stream-text">${formatText(summary.prompt)}</div>
      </section>
      ${streamBlocks
        .map((block) => {
          const tone = block.isReasoning
            ? "is-reasoning"
            : block.kind === "assistant"
              ? "is-assistant"
              : block.title === "Commentary"
                ? "is-commentary"
                : block.kind === "tool"
                  ? "is-tool"
                  : block.kind === "error"
                    ? "is-error"
                    : "is-note";
          return `
            <section class="turn-stream-block ${tone}">
              <div class="turn-stream-label">${escapeHtml(block.title || "Update")}</div>
              <div class="turn-stream-text">${formatText(block.plainText)}</div>
            </section>
          `;
        })
        .join("")}
    </div>
  `;
}

function buildTranscriptEntries(state, threadId, targetThreadId = threadId, cutoffTurnId = null) {
  const thread = state.threads[threadId];
  if (!thread) {
    return [];
  }
  const inheritedEntries = thread.parentThreadId
    ? buildTranscriptEntries(state, thread.parentThreadId, targetThreadId, thread.forkedFromTurnId)
    : [];
  const turns = getTurns(state, threadId);
  const cutoffIdx = cutoffTurnId ? turns.find((turn) => turn.turnId === cutoffTurnId)?.idx ?? turns.length : null;
  const visibleTurns = cutoffIdx === null ? turns : turns.filter((turn) => turn.idx <= cutoffIdx);
  const entries = [...inheritedEntries];
  const seenNodeIds = new Set(entries.map((entry) => getNodeId(entry.threadId, entry.turn.turnId)));

  for (const turn of visibleTurns) {
    for (const link of getContextLinks(turn)) {
      const importedTurn = getTurns(state, link.sourceThreadId).find((item) => item.turnId === link.sourceTurnId);
      const importedNodeId = getNodeId(link.sourceThreadId, link.sourceTurnId);
      if (!importedTurn || seenNodeIds.has(importedNodeId)) {
        continue;
      }
      entries.push({
        threadId: link.sourceThreadId,
        turn: importedTurn,
        inherited: false,
        imported: true,
        importedIntoTurnId: turn.turnId,
        importedIntoTurnIdx: turn.idx,
      });
      seenNodeIds.add(importedNodeId);
    }

    const nodeId = getNodeId(threadId, turn.turnId);
    if (!seenNodeIds.has(nodeId)) {
      entries.push({
        threadId,
        turn,
        inherited: threadId !== targetThreadId,
        imported: false,
      });
      seenNodeIds.add(nodeId);
    }
  }

  return entries;
}

function syncTranscriptFeed(container, state, selectedNode, selectedTurn, headTurn) {
  const feed = container.querySelector("[data-transcript-feed]");
  if (!feed) {
    return;
  }

  const activeNodeId = state.expandedTurnKey
    || (selectedNode?.thread?.threadId && selectedNode?.turn?.turnId
      ? getNodeId(selectedNode.thread.threadId, selectedNode.turn.turnId)
      : null);

  if (activeNodeId) {
    const activeRow = feed.querySelector(`[data-turn-node="${activeNodeId}"]`);
    if (activeRow) {
      activeRow.scrollIntoView({ block: "nearest" });
      return;
    }
  }

  if (!selectedTurn || headTurn?.turnId === selectedTurn.turnId) {
    feed.scrollTop = feed.scrollHeight;
  }
}

export function renderTranscript(container, state, handlers) {
  const thread = getSelectedThread(state);
  if (!thread) {
    container.innerHTML = '<div class="empty-state">Select a node to inspect a branch transcript.</div>';
    return;
  }

  const selectedNode = getSelectedNode(state) || { thread, turn: null, conversationId: thread.threadId };
  const turns = getTurns(state, thread.threadId);
  const headTurn = getHeadTurn(state, thread.threadId);
  const selectedTurn = selectedNode?.thread?.threadId === thread.threadId ? selectedNode.turn : null;
  const focusTurn = selectedTurn || headTurn;
  const focusCutoffIdx = focusTurn?.idx || turns[turns.length - 1]?.idx || 0;
  const branchLabel = getBranchLabel(state, thread.threadId);
  const transcriptEntries = buildTranscriptEntries(state, thread.threadId);
  const forcedBranchActive = state.forcedBranchNodeId && state.forcedBranchNodeId === selectedNode?.nodeId;
  const activeContextCount = turns
    .filter((turn) => turn.idx <= focusCutoffIdx)
    .reduce((count, turn) => count + getContextLinks(turn).length, 0);

  if (!turns.length) {
    container.innerHTML = `
      <section class="transcript-shell">
        <section class="transcript-header-line" title="This transcript follows the selected branch.">
          <strong>${branchLabel} / Start</strong>
        </section>
        <div class="transcript-feed" data-transcript-feed="1">
          <div class="empty-state">Select a node to inspect a branch transcript.</div>
        </div>
        <section class="transcript-composer">
          <form data-transcript-composer-form="1" class="composer-form">
            <textarea data-transcript-composer-input="1" rows="4" placeholder="Start the first turn on this branch..."></textarea>
            <div class="composer-actions">
              <button data-delete-conversation-button="1" class="danger-button" type="button">Delete</button>
              <button class="primary-button" type="submit">Send</button>
            </div>
          </form>
        </section>
      </section>
    `;

    container.querySelector("[data-delete-conversation-button]")?.addEventListener("click", () => {
      handlers.onDeleteConversation(selectedNode.conversationId || thread.threadId);
    });
    container.querySelector("[data-transcript-composer-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = container.querySelector("[data-transcript-composer-input]");
      const text = input?.value?.trim() || "";
      if (!text) {
        return;
      }
      await handlers.onSubmit(selectedNode, text);
      if (input) {
        input.value = "";
      }
    });
    return;
  }

  container.innerHTML = `
    <section class="transcript-shell">
      <section class="transcript-header-line" title="Imported-link highlighting updates as you browse the branch.">
        <strong>${branchLabel} / ${focusTurn ? `T${focusTurn.idx}` : "Start"}${activeContextCount ? ` / +${activeContextCount}` : ""}</strong>
      </section>
      <div class="transcript-feed" data-transcript-feed="1">
        ${transcriptEntries
          .map((entry) => {
            const rowThreadId = entry.threadId;
            const turn = entry.turn;
            const rowBranchLabel = getBranchLabel(state, rowThreadId);
            const key = `${rowThreadId}:${turn.turnId}`;
            const events = state.eventsByTurn[key] || [];
            const approvals = getApprovalsForTurn(state, rowThreadId, turn.turnId);
            const blocks = buildBlocks(turn, events, approvals);
            const summary = summarizeTurn(turn, blocks, approvals);
            const contextLinks = getContextLinks(turn);
            const isCurrentBranchTurn = rowThreadId === thread.threadId;
            const selected =
              selectedNode?.thread?.threadId === rowThreadId && selectedNode?.turn?.turnId === turn.turnId ? "selected" : "";
            const pendingApprovals = approvals.filter((approval) => approval.status === "pending");
            const isExpanded = state.expandedTurnKey === getNodeId(rowThreadId, turn.turnId);
            const hasContext = contextLinks.length > 0;
            const isImportedEntry = Boolean(entry.imported);
            const isActiveContextTurn = isCurrentBranchTurn && hasContext && turn.idx <= focusCutoffIdx;
            const statusMark = getStatusMark(turn, approvals);
            const promptPreview = summarizeText(summary.prompt || "No prompt", 88);
            const responsePreview = truncateText(getTurnResponse(blocks, summary) || "No response yet.", 110);
            const showBranchBadge = entry.inherited || entry.imported;
            const toolLines = blocks
              .filter((block) => block.kind === "tool" || block.kind === "error" || block.kind === "warning")
              .map((block) => block.plainText)
              .filter(Boolean);
            return `
              <article class="turn-row ${selected} ${entry.inherited ? "turn-row-lineage" : ""} ${isImportedEntry ? "turn-row-imported-entry" : ""} ${isActiveContextTurn ? "turn-row-import-active" : hasContext ? "turn-row-import-future" : ""} ${pendingApprovals.length ? "turn-row-needs-approval" : ""}" data-turn-node="${rowThreadId}:${turn.turnId}">
                <div class="turn-row-head" data-turn-head="${rowThreadId}:${turn.turnId}">
                  <div class="turn-row-main">
                    ${showBranchBadge ? `<span class="turn-row-branch-badge">${escapeHtml(rowBranchLabel)}</span>` : ""}
                    <span class="turn-row-id">T${turn.idx}</span>
                    <span class="turn-row-mark ${statusMark.tone}" title="${escapeHtml(statusMark.title)}">${statusMark.label}</span>
                    <div class="turn-row-preview-stack">
                      <div class="turn-row-prompt">${escapeHtml(promptPreview)}</div>
                      <div class="turn-row-response">${escapeHtml(responsePreview)}</div>
                    </div>
                    ${hasContext ? `<span class="turn-row-link-flag" title="${contextLinks.length} imported link${contextLinks.length === 1 ? "" : "s"}">+${contextLinks.length}</span>` : ""}
                    ${isImportedEntry ? `<span class="turn-row-link-flag" title="Imported into T${entry.importedIntoTurnIdx}">in T${entry.importedIntoTurnIdx}</span>` : ""}
                  </div>
                  <div class="turn-row-actions">
                    <span class="turn-row-expand-hint">${isExpanded ? "Hide" : "Open"}</span>
                  </div>
                </div>
                ${pendingApprovals.length ? `<div class="turn-approval-stack">${renderApprovalStrip(pendingApprovals)}</div>` : ""}
                ${
                  isExpanded
                    ? `
                      <div class="turn-row-body">
                        ${renderTurnStream(summary, blocks)}
                        ${
                          toolLines.length && !blocks.some((block) => block.kind === "tool")
                            ? `
                              <div class="turn-detail-block">
                                <span class="turn-detail-label">Tools</span>
                                <ul class="turn-detail-list">
                                  ${toolLines.slice(0, 6).map((line) => `<li>${escapeHtml(line)}</li>`).join("")}
                                </ul>
                              </div>
                            `
                            : ""
                        }
                        ${renderContextSummary(state, contextLinks)}
                        ${
                          approvals.filter((approval) => approval.status !== "pending").length
                            ? `<div class="turn-approval-stack">${renderApprovalStrip(approvals.filter((approval) => approval.status !== "pending"))}</div>`
                            : ""
                        }
                      </div>
                    `
                    : ""
                }
              </article>
            `;
          })
          .join("")}
      </div>
      <section class="transcript-composer">
        <div class="transcript-composer-intent">
          ${
            forcedBranchActive && selectedTurn
              ? `Branch from T${selectedTurn.idx}`
              : selectedTurn && headTurn?.turnId !== selectedTurn.turnId
                ? `Reply from T${selectedTurn.idx} to branch`
                : `Continue ${escapeHtml(branchLabel)}`
          }
        </div>
        <form data-transcript-composer-form="1" class="composer-form">
          <textarea
            data-transcript-composer-input="1"
            rows="4"
            placeholder="${escapeHtml(
              forcedBranchActive && selectedTurn
                ? `Create a new branch from T${selectedTurn.idx}...`
                : selectedTurn && headTurn?.turnId !== selectedTurn.turnId
                  ? `Send from T${selectedTurn.idx} to create a new branch...`
                  : "Send the next message on this branch...",
            )}"
          ></textarea>
          <div class="composer-actions">
            <button data-delete-conversation-button="1" class="danger-button" type="button">Delete</button>
            <button class="primary-button" type="submit">Send</button>
          </div>
        </form>
      </section>
    </section>
  `;

  container.querySelectorAll("[data-turn-head]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (event.target.closest("button")) {
        return;
      }
      const [threadId, turnId] = element.dataset.turnHead.split(":");
      const shouldToggle = handlers.onSelectNode(threadId, turnId);
      if (shouldToggle === false) {
        return;
      }
      handlers.onToggleTurn(threadId, turnId);
    });
  });

  container.querySelectorAll("[data-turn-response-scroll]").forEach((element) => {
    element.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    element.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
    });
    element.addEventListener("wheel", (event) => {
      event.stopPropagation();
    });
  });

  container.querySelectorAll("[data-approval-decision]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      await handlers.onApprovalDecision(button.dataset.approvalId, button.dataset.approvalDecision);
    });
  });

  container.querySelector("[data-delete-conversation-button]")?.addEventListener("click", () => {
    handlers.onDeleteConversation(selectedNode?.conversationId || thread.threadId);
  });

  container.querySelector("[data-transcript-composer-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = container.querySelector("[data-transcript-composer-input]");
    const text = input?.value?.trim() || "";
    if (!text) {
      return;
    }
    await handlers.onSubmit(selectedNode || { thread, turn: selectedTurn, conversationId: thread.threadId }, text);
    if (input) {
      input.value = "";
    }
  });

  requestAnimationFrame(() => syncTranscriptFeed(container, state, selectedNode, selectedTurn, headTurn));
}
