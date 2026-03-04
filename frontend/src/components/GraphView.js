import { describeDecision, escapeHtml, summarizeText } from "../rendering.js";
import {
  getActiveContextGraph,
  getApprovalsForTurn,
  getBranchLabel,
  getContextLinkAnchor,
  getContextLinkKey,
  getContextLinkMode,
  getContextLinks,
  getConversationChildrenMap,
  getConversationThreads,
  getNodeId,
  getSelectedConversation,
  getSelectedNode,
  getTurns,
} from "../selectors.js";

const LANE_ORDER_STORAGE_KEY = "codex-ui-graph-lane-order-v1";
const NODE_POSITION_STORAGE_KEY = "codex-ui-graph-node-positions-v1";
const laneOrderByConversation = new Map();
const nodePositionsByConversation = new Map();
const NODE_WIDTH = 196;
const NODE_HEIGHT = 56;
const MERGE_NODE_WIDTH = 92;
const MERGE_NODE_HEIGHT = 44;

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

function loadNodePositions() {
  if (nodePositionsByConversation.size || typeof window === "undefined") {
    return;
  }
  try {
    const raw = window.localStorage.getItem(NODE_POSITION_STORAGE_KEY);
    if (!raw) {
      return;
    }
    const parsed = JSON.parse(raw);
    for (const [conversationId, positions] of Object.entries(parsed)) {
      if (!positions || typeof positions !== "object") {
        continue;
      }
      const normalized = {};
      for (const [nodeId, point] of Object.entries(positions)) {
        if (!point || typeof point !== "object") {
          continue;
        }
        const x = Number(point.x);
        const y = Number(point.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) {
          continue;
        }
        normalized[String(nodeId)] = { x, y };
      }
      nodePositionsByConversation.set(conversationId, normalized);
    }
  } catch {
    return;
  }
}

function persistNodePositions() {
  if (typeof window === "undefined") {
    return;
  }
  const payload = Object.fromEntries(nodePositionsByConversation.entries());
  window.localStorage.setItem(NODE_POSITION_STORAGE_KEY, JSON.stringify(payload));
}

