function sortThreads(threads) {
  return [...threads].sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
}

function sortTurns(turns) {
  return [...turns].sort((a, b) => a.idx - b.idx);
}

export function createStore() {
  const listeners = new Set();
  const state = {
    threads: {},
    turnsByThread: {},
    eventsByTurn: {},
    approvals: {},
    selectedThreadId: null,
    lastEventId: 0,
    connectionStatus: "connecting",
    importSelection: {},
    importModal: {
      open: false,
      preview: null,
      sourceThreadId: null,
      targetThreadId: "",
      loading: false,
      error: "",
    },
    errorMessage: "",
  };

  function emit() {
    for (const listener of listeners) {
      listener(state);
    }
  }

  function ensureTurnBucket(threadId, turnId) {
    const key = `${threadId}:${turnId ?? "none"}`;
    if (!state.eventsByTurn[key]) {
      state.eventsByTurn[key] = [];
    }
    return key;
  }

  return {
    getState() {
      return state;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setConnectionStatus(status) {
      state.connectionStatus = status;
      emit();
    },
    setErrorMessage(message) {
      state.errorMessage = message;
      emit();
    },
    clearErrorMessage() {
      state.errorMessage = "";
      emit();
    },
    setSelectedThread(threadId) {
      state.selectedThreadId = threadId;
      emit();
    },
    applyBootstrap(payload) {
      state.threads = {};
      state.turnsByThread = {};
      state.eventsByTurn = {};
      state.approvals = {};
      for (const thread of payload.snapshot.threads) {
        state.threads[thread.threadId] = thread;
      }
      for (const turn of payload.snapshot.turns) {
        if (!state.turnsByThread[turn.threadId]) {
          state.turnsByThread[turn.threadId] = [];
        }
        state.turnsByThread[turn.threadId].push(turn);
      }
      for (const threadId of Object.keys(state.turnsByThread)) {
        state.turnsByThread[threadId] = sortTurns(state.turnsByThread[threadId]);
      }
      for (const approval of payload.snapshot.pendingApprovals) {
        state.approvals[approval.approvalId] = approval;
      }
      for (const event of payload.events) {
        this.applyEvent(event, false);
      }
      state.lastEventId = payload.lastEventId ?? 0;
      if (!state.selectedThreadId) {
        const ordered = sortThreads(Object.values(state.threads));
        state.selectedThreadId = ordered[0]?.threadId ?? null;
      }
      emit();
    },
    applyThread(thread) {
      state.threads[thread.threadId] = thread;
      if (!state.selectedThreadId) {
        state.selectedThreadId = thread.threadId;
      }
      emit();
    },
    applyTurn(turn) {
      const list = state.turnsByThread[turn.threadId] || [];
      const next = list.filter((item) => item.turnId !== turn.turnId);
      next.push(turn);
      state.turnsByThread[turn.threadId] = sortTurns(next);
      emit();
    },
    applyApproval(approval) {
      state.approvals[approval.approvalId] = approval;
      emit();
    },
    applyEvent(event, emitChange = true) {
      const key = ensureTurnBucket(event.threadId, event.turnId);
      state.eventsByTurn[key].push(event);
      state.eventsByTurn[key].sort((a, b) => a.seq - b.seq);
      state.lastEventId = Math.max(state.lastEventId, event.eventId || 0);
      if (emitChange) {
        emit();
      }
    },
    toggleImportTurn(threadId, turnId) {
      const key = `${threadId}:${turnId}`;
      state.importSelection[key] = !state.importSelection[key];
      emit();
    },
    clearImportSelection() {
      state.importSelection = {};
      emit();
    },
    openImportModal(threadId) {
      state.importModal = {
        open: true,
        preview: null,
        sourceThreadId: threadId,
        targetThreadId: "",
        loading: false,
        error: "",
      };
      emit();
    },
    closeImportModal() {
      state.importModal = {
        open: false,
        preview: null,
        sourceThreadId: null,
        targetThreadId: "",
        loading: false,
        error: "",
      };
      emit();
    },
    setImportModalState(patch) {
      state.importModal = { ...state.importModal, ...patch };
      emit();
    },
  };
}
