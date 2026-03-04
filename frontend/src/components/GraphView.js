import { describeDecision, escapeHtml, summarizeText } from "../rendering.js";
import { getApprovalsForTurn, getBranchLabel, getConversationChildrenMap, getConversationThreads, getNodeId, getSelectedConversation, getSelectedNode, getTurns } from "../selectors.js";

const LANE_ORDER_STORAGE_KEY = "codex-ui-graph-lane-order-v1";
const laneOrderByConversation = new Map();
const NODE_WIDTH = 196;
const NODE_HEIGHT = 56;

function loadLaneOrders() {
  if (laneOrderByConversation.size || typeof window === "undefined") {
    return;
  }
  try {
    const raw = window.localStorage.getItem(LANE_ORDER_STORAGE_KEY);
    if (!raw) {
      return;
    }
    const parsed = JSON.parse(raw);
    for (const [conversationId, threadIds] of Object.entries(parsed)) {
      if (Array.isArray(threadIds)) {
        laneOrderByConversation.set(conversationId, threadIds.map((threadId) => String(threadId)));
      }
    }
  } catch {
    return;
  }
}

function persistLaneOrders() {
  if (typeof window === "undefined") {
    return;
  }
  const payload = Object.fromEntries(laneOrderByConversation.entries());
  window.localStorage.setItem(LANE_ORDER_STORAGE_KEY, JSON.stringify(payload));
}

function getLaneOrder(conversationId, threadIds) {
  loadLaneOrders();
  const existing = laneOrderByConversation.get(conversationId) || [];
  const knownIds = new Set(threadIds);
  const next = [
    ...existing.filter((threadId) => knownIds.has(threadId)),
    ...threadIds.filter((threadId) => !existing.includes(threadId)),
  ];
  laneOrderByConversation.set(conversationId, next);
  return next;
}

function moveThreadLane(threadIds, threadId, nextIndex) {
  const currentIndex = threadIds.indexOf(threadId);
  if (currentIndex < 0) {
    return threadIds;
  }
  const normalizedIndex = Math.max(0, Math.min(nextIndex, threadIds.length - 1));
  if (normalizedIndex === currentIndex) {
    return threadIds;
  }
  const next = [...threadIds];
  next.splice(currentIndex, 1);
  next.splice(normalizedIndex, 0, threadId);
  return next;
}

function buildLaneMap(threadOrder) {
  const laneByThread = {};
  threadOrder.forEach((threadId, index) => {
    laneByThread[threadId] = index;
  });
  return { laneByThread, laneCount: Math.max(threadOrder.length, 1) };
}

function buildDepthMap(state, threads, childrenMap, rootId) {
  const turnIndexByThread = {};
  for (const thread of threads) {
    turnIndexByThread[thread.threadId] = Object.fromEntries(getTurns(state, thread.threadId).map((turn) => [turn.turnId, turn.idx]));
  }

  const baseDepth = { [rootId]: 0 };
  const queue = [rootId];
  while (queue.length) {
    const currentId = queue.shift();
    const children = childrenMap[currentId] || [];
    for (const child of children) {
      const parentForkDepth = turnIndexByThread[currentId]?.[child.forkedFromTurnId] || getTurns(state, currentId).length;
      baseDepth[child.threadId] = (baseDepth[currentId] || 0) + parentForkDepth;
      queue.push(child.threadId);
    }
  }
  return baseDepth;
}

function getContextLinks(turn) {
  const links = turn?.metadata?.contextLinks;
  return Array.isArray(links) ? links.filter((link) => link?.sourceThreadId && link?.sourceTurnId) : [];
}

function collectLineageNodeIds(state, threadId, cutoffTurnId = null, into = new Set()) {
  const thread = state.threads[threadId];
  if (!thread) {
    return into;
  }
  if (thread.parentThreadId) {
    collectLineageNodeIds(state, thread.parentThreadId, thread.forkedFromTurnId, into);
  }
  const turns = getTurns(state, threadId);
  const cutoffIdx = cutoffTurnId ? turns.find((turn) => turn.turnId === cutoffTurnId)?.idx ?? turns.length : turns[turns.length - 1]?.idx ?? 0;
  for (const turn of turns) {
    if (turn.idx <= cutoffIdx) {
      into.add(getNodeId(threadId, turn.turnId));
    }
  }
  return into;
}

function getBranchSummary(state, threadId) {
  const turns = getTurns(state, threadId);
  const firstTurn = turns[0];
  if (firstTurn?.userText) {
    return summarizeText(firstTurn.userText, 28);
  }
  return turns.length ? "Branch in progress" : "No turns yet";
}

