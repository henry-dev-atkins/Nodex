import { apiGet, apiPost, getToken } from "./api.js";
import { renderActionBar } from "./components/ActionBar.js";
import { renderAppShell } from "./components/AppShell.js";
import { renderComparePanel } from "./components/ComparePanel.js";
import { renderContextPanel } from "./components/ContextPanel.js";
import { renderGraphView } from "./components/GraphView.js";
import { renderImportPreviewModal } from "./components/ImportPreviewModal.js";
import { renderMergeModePickerModal } from "./components/MergeModePickerModal.js";
import { renderThreadList } from "./components/ThreadList.js";
import { renderTranscript } from "./components/Transcript.js";
import { createLayoutController } from "./layout.js";
import { threadLabel } from "./rendering.js";
import { getBranchLabel, getHeadTurn, getNodeId, getSelectedNode, getSelectedThread, getTurns, parseNodeId } from "./selectors.js";
import { createStore } from "./store.js";
import { createUiActions } from "./uiActions.js";
import { connectEventStream } from "./ws.js";

const store = createStore();
const elements = renderAppShell(document.querySelector("#app"));
const layout = createLayoutController(elements);
const uiActions = createUiActions(store);

let lastComposerFocusNonce = 0;
let contextMenuCleanup = null;

function setStatusIndicator(status) {
  const labels = {
    connecting: "Connecting",
    replaying: "Syncing",
    live: "Live",
    offline: "Offline",
    error: "Error",
  };
  const tone = status === "live"
    ? "is-live"
    : status === "error"
      ? "is-error"
      : status === "connecting" || status === "replaying"
        ? "is-running"
        : "is-idle";
  elements.status.className = `status-dot ${tone}`;
  elements.status.title = labels[status] || status;
  elements.status.setAttribute("aria-label", labels[status] || status);
}

function workspaceNameFromPath(path) {
  const value = String(path || "").trim();
  if (!value) {
    return "";
  }
  const trimmed = value.replace(/[\\/]+$/, "");
  if (!trimmed) {
    return "";
  }
  const parts = trimmed.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || trimmed;
}

function resolveWorkspacePath(state, selectedThread) {
  if (selectedThread?.metadata?.cwd) {
    return selectedThread.metadata.cwd;
  }
  for (const thread of Object.values(state.threads)) {
    if (thread?.metadata?.cwd) {
      return thread.metadata.cwd;
    }
  }
  return "";
}

function focusComposer(state) {
  if (state.composerFocusNonce === lastComposerFocusNonce) {
    return;
  }
  lastComposerFocusNonce = state.composerFocusNonce;
  if (state.viewMode !== "focus") {
    return;
  }
  const composer = elements.focusTranscript.querySelector("[data-transcript-composer-input]");
  composer?.focus();
}

function closeContextMenu() {
  contextMenuCleanup?.();
  contextMenuCleanup = null;
  elements.contextMenu.hidden = true;
  elements.contextMenu.innerHTML = "";
  elements.contextMenu.removeAttribute("style");
}

