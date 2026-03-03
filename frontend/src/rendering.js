export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function normalizeText(value) {
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

export function formatText(value) {
  return escapeHtml(normalizeText(value)).replaceAll("\n", "<br />");
}

export function truncateText(value, limit = 140) {
  const text = normalizeText(value).trim();
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, Math.max(limit - 1, 1)).trimEnd()}...`;
}

export function summarizeText(value, limit = 96) {
  const normalized = normalizeText(value);
  const lines = normalized
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => line !== "```");
  const seed = lines[0] || normalized.replace(/\s+/g, " ").trim();
  const cleaned = seed
    .replace(/^[-*]\s+/, "")
    .replace(/^\d+[.)]\s+/, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!cleaned) {
    return "";
  }
  const sentenceMatch = cleaned.match(/^(.{24,}?[.!?])(?:\s|$)/);
  const summary = sentenceMatch?.[1] || cleaned;
  return truncateText(summary, limit);
}

export function humanizeTurnStatus(status) {
  if (status === "inProgress" || status === "running") {
    return "Running";
  }
  if (status === "completed") {
    return "Completed";
  }
  if (status === "denied") {
    return "Denied";
  }
  if (status === "error" || status === "failed") {
    return "Error";
  }
  if (status === "interrupted") {
    return "Interrupted";
  }
  return status || "Pending";
}

export function humanizeThreadStatus(status) {
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

export function threadLabel(thread) {
  return normalizeText(thread?.title || thread?.metadata?.preview || "Untitled conversation");
}

export function describeDecision(turn, approvals = []) {
  const decided = approvals.filter((approval) => approval.status === "approve" || approval.status === "deny");
  const latestApproval = decided[decided.length - 1] || null;
  if (latestApproval?.status === "deny") {
    return { label: "Approval denied", tone: "danger" };
  }
  if (latestApproval?.status === "approve") {
    return { label: "Approval approved", tone: "success" };
  }
  if (turn.status === "error" || turn.status === "failed") {
    return { label: "Turn failed", tone: "danger" };
  }
  if (turn.status === "interrupted") {
    return { label: "Interrupted", tone: "warning" };
  }
  if (turn.status === "running" || turn.status === "inProgress") {
    return { label: "Running", tone: "live" };
  }
  return { label: humanizeTurnStatus(turn.status), tone: "muted" };
}

function shortenPath(path) {
  if (!path) {
    return "";
  }
  const normalized = normalizeText(path);
  const parts = normalized.split(/[\\/]/);
  return parts[parts.length - 1] || normalized;
}

function extractItemText(item) {
  if (!item || typeof item !== "object") {
    return "";
  }
  if (item.text) {
    return normalizeText(item.text);
  }
  if (Array.isArray(item.content)) {
    return normalizeText(
      item.content
        .map((part) => part?.text || "")
        .filter(Boolean)
        .join("\n"),
    );
  }
  return "";
}

function formatCommand(item, stage) {
  const command = normalizeText(Array.isArray(item.command) ? item.command.join(" ") : item.command || "Command");
  const details = [stage === "started" ? "Running" : humanizeTurnStatus(item.status), command];
  if (item.cwd) {
    details.push(`cwd ${normalizeText(item.cwd)}`);
  }
  if (item.exitCode !== undefined && item.exitCode !== null) {
    details.push(`exit ${item.exitCode}`);
  }
  return {
    id: item.id || `command-${command}`,
    kind: item.status === "failed" ? "error" : "tool",
    title: "Command",
    plainText: details.join(" | "),
    isReasoning: false,
  };
}

function formatFileChange(item, stage) {
  const changes = Array.isArray(item.changes) ? item.changes : [];
  const title = item.status === "denied" ? "File change denied" : stage === "started" ? "File change proposed" : "File change applied";
  const details = changes.length
    ? changes
        .slice(0, 6)
        .map((change) => {
          const kind = change.kind?.type || change.type || "update";
          return `${shortenPath(change.path)} (${kind})`;
        })
        .join(", ")
    : "No file details available.";
  return {
    id: item.id || title,
    kind: item.status === "denied" ? "warning" : "tool",
    title,
    plainText: `${title}: ${details}`,
    isReasoning: false,
  };
}

function formatReasoning(item) {
  const summary = normalizeText(Array.isArray(item.summary) ? item.summary.join("\n") : item.text || "");
  if (!summary.trim()) {
    return null;
  }
  return {
    id: item.id || `reasoning-${summary.slice(0, 24)}`,
    kind: "note",
    title: "Reasoning",
    plainText: summary,
    isReasoning: true,
  };
}

function formatWebSearch(item) {
  const query = normalizeText(item.query || item.action?.query || "Search");
  return {
    id: item.id || `search-${query}`,
    kind: "tool",
    title: "Web search",
    plainText: query,
    isReasoning: false,
  };
}

function buildItemBlock(item, stage) {
  if (!item || typeof item !== "object") {
    return null;
  }
  if (item.type === "agentMessage") {
    const text = extractItemText(item);
    if (!text) {
      return null;
    }
    const commentary = item.phase === "commentary";
    return {
      id: item.id || `agent-${text.slice(0, 24)}`,
      kind: commentary ? "note" : "assistant",
      title: commentary ? "Commentary" : "Assistant",
      plainText: text,
      isReasoning: false,
    };
  }
  if (item.type === "commandExecution") {
    return formatCommand(item, stage);
  }
  if (item.type === "fileChange") {
    return formatFileChange(item, stage);
  }
  if (item.type === "reasoning") {
    return formatReasoning(item);
  }
  if (item.type === "webSearch") {
    return formatWebSearch(item);
  }
  return null;
}

function buildApprovalBlocks(approvals) {
  return approvals
    .slice()
    .sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)))
    .map((approval) => {
      const details = approval.details || {};
      const isCommand = approval.requestMethod === "item/commandExecution/requestApproval";
      const title =
        approval.status === "pending"
          ? "Approval requested"
          : approval.status === "approve"
            ? "Approval approved"
            : "Approval denied";
      const lines = [];
      if (isCommand && details.command) {
        lines.push(normalizeText(details.command));
      }
      if (isCommand && details.cwd) {
        lines.push(`cwd ${normalizeText(details.cwd)}`);
      }
      if (!isCommand) {
        lines.push(normalizeText(details.reason || "Codex requested permission to change files in this workspace."));
      }
      if (details.grantRoot) {
        lines.push(`scope ${normalizeText(details.grantRoot)}`);
      }
      return {
        id: approval.approvalId,
        kind: approval.status === "deny" ? "warning" : "note",
        title,
        plainText: `${title}: ${lines.filter(Boolean).join(" | ") || approval.requestMethod}`,
        isReasoning: false,
      };
    });
}