function buildActiveContextState(state) {
  const selectedNode = getSelectedNode(state);
  const thread = selectedNode?.thread;
  if (!thread) {
    return { sourceNodeIds: new Set(), destinationNodeIds: new Set(), lineageNodeIds: new Set() };
  }
  const turns = getTurns(state, thread.threadId);
  const cutoffIdx = selectedNode?.turn?.idx || turns[turns.length - 1]?.idx || 0;
  const sourceNodeIds = new Set();
  const destinationNodeIds = new Set();
  for (const turn of turns) {
    if (turn.idx > cutoffIdx) {
      continue;
    }
    const destinationNodeId = getNodeId(thread.threadId, turn.turnId);
    for (const link of getContextLinks(turn)) {
      sourceNodeIds.add(getNodeId(link.sourceThreadId, link.sourceTurnId));
      destinationNodeIds.add(destinationNodeId);
    }
  }
  const lineageNodeIds = collectLineageNodeIds(
    state,
    thread.threadId,
    selectedNode?.turn?.turnId || turns[turns.length - 1]?.turnId || null,
  );
  return { sourceNodeIds, destinationNodeIds, lineageNodeIds };
}

function edgePath(from, to) {
  const fromX = from.x;
  const fromY = from.y + NODE_HEIGHT / 2;
  const toX = to.x;
  const toY = to.y - NODE_HEIGHT / 2;
  const midY = (fromY + toY) / 2;
  return `M ${fromX} ${fromY} C ${fromX} ${midY}, ${toX} ${midY}, ${toX} ${toY}`;
}

function previewPath(from, to) {
  const fromX = from.x + NODE_WIDTH / 2;
  const fromY = from.y;
  const midY = (fromY + to.y) / 2;
  return `M ${fromX} ${fromY} C ${fromX} ${midY}, ${to.x} ${midY}, ${to.x} ${to.y}`;
}

function getCanvasPoint(event, canvasElement) {
  const rect = canvasElement.getBoundingClientRect();
  return {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  };
}

function isValidLink(sourceNode, targetNode) {
  if (!sourceNode?.turnId || !targetNode?.turnId) {
    return false;
  }
  return sourceNode.id !== targetNode.id;
}

function getGraphBounds(nodes) {
  if (!nodes.length) {
    return {
      minX: 0,
      maxX: NODE_WIDTH,
      minY: 0,
      maxY: NODE_HEIGHT,
    };
  }
  const minNodeX = Math.min(...nodes.map((node) => node.x - NODE_WIDTH / 2));
  const maxNodeX = Math.max(...nodes.map((node) => node.x + NODE_WIDTH / 2));
  const minNodeY = Math.min(...nodes.map((node) => node.y - NODE_HEIGHT / 2));
  const maxNodeY = Math.max(...nodes.map((node) => node.y + NODE_HEIGHT / 2));
  return {
    minX: minNodeX - 32,
    maxX: maxNodeX + 32,
    minY: Math.min(24, minNodeY - 48),
    maxY: maxNodeY + 48,
  };
}

