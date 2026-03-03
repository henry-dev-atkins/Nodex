export function sortThreads(threads) {
  return [...threads].sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
}

export function sortTurns(turns) {
  return [...turns].sort((a, b) => a.idx - b.idx);
}

export function getNodeId(threadId, turnId = null) {
  return `${threadId}:${turnId || "head"}`;
}

export function parseNodeId(nodeId) {
  if (!nodeId) {
    return { threadId: null, turnId: null };
  }
  const [threadId, rawTurnId] = String(nodeId).split(":");
  return { threadId: threadId || null, turnId: rawTurnId && rawTurnId !== "head" ? rawTurnId : null };
}

export function getThread(state, threadId) {
  return threadId ? state.threads[threadId] || null : null;
}

export function getTurns(state, threadId) {
  return sortTurns(state.turnsByThread[threadId] || []);
}

export function getTurn(state, threadId, turnId) {
  return getTurns(state, threadId).find((turn) => turn.turnId === turnId) || null;
}

export function getApprovalsForTurn(state, threadId, turnId) {
  if (!threadId || !turnId) {
    return [];
  }
  return Object.values(state.approvals)
    .filter((approval) => approval.threadId === threadId && approval.turnId === turnId)
    .sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
}

export function getHeadTurn(state, threadId) {
  const turns = getTurns(state, threadId);
  return turns[turns.length - 1] || null;
}

export function getConversationRootId(state, threadId) {
  if (!threadId) {
    return null;
  }
  let current = getThread(state, threadId);
  if (!current) {
    return null;
  }
  while (current.parentThreadId && state.threads[current.parentThreadId]) {
    current = state.threads[current.parentThreadId];
  }
  return current.threadId;
}

export function getConversationThreads(state, conversationId) {
  if (!conversationId) {
    return [];
  }
  return Object.values(state.threads)
    .filter((thread) => getConversationRootId(state, thread.threadId) === conversationId)
    .sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
}

export function getBranchLabel(state, threadId) {
  if (!threadId) {
    return "Branch";
  }
  const conversationId = getConversationRootId(state, threadId);
  if (!conversationId) {
    return "Branch";
  }
  if (conversationId === threadId) {
    return "Main";
  }
  const branches = getConversationThreads(state, conversationId).filter((thread) => thread.threadId !== conversationId);
  const index = branches.findIndex((thread) => thread.threadId === threadId);
  return index >= 0 ? `Branch ${index + 1}` : "Branch";
}

export function getPeerThreads(state, threadId) {
  const conversationId = getConversationRootId(state, threadId);
  return getConversationThreads(state, conversationId).filter((thread) => thread.threadId !== threadId);
}

function getConversationUpdatedAt(state, conversationId) {
  return getConversationThreads(state, conversationId).reduce((latest, thread) => {
    return String(thread.updatedAt) > String(latest) ? String(thread.updatedAt) : String(latest);
  }, "");
}

export function getConversationRoots(state) {
  return Object.values(state.threads)
    .filter((thread) => !thread.parentThreadId)
    .sort((a, b) => getConversationUpdatedAt(state, b.threadId).localeCompare(getConversationUpdatedAt(state, a.threadId)));
}

export function getConversationChildrenMap(state, conversationId) {
  const children = {};
  for (const thread of getConversationThreads(state, conversationId)) {
    if (!thread.parentThreadId) {
      continue;
    }
    if (!children[thread.parentThreadId]) {
      children[thread.parentThreadId] = [];
    }
    children[thread.parentThreadId].push(thread);
  }
  for (const key of Object.keys(children)) {
    children[key].sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
  }
  return children;
}

export function pickDefaultThreadId(state, conversationId) {
  const threads = getConversationThreads(state, conversationId);
  if (!threads.length) {
    return null;
  }
  return sortThreads(threads)[0].threadId;
}

export function pickDefaultNodeId(state, threadId) {
  if (!threadId) {
    return null;
  }
  const headTurn = getHeadTurn(state, threadId);
  return getNodeId(threadId, headTurn?.turnId || null);
}

export function getSelectedConversation(state) {
  const conversationId = state.selectedConversationId || getConversationRoots(state)[0]?.threadId || null;
  return conversationId ? state.threads[conversationId] || null : null;
}

export function getSelectedThread(state) {
  const threadId = state.selectedThreadId || pickDefaultThreadId(state, state.selectedConversationId);
  return threadId ? state.threads[threadId] || null : null;
}

export function getSelectedNode(state) {
  const { threadId, turnId } = parseNodeId(state.selectedNodeId);
  const selectedThreadId = threadId || state.selectedThreadId;
  const thread = getThread(state, selectedThreadId);
  if (!thread) {
    return null;
  }
  const turn = turnId ? getTurn(state, thread.threadId, turnId) : null;
  const headTurn = getHeadTurn(state, thread.threadId);
  return {
    nodeId: getNodeId(thread.threadId, turn?.turnId || null),
    thread,
    turn,
    turnId: turn?.turnId || null,
    isHead: !turn || headTurn?.turnId === turn.turnId,
    headTurn,
    conversationId: getConversationRootId(state, thread.threadId),
  };
}

export function countConversationTurns(state, conversationId) {
  return getConversationThreads(state, conversationId).reduce((count, thread) => count + getTurns(state, thread.threadId).length, 0);
}

export function countConversationBranches(state, conversationId) {
  return getConversationThreads(state, conversationId).length;
}
