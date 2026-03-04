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

export function getContextLinks(turn) {
  const links = turn?.metadata?.contextLinks;
  return Array.isArray(links)
    ? links.filter((link) => link?.sourceThreadId && (link?.sourceTurnId || link?.sourceAnchorTurnId))
    : [];
}

export function getContextLinkMode(link) {
  return String(link?.mergeMode || "verbose");
}

export function getContextLinkSourceNodes(link) {
  if (Array.isArray(link?.sourceNodes) && link.sourceNodes.length) {
    return link.sourceNodes
      .filter((node) => node?.threadId && node?.turnId)
      .map((node) => ({ threadId: String(node.threadId), turnId: String(node.turnId) }));
  }
  const turnId = link?.sourceAnchorTurnId || link?.sourceTurnId;
  if (!link?.sourceThreadId || !turnId) {
    return [];
  }
  return [{ threadId: String(link.sourceThreadId), turnId: String(turnId) }];
}

export function getContextLinkAnchor(link) {
  const sourceNodes = getContextLinkSourceNodes(link);
  return sourceNodes[sourceNodes.length - 1] || null;
}

export function getContextLinkScopeCount(link) {
  return getContextLinkSourceNodes(link).length;
}

export function getContextLinkKey(link, destThreadId = "", destTurnId = "", index = 0) {
  const anchor = getContextLinkAnchor(link);
  const previewId = link?.previewId ? String(link.previewId) : "";
  if (previewId) {
    return `${destThreadId}:${destTurnId}:${previewId}`;
  }
  return [
    destThreadId,
    destTurnId,
    link?.sourceThreadId || "",
    anchor?.turnId || link?.sourceTurnId || "",
    getContextLinkMode(link),
    String(index),
  ].join(":");
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
  const contextLinks = getContextLinks(turn);
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
  const visitedNodes = new Set();

  function collectImportsForTurn(currentThreadId, currentTurnId, importedIntoNodeId) {
    const nodeId = getNodeId(currentThreadId, currentTurnId);
    if (visitedNodes.has(nodeId)) {
      return;
    }
    visitedNodes.add(nodeId);
    const snapshot = getNodeSnapshot(state, currentThreadId, currentTurnId);
    const links = snapshot?.contextLinks || [];
    links.forEach((link, index) => {
      const anchor = getContextLinkAnchor(link);
      if (!anchor) {
        return;
      }
      const linkKey = getContextLinkKey(link, currentThreadId, currentTurnId, index);
      if (!seenImports.has(linkKey)) {
        seenImports.add(linkKey);
        importedEntries.push({
          kind: "import",
          threadId: anchor.threadId,
          turnId: anchor.turnId,
          nodeId: getNodeId(anchor.threadId, anchor.turnId),
          importedIntoNodeId,
          mergeMode: getContextLinkMode(link),
          sourceNodeCount: getContextLinkScopeCount(link),
          linkKey,
        });
      }
      getContextLinkSourceNodes(link).forEach((sourceNode) => {
        collectImportsForTurn(sourceNode.threadId, sourceNode.turnId, importedIntoNodeId);
      });
    });
  }

  lineageEntries.forEach((entry) => collectImportsForTurn(entry.threadId, entry.turnId, entry.nodeId));

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

export function getActiveContextGraph(state) {
  const selected = getSelectedNode(state);
  if (!selected?.thread) {
    return {
      lineageNodeIds: new Set(),
      importNodeIds: new Set(),
      activeNodeIds: new Set(),
      activeImportLinkKeys: new Set(),
    };
  }
  const lineageEntries = buildLineageEntries(state, selected.thread.threadId, selected.turn?.turnId || null);
  const lineageNodeIds = new Set(lineageEntries.map((entry) => entry.nodeId));
  const importNodeIds = new Set();
  const activeNodeIds = new Set(lineageEntries.map((entry) => entry.nodeId));
  const activeImportLinkKeys = new Set();
  const visitedTurns = new Set();

  function walkTurn(threadId, turnId) {
    const nodeId = getNodeId(threadId, turnId);
    if (visitedTurns.has(nodeId)) {
      return;
    }
    visitedTurns.add(nodeId);
    const turn = getTurn(state, threadId, turnId);
    if (!turn) {
      return;
    }
    getContextLinks(turn).forEach((link, index) => {
      activeImportLinkKeys.add(getContextLinkKey(link, threadId, turn.turnId, index));
      getContextLinkSourceNodes(link).forEach((sourceNode) => {
        const sourceNodeId = getNodeId(sourceNode.threadId, sourceNode.turnId);
        importNodeIds.add(sourceNodeId);
        activeNodeIds.add(sourceNodeId);
        walkTurn(sourceNode.threadId, sourceNode.turnId);
      });
    });
  }

  lineageEntries.forEach((entry) => walkTurn(entry.threadId, entry.turnId));
  return {
    lineageNodeIds,
    importNodeIds,
    activeNodeIds,
    activeImportLinkKeys,
  };
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
