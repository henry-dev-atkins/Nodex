import { apiDelete, apiGet, apiPost, getToken } from "./api.js";
import { renderGraphView } from "./components/GraphView.js";
import { renderImportPreviewModal } from "./components/ImportPreviewModal.js";
import { renderThreadList } from "./components/ThreadList.js";
import { renderTranscript } from "./components/Transcript.js";
import { threadLabel } from "./rendering.js";
import { getBranchLabel, getHeadTurn, getSelectedNode, getSelectedThread } from "./selectors.js";
import { createStore } from "./store.js";
import { connectEventStream } from "./ws.js";

const store = createStore();
const LAYOUT_STORAGE_KEY = "codex-ui-layout-v3";
const DEFAULT_LAYOUT = {
  sidebarWidth: 240,
  graphHeight: 320,
};

const elements = {
  app: document.querySelector("#app"),
  mainShell: document.querySelector(".main-shell"),
  graphPanel: document.querySelector(".graph-panel"),
  threadList: document.querySelector("#thread-list"),
  graphView: document.querySelector("#graph-view"),
  errorBanner: document.querySelector("#error-banner"),
  transcript: document.querySelector("#transcript-view"),
  importModal: document.querySelector("#import-modal-root"),
  title: document.querySelector("#thread-title"),
  turnLabel: document.querySelector("#thread-turn-label"),
  status: document.querySelector("#connection-status"),
  newThread: document.querySelector("#new-thread-button"),
  sidebarResizer: document.querySelector("#sidebar-resizer"),
  graphTranscriptResizer: document.querySelector("#graph-transcript-resizer"),
};

let layoutState = readLayoutState();

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function readLayoutState() {
  try {
    const raw = window.localStorage.getItem(LAYOUT_STORAGE_KEY);
    if (!raw) {
      return { ...DEFAULT_LAYOUT };
    }
    const parsed = JSON.parse(raw);
    return {
      sidebarWidth: Number(parsed.sidebarWidth) || DEFAULT_LAYOUT.sidebarWidth,
      graphHeight: Number(parsed.graphHeight) || DEFAULT_LAYOUT.graphHeight,
    };
  } catch {
    return { ...DEFAULT_LAYOUT };
  }
}

function persistLayoutState() {
  window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layoutState));
}

function clampLayoutState(nextState) {
  const appWidth = elements.app?.clientWidth || window.innerWidth;
  const mainHeight = elements.mainShell?.clientHeight || Math.max(window.innerHeight - 120, 480);
  return {
    sidebarWidth: clamp(nextState.sidebarWidth, 180, Math.max(224, Math.min(340, appWidth * 0.28))),
    graphHeight: clamp(nextState.graphHeight, 208, Math.max(248, mainHeight - 224)),
  };
}

function applyLayoutState(partial = {}, persist = false) {
  layoutState = clampLayoutState({ ...layoutState, ...partial });
  document.documentElement.style.setProperty("--sidebar-width", `${Math.round(layoutState.sidebarWidth)}px`);
  document.documentElement.style.setProperty("--graph-height", `${Math.round(layoutState.graphHeight)}px`);
  if (persist) {
    persistLayoutState();
  }
}

function bindResizer(handle, onMove) {
  if (!handle) {
    return;
  }
  handle.addEventListener("pointerdown", (event) => {
    if (window.matchMedia("(max-width: 1080px)").matches) {
      return;
    }
    event.preventDefault();
    document.body.classList.add("is-resizing");
    const cleanup = () => {
      document.body.classList.remove("is-resizing");
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointercancel", handleUp);
    };
    const handleMove = (moveEvent) => {
      onMove(moveEvent);
    };
    const handleUp = () => {
      cleanup();
      persistLayoutState();
    };
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp);
    window.addEventListener("pointercancel", handleUp);
  });
}

function setStatusIndicator(status) {
  const labels = {
    connecting: "Connecting",
    replaying: "Syncing",
    live: "Live",
    offline: "Offline",
    error: "Error",
  };
  const tone = status === "live" ? "is-live" : status === "error" ? "is-error" : status === "connecting" || status === "replaying" ? "is-running" : "is-idle";
  elements.status.className = `status-dot ${tone}`;
  elements.status.title = labels[status] || status;
  elements.status.setAttribute("aria-label", labels[status] || status);
}

async function submitFromNode(node, text) {
  const state = store.getState();
  const headTurn = getHeadTurn(state, node.thread.threadId);
  const shouldBranch = Boolean(node.turn && headTurn?.turnId !== node.turn.turnId);
  if (shouldBranch) {
    const response = await apiPost(`/api/threads/${node.thread.threadId}/branch`, {
      turnId: node.turn.turnId,
      title: null,
    });
    const branchTurns = response.turns || [];
    store.applyThread(response.thread);
    store.applyTurns(branchTurns);
    store.selectNode(response.thread.threadId, branchTurns.length ? branchTurns[branchTurns.length - 1].turnId : null);
    await apiPost(`/api/threads/${response.thread.threadId}/turns`, { text });
    return;
  }
  await apiPost(`/api/threads/${node.thread.threadId}/turns`, { text });
}

