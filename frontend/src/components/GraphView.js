function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function normalizeText(value) {
  const text = String(value ?? "");
  const hasUtf8Mojibake =
    text.includes(String.fromCharCode(195)) ||
    text.includes(String.fromCharCode(194)) ||
    text.includes(String.fromCharCode(226));
  if (!hasUtf8Mojibake) {
    return text;
  }
  try {
    const bytes = Uint8Array.from(Array.from(text), (char) => char.codePointAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return text;
  }
}

function threadLabel(thread) {
  return normalizeText(thread.title || thread.metadata?.preview || "Untitled thread");
}

function statusLabel(status) {
  if (status === "running" || status === "inProgress") {
    return "Running";
  }
  if (status === "idle") {
    return "Idle";
  }
  if (status === "error") {
    return "Error";
  }
  return status || "Idle";
}

function buildEdgePath(from, to) {
  const startX = from.x + 20;
  const endX = to.x - 20;
  const midX = (startX + endX) / 2;
  return `M ${startX} ${from.y} C ${midX} ${from.y}, ${midX} ${to.y}, ${endX} ${to.y}`;
}

export function renderGraphView(container, state, onSelect) {
  const threads = Object.values(state.threads);
  if (!threads.length) {
    container.innerHTML = '<div class="empty-state">Thread branches will appear here.</div>';
    return;
  }

  const nodeIdForTurn = (threadId, turnId) => `${threadId}:${turnId}`;
  const orderedThreads = threads.slice().sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
  const leftGutter = 190;
  const laneHeight = 96;
  const stepX = 152;
  const laneTop = 58;

  const laneMap = {};
  orderedThreads.forEach((thread, index) => {
    laneMap[thread.threadId] = index;
  });

  const nodes = [];
  const edges = [];
  for (const thread of threads) {
    const turns = (state.turnsByThread[thread.threadId] || []).slice().sort((a, b) => a.idx - b.idx);
    const y = laneTop + laneMap[thread.threadId] * laneHeight;
    if (!turns.length) {
      nodes.push({
        id: `${thread.threadId}:head`,
        x: leftGutter,
        y,
        label: "Start",
        selected: state.selectedThreadId === thread.threadId,
        threadId: thread.threadId,
        title: threadLabel(thread),
      });
    }
    turns.forEach((turn, index) => {
      nodes.push({
        id: nodeIdForTurn(thread.threadId, turn.turnId),
        x: leftGutter + (turn.idx - 1) * stepX,
        y,
        label: `T${turn.idx}`,
        selected: state.selectedThreadId === thread.threadId,
        threadId: thread.threadId,
        title: threadLabel(thread),
      });
      if (index > 0) {
        edges.push({
          from: nodeIdForTurn(thread.threadId, turns[index - 1].turnId),
          to: nodeIdForTurn(thread.threadId, turn.turnId),
        });
      }
    });
    if (thread.parentThreadId && thread.forkedFromTurnId) {
      const childFirst = turns[0]?.turnId || `${thread.threadId}:head`;
      edges.push({
        from: nodeIdForTurn(thread.parentThreadId, thread.forkedFromTurnId),
        to: turns[0] ? nodeIdForTurn(thread.threadId, turns[0].turnId) : childFirst,
        fork: true,
      });
    }
  }

  const nodeMap = Object.fromEntries(nodes.map((node) => [node.id, node]));
  const width = Math.max(860, ...nodes.map((node) => node.x + 170));
  const height = Math.max(220, laneTop + orderedThreads.length * laneHeight);

  container.innerHTML = `
    <svg class="graph-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="xMinYMin meet">
      ${orderedThreads
        .map((thread) => {
          const y = laneTop + laneMap[thread.threadId] * laneHeight;
          const selected = state.selectedThreadId === thread.threadId;
          return `
            <g class="graph-lane" data-thread-id="${thread.threadId}">
              <rect x="10" y="${y - 30}" width="${width - 20}" height="60" rx="18" fill="${selected ? "rgba(182, 92, 50, 0.12)" : "rgba(255, 250, 241, 0.55)"}" stroke="${selected ? "rgba(182, 92, 50, 0.24)" : "rgba(216, 205, 191, 0.6)"}" />
              <text x="28" y="${y - 4}" font-size="14" font-weight="600" fill="#1f1a17">${escapeHtml(threadLabel(thread))}</text>
              <text x="28" y="${y + 15}" font-size="12" fill="#6a5f56">${escapeHtml(statusLabel(thread.status))}${thread.parentThreadId ? " | fork" : ""}</text>
            </g>
          `;
        })
        .join("")}
      ${edges
        .map((edge) => {
          const from = nodeMap[edge.from];
          const to = nodeMap[edge.to];
          if (!from || !to) {
            return "";
          }
          return `<path d="${buildEdgePath(from, to)}" fill="none" stroke="${edge.fork ? "#b65c32" : "#8a7b6b"}" stroke-width="${edge.fork ? "2.5" : "2"}" stroke-dasharray="${edge.fork ? "7 6" : "0"}" />`;
        })
        .join("")}
      ${nodes
        .map(
          (node) => `
            <g class="graph-node" data-thread-id="${node.threadId}">
              <title>${escapeHtml(node.title)}</title>
              <circle cx="${node.x}" cy="${node.y}" r="19" fill="${node.selected ? "#b65c32" : "#fffaf2"}" stroke="${node.selected ? "#8c3f1f" : "#8a7b6b"}" stroke-width="2.5" />
              <text x="${node.x}" y="${node.y + 4}" text-anchor="middle" font-size="11" font-weight="600" fill="${node.selected ? "#fff" : "#1f1a17"}">${node.label}</text>
            </g>
          `,
        )
        .join("")}
    </svg>
  `;

  container.querySelectorAll("[data-thread-id]").forEach((element) => {
    element.addEventListener("click", () => onSelect?.(element.dataset.threadId));
  });
}