function openContextMenu({ x, y, items }) {
  const menuItems = items.filter(Boolean);
  if (!menuItems.length) {
    closeContextMenu();
    return;
  }
  closeContextMenu();
  elements.contextMenu.innerHTML = `
    <div class="context-menu-panel" role="menu">
      ${menuItems
        .map(
          (item, index) => `
            <button
              type="button"
              class="context-menu-item ${item.danger ? "is-danger" : ""}"
              data-context-menu-index="${index}"
              ${item.disabled ? "disabled" : ""}
            >
              ${item.label}
            </button>
          `,
        )
        .join("")}
    </div>
  `;
  elements.contextMenu.hidden = false;
  elements.contextMenu.style.left = `${x}px`;
  elements.contextMenu.style.top = `${y}px`;

  elements.contextMenu.querySelectorAll("[data-context-menu-index]").forEach((button) => {
    button.addEventListener("click", async () => {
      const item = menuItems[Number(button.dataset.contextMenuIndex)];
      closeContextMenu();
      if (item?.disabled) {
        return;
      }
      await item?.onSelect?.();
    });
  });

  requestAnimationFrame(() => {
    const panel = elements.contextMenu.querySelector(".context-menu-panel");
    if (!panel) {
      return;
    }
    const rect = panel.getBoundingClientRect();
    const clampedX = Math.max(8, Math.min(x, window.innerWidth - rect.width - 8));
    const clampedY = Math.max(8, Math.min(y, window.innerHeight - rect.height - 8));
    elements.contextMenu.style.left = `${clampedX}px`;
    elements.contextMenu.style.top = `${clampedY}px`;
  });

  const handlePointerDown = (event) => {
    if (!elements.contextMenu.contains(event.target)) {
      closeContextMenu();
    }
  };
  const handleKeyDown = (event) => {
    if (event.key === "Escape") {
      closeContextMenu();
    }
  };
  const handleViewportChange = () => {
    closeContextMenu();
  };

  window.addEventListener("pointerdown", handlePointerDown, true);
  window.addEventListener("keydown", handleKeyDown);
  window.addEventListener("resize", handleViewportChange);
  window.addEventListener("scroll", handleViewportChange, true);
  contextMenuCleanup = () => {
    window.removeEventListener("pointerdown", handlePointerDown, true);
    window.removeEventListener("keydown", handleKeyDown);
    window.removeEventListener("resize", handleViewportChange);
    window.removeEventListener("scroll", handleViewportChange, true);
  };
}

