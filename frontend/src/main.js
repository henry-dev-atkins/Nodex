import { apiGet, apiPost, getToken } from "./api.js";
import { renderApprovalModal } from "./components/ApprovalModal.js";
import { updateComposer } from "./components/Composer.js";
import { renderGraphView } from "./components/GraphView.js";
import { renderImportPreviewModal } from "./components/ImportPreviewModal.js";
import { renderThreadList } from "./components/ThreadList.js";
import { renderTranscript } from "./components/Transcript.js";
import { createStore } from "./store.js";
import { connectEventStream } from "./ws.js";

const store = createStore();
const elements = {
  threadList: document.querySelector("#thread-list"),
  graphView: document.querySelector("#graph-view"),
  errorBanner: document.querySelector("#error-banner"),
  transcript: document.querySelector("#transcript-view"),
  approvalModal: document.querySelector("#approval-modal-root"),
  importModal: document.querySelector("#import-modal-root"),
  title: document.querySelector("#thread-title"),
  status: document.querySelector("#connection-status"),
  newThread: document.querySelector("#new-thread-button"),
  fork: document.querySelector("#fork-thread-button"),
  importButton: document.querySelector("#begin-import-button"),
  form: document.querySelector("#composer-form"),
  input: document.querySelector("#composer-input"),
  submit: document.querySelector("#composer-submit"),
};

function setStatusChip(status) {
  const labels = {
    connecting: "Connecting",
    replaying: "Syncing",
    live: "Live",
    offline: "Offline",
    error: "Error",
  };
  elements.status.textContent = labels[status] || status;
  elements.status.className = `status-chip ${status === "live" ? "live" : status === "error" ? "error" : ""}`;
}

function render() {
  const state = store.getState();
  setStatusChip(state.connectionStatus);
  elements.errorBanner.className = state.errorMessage ? "error-banner" : "";
  elements.errorBanner.textContent = state.errorMessage || "";
  renderThreadList(elements.threadList, state, (threadId) => store.setSelectedThread(threadId));
  renderGraphView(elements.graphView, state, (threadId) => store.setSelectedThread(threadId));
  renderTranscript(elements.transcript, state, {
    onToggleImportTurn(threadId, turnId) {
      store.toggleImportTurn(threadId, turnId);
    },
  });
  renderApprovalModal(elements.approvalModal, state, {
    async onDecision(approvalId, decision) {
      try {
        await apiPost(`/api/approvals/${approvalId}`, { decision });
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
  });
  renderImportPreviewModal(elements.importModal, state, {
    onClose() {
      store.closeImportModal();
    },
    async onPreview(targetThreadId) {
      const current = store.getState();
      const selected = Object.keys(current.importSelection)
        .filter((key) => current.importSelection[key] && key.startsWith(`${current.selectedThreadId}:`))
        .map((key) => key.split(":")[1]);
      if (!targetThreadId) {
        store.setImportModalState({ error: "Select a destination thread." });
        return;
      }
      try {
        store.setImportModalState({ loading: true, error: "", targetThreadId });
        const preview = await apiPost("/api/import/preview", {
          sourceThreadId: current.selectedThreadId,
          sourceTurnIds: selected,
          destThreadId: targetThreadId,
        });
        store.setImportModalState({ loading: false, preview, targetThreadId });
      } catch (error) {
        store.setImportModalState({ loading: false, error: error.message });
      }
    },
    async onCommit(editedTransferBlob) {
      const current = store.getState();
      try {
        await apiPost("/api/import/commit", {
          previewId: current.importModal.preview.previewId,
          confirmed: true,
          editedTransferBlob,
        });
        store.closeImportModal();
        store.clearImportSelection();
      } catch (error) {
        store.setImportModalState({ error: error.message });
      }
    },
  });
  updateComposer(state, elements, {
    async onSubmit(threadId, text) {
      try {
        await apiPost(`/api/threads/${threadId}/turns`, { text });
      } catch (error) {
        store.setErrorMessage(error.message);
      }
    },
  });
}

store.subscribe(render);

elements.newThread.addEventListener("click", async () => {
  try {
    const response = await apiPost("/api/threads", { title: null });
    store.applyThread(response.thread);
    store.setSelectedThread(response.thread.threadId);
  } catch (error) {
    store.setErrorMessage(error.message);
  }
});

elements.fork.addEventListener("click", async () => {
  const state = store.getState();
  if (!state.selectedThreadId) {
    return;
  }
  try {
    const response = await apiPost(`/api/threads/${state.selectedThreadId}/fork`, { title: null });
    store.applyThread(response.thread);
  } catch (error) {
    store.setErrorMessage(error.message);
  }
});

elements.importButton.addEventListener("click", () => {
  const state = store.getState();
  if (state.selectedThreadId) {
    store.openImportModal(state.selectedThreadId);
  }
});

async function bootstrap() {
  try {
    const payload = await apiGet("/api/bootstrap");
    store.applyBootstrap(payload);
    connectEventStream(getToken(), store);
  } catch (error) {
    store.setErrorMessage(error.message);
    setStatusChip("error");
  }
}

bootstrap();
render();
