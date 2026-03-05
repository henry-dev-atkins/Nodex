import { escapeHtml, formatText } from "../rendering.js";
import { getNodeId } from "../selectors.js";
import { buildTranscriptViewModel, CLAMP_LINE_COUNT, getApprovalSummary } from "../transcriptViewModel.js";

const FEED_SYNC_STATE = new WeakMap();

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

function renderOriginBadge(originBadge, rowBranchLabel) {
  if (!originBadge) {
    return "";
  }
  if (originBadge.kind === "imported") {
    const scopeCount = originBadge.sourceNodeCount;
    const mode = originBadge.mergeMode;
    return `
      <span class="chat-turn-origin is-imported">
        &#8627; Imported from ${escapeHtml(rowBranchLabel)} into T${originBadge.importedIntoTurnIdx} | ${escapeHtml(mode)} | ${scopeCount} turn${scopeCount === 1 ? "" : "s"}
      </span>
    `;
  }
  if (originBadge.kind === "inherited") {
    return `<span class="chat-turn-origin is-inherited">&#8627; Inherited from ${escapeHtml(rowBranchLabel)}</span>`;
  }
  return "";
}

function renderContextSummary(items) {
  if (!items.length) {
    return "";
  }
  return `
    <div class="chat-context-summary">
      ${items
        .map((item) => {
          return `
          <span class="chat-context-chip">
            ${escapeHtml(item.branchLabel)} / ${escapeHtml(item.label)} | ${escapeHtml(item.mode)} | ${item.scopeCount}
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

function syncTranscriptFeed(container, selectedNode, selectedTurn, headTurn) {
  const feed = container.querySelector("[data-transcript-feed]");
  if (!feed) {
    return;
  }

  const activeNodeId = selectedNode?.thread?.threadId && selectedNode?.turn?.turnId
    ? getNodeId(selectedNode.thread.threadId, selectedNode.turn.turnId)
    : null;

  const previous = FEED_SYNC_STATE.get(container) || null;
  const current = {
    activeNodeId,
    selectedTurnId: selectedTurn?.turnId || null,
    headTurnId: headTurn?.turnId || null,
  };
  FEED_SYNC_STATE.set(container, current);

  const selectionChanged = !previous || previous.activeNodeId !== current.activeNodeId;
  const headChanged = !previous || previous.headTurnId !== current.headTurnId;

  if (activeNodeId && selectionChanged) {
    const activeRow = feed.querySelector(`[data-turn-node="${activeNodeId}"]`);
    if (activeRow) {
      activeRow.scrollIntoView({ block: "nearest" });
      return;
    }
  }

  if ((!selectedTurn || headTurn?.turnId === selectedTurn.turnId) && headChanged) {
    feed.scrollTop = feed.scrollHeight;
  }
}

function renderComposerIntent(vm) {
  if (vm.composerDisabledReason) {
    return vm.composerDisabledReason;
  }
  if (vm.forcedBranchActive && vm.selectedTurn) {
    return `Branch from T${vm.selectedTurn.idx}`;
  }
  if (vm.selectedTurn && vm.headTurn?.turnId !== vm.selectedTurn.turnId) {
    return `Reply from T${vm.selectedTurn.idx} to branch`;
  }
  return `Continue ${vm.branchLabel}`;
}

export function renderTranscript(container, state, handlers) {
  const vm = buildTranscriptViewModel(state);
  if (!vm.hasThread) {
    container.innerHTML = '<div class="empty-state">Select a node to inspect a branch transcript.</div>';
    return;
  }

  if (!vm.turns.length) {
    container.innerHTML = `
      <section class="transcript-shell">
        <section class="transcript-header-line" title="This transcript follows the selected branch.">
          <strong>${vm.branchLabel} / Start</strong>
        </section>
        <div class="transcript-feed" data-transcript-feed="1">
          <div class="empty-state">Select a node to inspect a branch transcript.</div>
        </div>
        <section class="transcript-composer">
          <form data-transcript-composer-form="1" class="composer-form">
            <div class="composer-input-shell">
              <textarea
                data-transcript-composer-input="1"
                rows="4"
                ${vm.composerDisabled ? "disabled" : ""}
                placeholder="${escapeHtml(vm.composerDisabled ? vm.composerDisabledReason : "Start the first turn on this branch...")}"
              ></textarea>
              <div class="composer-actions">
                <button class="primary-button" type="submit" ${vm.composerDisabled ? "disabled" : ""}>Send</button>
              </div>
            </div>
          </form>
        </section>
      </section>
    `;

    container.querySelector("[data-transcript-composer-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (vm.composerDisabled) {
        return;
      }
      const input = container.querySelector("[data-transcript-composer-input]");
      const text = input?.value?.trim() || "";
      if (!text) {
        return;
      }
      await handlers.onSubmit(vm.selectedNode, text);
      if (input) {
        input.value = "";
      }
    });
    return;
  }

  container.innerHTML = `
    <section class="transcript-shell">
      <section class="transcript-header-line" title="Imported-link highlighting updates as you browse the branch.">
        <strong>${vm.branchLabel} / ${vm.focusTurn ? `T${vm.focusTurn.idx}` : "Start"}${vm.activeContextCount ? ` / +${vm.activeContextCount}` : ""}</strong>
      </section>
      <div class="transcript-feed" data-transcript-feed="1">
        ${vm.transcriptRows
          .map((row) => {
            const entry = row.entry;
            return `
              <article class="chat-turn ${row.selected ? "selected" : ""} ${entry.inherited ? "is-inherited" : ""} ${entry.imported ? "is-imported" : ""}" data-turn-node="${row.nodeId}">
                <header class="chat-turn-head">
                  <div class="chat-turn-head-main">
                    <span class="chat-turn-id">T${row.turn.idx}</span>
                    <span class="chat-turn-branch">${escapeHtml(row.rowBranchLabel)}</span>
                    <span class="chat-turn-status ${row.statusMark.tone}" title="${escapeHtml(row.statusMark.title)}">${escapeHtml(row.statusMark.label)}</span>
                  </div>
                  ${renderOriginBadge(row.originBadge, row.rowBranchLabel)}
                </header>
                <div class="chat-row chat-row-user" data-turn-select-thread="${row.rowThreadId}" data-turn-select-turn="${row.turn.turnId}">
                  <section class="chat-bubble chat-bubble-user">
                    <div class="chat-bubble-text ${!row.userExpanded ? "is-collapsed" : ""}" style="${!row.userExpanded ? `--chat-line-clamp:${CLAMP_LINE_COUNT}` : ""}">${formatText(row.userText)}</div>
                    ${
                      row.userNeedsToggle
                        ? `
                          <button
                            type="button"
                            class="ghost-button chat-inline-toggle"
                            data-toggle-user-thread="${row.rowThreadId}"
                            data-toggle-user-turn="${row.turn.turnId}"
                          >${row.userExpanded ? "less" : "more"}</button>
                        `
                        : ""
                    }
                  </section>
                </div>
                <div class="chat-row chat-row-assistant" data-turn-select-thread="${row.rowThreadId}" data-turn-select-turn="${row.turn.turnId}">
                  <section class="chat-bubble chat-bubble-assistant">
                    <div class="chat-bubble-text ${!row.assistantExpanded ? "is-collapsed" : ""}" style="${!row.assistantExpanded ? `--chat-line-clamp:${CLAMP_LINE_COUNT}` : ""}">${formatText(row.assistantText)}</div>
                    ${
                      row.assistantNeedsToggle
                        ? `
                          <button
                            type="button"
                            class="ghost-button chat-inline-toggle"
                            data-toggle-assistant-thread="${row.rowThreadId}"
                            data-toggle-assistant-turn="${row.turn.turnId}"
                          >${row.assistantExpanded ? "less" : "more"}</button>
                        `
                        : ""
                    }
                    ${renderApprovalAttachments(row.approvals)}
                    ${
                      row.reasoningBlocks.length || row.commandBlocks.length
                        ? `
                          <div class="chat-aux-actions">
                            ${
                              row.reasoningBlocks.length
                                ? `
                                  <button
                                    type="button"
                                    class="ghost-button chat-aux-toggle ${row.auxPanel === "reasoning" ? "is-active" : ""}"
                                    data-toggle-aux-thread="${row.rowThreadId}"
                                    data-toggle-aux-turn="${row.turn.turnId}"
                                    data-toggle-aux-panel="reasoning"
                                  >Reasoning (${row.reasoningBlocks.length})</button>
                                `
                                : ""
                            }
                            ${
                              row.commandBlocks.length
                                ? `
                                  <button
                                    type="button"
                                    class="ghost-button chat-aux-toggle ${row.auxPanel === "commands" ? "is-active" : ""}"
                                    data-toggle-aux-thread="${row.rowThreadId}"
                                    data-toggle-aux-turn="${row.turn.turnId}"
                                    data-toggle-aux-panel="commands"
                                  >Commands (${row.commandBlocks.length})</button>
                                `
                                : ""
                            }
                          </div>
                        `
                        : ""
                    }
                    ${renderAuxPanel(row.auxPanel, row.reasoningBlocks, row.commandBlocks)}
                    ${renderContextSummary(row.contextSummaryItems)}
                  </section>
                </div>
              </article>
            `;
          })
          .join("")}
      </div>
      <section class="transcript-composer">
        <div class="transcript-composer-intent">
          ${escapeHtml(renderComposerIntent(vm))}
        </div>
        <form data-transcript-composer-form="1" class="composer-form">
          <div class="composer-input-shell">
            <textarea
              data-transcript-composer-input="1"
              rows="4"
              ${vm.composerDisabled ? "disabled" : ""}
              placeholder="${escapeHtml(
                vm.composerDisabled
                  ? vm.composerDisabledReason
                  : vm.forcedBranchActive && vm.selectedTurn
                    ? `Create a new branch from T${vm.selectedTurn.idx}...`
                    : vm.selectedTurn && vm.headTurn?.turnId !== vm.selectedTurn.turnId
                      ? `Send from T${vm.selectedTurn.idx} to create a new branch...`
                      : "Send the next message on this branch...",
              )}"
            ></textarea>
            <div class="composer-actions">
              <button class="primary-button" type="submit" ${vm.composerDisabled ? "disabled" : ""}>Send</button>
            </div>
          </div>
          ${vm.composerDisabled && !vm.busyTurnNeedsApproval ? '<div class="composer-stop-row"><button data-interrupt-turn-button="1" class="ghost-button" type="button">Stop</button></div>' : ""}
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

  const withPreservedFeedScroll = (action) => {
    const feed = container.querySelector("[data-transcript-feed]");
    const previousScrollTop = feed?.scrollTop ?? null;
    action();
    if (previousScrollTop === null) {
      return;
    }
    requestAnimationFrame(() => {
      const nextFeed = container.querySelector("[data-transcript-feed]");
      if (!nextFeed) {
        return;
      }
      nextFeed.scrollTop = previousScrollTop;
    });
  };

  container.querySelectorAll("[data-toggle-assistant-thread][data-toggle-assistant-turn]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      withPreservedFeedScroll(() => {
        handlers.onToggleAssistantExpanded(button.dataset.toggleAssistantThread, button.dataset.toggleAssistantTurn);
      });
    });
  });

  container.querySelectorAll("[data-toggle-user-thread][data-toggle-user-turn]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      withPreservedFeedScroll(() => {
        handlers.onToggleUserExpanded(button.dataset.toggleUserThread, button.dataset.toggleUserTurn);
      });
    });
  });

  container.querySelectorAll("[data-toggle-aux-thread][data-toggle-aux-turn][data-toggle-aux-panel]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      withPreservedFeedScroll(() => {
        handlers.onToggleAuxPanel(
          button.dataset.toggleAuxThread,
          button.dataset.toggleAuxTurn,
          button.dataset.toggleAuxPanel,
        );
      });
    });
  });

  container.querySelectorAll("[data-approval-decision]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      await handlers.onApprovalDecision(button.dataset.approvalId, button.dataset.approvalDecision);
    });
  });

  container.querySelector("[data-interrupt-turn-button]")?.addEventListener("click", async () => {
    await handlers.onInterrupt(vm.thread.threadId);
  });

  container.querySelector("[data-transcript-composer-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (vm.composerDisabled) {
      return;
    }
    const input = container.querySelector("[data-transcript-composer-input]");
    const text = input?.value?.trim() || "";
    if (!text) {
      return;
    }
    await handlers.onSubmit(vm.selectedNode || { thread: vm.thread, turn: vm.selectedTurn, conversationId: vm.thread.threadId }, text);
    if (input) {
      input.value = "";
    }
  });

  requestAnimationFrame(() => syncTranscriptFeed(container, vm.selectedNode, vm.selectedTurn, vm.headTurn));
}