function renderTranscriptInto(container, state) {
  renderTranscript(container, state, {
    onSelectNode(threadId, turnId) {
      return uiActions.selectNodeOrMerge(threadId, turnId);
    },
    onToggleUserExpanded(threadId, turnId) {
      store.toggleUserExpanded(threadId, turnId);
    },
    onToggleAssistantExpanded(threadId, turnId) {
      store.toggleAssistantExpanded(threadId, turnId);
    },
    onToggleAuxPanel(threadId, turnId, panel) {
      store.toggleTurnAuxPanel(threadId, turnId, panel);
    },
    async onApprovalDecision(approvalId, decision) {
      try {
        await apiPost(`/api/approvals/${approvalId}`, { decision });
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
    async onDeleteConversation(conversationId) {
      try {
        await uiActions.deleteConversation(conversationId);
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
    async onSubmit(node, text) {
      try {
        await uiActions.submitFromNode(node, text);
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
    async onInterrupt(threadId) {
      try {
        await uiActions.interruptThread(threadId);
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
  });
}

function renderShell(state) {
  const selectedThread = getSelectedThread(state);
  const selectedNode = getSelectedNode(state);
  const branchLabel = selectedThread ? getBranchLabel(state, selectedThread.threadId) : "Branch";
  const currentTurnLabel = `${branchLabel} | ${selectedNode?.turn ? `T${selectedNode.turn.idx}` : "Start"}`;
  const workspacePath = resolveWorkspacePath(state, selectedThread);
  const workspaceName = workspaceNameFromPath(workspacePath);

  setStatusIndicator(state.connectionStatus);
  elements.errorBanner.className = state.errorMessage ? "error-banner" : "";
  elements.errorBanner.textContent = state.errorMessage || "";
  elements.title.textContent = selectedThread ? threadLabel(selectedThread) : "No conversation";
  elements.turnLabel.textContent = currentTurnLabel;
  elements.workspaceLabel.textContent = workspaceName || "workspace";
  elements.workspaceLabel.title = workspacePath || "";
  elements.mainShell.dataset.viewMode = state.viewMode;
  elements.focusModeButton.classList.toggle("is-active", state.viewMode === "focus");
  elements.mapModeButton.classList.toggle("is-active", state.viewMode === "map");
}

function renderViews(state) {
  const selectedThread = getSelectedThread(state);
  const selectedNode = getSelectedNode(state);

  renderThreadList(elements.threadList, state, {
    onSelect(threadId) {
      closeContextMenu();
      store.selectConversation(threadId);
    },
    onContextMenu({ threadId, x, y }) {
      const thread = state.threads[threadId];
      if (!thread) {
        return;
      }
      openContextMenu({
        x,
        y,
        items: [
          {
            label: "Rename",
            async onSelect() {
              const nextTitle = window.prompt("Rename conversation", thread.title || threadLabel(thread));
              if (!nextTitle || nextTitle.trim() === (thread.title || threadLabel(thread)).trim()) {
                return;
              }
              try {
                await uiActions.renameThread(thread.threadId, nextTitle.trim());
              } catch (error) {
                store.setErrorMessage(error.message);
              }
            },
          },
          {
            label: "Delete",
            danger: true,
            async onSelect() {
              if (!window.confirm(`Delete conversation "${threadLabel(thread)}"?`)) {
                return;
              }
              try {
                await uiActions.deleteConversation(thread.threadId);
              } catch (error) {
                store.setErrorMessage(error.message);
              }
            },
          },
        ],
      });
    },
  });
  renderActionBar(elements.actionBar, state, {
    onContinue() {
      if (!selectedThread) {
        return;
      }
      const headTurn = getHeadTurn(state, selectedThread.threadId);
      store.clearPendingMerge();
      store.clearBranchIntent();
      store.selectNode(selectedThread.threadId, headTurn?.turnId || null);
      store.requestComposerFocus();
    },
    onBranch() {
      if (!selectedThread) {
        return;
      }
      const anchorTurn = selectedNode?.thread?.threadId === selectedThread.threadId
        ? selectedNode?.turn
        : getHeadTurn(state, selectedThread.threadId);
      if (!anchorTurn) {
        store.requestComposerFocus();
        return;
      }
      store.clearPendingMerge();
      store.armBranchFromNode(selectedThread.threadId, anchorTurn.turnId);
    },
    onMerge() {
      if (!selectedNode?.turn) {
        return;
      }
      if (state.pendingMergeSourceNodeId === selectedNode.nodeId) {
        store.clearPendingMerge();
        return;
      }
      store.clearBranchIntent();
      store.startMergeFromNode(selectedNode.thread.threadId, selectedNode.turn.turnId);
    },
    onCompare() {
      if (!selectedNode?.turn) {
        return;
      }
      if (state.compare.open && state.compare.leftNodeId && state.compare.leftNodeId !== selectedNode.nodeId) {
        store.setCompareRight(selectedNode.nodeId);
        return;
      }
      store.openCompare(selectedNode.nodeId);
    },
  });
  renderComparePanel(elements.comparePanel, state, {
    onUseCurrent() {
      const current = getSelectedNode(store.getState());
      if (current?.turn) {
        store.setCompareRight(current.nodeId);
      }
    },
    onSwap() {
      store.swapCompareSides();
    },
    onClose() {
      store.closeCompare();
    },
  });
  renderContextPanel(elements.contextPanel, state, {
    onSelectNode(nodeId) {
      const node = parseNodeId(nodeId);
      uiActions.selectNodeOrMerge(node.threadId, node.turnId);
    },
  });
  renderGraphView(elements.graphView, state, {
    onSelectNode({ threadId, turnId }) {
      uiActions.selectNodeOrMerge(threadId, turnId);
    },
    onNodeContextMenu({ threadId, turnId, x, y }) {
      const thread = state.threads[threadId];
      const turn = turnId ? getTurns(state, threadId).find((item) => item.turnId === turnId) || null : null;
      if (!thread) {
        return;
      }
      const turns = getTurns(state, threadId);
      const isEmptyStartNode = !turn && turns.length === 0;
      if (isEmptyStartNode) {
        const canDeleteBranch = Boolean(thread.parentThreadId);
        openContextMenu({
          x,
          y,
          items: [
            {
              label: canDeleteBranch ? "Delete Empty Branch" : "Delete Empty Conversation",
              danger: true,
              async onSelect() {
                const label = canDeleteBranch ? getBranchLabel(state, threadId) : threadLabel(thread);
                if (!window.confirm(`Delete "${label}"?`)) {
                  return;
                }
                try {
                  if (canDeleteBranch) {
                    await uiActions.deleteBranch(threadId);
                  } else {
                    await uiActions.deleteConversation(threadId);
                  }
                } catch (error) {
                  store.setErrorMessage(error.message);
                }
              },
            },
          ],
        });
        return;
      }
      if (!turn) {
        return;
      }
      const canDeleteBranch = Boolean(thread.parentThreadId);
      const isHead = turns[turns.length - 1]?.turnId === turnId;
      const node = {
        nodeId: getNodeId(threadId, turnId),
        thread,
        turn,
      };
      openContextMenu({
        x,
        y,
        items: [
          {
            label: isHead ? "Current Head" : "Set Head",
            disabled: isHead,
            async onSelect() {
              try {
                await uiActions.setHeadFromNode(node);
              } catch (error) {
                store.setErrorMessage(error.message);
              }
            },
          },
          {
            label: canDeleteBranch ? "Delete Branch" : "Delete Branch (main locked)",
            danger: true,
            disabled: !canDeleteBranch,
            async onSelect() {
              if (!window.confirm(`Delete branch "${getBranchLabel(state, threadId)}" and its descendants?`)) {
                return;
              }
              try {
                await uiActions.deleteBranch(threadId);
              } catch (error) {
                store.setErrorMessage(error.message);
              }
            },
          },
        ],
      });
    },
    onCreateLink({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId }) {
      store.clearPendingMerge();
      uiActions.openMergeModePicker({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId });
    },
    onLaneOrderChange() {
      render();
    },
    onNodePositionChange() {
      render();
    },
  });
  renderTranscriptInto(elements.focusTranscript, state);
  renderImportPreviewModal(elements.importModal, state, {
    onClose() {
      store.closeImportModal();
    },
    async onSelectMode(mergeMode) {
      try {
        await uiActions.requestImportPreview({
          sourceThreadId: state.importModal.sourceThreadId,
          sourceTurnId: state.importModal.sourceAnchorTurnId,
          targetThreadId: state.importModal.targetThreadId,
          targetTurnId: state.importModal.targetTurnId,
          mergeMode,
        });
      } catch (error) {
        store.setImportModalState({ error: error.message });
      }
    },
    async onCommit(editedTransferBlob) {
      try {
        await uiActions.commitImport(editedTransferBlob);
      } catch (error) {
        store.setImportModalState({ error: error.message });
      }
    },
  });
  renderMergeModePickerModal(elements.mergeModePicker, state, {
    onClose() {
      store.closeMergeModePicker();
    },
    onSelectMode(mode) {
      const picker = state.mergeModePicker;
      if (!picker.sourceThreadId || !picker.sourceTurnId || !picker.targetThreadId || !picker.targetTurnId) {
        store.closeMergeModePicker();
        return;
      }
      store.closeMergeModePicker();
      uiActions.openLinkedChildModal({
        sourceThreadId: picker.sourceThreadId,
        sourceTurnId: picker.sourceTurnId,
        targetThreadId: picker.targetThreadId,
        targetTurnId: picker.targetTurnId,
        mergeMode: mode,
      });
    },
  });
}

function render() {
  const state = store.getState();
  renderShell(state);
  renderViews(state);
  focusComposer(state);
}

async function createConversation() {
  try {
    const response = await apiPost("/api/threads", { title: null });
    store.applyThread(response.thread);
    store.selectConversation(response.thread.threadId);
    store.selectNode(response.thread.threadId, null);
  } catch (error) {
    store.setErrorMessage(error.message);
  }
}

async function bootstrap() {
  try {
    const payload = await apiGet("/api/bootstrap");
    store.applyBootstrap(payload);
    connectEventStream(getToken(), store);
  } catch (error) {
    store.setErrorMessage(error.message);
    setStatusIndicator("error");
  }
}

store.subscribe(render);

elements.newThread.addEventListener("click", createConversation);
elements.focusModeButton.addEventListener("click", () => store.setViewMode("focus"));
elements.mapModeButton.addEventListener("click", () => store.setViewMode("map"));

layout.attachResizers();
window.addEventListener("resize", () => layout.applyLayoutState());
layout.applyLayoutState();

bootstrap();
render();