export function buildBlocks(turn, events, approvals) {
  const blocks = [];
  let delta = "";
  let reasoningDelta = "";
  let syntheticIndex = 0;

  const pushBlock = (block) => {
    if (!block) {
      return;
    }
    const last = blocks[blocks.length - 1];
    if (last && last.kind === block.kind && last.title === block.title && last.plainText === block.plainText) {
      return;
    }
    blocks.push(block);
  };

  const flushDelta = (phase = "assistant") => {
    if (!delta.trim()) {
      return;
    }
    syntheticIndex += 1;
    pushBlock({
      id: `delta-${syntheticIndex}`,
      kind: phase === "commentary" ? "note" : "assistant",
      title: phase === "commentary" ? "Commentary" : "Assistant",
      plainText: delta.trim(),
      isReasoning: false,
    });
    delta = "";
  };

  const flushReasoning = () => {
    const text = normalizeText(reasoningDelta).trim();
    if (!text) {
      return;
    }
    syntheticIndex += 1;
    pushBlock({
      id: `reasoning-${syntheticIndex}`,
      kind: "note",
      title: "Reasoning",
      plainText: text,
      isReasoning: true,
    });
    reasoningDelta = "";
  };

  for (const event of events) {
    if (String(event.type).startsWith("codex/event/")) {
      continue;
    }
    if (event.type === "item/agentMessage/delta") {
      delta += event.payload.delta || "";
      continue;
    }
    if (event.type === "item/reasoning/summaryTextDelta") {
      reasoningDelta += event.payload.delta || "";
      continue;
    }
    if (event.type === "item/completed") {
      const item = event.payload.item || {};
      if (item.type === "agentMessage") {
        const commentary = item.phase === "commentary";
        const text = extractItemText(item).trim();
        if (delta.trim() === text) {
          delta = "";
          flushReasoning();
          pushBlock({
            id: item.id || `agent-${text.slice(0, 24)}`,
            kind: commentary ? "note" : "assistant",
            title: commentary ? "Commentary" : "Assistant",
            plainText: text,
            isReasoning: false,
          });
          continue;
        }
      }
      flushDelta();
      flushReasoning();
      pushBlock(buildItemBlock(item, "completed"));
      continue;
    }
    if (event.type === "item/started") {
      flushDelta();
      flushReasoning();
      pushBlock(buildItemBlock(event.payload.item || {}, "started"));
      continue;
    }
    if (event.type === "error") {
      flushDelta();
      flushReasoning();
      pushBlock({
        id: `error-${event.eventId || syntheticIndex + 1}`,
        kind: "error",
        title: "Error",
        plainText: String(event.payload.error?.message || "The turn failed."),
        isReasoning: false,
      });
    }
  }
  flushDelta();
  flushReasoning();

  for (const approvalBlock of buildApprovalBlocks(approvals)) {
    pushBlock(approvalBlock);
  }
  if (blocks.length) {
    return blocks;
  }
  return (turn.metadata?.items || []).map((item) => buildItemBlock(item, "completed")).filter(Boolean);
}

export function summarizeTurn(turn, blocks, approvals = []) {
  const assistant = blocks.findLast?.((block) => block.kind === "assistant") || [...blocks].reverse().find((block) => block.kind === "assistant");
  const commentary = [...blocks].reverse().find((block) => block.title === "Commentary");
  const reasoning = [...blocks].reverse().find((block) => block.isReasoning);
  const reasoningCount = blocks.filter((block) => block.isReasoning).length;
  const toolCount = blocks.filter((block) => block.kind === "tool").length;
  const approvalCount = blocks.filter((block) => block.title.startsWith("Approval")).length;
  const decision = describeDecision(turn, approvals);
  const preview = assistant?.plainText || commentary?.plainText || "No assistant message captured yet.";
  const reasoningPreview = reasoning?.plainText || "";
  return {
    prompt: normalizeText(turn.userText || "No prompt captured."),
    promptShort: summarizeText(turn.userText || "No prompt captured.", 96),
    preview,
    previewShort: truncateText(preview, 180),
    summary: truncateText(reasoningPreview || preview || turn.userText || "No summary captured yet.", 180),
    reasoningPreview: truncateText(reasoningPreview, 160),
    reasoningCount,
    toolCount,
    approvalCount,
    decisionLabel: decision.label,
    decisionTone: decision.tone,
  };
}