function getNodePositions(conversationId, nodeIds) {
  loadNodePositions();
  const existing = nodePositionsByConversation.get(conversationId) || {};
  const knownIds = new Set(nodeIds);
  const next = {};
  for (const [nodeId, point] of Object.entries(existing)) {
    if (!knownIds.has(nodeId) || !point) {
      continue;
    }
    const x = Number(point.x);
    const y = Number(point.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      continue;
    }
    next[nodeId] = { x, y };
  }
  nodePositionsByConversation.set(conversationId, next);
  return next;
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
  return { laneByThread };
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

function getBranchSummary(state, threadId) {
  const turns = getTurns(state, threadId);
  const firstTurn = turns[0];
  if (firstTurn?.userText) {
    return summarizeText(firstTurn.userText, 28);
  }
  return turns.length ? "Branch in progress" : "No turns yet";
}

function edgePath(from, to) {
  const fromX = from.x;
  const fromY = from.y;
  const toX = to.x;
  const toY = to.y;
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
  const minNodeX = Math.min(...nodes.map((node) => node.x - (node.width || NODE_WIDTH) / 2));
  const maxNodeX = Math.max(...nodes.map((node) => node.x + (node.width || NODE_WIDTH) / 2));
  const minNodeY = Math.min(...nodes.map((node) => node.y - (node.height || NODE_HEIGHT) / 2));
  const maxNodeY = Math.max(...nodes.map((node) => node.y + (node.height || NODE_HEIGHT) / 2));
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
  const { laneByThread } = buildLaneMap(laneOrder);
  const baseDepth = buildDepthMap(state, threads, childrenMap, conversation.threadId);
  const selectedNode = getSelectedNode(state);
  const activeContext = getActiveContextGraph(state);
  const pendingMergeNodeId = state.pendingMergeSourceNodeId;
  const laneGap = 244;
  const rowGap = 88;
  const leftPadding = 148;
  const topPadding = 56;
  const nodes = [];
  const primaryEdges = [];
  const transformEdges = [];
  const mergeNodes = [];
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

  const paddingX = 52;
  const paddingY = 40;
  const baseBounds = getGraphBounds(nodes);
  const baseOffsetX = paddingX - baseBounds.minX;
  const baseOffsetY = paddingY - baseBounds.minY;
  nodes.forEach((node) => {
    node.x += baseOffsetX;
    node.y += baseOffsetY;
  });
  laneLabelRows.forEach((row) => {
    row.x += baseOffsetX;
  });
  let nodePositions = getNodePositions(
    conversation.threadId,
    nodes.map((node) => node.id),
  );
  nodes.forEach((node) => {
    const saved = nodePositions[node.id];
    if (!saved) {
      return;
    }
    node.x = saved.x;
    node.y = saved.y;
  });

  for (const thread of threads) {
    for (const turn of getTurns(state, thread.threadId)) {
      const destinationNode = nodeMap[getNodeId(thread.threadId, turn.turnId)];
      if (!destinationNode) {
        continue;
      }
      const links = getContextLinks(turn);
      for (const [index, link] of links.entries()) {
        const anchor = getContextLinkAnchor(link);
        if (!anchor) {
          continue;
        }
        const sourceNode = nodeMap[getNodeId(anchor.threadId, anchor.turnId)];
        if (!sourceNode) {
          continue;
        }
        const linkKey = getContextLinkKey(link, thread.threadId, turn.turnId, index);
        const mergeNode = {
          id: `merge:${linkKey}`,
          x: (sourceNode.x + destinationNode.x) / 2,
          y: destinationNode.y - 54 - index * 34,
          width: MERGE_NODE_WIDTH,
          height: MERGE_NODE_HEIGHT,
          mode: getContextLinkMode(link),
          active: activeContext.activeImportLinkKeys.has(linkKey),
        };
        mergeNodes.push(mergeNode);
        transformEdges.push({
          from: sourceNode,
          to: mergeNode,
          active: mergeNode.active,
        });
        transformEdges.push({
          from: mergeNode,
          to: destinationNode,
          active: mergeNode.active,
        });
      }
    }
  }

  let graphBounds = getGraphBounds([...nodes, ...mergeNodes]);
  const graphShiftX = graphBounds.minX < paddingX ? paddingX - graphBounds.minX : 0;
  const graphShiftY = graphBounds.minY < paddingY ? paddingY - graphBounds.minY : 0;
  if (graphShiftX || graphShiftY) {
    nodes.forEach((node) => {
      node.x += graphShiftX;
      node.y += graphShiftY;
    });
    laneLabelRows.forEach((row) => {
      row.x += graphShiftX;
    });
    mergeNodes.forEach((node) => {
      node.x += graphShiftX;
      node.y += graphShiftY;
    });
    graphBounds = getGraphBounds([...nodes, ...mergeNodes]);
  }

  const laneOriginX = leftPadding + baseOffsetX + graphShiftX;
  const width = Math.max(720, Math.ceil(graphBounds.maxX + paddingX));
  const height = Math.max(520, Math.ceil(graphBounds.maxY + paddingY));

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
            <defs>
              <marker id="graph-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M 0 0 L 8 4 L 0 8 z" fill="currentColor" />
              </marker>
            </defs>
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
                (edge) => `<path class="graph-primary-edge${edge.branch ? " is-branch-edge" : ""} ${activeContext.activeNodeIds.has(edge.from.id) && activeContext.activeNodeIds.has(edge.to.id) ? "is-active" : ""}" d="${edgePath(edge.from, edge.to)}" marker-end="url(#graph-arrow)" />`,
              )
              .join("")}
            ${transformEdges
              .map(
                (edge) => `<path class="graph-transform-edge${edge.active ? " is-active" : ""}" d="${edgePath(edge.from, edge.to)}" marker-end="url(#graph-arrow)" />`,
              )
              .join("")}
            <path class="graph-link-preview" data-link-preview style="display:none" />
            ${mergeNodes
              .map((node) => {
                const points = [
                  `${node.x},${node.y - MERGE_NODE_HEIGHT / 2}`,
                  `${node.x + MERGE_NODE_WIDTH / 2},${node.y}`,
                  `${node.x},${node.y + MERGE_NODE_HEIGHT / 2}`,
                  `${node.x - MERGE_NODE_WIDTH / 2},${node.y}`,
                ].join(" ");
                return `
                  <g class="graph-merge-node ${node.active ? "is-active" : ""}" data-merge-node-id="${node.id}">
                    <polygon class="graph-merge-diamond" points="${points}" />
                    <text class="graph-merge-label" x="${node.x}" y="${node.y + 4}">${escapeHtml(node.mode)}</text>
                  </g>
                `;
              })
              .join("")}
            ${nodes
              .map((node) => {
                const activeContextSource = activeContext.importNodeIds.has(node.id);
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
  let linkState = null;
  let laneDragState = null;
  let nodeDragState = null;
  let hoveredTargetElement = null;

  container.querySelectorAll("[data-node-id]").forEach((element) => {
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
    element.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("[data-link-handle]") || linkState || laneDragState) {
        return;
      }
      const node = nodeMap[element.dataset.nodeId];
      if (!node || !canvasElement) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      nodeDragState = {
        pointerId: event.pointerId,
        nodeId: node.id,
        threadId: element.dataset.threadId,
        turnId: element.dataset.turnId || null,
        element,
        startPoint: getCanvasPoint(event, canvasElement),
        startX: node.x,
        startY: node.y,
        deltaX: 0,
        deltaY: 0,
        dragging: false,
      };
      viewportElement?.setPointerCapture(event.pointerId);
    });
  });

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

  function stopNodeDrag(event, commit = true) {
    if (!nodeDragState || (event && event.pointerId !== nodeDragState.pointerId)) {
      return;
    }
    if (viewportElement?.hasPointerCapture?.(nodeDragState.pointerId)) {
      viewportElement.releasePointerCapture(nodeDragState.pointerId);
    }
    viewportElement?.classList.remove("is-node-dragging");
    nodeDragState.element.classList.remove("is-dragging");
    nodeDragState.element.removeAttribute("transform");

    if (commit && nodeDragState.dragging) {
      const node = nodeMap[nodeDragState.nodeId];
      if (node) {
        const nextX = nodeDragState.startX + nodeDragState.deltaX;
        const nextY = nodeDragState.startY + nodeDragState.deltaY;
        node.x = nextX;
        node.y = nextY;
        nodePositions = {
          ...nodePositions,
          [node.id]: { x: nextX, y: nextY },
        };
        nodePositionsByConversation.set(conversation.threadId, nodePositions);
        persistNodePositions();
        handlers.onNodePositionChange?.({
          conversationId: conversation.threadId,
          nodeId: node.id,
          x: nextX,
          y: nextY,
        });
      }
    }
    nodeDragState = null;
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
    if (nodeDragState && event.pointerId === nodeDragState.pointerId && canvasElement) {
      const worldPoint = getCanvasPoint(event, canvasElement);
      const deltaX = worldPoint.x - nodeDragState.startPoint.x;
      const deltaY = worldPoint.y - nodeDragState.startPoint.y;
      nodeDragState.deltaX = deltaX;
      nodeDragState.deltaY = deltaY;
      if (!nodeDragState.dragging) {
        if (Math.abs(deltaX) + Math.abs(deltaY) < 8) {
          return;
        }
        nodeDragState.dragging = true;
        nodeDragState.element.classList.add("is-dragging");
        viewportElement?.classList.add("is-node-dragging");
      }
      nodeDragState.element.setAttribute("transform", `translate(${deltaX} ${deltaY})`);
      return;
    }
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
    if (nodeDragState && event.pointerId === nodeDragState.pointerId) {
      const pointerTarget = document.elementFromPoint(event.clientX, event.clientY);
      const targetNodeElement = pointerTarget?.closest("[data-node-id]");
      const handleUnderPointer = pointerTarget?.closest("[data-link-handle]");
      const shouldSelect =
        !nodeDragState.dragging &&
        targetNodeElement?.dataset.nodeId === nodeDragState.nodeId &&
        !handleUnderPointer;
      const selectedThreadId = nodeDragState.threadId;
      const selectedTurnId = nodeDragState.turnId;
      stopNodeDrag(event, true);
      if (shouldSelect) {
        handlers.onSelectNode?.({
          threadId: selectedThreadId,
          turnId: selectedTurnId,
        });
      }
      return;
    }
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
    stopNodeDrag(event, false);
    stopLaneDrag(event, false);
    stopLink(event);
  });
}
