import { getConversationRootId, getConversationRoots, getNodeId, getTurns, parseNodeId, pickDefaultNodeId, pickDefaultThreadId, sortTurns } from "./selectors.js";

function ensureTurnBucket(state, threadId, turnId) {
  const key = `${threadId}:${turnId ?? "none"}`;
  if (!state.eventsByTurn[key]) {
    state.eventsByTurn[key] = [];
  }
  return key;
}

function nodeExists(state, nodeId) {
  const { threadId, turnId } = parseNodeId(nodeId);
  if (!threadId || !state.threads[threadId]) {
    return false;
  }
  if (!turnId) {
    return true;
  }
  return Boolean(getTurns(state, threadId).find((turn) => turn.turnId === turnId));
}

function ensureSelection(state) {
  const roots = getConversationRoots(state);
  if (!roots.length) {
    state.selectedConversationId = null;
    state.selectedThreadId = null;
    state.selectedNodeId = null;
    state.expandedTurnKey = null;
    state.forcedBranchNodeId = null;
    state.pendingMergeSourceNodeId = null;
    state.compare = {
      open: false,
      leftNodeId: null,
      rightNodeId: null,
    };
    return;
  }

  if (!state.selectedConversationId || !state.threads[state.selectedConversationId] || state.threads[state.selectedConversationId].parentThreadId) {
    state.selectedConversationId = roots[0].threadId;
  }

  const conversationId = state.selectedConversationId;
  if (!state.selectedThreadId || getConversationRootId(state, state.selectedThreadId) !== conversationId) {
    state.selectedThreadId = pickDefaultThreadId(state, conversationId);
  }

  if (!state.selectedNodeId || !nodeExists(state, state.selectedNodeId)) {
    state.selectedNodeId = pickDefaultNodeId(state, state.selectedThreadId);
  }

  if (state.selectedNodeId) {
    const { threadId } = parseNodeId(state.selectedNodeId);
    if (threadId) {
      state.selectedThreadId = threadId;
    }
  }

  if (state.expandedTurnKey && !nodeExists(state, state.expandedTurnKey)) {
    state.expandedTurnKey = null;
  }
  if (state.forcedBranchNodeId && !nodeExists(state, state.forcedBranchNodeId)) {
    state.forcedBranchNodeId = null;
  }
  if (state.pendingMergeSourceNodeId && !nodeExists(state, state.pendingMergeSourceNodeId)) {
    state.pendingMergeSourceNodeId = null;
  }
  if (state.compare.leftNodeId && !nodeExists(state, state.compare.leftNodeId)) {
    state.compare.leftNodeId = null;
  }
  if (state.compare.rightNodeId && !nodeExists(state, state.compare.rightNodeId)) {
    state.compare.rightNodeId = null;
  }
  if (state.compare.open && !state.compare.leftNodeId && !state.compare.rightNodeId) {
    state.compare.open = false;
  }
}

function ensureModalState(state) {
  const { sourceThreadId, targetThreadId, targetTurnId } = state.importModal;
  if (sourceThreadId && !state.threads[sourceThreadId]) {
    state.importModal = {
      open: false,
      preview: null,
      sourceThreadId: null,
      sourceTurnIds: [],
      targetThreadId: null,
      targetTurnId: null,
      loading: false,
      error: "",
    };
    return;
  }
  if (targetThreadId && !state.threads[targetThreadId]) {
    state.importModal.targetThreadId = null;
    state.importModal.targetTurnId = null;
    state.importModal.preview = null;
  }
  if (targetThreadId && targetTurnId && !getTurns(state, targetThreadId).some((turn) => turn.turnId === targetTurnId)) {
    state.importModal.targetTurnId = null;
    state.importModal.preview = null;
  }
}

