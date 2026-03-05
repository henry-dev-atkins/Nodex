import { buildBlocks, humanizeTurnStatus, normalizeText, summarizeTurn } from "./rendering.js";
import {
  getApprovalsForTurn,
  getBranchLabel,
  getContextLinkAnchor,
  getContextLinkMode,
  getContextLinkScopeCount,
  getContextLinks,
  getHeadTurn,
  getNodeId,
  getSelectedNode,
  getSelectedThread,
  getTurns,
} from "./selectors.js";

export const CLAMP_LINE_COUNT = 2;

function getStatusMark(turn, approvals) {
  if (approvals.some((approval) => approval.status === "pending")) {
    return { label: "...", tone: "is-running", title: "Waiting on approval" };
  }
  const decided = approvals.filter((approval) => approval.status === "approve" || approval.status === "deny");
  const latest = decided[decided.length - 1];
  if (latest?.status === "approve") {
    return { label: "ok", tone: "is-ok", title: "Approved" };
  }
  if (latest?.status === "deny") {
    return { label: "deny", tone: "is-error", title: "Denied" };
  }
  if (turn.status === "running" || turn.status === "inProgress") {
    return { label: "...", tone: "is-running", title: "Running" };
  }
  if (turn.status === "error" || turn.status === "failed" || turn.status === "interrupted") {
    return { label: "!", tone: "is-error", title: humanizeTurnStatus(turn.status) };
  }
  return { label: "ok", tone: "is-done", title: humanizeTurnStatus(turn.status) };
}

export function getApprovalSummary(approval) {
  const details = approval.details || {};
  if (approval.requestMethod === "item/commandExecution/requestApproval") {
    return normalizeText(details.command || "Codex requested command approval.");
  }
  return normalizeText(details.reason || "Codex requested file-change approval.");
}

function getAssistantText(blocks, summary) {
  const assistant = [...blocks].reverse().find((block) => block.kind === "assistant");
  if (assistant?.plainText?.trim()) {
    return assistant.plainText.trim();
  }
  return summary.preview || "No response captured yet.";
}

function shouldOfferExpand(text) {
  const normalized = normalizeText(text || "").trim();
  if (!normalized) {
    return false;
  }
  if (normalized.includes("\n")) {
    return true;
  }
  return normalized.length > 120;
}

