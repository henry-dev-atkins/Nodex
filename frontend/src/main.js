import { apiGet, apiPost, getToken } from "./api.js";
import { renderActionBar } from "./components/ActionBar.js";
import { renderAppShell } from "./components/AppShell.js";
import { renderComparePanel } from "./components/ComparePanel.js";
import { renderContextPanel } from "./components/ContextPanel.js";
import { renderGraphView } from "./components/GraphView.js";
import { renderImportPreviewModal } from "./components/ImportPreviewModal.js";
import { renderThreadList } from "./components/ThreadList.js";
import { renderTranscript } from "./components/Transcript.js";
import { createLayoutController } from "./layout.js";
import { threadLabel } from "./rendering.js";
import { getBranchLabel, getHeadTurn, getSelectedNode, getSelectedThread, parseNodeId } from "./selectors.js";
import { createStore } from "./store.js";
import { createUiActions } from "./uiActions.js";
import { connectEventStream } from "./ws.js";

const store = createStore();
const elements = renderAppShell(document.querySelector("#app"));
const layout = createLayoutController(elements);
const uiActions = createUiActions(store);

let lastComposerFocusNonce = 0;

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

function focusComposer(state) {
  if (state.composerFocusNonce === lastComposerFocusNonce) {
    return;
  }
  lastComposerFocusNonce = state.composerFocusNonce;
  const composer = state.viewMode === "map"
    ? elements.mapTranscript.querySelector("[data-transcript-composer-input]")
    : elements.focusTranscript.querySelector("[data-transcript-composer-input]");
  composer?.focus();
}

function renderTranscriptInto(container, state) {
  renderTranscript(container, state, {
    onToggleTurn(threadId, turnId) {
      store.toggleTurnExpanded(threadId, turnId);
    },
    onSelectNode(threadId, turnId) {
      return uiActions.selectNodeOrMerge(threadId, turnId);
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
  });
}

function renderShell(state) {
  const selectedThread = getSelectedThread(state);
  const selectedNode = getSelectedNode(state);
  const branchLabel = selectedThread ? getBranchLabel(state, selectedThread.threadId) : "Branch";
  const currentTurnLabel = `${branchLabel} | ${selectedNode?.turn ? `T${selectedNode.turn.idx}` : "Start"}`;

  setStatusIndicator(state.connectionStatus);
  elements.errorBanner.className = state.errorMessage ? "error-banner" : "";
  elements.errorBanner.textContent = state.errorMessage || "";
  elements.title.textContent = selectedThread ? threadLabel(selectedThread) : "No conversation";
  elements.turnLabel.textContent = currentTurnLabel;
  elements.mainShell.dataset.viewMode = state.viewMode;
  elements.focusModeButton.classList.toggle("is-active", state.viewMode === "focus");
  elements.mapModeButton.classList.toggle("is-active", state.viewMode === "map");
}

function renderViews(state) {
  const selectedThread = getSelectedThread(state);
  const selectedNode = getSelectedNode(state);

  renderThreadList(elements.threadList, state, (threadId) => store.selectConversation(threadId));
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
      const anchorTurn = selectedNode?.turn || getHeadTurn(state, selectedThread.threadId);
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
    onCreateLink({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId }) {
      store.clearPendingMerge();
      uiActions.openLinkedChildModal({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId });
    },
    onLaneOrderChange() {
      render();
    },
  });
  renderTranscriptInto(elements.focusTranscript, state);
  renderTranscriptInto(elements.mapTranscript, state);
  renderImportPreviewModal(elements.importModal, state, {
    onClose() {
      store.closeImportModal();
    },
    async onCommit(editedTransferBlob) {
      try {
        await uiActions.commitImport(editedTransferBlob);
      } catch (error) {
        store.setImportModalState({ error: error.message });
      }
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
