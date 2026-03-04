import { apiDelete, apiPost } from "./api.js";
import { getHeadTurn, parseNodeId } from "./selectors.js";

export function createUiActions(store) {
  async function submitFromNode(node, text) {
    const state = store.getState();
    const forcedBranch = state.forcedBranchNodeId && state.forcedBranchNodeId === node.nodeId;
    const headTurn = getHeadTurn(state, node.thread.threadId);
    const shouldBranch = Boolean((node.turn && headTurn?.turnId !== node.turn.turnId) || forcedBranch);
    if (shouldBranch) {
      const response = await apiPost(`/api/threads/${node.thread.threadId}/branch`, {
        turnId: node.turn?.turnId || headTurn?.turnId,
        title: null,
      });
      const branchTurns = response.turns || [];
      store.applyThread(response.thread);
      store.applyTurns(branchTurns);
      store.selectNode(response.thread.threadId, branchTurns.length ? branchTurns[branchTurns.length - 1].turnId : null);
      store.clearBranchIntent();
      await apiPost(`/api/threads/${response.thread.threadId}/turns`, { text });
      return;
    }
    store.clearBranchIntent();
    await apiPost(`/api/threads/${node.thread.threadId}/turns`, { text });
  }

  async function deleteConversation(threadId) {
    const response = await apiDelete(`/api/conversations/${threadId}`);
    for (const deletedId of response.deletedThreadIds || []) {
      store.removeThread(deletedId);
    }
  }

  async function renameThread(threadId, title) {
    const response = await apiPost(`/api/threads/${threadId}/rename`, { title });
    if (response.thread) {
      store.applyThread(response.thread);
    }
    return response.thread;
  }

  async function deleteBranch(threadId) {
    const response = await apiDelete(`/api/threads/${threadId}`);
    for (const deletedId of response.deletedThreadIds || []) {
      store.removeThread(deletedId);
    }
    return response;
  }

  async function setHeadFromNode(node) {
    if (!node?.thread?.threadId || !node?.turn?.turnId) {
      return null;
    }
    const state = store.getState();
    const headTurn = getHeadTurn(state, node.thread.threadId);
    if (headTurn?.turnId === node.turn.turnId) {
      store.selectNode(node.thread.threadId, node.turn.turnId);
      store.setViewMode("focus");
      return null;
    }
    const response = await apiPost(`/api/threads/${node.thread.threadId}/branch`, {
      turnId: node.turn.turnId,
      title: null,
    });
    const branchTurns = response.turns || [];
    if (response.thread) {
      store.applyThread(response.thread);
    }
    if (branchTurns.length) {
      store.applyTurns(branchTurns);
      const nextHead = branchTurns[branchTurns.length - 1];
      store.selectNode(response.thread.threadId, nextHead.turnId);
    } else if (response.thread) {
      store.selectNode(response.thread.threadId, null);
    }
    store.clearBranchIntent();
    store.clearPendingMerge();
    store.setViewMode("focus");
    return response.thread || null;
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

  function maybeHandlePendingMergeTarget(threadId, turnId) {
    const state = store.getState();
    const pendingSource = state.pendingMergeSourceNodeId ? parseNodeId(state.pendingMergeSourceNodeId) : null;
    if (!pendingSource?.threadId || !pendingSource.turnId || !turnId) {
      return false;
    }
    if (pendingSource.threadId === threadId && pendingSource.turnId === turnId) {
      return false;
    }
    openLinkedChildModal({
      sourceThreadId: pendingSource.threadId,
      sourceTurnId: pendingSource.turnId,
      targetThreadId: threadId,
      targetTurnId: turnId,
    });
    store.clearPendingMerge();
    return true;
  }

  function selectNodeOrMerge(threadId, turnId) {
    if (maybeHandlePendingMergeTarget(threadId, turnId)) {
      return false;
    }
    store.selectNode(threadId, turnId);
    return true;
  }

  async function commitImport(editedTransferBlob) {
    const current = store.getState();
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
  }

  return {
    deleteBranch,
    deleteConversation,
    requestImportPreview,
    openLinkedChildModal,
    selectNodeOrMerge,
    submitFromNode,
    commitImport,
    renameThread,
    setHeadFromNode,
  };
}