export function createStore() {
  const listeners = new Set();
  let eventEmitTimer = null;
  const state = {
    threads: {},
    turnsByThread: {},
    eventsByTurn: {},
    approvals: {},
    selectedConversationId: null,
    selectedThreadId: null,
    selectedNodeId: null,
    expandedTurnKey: null,
    viewMode: "focus",
    composerFocusNonce: 0,
    forcedBranchNodeId: null,
    pendingMergeSourceNodeId: null,
    compare: {
      open: false,
      leftNodeId: null,
      rightNodeId: null,
    },
    lastEventId: 0,
    connectionStatus: "connecting",
    importModal: {
      open: false,
      preview: null,
      sourceThreadId: null,
      sourceTurnIds: [],
      targetThreadId: null,
      targetTurnId: null,
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

  function scheduleEventEmit() {
    if (eventEmitTimer) {
      return;
    }
    eventEmitTimer = window.setTimeout(() => {
      eventEmitTimer = null;
      emit();
    }, 75);
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
    selectConversation(threadId) {
      state.selectedConversationId = getConversationRootId(state, threadId) || threadId;
      state.selectedThreadId = pickDefaultThreadId(state, state.selectedConversationId);
      state.selectedNodeId = pickDefaultNodeId(state, state.selectedThreadId);
      state.forcedBranchNodeId = null;
      state.pendingMergeSourceNodeId = null;
      state.compare = {
        open: false,
        leftNodeId: null,
        rightNodeId: null,
      };
      ensureModalState(state);
      emit();
    },
    selectNode(threadId, turnId = null) {
      const nextConversationId = getConversationRootId(state, threadId) || threadId;
      const nextNodeId = getNodeId(threadId, turnId);
      if (state.selectedConversationId && state.selectedConversationId !== nextConversationId) {
        state.pendingMergeSourceNodeId = null;
        state.compare = {
          open: false,
          leftNodeId: null,
          rightNodeId: null,
        };
      }
      state.selectedConversationId = nextConversationId;
      state.selectedThreadId = threadId;
      state.selectedNodeId = nextNodeId;
      if (state.forcedBranchNodeId && state.forcedBranchNodeId !== nextNodeId) {
        state.forcedBranchNodeId = null;
      }
      ensureModalState(state);
      emit();
    },
    toggleTurnExpanded(threadId, turnId) {
      const key = getNodeId(threadId, turnId);
      state.selectedConversationId = getConversationRootId(state, threadId) || threadId;
      state.selectedThreadId = threadId;
      state.selectedNodeId = key;
      state.expandedTurnKey = state.expandedTurnKey === key ? null : key;
      emit();
    },
    setViewMode(mode) {
      state.viewMode = mode === "map" ? "map" : "focus";
      emit();
    },
    requestComposerFocus() {
      state.composerFocusNonce += 1;
      emit();
    },
    armBranchFromNode(threadId, turnId = null) {
      state.selectedConversationId = getConversationRootId(state, threadId) || threadId;
      state.selectedThreadId = threadId;
      state.selectedNodeId = getNodeId(threadId, turnId);
      state.forcedBranchNodeId = getNodeId(threadId, turnId);
      state.composerFocusNonce += 1;
      emit();
    },
    clearBranchIntent() {
      state.forcedBranchNodeId = null;
      emit();
    },
    startMergeFromNode(threadId, turnId = null) {
      state.selectedConversationId = getConversationRootId(state, threadId) || threadId;
      state.selectedThreadId = threadId;
      state.selectedNodeId = getNodeId(threadId, turnId);
      state.pendingMergeSourceNodeId = getNodeId(threadId, turnId);
      state.viewMode = "map";
      emit();
    },
    clearPendingMerge() {
      state.pendingMergeSourceNodeId = null;
      emit();
    },
    openCompare(nodeId) {
      state.compare = {
        open: true,
        leftNodeId: nodeId,
        rightNodeId: null,
      };
      emit();
    },
    setCompareRight(nodeId) {
      if (!state.compare.open || !state.compare.leftNodeId) {
        state.compare = {
          open: true,
          leftNodeId: nodeId,
          rightNodeId: null,
        };
      } else if (state.compare.leftNodeId !== nodeId) {
        state.compare.rightNodeId = nodeId;
      }
      emit();
    },
    swapCompareSides() {
      if (!state.compare.open || !state.compare.leftNodeId || !state.compare.rightNodeId) {
        return;
      }
      state.compare = {
        ...state.compare,
        leftNodeId: state.compare.rightNodeId,
        rightNodeId: state.compare.leftNodeId,
      };
      emit();
    },
    closeCompare() {
      state.compare = {
        open: false,
        leftNodeId: null,
        rightNodeId: null,
      };
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
      for (const approval of payload.snapshot.approvals || payload.snapshot.pendingApprovals || []) {
        state.approvals[approval.approvalId] = approval;
      }
      for (const event of payload.events) {
        this.applyEvent(event, false);
      }
      state.lastEventId = payload.lastEventId ?? 0;
      ensureSelection(state);
      ensureModalState(state);
      emit();
    },
    applyThread(thread) {
      state.threads[thread.threadId] = thread;
      ensureSelection(state);
      ensureModalState(state);
      emit();
    },
    applyTurn(turn) {
      const list = state.turnsByThread[turn.threadId] || [];
      const next = list.filter((item) => item.turnId !== turn.turnId);
      next.push(turn);
      state.turnsByThread[turn.threadId] = sortTurns(next);
      ensureSelection(state);
      ensureModalState(state);
      emit();
    },
    applyTurns(turns) {
      for (const turn of turns) {
        const list = state.turnsByThread[turn.threadId] || [];
        const next = list.filter((item) => item.turnId !== turn.turnId);
        next.push(turn);
        state.turnsByThread[turn.threadId] = sortTurns(next);
      }
      ensureSelection(state);
      ensureModalState(state);
      emit();
    },
    removeThread(threadId) {
      delete state.threads[threadId];
      delete state.turnsByThread[threadId];
      for (const key of Object.keys(state.eventsByTurn)) {
        if (key.startsWith(`${threadId}:`)) {
          delete state.eventsByTurn[key];
        }
      }
      for (const approvalId of Object.keys(state.approvals)) {
        if (state.approvals[approvalId].threadId === threadId) {
          delete state.approvals[approvalId];
        }
      }
      ensureSelection(state);
      ensureModalState(state);
      emit();
    },
    applyApproval(approval) {
      state.approvals[approval.approvalId] = approval;
      emit();
    },
    applyEvent(event, emitChange = true) {
      const key = ensureTurnBucket(state, event.threadId, event.turnId);
      state.eventsByTurn[key].push(event);
      state.eventsByTurn[key].sort((a, b) => a.seq - b.seq);
      state.lastEventId = Math.max(state.lastEventId, event.eventId || 0);
      if (emitChange) {
        scheduleEventEmit();
      }
    },
    openImportModal({ sourceThreadId, sourceTurnIds = [], targetThreadId = null, targetTurnId = null }) {
      state.importModal = {
        open: true,
        preview: null,
        sourceThreadId,
        sourceTurnIds,
        targetThreadId,
        targetTurnId,
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
        sourceTurnIds: [],
        targetThreadId: null,
        targetTurnId: null,
        loading: false,
        error: "",
      };
      emit();
    },
    setImportModalState(patch) {
      state.importModal = { ...state.importModal, ...patch };
      ensureModalState(state);
      emit();
    },
  };
}