function getReasoningBlocks(blocks) {
  const seen = new Set();
  return blocks
    .filter((block) => block.isReasoning || block.title === "Commentary")
    .filter((block) => {
      const text = normalizeText(block.plainText || "").trim();
      if (!text) {
        return false;
      }
      const key = `${block.title}:${text}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
}

function getCommandBlocks(blocks) {
  const seen = new Set();
  return blocks
    .filter((block) => block.kind === "tool" || block.kind === "warning" || block.kind === "error")
    .filter((block) => {
      const text = normalizeText(block.plainText || "").trim();
      if (!text) {
        return false;
      }
      const key = `${block.title}:${text}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
}

function getTranscriptUiState(state, nodeId) {
  return state.transcriptUiByNode?.[nodeId] || {
    userExpanded: false,
    assistantExpanded: false,
    openAux: null,
  };
}

function buildTranscriptEntries(state, threadId, targetThreadId = threadId, cutoffTurnId = null) {
  const thread = state.threads[threadId];
  if (!thread) {
    return [];
  }
  const inheritedEntries = thread.parentThreadId
    ? buildTranscriptEntries(state, thread.parentThreadId, targetThreadId, thread.forkedFromTurnId)
    : [];
  const turns = getTurns(state, threadId);
  const cutoffIdx = cutoffTurnId ? turns.find((turn) => turn.turnId === cutoffTurnId)?.idx ?? turns.length : null;
  const visibleTurns = cutoffIdx === null ? turns : turns.filter((turn) => turn.idx <= cutoffIdx);
  const entries = [...inheritedEntries];
  const seenNodeIds = new Set(entries.map((entry) => getNodeId(entry.threadId, entry.turn.turnId)));

  for (const turn of visibleTurns) {
    for (const link of getContextLinks(turn)) {
      const anchor = getContextLinkAnchor(link);
      if (!anchor) {
        continue;
      }
      const importedTurn = getTurns(state, anchor.threadId).find((item) => item.turnId === anchor.turnId);
      const importedNodeId = getNodeId(anchor.threadId, anchor.turnId);
      if (!importedTurn || seenNodeIds.has(importedNodeId)) {
        continue;
      }
      entries.push({
        threadId: anchor.threadId,
        turn: importedTurn,
        inherited: false,
        imported: true,
        importedIntoTurnId: turn.turnId,
        importedIntoTurnIdx: turn.idx,
        mergeMode: getContextLinkMode(link),
        sourceNodeCount: getContextLinkScopeCount(link),
      });
      seenNodeIds.add(importedNodeId);
    }

    const nodeId = getNodeId(threadId, turn.turnId);
    if (!seenNodeIds.has(nodeId)) {
      entries.push({
        threadId,
        turn,
        inherited: threadId !== targetThreadId,
        imported: false,
      });
      seenNodeIds.add(nodeId);
    }
  }

  return entries;
}

function buildTurnRowViewModel(state, selectedNode, entry) {
  const rowThreadId = entry.threadId;
  const turn = entry.turn;
  const rowBranchLabel = getBranchLabel(state, rowThreadId);
  const nodeId = getNodeId(rowThreadId, turn.turnId);
  const events = state.eventsByTurn[`${rowThreadId}:${turn.turnId}`] || [];
  const approvals = getApprovalsForTurn(state, rowThreadId, turn.turnId);
  const blocks = buildBlocks(turn, events, approvals);
  const summary = summarizeTurn(turn, blocks, approvals);
  const contextLinks = getContextLinks(turn);
  const selected = selectedNode?.thread?.threadId === rowThreadId && selectedNode?.turn?.turnId === turn.turnId;
  const statusMark = getStatusMark(turn, approvals);
  const reasoningBlocks = getReasoningBlocks(blocks);
  const commandBlocks = getCommandBlocks(blocks);
  const contextSummaryItems = contextLinks
    .map((link) => {
      const anchor = getContextLinkAnchor(link);
      if (!anchor) {
        return null;
      }
      const sourceTurn = getTurns(state, anchor.threadId).find((item) => item.turnId === anchor.turnId);
      const label = sourceTurn ? `T${sourceTurn.idx}` : anchor.turnId.slice(0, 8);
      return {
        branchLabel: getBranchLabel(state, anchor.threadId),
        label,
        mode: getContextLinkMode(link),
        scopeCount: getContextLinkScopeCount(link),
      };
    })
    .filter(Boolean);

  const originBadge = entry.imported
    ? {
        kind: "imported",
        importedIntoTurnIdx: entry.importedIntoTurnIdx,
        mergeMode: entry.mergeMode || "verbose",
        sourceNodeCount: entry.sourceNodeCount || 1,
      }
    : entry.inherited
      ? {
          kind: "inherited",
        }
      : null;

  const ui = getTranscriptUiState(state, nodeId);
  const userText = normalizeText(summary.prompt || "No prompt captured.");
  const assistantText = normalizeText(getAssistantText(blocks, summary)).trim() || "No response captured yet.";
  return {
    entry,
    rowThreadId,
    rowBranchLabel,
    originBadge,
    nodeId,
    turn,
    selected,
    statusMark,
    contextSummaryItems,
    approvals,
    reasoningBlocks,
    commandBlocks,
    userText,
    assistantText,
    userNeedsToggle: shouldOfferExpand(userText),
    assistantNeedsToggle: shouldOfferExpand(assistantText),
    userExpanded: Boolean(ui.userExpanded),
    assistantExpanded: Boolean(ui.assistantExpanded),
    auxPanel: ui.openAux || null,
  };
}

export function buildTranscriptViewModel(state) {
  const thread = getSelectedThread(state);
  if (!thread) {
    return { hasThread: false };
  }
  const selectedNode = getSelectedNode(state) || { thread, turn: null, conversationId: thread.threadId };
  const turns = getTurns(state, thread.threadId);
  const headTurn = getHeadTurn(state, thread.threadId);
  const selectedTurn = selectedNode?.thread?.threadId === thread.threadId ? selectedNode.turn : null;
  const focusTurn = selectedTurn || headTurn;
  const focusCutoffIdx = focusTurn?.idx || turns[turns.length - 1]?.idx || 0;
  const branchLabel = getBranchLabel(state, thread.threadId);
  const transcriptEntries = buildTranscriptEntries(state, thread.threadId);
  const forcedBranchActive = state.forcedBranchNodeId && state.forcedBranchNodeId === selectedNode?.nodeId;
  const activeContextCount = turns
    .filter((turn) => turn.idx <= focusCutoffIdx)
    .reduce((count, turn) => count + getContextLinks(turn).length, 0);
  const busyTurn = turns.find((turn) => {
    if (turn.status === "running" || turn.status === "inProgress") {
      return true;
    }
    return getApprovalsForTurn(state, thread.threadId, turn.turnId).some((approval) => approval.status === "pending");
  }) || null;
  const busyTurnNeedsApproval = busyTurn
    ? getApprovalsForTurn(state, thread.threadId, busyTurn.turnId).some((approval) => approval.status === "pending")
    : false;
  const composerDisabled = Boolean(busyTurn);
  const composerDisabledReason = busyTurn
    ? busyTurnNeedsApproval
      ? `Waiting for approval on T${busyTurn.idx}`
      : `Waiting for T${busyTurn.idx} to finish`
    : "";
  return {
    hasThread: true,
    thread,
    selectedNode,
    turns,
    headTurn,
    selectedTurn,
    focusTurn,
    focusCutoffIdx,
    branchLabel,
    transcriptEntries,
    transcriptRows: transcriptEntries.map((entry) => buildTurnRowViewModel(state, selectedNode, entry)),
    forcedBranchActive,
    activeContextCount,
    busyTurn,
    busyTurnNeedsApproval,
    composerDisabled,
    composerDisabledReason,
  };
}