export function renderGraphView(container, state, handlers) {
  const conversation = getSelectedConversation(state);
  if (!conversation) {
    container.innerHTML = '<div class="empty-state">Select a conversation to inspect its branch graph.</div>';
    return;
  }

  const threads = getConversationThreads(state, conversation.threadId);
  if (!threads.length) {
    container.innerHTML = '<div class="empty-state">This conversation has no branch data yet.</div>';
    return;
  }

  const childrenMap = getConversationChildrenMap(state, conversation.threadId);
  const laneOrder = getLaneOrder(
    conversation.threadId,
    threads.map((thread) => thread.threadId),
  );
  const { laneByThread, laneCount } = buildLaneMap(laneOrder);
  const baseDepth = buildDepthMap(state, threads, childrenMap, conversation.threadId);
  const selectedNode = getSelectedNode(state);
  const activeContext = buildActiveContextState(state);
  const pendingMergeNodeId = state.pendingMergeSourceNodeId;
  const laneGap = 244;
  const rowGap = 88;
  const leftPadding = 148;
  const topPadding = 56;
  const nodes = [];
  const primaryEdges = [];
  const contextEdges = [];
  const nodeMap = {};
  const firstNodeByThread = {};
  const laneLabelRows = [];

  for (const thread of threads) {
    const x = leftPadding + laneByThread[thread.threadId] * laneGap;
    const threadBase = baseDepth[thread.threadId] || 0;
    const turns = getTurns(state, thread.threadId);
    laneLabelRows.push({
      thread,
      x,
      branchLabel: getBranchLabel(state, thread.threadId),
      branchSummary: getBranchSummary(state, thread.threadId),
    });

    if (!turns.length) {
      const node = {
        id: getNodeId(thread.threadId, null),
        threadId: thread.threadId,
        turnId: null,
        x,
        y: topPadding + threadBase * rowGap,
        title: "Start here",
        meta: "Start",
        selected: selectedNode?.thread?.threadId === thread.threadId && !selectedNode?.turn,
        running: thread.status === "running" || thread.status === "inProgress",
        contextLinkCount: 0,
      };
      nodes.push(node);
      nodeMap[node.id] = node;
      firstNodeByThread[thread.threadId] = node;
      continue;
    }

    turns.forEach((turn, index) => {
      const approvals = getApprovalsForTurn(state, thread.threadId, turn.turnId);
      const contextLinks = getContextLinks(turn);
      const node = {
        id: getNodeId(thread.threadId, turn.turnId),
        threadId: thread.threadId,
        turnId: turn.turnId,
        x,
        y: topPadding + (threadBase + index) * rowGap,
        title: summarizeText(turn.userText || "No prompt", 42),
        meta: `T${turn.idx}${contextLinks.length ? ` +${contextLinks.length}` : ""}`,
        selected:
          selectedNode?.thread?.threadId === thread.threadId &&
          (selectedNode?.turn?.turnId || selectedNode?.headTurn?.turnId) === turn.turnId,
        running: turn.status === "running" || turn.status === "inProgress",
        denied: describeDecision(turn, approvals).tone === "danger",
        contextLinkCount: contextLinks.length,
      };
      nodes.push(node);
      nodeMap[node.id] = node;
      if (index === 0) {
        firstNodeByThread[thread.threadId] = node;
      }
      if (index > 0) {
        primaryEdges.push({
          from: nodeMap[getNodeId(thread.threadId, turns[index - 1].turnId)],
          to: node,
          branch: false,
        });
      }
    });

    if (thread.parentThreadId && thread.forkedFromTurnId) {
      const from = nodeMap[getNodeId(thread.parentThreadId, thread.forkedFromTurnId)];
      const to = firstNodeByThread[thread.threadId];
      if (from && to) {
        primaryEdges.push({ from, to, branch: true });
      }
    }
  }

  const seenContextEdges = new Set();
  for (const thread of threads) {
    for (const turn of getTurns(state, thread.threadId)) {
      const destinationNode = nodeMap[getNodeId(thread.threadId, turn.turnId)];
      if (!destinationNode) {
        continue;
      }
      for (const link of getContextLinks(turn)) {
        const sourceNodeId = getNodeId(link.sourceThreadId, link.sourceTurnId);
        const destinationNodeId = destinationNode.id;
        const edgeKey = `${sourceNodeId}:${destinationNodeId}`;
        if (seenContextEdges.has(edgeKey) || !nodeMap[sourceNodeId]) {
          continue;
        }
        seenContextEdges.add(edgeKey);
        contextEdges.push({
          from: nodeMap[sourceNodeId],
          to: destinationNode,
          active: activeContext.sourceNodeIds.has(sourceNodeId) && activeContext.destinationNodeIds.has(destinationNodeId),
        });
      }
    }
  }

  const graphBounds = getGraphBounds(nodes);
  const paddingX = 52;
  const paddingY = 40;
  const offsetX = paddingX - graphBounds.minX;
  const offsetY = paddingY - graphBounds.minY;
  nodes.forEach((node) => {
    node.x += offsetX;
    node.y += offsetY;
  });
  laneLabelRows.forEach((row) => {
    row.x += offsetX;
  });
  const laneOriginX = leftPadding + offsetX;
  const width = Math.max(720, Math.ceil(graphBounds.maxX - graphBounds.minX + paddingX * 2));
  const height = Math.max(520, Math.ceil(graphBounds.maxY - graphBounds.minY + paddingY * 2));

  container.innerHTML = `
    <div class="graph-toolbar">
      ${pendingMergeNodeId ? '<div class="graph-toolbar-note">Merge armed. Select a destination turn.</div>' : '<div class="graph-toolbar-note">Overview</div>'}
      <div class="graph-controls">
        <span class="graph-zoom-readout">Centered</span>
      </div>
    </div>
    <div class="graph-viewport" data-graph-viewport>
      <div class="graph-stage" data-graph-stage>
        <div class="graph-canvas" data-graph-canvas style="width:${width}px;height:${height}px">
          <svg class="graph-svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}">
            <rect class="graph-canvas-fill" x="0" y="0" width="${width}" height="${height}" />
            ${laneLabelRows
              .map(
                ({ thread, x, branchLabel, branchSummary }) => `
                  <g class="graph-lane-header" data-lane-thread-id="${thread.threadId}" transform="translate(${x - NODE_WIDTH / 2}, 24)" title="${escapeHtml(branchSummary)}">
                    <text class="graph-lane-label" x="0" y="0">${escapeHtml(branchLabel)}</text>
                  </g>
                `,
              )
              .join("")}
            ${primaryEdges
              .map(
                (edge) => `<path class="graph-primary-edge${edge.branch ? " is-branch-edge" : ""}" d="${edgePath(edge.from, edge.to)}" />`,
              )
              .join("")}
            ${contextEdges
              .map(
                (edge) => `<path class="graph-context-edge${edge.active ? " is-active" : ""}" d="${edgePath(edge.from, edge.to)}" />`,
              )
              .join("")}
            <path class="graph-link-preview" data-link-preview style="display:none" />
            ${nodes
              .map((node) => {
                const activeContextSource = activeContext.sourceNodeIds.has(node.id);
                const activeContextDestination = activeContext.destinationNodeIds.has(node.id);
                const isLineageNode = activeContext.lineageNodeIds.has(node.id);
                const classes = [
                  "graph-node",
                  node.selected ? "selected" : "",
                  node.running ? "is-running" : "",
                  node.denied ? "is-denied" : "",
                  node.contextLinkCount ? "has-import" : "",
                  pendingMergeNodeId === node.id ? "is-merge-source" : "",
                  isLineageNode ? "is-lineage-node" : "",
                  activeContextSource ? "is-context-source" : "",
                  activeContextDestination ? "is-context-destination" : "",
                ]
                  .filter(Boolean)
                  .join(" ");
                return `
                  <g class="${classes}" data-node-id="${node.id}" data-thread-id="${node.threadId}" data-turn-id="${node.turnId || ""}">
                    <rect class="graph-node-box" x="${node.x - NODE_WIDTH / 2}" y="${node.y - NODE_HEIGHT / 2}" width="${NODE_WIDTH}" height="${NODE_HEIGHT}" rx="4" />
                    ${
                      node.contextLinkCount
                        ? `<rect class="graph-node-import-bar" x="${node.x + NODE_WIDTH / 2 - 4}" y="${node.y - NODE_HEIGHT / 2}" width="4" height="${NODE_HEIGHT}" rx="2" />`
                        : ""
                    }
                    <text class="graph-node-title" x="${node.x - NODE_WIDTH / 2 + 10}" y="${node.y - 6}">${escapeHtml(node.title)}</text>
                    <text class="graph-node-preview" x="${node.x - NODE_WIDTH / 2 + 10}" y="${node.y + 14}">${escapeHtml(node.meta)}</text>
                    ${
                      node.turnId
                        ? `<circle class="graph-link-handle" cx="${node.x + NODE_WIDTH / 2 + 6}" cy="${node.y}" r="4" data-link-handle="1" data-source-node-id="${node.id}"></circle>`
                        : ""
                    }
                  </g>
                `;
              })
              .join("")}
          </svg>
        </div>
      </div>
    </div>
  `;

  const viewportElement = container.querySelector("[data-graph-viewport]");
  const canvasElement = container.querySelector("[data-graph-canvas]");
  const previewLink = container.querySelector("[data-link-preview]");

  container.querySelectorAll("[data-node-id]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (event.target.closest("[data-link-handle]")) {
        return;
      }
      handlers.onSelectNode?.({
        threadId: element.dataset.threadId,
        turnId: element.dataset.turnId || null,
      });
    });
    element.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      event.stopPropagation();
      handlers.onNodeContextMenu?.({
        threadId: element.dataset.threadId,
        turnId: element.dataset.turnId || null,
        x: event.clientX,
        y: event.clientY,
      });
    });
  });

  let linkState = null;
  let laneDragState = null;
  let hoveredTargetElement = null;

  function clearHoveredTarget() {
    if (hoveredTargetElement) {
      hoveredTargetElement.classList.remove("is-link-target");
      hoveredTargetElement = null;
    }
  }

  function stopLink(event) {
    if (!linkState || (event && event.pointerId !== linkState.pointerId)) {
      return;
    }
    clearHoveredTarget();
    if (viewportElement?.hasPointerCapture?.(linkState.pointerId)) {
      viewportElement.releasePointerCapture(linkState.pointerId);
    }
    viewportElement?.classList.remove("is-linking");
    if (previewLink) {
      previewLink.style.display = "none";
      previewLink.removeAttribute("d");
    }
    linkState = null;
  }

  function stopLaneDrag(event, commit = true) {
    if (!laneDragState || (event && event.pointerId !== laneDragState.pointerId)) {
      return;
    }
    if (viewportElement?.hasPointerCapture?.(laneDragState.pointerId)) {
      viewportElement.releasePointerCapture(laneDragState.pointerId);
    }
    viewportElement?.classList.remove("is-lane-dragging");
    const laneHeader = container.querySelector(`[data-lane-thread-id="${laneDragState.threadId}"]`);
    laneHeader?.classList.remove("is-dragging");
    if (commit && event && canvasElement) {
      const worldPoint = getCanvasPoint(event, canvasElement);
      const nextIndex = Math.round((worldPoint.x - laneOriginX) / laneGap);
      const nextOrder = moveThreadLane(laneOrder, laneDragState.threadId, nextIndex);
      if (nextOrder.join("|") !== laneOrder.join("|")) {
        laneOrderByConversation.set(conversation.threadId, nextOrder);
        persistLaneOrders();
        handlers.onLaneOrderChange?.(nextOrder);
      }
    }
    laneDragState = null;
  }

  container.querySelectorAll("[data-lane-thread-id]").forEach((laneHeader) => {
    laneHeader.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || linkState) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      laneDragState = {
        pointerId: event.pointerId,
        threadId: laneHeader.dataset.laneThreadId,
      };
      laneHeader.classList.add("is-dragging");
      viewportElement?.classList.add("is-lane-dragging");
      viewportElement?.setPointerCapture(event.pointerId);
    });
  });

  container.querySelectorAll("[data-link-handle]").forEach((handle) => {
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const sourceNode = nodeMap[handle.dataset.sourceNodeId];
      if (!sourceNode) {
        return;
      }
      linkState = {
        pointerId: event.pointerId,
        sourceNode,
      };
      viewportElement?.classList.add("is-linking");
      viewportElement?.setPointerCapture(event.pointerId);
      if (previewLink) {
        previewLink.style.display = "block";
        previewLink.setAttribute("d", previewPath({ x: sourceNode.x + NODE_WIDTH / 2, y: sourceNode.y }, { x: sourceNode.x + NODE_WIDTH / 2, y: sourceNode.y }));
      }
    });
  });

  viewportElement?.addEventListener("pointermove", (event) => {
    if (laneDragState && event.pointerId === laneDragState.pointerId) {
      return;
    }
    if (linkState && event.pointerId === linkState.pointerId && canvasElement) {
      const worldPoint = getCanvasPoint(event, canvasElement);
      previewLink?.setAttribute(
        "d",
        previewPath(
          { x: linkState.sourceNode.x + NODE_WIDTH / 2, y: linkState.sourceNode.y },
          worldPoint,
        ),
      );
      clearHoveredTarget();
      const candidateElement = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-node-id]");
      if (candidateElement) {
        const candidateNode = nodeMap[candidateElement.dataset.nodeId];
        if (candidateNode && isValidLink(linkState.sourceNode, candidateNode)) {
          hoveredTargetElement = candidateElement;
          hoveredTargetElement.classList.add("is-link-target");
        }
      }
    }
  });

  viewportElement?.addEventListener("pointerup", (event) => {
    if (laneDragState && event.pointerId === laneDragState.pointerId) {
      stopLaneDrag(event, true);
      return;
    }
    if (linkState && event.pointerId === linkState.pointerId) {
      const candidateElement = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-node-id]");
      const candidateNode = candidateElement ? nodeMap[candidateElement.dataset.nodeId] : null;
      if (candidateNode && isValidLink(linkState.sourceNode, candidateNode)) {
        handlers.onCreateLink?.({
          sourceThreadId: linkState.sourceNode.threadId,
          sourceTurnId: linkState.sourceNode.turnId,
          targetThreadId: candidateNode.threadId,
          targetTurnId: candidateNode.turnId,
        });
      }
      stopLink(event);
      return;
    }
  });

  viewportElement?.addEventListener("pointercancel", (event) => {
    stopLaneDrag(event, false);
    stopLink(event);
  });
}
