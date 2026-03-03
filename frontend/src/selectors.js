import { buildBlocks, summarizeText, summarizeTurn } from "./rendering.js";

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

export function getNodeSnapshot(state, threadId, turnId = null) {
  const thread = getThread(state, threadId);
  if (!thread) {
    return null;
  }
  const turn = turnId ? getTurn(state, threadId, turnId) : null;
  const approvals = turn ? getApprovalsForTurn(state, threadId, turn.turnId) : [];
  const events = turn ? state.eventsByTurn[`${threadId}:${turn.turnId}`] || [] : [];
  const blocks = turn ? buildBlocks(turn, events, approvals) : [];
  const summary = turn ? summarizeTurn(turn, blocks, approvals) : null;
  const contextLinks = Array.isArray(turn?.metadata?.contextLinks)
    ? turn.metadata.contextLinks.filter((link) => link?.sourceThreadId && link?.sourceTurnId)
    : [];
  return {
    nodeId: getNodeId(threadId, turn?.turnId || null),
    thread,
    turn,
    approvals,
    events,
    blocks,
    summary,
    contextLinks,
    branchLabel: getBranchLabel(state, threadId),
    promptSummary: turn ? summarizeText(turn.userText || "No prompt", 72) : "Start",
  };
}

function buildLineageEntries(state, threadId, cutoffTurnId = null, into = []) {
  const thread = getThread(state, threadId);
  if (!thread) {
    return into;
  }
  if (thread.parentThreadId) {
    buildLineageEntries(state, thread.parentThreadId, thread.forkedFromTurnId, into);
  }
  const turns = getTurns(state, threadId);
  const cutoffIdx = cutoffTurnId ? getTurn(state, threadId, cutoffTurnId)?.idx ?? turns.length : turns[turns.length - 1]?.idx ?? 0;
  for (const turn of turns) {
    if (turn.idx > cutoffIdx) {
      continue;
    }
    into.push({
      kind: "lineage",
      threadId,
      turnId: turn.turnId,
      nodeId: getNodeId(threadId, turn.turnId),
    });
  }
  return into;
}

export function getContextStack(state, threadId, turnId = null) {
  if (!getNodeSnapshot(state, threadId, turnId)) {
    return [];
  }
  const lineageEntries = buildLineageEntries(state, threadId, turnId);
  const seenImports = new Set();
  const importedEntries = [];
  for (const entry of lineageEntries) {
    const turnSnapshot = getNodeSnapshot(state, entry.threadId, entry.turnId);
    for (const link of turnSnapshot?.contextLinks || []) {
      const nodeId = getNodeId(link.sourceThreadId, link.sourceTurnId);
      if (seenImports.has(nodeId)) {
        continue;
      }
      seenImports.add(nodeId);
      importedEntries.push({
        kind: "import",
        threadId: link.sourceThreadId,
        turnId: link.sourceTurnId,
        nodeId,
        importedIntoNodeId: entry.nodeId,
      });
    }
  }
  return [...lineageEntries, ...importedEntries]
    .map((entry) => {
      const entrySnapshot = getNodeSnapshot(state, entry.threadId, entry.turnId);
      if (!entrySnapshot?.turn) {
        return null;
      }
      return {
        ...entry,
        snapshot: entrySnapshot,
      };
    })
    .filter(Boolean);
}

export function getSelectedContextStack(state) {
  const selected = getSelectedNode(state);
  if (!selected?.thread) {
    return [];
  }
  return getContextStack(state, selected.thread.threadId, selected.turn?.turnId || null);
}

export function getCompareSnapshots(state) {
  const left = parseNodeId(state.compare.leftNodeId);
  const right = parseNodeId(state.compare.rightNodeId);
  return {
    left: left.threadId ? getNodeSnapshot(state, left.threadId, left.turnId) : null,
    right: right.threadId ? getNodeSnapshot(state, right.threadId, right.turnId) : null,
  };
}

export function countConversationTurns(state, conversationId) {
  return getConversationThreads(state, conversationId).reduce((count, thread) => count + getTurns(state, thread.threadId).length, 0);
}

export function countConversationBranches(state, conversationId) {
  return getConversationThreads(state, conversationId).length;
}
