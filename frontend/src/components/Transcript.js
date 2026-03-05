import { buildBlocks, escapeHtml, formatText, humanizeTurnStatus, normalizeText, summarizeTurn } from "../rendering.js";
import {
  getApprovalsForTurn,
  getBranchLabel,
  getContextLinkAnchor,
  getContextLinkMode,
  getContextLinkScopeCount,
  getContextLinks,
  getHeadTurn,
  getNodeId,
  getSelectedNode,
  getSelectedThread,
  getTurns,
} from "../selectors.js";

const CLAMP_LINE_COUNT = 2;

function getStatusMark(turn, approvals) {
  if (approvals.some((approval) => approval.status === "pending")) {
    return { label: "...", tone: "is-running", title: "Waiting on approval" };
  }
  const decided = approvals.filter((approval) => approval.status === "approve" || approval.status === "deny");
  const latest = decided[decided.length - 1];
  if (latest?.status === "approve") {
    return { label: "ok", tone: "is-ok", title: "Approved" };
  }
  if (latest?.status === "deny") {
    return { label: "deny", tone: "is-error", title: "Denied" };
  }
  if (turn.status === "running" || turn.status === "inProgress") {
    return { label: "...", tone: "is-running", title: "Running" };
  }
  if (turn.status === "error" || turn.status === "failed" || turn.status === "interrupted") {
    return { label: "!", tone: "is-error", title: humanizeTurnStatus(turn.status) };
  }
  return { label: "ok", tone: "is-done", title: humanizeTurnStatus(turn.status) };
}

function getApprovalSummary(approval) {
  const details = approval.details || {};
  if (approval.requestMethod === "item/commandExecution/requestApproval") {
    return normalizeText(details.command || "Codex requested command approval.");
  }
  return normalizeText(details.reason || "Codex requested file-change approval.");
}

function getAssistantText(blocks, summary) {
  const assistant = [...blocks].reverse().find((block) => block.kind === "assistant");
  if (assistant?.plainText?.trim()) {
    return assistant.plainText.trim();
  }
  return summary.preview || "No response captured yet.";
}

function shouldOfferExpand(text) {
  const normalized = normalizeText(text || "").trim();
  if (!normalized) {
    return false;
  }
  if (normalized.includes("\n")) {
    return true;
  }
  return normalized.length > 120;
}