async function deleteConversation(threadId) {
  const response = await apiDelete(`/api/conversations/${threadId}`);
  for (const deletedId of response.deletedThreadIds || []) {
    store.removeThread(deletedId);
  }
}

async function requestImportPreview({ sourceThreadId, sourceTurnIds = [], targetThreadId, targetTurnId }) {
  try {
    store.setImportModalState({
      loading: true,
      error: "",
      targetThreadId,
      targetTurnId,
      sourceThreadId,
      sourceTurnIds,
      preview: null,
    });
    const preview = await apiPost("/api/import/preview", {
      sourceThreadId,
      sourceTurnIds,
      destThreadId: targetThreadId,
      destTurnId: targetTurnId,
    });
    store.setImportModalState({
      loading: false,
      preview,
      targetThreadId,
      targetTurnId,
      sourceThreadId,
      sourceTurnIds,
    });
  } catch (error) {
    store.setImportModalState({ loading: false, error: error.message });
  }
}

function openLinkedChildModal({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId }) {
  store.openImportModal({
    sourceThreadId,
    sourceTurnIds: [sourceTurnId],
    targetThreadId,
    targetTurnId,
  });
  void requestImportPreview({
    sourceThreadId,
    sourceTurnIds: [sourceTurnId],
    targetThreadId,
    targetTurnId,
  });
}

function render() {
  const state = store.getState();
  const selectedThread = getSelectedThread(state);
  const selectedNode = getSelectedNode(state);
  const branchLabel = selectedThread ? getBranchLabel(state, selectedThread.threadId) : "Branch";
  const currentTurnLabel = `${branchLabel} | ${selectedNode?.turn ? `T${selectedNode.turn.idx}` : "Start"}`;

  setStatusIndicator(state.connectionStatus);
  elements.errorBanner.className = state.errorMessage ? "error-banner" : "";
  elements.errorBanner.textContent = state.errorMessage || "";
  elements.title.textContent = selectedThread ? threadLabel(selectedThread) : "No conversation";
  elements.turnLabel.textContent = currentTurnLabel;

  renderThreadList(elements.threadList, state, (threadId) => store.selectConversation(threadId));
  renderGraphView(elements.graphView, state, {
    onSelectNode({ threadId, turnId }) {
      store.selectNode(threadId, turnId);
    },
    onCreateLink({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId }) {
      openLinkedChildModal({ sourceThreadId, sourceTurnId, targetThreadId, targetTurnId });
    },
    onLaneOrderChange() {
      render();
    },
  });
  renderTranscript(elements.transcript, state, {
    onToggleTurn(threadId, turnId) {
      store.toggleTurnExpanded(threadId, turnId);
    },
    onToggleTurnMenu(threadId, turnId) {
      store.toggleTurnMenu(threadId, turnId);
    },
    onSelectNode(threadId, turnId) {
      store.selectNode(threadId, turnId);
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
        await deleteConversation(conversationId);
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
    async onSubmit(node, text) {
      try {
        await submitFromNode(node, text);
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
  });
  renderImportPreviewModal(elements.importModal, state, {
    onClose() {
      store.closeImportModal();
    },
    async onCommit(editedTransferBlob) {
      const current = store.getState();
      try {
        const response = await apiPost("/api/import/commit", {
          previewId: current.importModal.preview.previewId,
          confirmed: true,
          editedTransferBlob,
        });
        if (response.thread) {
          store.applyThread(response.thread);
        }
        if (response.turns) {
          store.applyTurns(response.turns);
        }
        if (response.turn) {
          store.applyTurn(response.turn);
          store.selectNode(response.turn.threadId, response.turn.turnId);
        }
        store.closeImportModal();
      } catch (error) {
        store.setImportModalState({ error: error.message });
      }
    },
  });
}

store.subscribe(render);

elements.newThread.addEventListener("click", async () => {
  try {
    const response = await apiPost("/api/threads", { title: null });
    store.applyThread(response.thread);
    store.selectConversation(response.thread.threadId);
    store.selectNode(response.thread.threadId, null);
  } catch (error) {
    store.setErrorMessage(error.message);
  }
});

bindResizer(elements.sidebarResizer, (event) => {
  const appRect = elements.app.getBoundingClientRect();
  applyLayoutState({ sidebarWidth: event.clientX - appRect.left });
});

bindResizer(elements.graphTranscriptResizer, (event) => {
  const panelRect = elements.graphPanel.getBoundingClientRect();
  applyLayoutState({ graphHeight: event.clientY - panelRect.top });
});

window.addEventListener("resize", () => applyLayoutState());
applyLayoutState();

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

bootstrap();
render();