function getReasoningBlocks(blocks) {
  const seen = new Set();
  return blocks
    .filter((block) => block.isReasoning || block.title === "Commentary")
    .filter((block) => {
      const text = normalizeText(block.plainText || "").trim();
      if (!text) {
        return false;
      }
      const key = `${block.title}:${text}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
}

function getCommandBlocks(blocks) {
  const seen = new Set();
  return blocks
    .filter((block) => block.kind === "tool" || block.kind === "warning" || block.kind === "error")
    .filter((block) => {
      const text = normalizeText(block.plainText || "").trim();
      if (!text) {
        return false;
      }
      const key = `${block.title}:${text}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
}

function renderApprovalAttachments(approvals) {
  if (!approvals.length) {
    return "";
  }
  return `
    <div class="chat-approval-stack">
      ${approvals
        .map((approval) => {
          const pending = approval.status === "pending";
          const tone = pending ? "is-pending" : approval.status === "deny" ? "is-denied" : "is-approved";
          return `
            <div class="chat-approval ${tone}">
              <div class="chat-approval-copy">
                <strong>${pending ? "Approval" : approval.status === "approve" ? "Approved" : "Denied"}</strong>
                <span>${escapeHtml(getApprovalSummary(approval))}</span>
              </div>
              ${
                pending
                  ? `
                    <div class="chat-approval-actions">
                      <button type="button" class="ghost-button chat-approval-button" data-approval-decision="deny" data-approval-id="${approval.approvalId}">Deny</button>
                      <button type="button" class="primary-button chat-approval-button" data-approval-decision="approve" data-approval-id="${approval.approvalId}">Approve</button>
                    </div>
                  `
                  : ""
              }
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderOriginBadge(entry, rowBranchLabel) {
  if (entry.imported) {
    const scopeCount = entry.sourceNodeCount || 1;
    const mode = entry.mergeMode || "verbose";
    return `
      <span class="chat-turn-origin is-imported">
        &#8627; Imported from ${escapeHtml(rowBranchLabel)} into T${entry.importedIntoTurnIdx} | ${escapeHtml(mode)} | ${scopeCount} turn${scopeCount === 1 ? "" : "s"}
      </span>
    `;
  }
  if (entry.inherited) {
    return `<span class="chat-turn-origin is-inherited">&#8627; Inherited from ${escapeHtml(rowBranchLabel)}</span>`;
  }
  return "";
}

function renderContextSummary(state, contextLinks) {
  if (!contextLinks.length) {
    return "";
  }
  return `
    <div class="chat-context-summary">
      ${contextLinks
        .map((link) => {
          const anchor = getContextLinkAnchor(link);
          if (!anchor) {
            return "";
          }
          const sourceTurn = getTurns(state, anchor.threadId).find((item) => item.turnId === anchor.turnId);
          const label = sourceTurn ? `T${sourceTurn.idx}` : anchor.turnId.slice(0, 8);
          const scopeCount = getContextLinkScopeCount(link);
          return `
            <span class="chat-context-chip">
              ${escapeHtml(getBranchLabel(state, anchor.threadId))} / ${escapeHtml(label)} | ${escapeHtml(getContextLinkMode(link))} | ${scopeCount}
            </span>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderAuxPanel(auxPanel, reasoningBlocks, commandBlocks) {
  if (auxPanel === "reasoning" && reasoningBlocks.length) {
    return `
      <div class="chat-aux-panel" data-turn-aux-panel="reasoning">
        ${reasoningBlocks
          .map((block) => {
            return `
              <article class="chat-aux-item">
                <header>${escapeHtml(block.title || "Reasoning")}</header>
                <div>${formatText(block.plainText || "")}</div>
              </article>
            `;
          })
          .join("")}
      </div>
    `;
  }
  if (auxPanel === "commands" && commandBlocks.length) {
    return `
      <div class="chat-aux-panel" data-turn-aux-panel="commands">
        ${commandBlocks
          .map((block) => {
            return `
              <article class="chat-aux-item">
                <header>${escapeHtml(block.title || "Command")}</header>
                <div>${formatText(block.plainText || "")}</div>
              </article>
            `;
          })
          .join("")}
      </div>
    `;
  }
  return "";
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
      const anchor = getContextLinkAnchor(link);
      if (!anchor) {
        continue;
      }
      const importedTurn = getTurns(state, anchor.threadId).find((item) => item.turnId === anchor.turnId);
      const importedNodeId = getNodeId(anchor.threadId, anchor.turnId);
      if (!importedTurn || seenNodeIds.has(importedNodeId)) {
        continue;
      }
      entries.push({
        threadId: anchor.threadId,
        turn: importedTurn,
        inherited: false,
        imported: true,
        importedIntoTurnId: turn.turnId,
        importedIntoTurnIdx: turn.idx,
        mergeMode: getContextLinkMode(link),
        sourceNodeCount: getContextLinkScopeCount(link),
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
  const activeNodeId = selectedNode?.thread?.threadId && selectedNode?.turn?.turnId
    ? getNodeId(selectedNode.thread.threadId, selectedNode.turn.turnId)
    : null;
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
  const busyTurn = turns.find((turn) => {
    if (turn.status === "running" || turn.status === "inProgress") {
      return true;
    }
    return getApprovalsForTurn(state, thread.threadId, turn.turnId).some((approval) => approval.status === "pending");
  }) || null;
  const busyTurnNeedsApproval = busyTurn
    ? getApprovalsForTurn(state, thread.threadId, busyTurn.turnId).some((approval) => approval.status === "pending")
    : false;
  const composerDisabled = Boolean(busyTurn);
  const composerDisabledReason = busyTurn
    ? busyTurnNeedsApproval
      ? `Waiting for approval on T${busyTurn.idx}`
      : `Waiting for T${busyTurn.idx} to finish`
    : "";

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
            <div class="composer-input-shell">
              <textarea data-transcript-composer-input="1" rows="4" placeholder="Start the first turn on this branch..."></textarea>
              <div class="composer-actions">
                <button data-delete-conversation-button="1" class="danger-button" type="button">Delete</button>
                <button class="primary-button" type="submit">Send</button>
              </div>
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
            const nodeId = getNodeId(rowThreadId, turn.turnId);
            const events = state.eventsByTurn[`${rowThreadId}:${turn.turnId}`] || [];
            const approvals = getApprovalsForTurn(state, rowThreadId, turn.turnId);
            const blocks = buildBlocks(turn, events, approvals);
            const summary = summarizeTurn(turn, blocks, approvals);
            const contextLinks = getContextLinks(turn);
            const selected = selectedNode?.thread?.threadId === rowThreadId && selectedNode?.turn?.turnId === turn.turnId;
            const statusMark = getStatusMark(turn, approvals);
            const reasoningBlocks = getReasoningBlocks(blocks);
            const commandBlocks = getCommandBlocks(blocks);
            const auxPanel = state.auxPanelByNode?.[nodeId] || null;
            const userExpanded = Boolean(state.userExpandedByNode?.[nodeId]);
            const assistantExpanded = Boolean(state.assistantExpandedByNode?.[nodeId]);
            const userText = normalizeText(summary.prompt || "No prompt captured.");
            const assistantText = normalizeText(getAssistantText(blocks, summary)).trim();
            const userNeedsToggle = shouldOfferExpand(userText);
            const assistantNeedsToggle = shouldOfferExpand(assistantText || "No response captured yet.");
            return `
              <article class="chat-turn ${selected ? "selected" : ""} ${entry.inherited ? "is-inherited" : ""} ${entry.imported ? "is-imported" : ""}" data-turn-node="${nodeId}">
                <header class="chat-turn-head">
                  <div class="chat-turn-head-main">
                    <span class="chat-turn-id">T${turn.idx}</span>
                    <span class="chat-turn-branch">${escapeHtml(rowBranchLabel)}</span>
                    <span class="chat-turn-status ${statusMark.tone}" title="${escapeHtml(statusMark.title)}">${escapeHtml(statusMark.label)}</span>
                  </div>
                  ${renderOriginBadge(entry, rowBranchLabel)}
                </header>
                <div class="chat-row chat-row-user" data-turn-select-thread="${rowThreadId}" data-turn-select-turn="${turn.turnId}">
                  <section class="chat-bubble chat-bubble-user">
                    <div class="chat-bubble-label">You</div>
                    <div class="chat-bubble-text ${!userExpanded ? "is-collapsed" : ""}" style="${!userExpanded ? `--chat-line-clamp:${CLAMP_LINE_COUNT}` : ""}">${formatText(userText)}</div>
                    ${
                      userNeedsToggle
                        ? `
                          <button
                            type="button"
                            class="ghost-button chat-inline-toggle"
                            data-toggle-user-thread="${rowThreadId}"
                            data-toggle-user-turn="${turn.turnId}"
                          >${userExpanded ? "less" : "more"}</button>
                        `
                        : ""
                    }
                  </section>
                </div>
                <div class="chat-row chat-row-assistant" data-turn-select-thread="${rowThreadId}" data-turn-select-turn="${turn.turnId}">
                  <section class="chat-bubble chat-bubble-assistant">
                    <div class="chat-bubble-label">Assistant</div>
                    <div class="chat-bubble-text ${!assistantExpanded ? "is-collapsed" : ""}" style="${!assistantExpanded ? `--chat-line-clamp:${CLAMP_LINE_COUNT}` : ""}">${formatText(assistantText || "No response captured yet.")}</div>
                    ${
                      assistantNeedsToggle
                        ? `
                          <button
                            type="button"
                            class="ghost-button chat-inline-toggle"
                            data-toggle-assistant-thread="${rowThreadId}"
                            data-toggle-assistant-turn="${turn.turnId}"
                          >${assistantExpanded ? "less" : "more"}</button>
                        `
                        : ""
                    }
                    ${renderApprovalAttachments(approvals)}
                    ${
                      reasoningBlocks.length || commandBlocks.length
                        ? `
                          <div class="chat-aux-actions">
                            ${
                              reasoningBlocks.length
                                ? `
                                  <button
                                    type="button"
                                    class="ghost-button chat-aux-toggle ${auxPanel === "reasoning" ? "is-active" : ""}"
                                    data-toggle-aux-thread="${rowThreadId}"
                                    data-toggle-aux-turn="${turn.turnId}"
                                    data-toggle-aux-panel="reasoning"
                                  >Reasoning (${reasoningBlocks.length})</button>
                                `
                                : ""
                            }
                            ${
                              commandBlocks.length
                                ? `
                                  <button
                                    type="button"
                                    class="ghost-button chat-aux-toggle ${auxPanel === "commands" ? "is-active" : ""}"
                                    data-toggle-aux-thread="${rowThreadId}"
                                    data-toggle-aux-turn="${turn.turnId}"
                                    data-toggle-aux-panel="commands"
                                  >Commands (${commandBlocks.length})</button>
                                `
                                : ""
                            }
                          </div>
                        `
                        : ""
                    }
                    ${renderAuxPanel(auxPanel, reasoningBlocks, commandBlocks)}
                    ${renderContextSummary(state, contextLinks)}
                  </section>
                </div>
              </article>
            `;
          })
          .join("")}
      </div>
      <section class="transcript-composer">
        <div class="transcript-composer-intent">
          ${composerDisabledReason || (
            forcedBranchActive && selectedTurn
              ? `Branch from T${selectedTurn.idx}`
              : selectedTurn && headTurn?.turnId !== selectedTurn.turnId
                ? `Reply from T${selectedTurn.idx} to branch`
                : `Continue ${escapeHtml(branchLabel)}`
          )}
        </div>
        <form data-transcript-composer-form="1" class="composer-form">
          <div class="composer-input-shell">
            <textarea
              data-transcript-composer-input="1"
              rows="4"
              ${composerDisabled ? "disabled" : ""}
              placeholder="${escapeHtml(
                composerDisabled
                  ? composerDisabledReason
                  : forcedBranchActive && selectedTurn
                    ? `Create a new branch from T${selectedTurn.idx}...`
                    : selectedTurn && headTurn?.turnId !== selectedTurn.turnId
                      ? `Send from T${selectedTurn.idx} to create a new branch...`
                      : "Send the next message on this branch...",
              )}"
            ></textarea>
            <div class="composer-actions">
              <button data-delete-conversation-button="1" class="danger-button" type="button">Delete</button>
              <button class="primary-button" type="submit" ${composerDisabled ? "disabled" : ""}>Send</button>
            </div>
          </div>
          ${composerDisabled && !busyTurnNeedsApproval ? '<div class="composer-stop-row"><button data-interrupt-turn-button="1" class="ghost-button" type="button">Stop</button></div>' : ""}
        </form>
      </section>
    </section>
  `;

  container.querySelectorAll("[data-turn-select-thread][data-turn-select-turn]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (event.target.closest("button")) {
        return;
      }
      const threadId = element.dataset.turnSelectThread;
      const turnId = element.dataset.turnSelectTurn;
      handlers.onSelectNode(threadId, turnId);
    });
  });

  container.querySelectorAll("[data-toggle-assistant-thread][data-toggle-assistant-turn]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      handlers.onToggleAssistantExpanded(button.dataset.toggleAssistantThread, button.dataset.toggleAssistantTurn);
    });
  });

  container.querySelectorAll("[data-toggle-user-thread][data-toggle-user-turn]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      handlers.onToggleUserExpanded(button.dataset.toggleUserThread, button.dataset.toggleUserTurn);
    });
  });

  container.querySelectorAll("[data-toggle-aux-thread][data-toggle-aux-turn][data-toggle-aux-panel]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      handlers.onToggleAuxPanel(
        button.dataset.toggleAuxThread,
        button.dataset.toggleAuxTurn,
        button.dataset.toggleAuxPanel,
      );
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
  container.querySelector("[data-interrupt-turn-button]")?.addEventListener("click", async () => {
    await handlers.onInterrupt(thread.threadId);
  });

  container.querySelector("[data-transcript-composer-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (composerDisabled) {
      return;
    }
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
