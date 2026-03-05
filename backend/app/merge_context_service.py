from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from .db import Database
from .models import ImportPreviewRecord, TurnRecord
from .util import utc_now


MERGE_MODES = {"verbose", "summary", "decision", "analysis"}


class MergeContextService:
    def __init__(self, db: Database, *, now_iso: Callable[[], str] = utc_now) -> None:
        self.db = db
        self._now_iso = now_iso

    def normalize_merge_mode(self, merge_mode: str | None) -> str:
        normalized = str(merge_mode or "verbose").strip().lower()
        if normalized not in MERGE_MODES:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_request", "message": f"Unsupported merge mode: {merge_mode}", "details": {}}},
            )
        return normalized

    def build_transfer_blob(self, source_thread_id: str, source_turn_id: str | list[str]) -> str:
        anchor_turn_id = source_turn_id[-1] if isinstance(source_turn_id, list) else source_turn_id
        source_nodes = self.resolve_branch_scope(source_thread_id, anchor_turn_id)
        return self.build_verbose_transfer_blob(source_thread_id, anchor_turn_id, source_nodes)

    def resolve_branch_scope(self, source_thread_id: str, anchor_turn_id: str) -> list[dict[str, str]]:
        thread = self.db.get_thread(source_thread_id)
        anchor_turn = self.db.get_turn(source_thread_id, anchor_turn_id)
        if not thread or not anchor_turn:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "turn_not_found", "message": f"Unknown source turn: {anchor_turn_id}", "details": {}}},
            )
        ordered: list[dict[str, str]] = []
        if thread.parentThreadId and thread.forkedFromTurnId:
            ordered.extend(self.resolve_branch_scope(thread.parentThreadId, thread.forkedFromTurnId))
        turns = self.db.list_turns(source_thread_id)
        for turn in turns:
            if turn.idx <= anchor_turn.idx:
                ordered.append({"threadId": source_thread_id, "turnId": turn.turnId})
        deduped: list[dict[str, str]] = []
        seen = set()
        for node in ordered:
            key = (node["threadId"], node["turnId"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(node)
        return deduped

    def build_verbose_transfer_blob(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
    ) -> str:
        source_thread = self.db.get_thread(source_thread_id)
        source_label = source_thread.title if source_thread and source_thread.title else source_thread_id
        lines = [
            "Copied branch context",
            "",
            f"Source branch: {source_label}",
            f"Source thread ID: {source_thread_id}",
            f"Source anchor turn ID: {source_turn_id}",
            f"Selected turns: {len(source_nodes)}",
            "",
        ]
        seen_summaries = set()
        seen_results = set()
        for node in source_nodes:
            thread_id = node["threadId"]
            turn = self.db.get_turn(thread_id, node["turnId"])
            if not turn:
                continue
            final_message = self.extract_final_agent_text(thread_id, turn.turnId)
            reasoning_summary = self.extract_reasoning_summary(thread_id, turn.turnId)
            decision_summary = self.extract_decision_summary(thread_id, turn.turnId, turn.status)
            if final_message:
                result_text = final_message
            else:
                result_text = "No final assistant result captured yet."
            summary_text = reasoning_summary or final_message or turn.userText
            normalized_summary = self.normalize_merge_block(summary_text)
            normalized_result = self.normalize_merge_block(result_text)
            command_summaries = self.extract_command_summaries(thread_id, turn.turnId)
            lines.append(f"{self.merge_branch_label(thread_id)} / Turn {turn.idx} ({turn.turnId})")
            lines.append("Prompt:")
            lines.append(turn.userText)
            lines.append("")
            if normalized_summary not in seen_summaries:
                lines.append("Summary:")
                lines.append(summary_text)
                lines.append("")
                seen_summaries.add(normalized_summary)
            lines.append("Decision:")
            lines.append(decision_summary)
            lines.append("")
            if normalized_result not in seen_results:
                lines.append("Result:")
                lines.append(result_text)
                seen_results.add(normalized_result)
            if command_summaries:
                lines.append("")
                lines.append("Commands:")
                lines.extend(f"- {summary}" for summary in command_summaries)
            lines.append("")
        lines.append("This is copied context, not a true merge. Use it as reference material in the destination branch.")
        return "\n".join(lines).strip()

    def annotate_imported_turn(self, turn: TurnRecord, preview: ImportPreviewRecord) -> TurnRecord:
        existing_links = turn.metadata.get("contextLinks", [])
        if not isinstance(existing_links, list):
            existing_links = []
        next_links = list(existing_links)
        linked_at = self._now_iso()
        next_links.append(
            {
                "kind": "contextImport",
                "mergeMode": preview.mergeMode,
                "sourceThreadId": preview.sourceThreadId,
                "sourceTurnId": preview.sourceAnchorTurnId,
                "sourceAnchorTurnId": preview.sourceAnchorTurnId,
                "sourceNodes": preview.sourceNodes,
                "previewId": preview.previewId,
                "linkedAt": linked_at,
            }
        )
        updated = self.db.update_turn_status(
            turn.threadId,
            turn.turnId,
            turn.status,
            completed_at=turn.completedAt,
            metadata={"contextLinks": next_links},
        )
        return updated or turn

    def merge_branch_label(self, thread_id: str) -> str:
        thread = self.db.get_thread(thread_id)
        return thread.title if thread and thread.title else thread_id

    def normalize_merge_block(self, text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).lower()

    def build_merge_scope_notes(self, source_nodes: list[dict[str, str]]) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        seen_summary_blocks = set()
        seen_result_blocks = set()
        for node in source_nodes:
            thread_id = node["threadId"]
            turn = self.db.get_turn(thread_id, node["turnId"])
            if not turn:
                continue
            final_message = self.extract_final_agent_text(thread_id, turn.turnId) or "No final assistant result captured yet."
            reasoning_summary = self.extract_reasoning_summary(thread_id, turn.turnId)
            summary_text = reasoning_summary or final_message or turn.userText
            decision_summary = self.extract_decision_summary(thread_id, turn.turnId, turn.status)
            command_summaries = self.extract_command_summaries(thread_id, turn.turnId)
            summary_text = summary_text if self.normalize_merge_block(summary_text) not in seen_summary_blocks else ""
            result_text = final_message if self.normalize_merge_block(final_message) not in seen_result_blocks else ""
            if summary_text:
                seen_summary_blocks.add(self.normalize_merge_block(summary_text))
            if result_text:
                seen_result_blocks.add(self.normalize_merge_block(result_text))
            notes.append(
                {
                    "threadId": thread_id,
                    "turnId": turn.turnId,
                    "turnIdx": turn.idx,
                    "branchLabel": self.merge_branch_label(thread_id),
                    "prompt": turn.userText,
                    "summary": summary_text,
                    "decision": decision_summary,
                    "result": result_text,
                    "commands": command_summaries,
                }
            )
        return notes

    def build_condensed_merge_prompt(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
        merge_mode: str,
    ) -> str:
        notes = self.build_merge_scope_notes(source_nodes)
        length_instruction = {
            "summary": "Write exactly 4 sentences.",
            "decision": "Write exactly 2 sentences.",
            "analysis": "Write one short paragraph.",
        }[merge_mode]
        purpose_instruction = {
            "summary": "Summarize the branch for reuse in another branch without losing the important facts and conclusions.",
            "decision": "State the branch-level decision centered on the selected turn's final state, including the important rationale.",
            "analysis": "Provide a concise analytical synthesis of the branch, preserving rationale, tradeoffs, and the current conclusion.",
        }[merge_mode]
        lines = [
            "You are condensing branch context so it can be merged into another branch.",
            "Respond with plain text only.",
            "Do not use tools, file changes, web searches, or approvals.",
            "Do not mention that this is a summary or copied context.",
            "Use only the material provided below.",
            "Focus on substantive facts, conclusions, decisions, and rationale.",
            "Ignore assistant process narration, planning chatter, and workflow bookkeeping unless it is itself the substantive outcome.",
            length_instruction,
            purpose_instruction,
            "",
            f"Selected source branch: {self.merge_branch_label(source_thread_id)}",
            f"Selected source thread ID: {source_thread_id}",
            f"Selected anchor turn ID: {source_turn_id}",
            f"Contributing turns: {len(notes)}",
            "",
            "Branch material:",
            "",
        ]
        for note in notes:
            lines.extend(
                [
                    f"{note['branchLabel']} / T{note['turnIdx']} ({note['threadId']}:{note['turnId']})",
                    "Prompt:",
                    note["prompt"],
                ]
            )
            if note["summary"]:
                lines.extend(["Summary:", note["summary"]])
            lines.extend(["Decision:", note["decision"]])
            if note["result"]:
                lines.extend(["Result:", note["result"]])
            if note["commands"]:
                lines.append("Commands:")
                lines.extend(f"- {command}" for command in note["commands"])
            lines.append("")
        return "\n".join(lines).strip()

    def build_condensed_merge_fallback(
        self,
        source_thread_id: str,
        source_turn_id: str,
        source_nodes: list[dict[str, str]],
        merge_mode: str,
    ) -> str:
        del source_thread_id, source_turn_id
        notes = self.build_merge_scope_notes(source_nodes)
        if not notes:
            return "No branch context was available to merge."
        final_note = notes[-1]
        if merge_mode == "decision":
            rationale = final_note["summary"] or final_note["result"] or final_note["prompt"]
            return f"{final_note['decision']} {self.truncate_merge_text(rationale, 220)}".strip()
        if merge_mode == "summary":
            sentences = []
            for note in notes:
                for candidate in [note["summary"], note["result"], note["decision"]]:
                    if candidate:
                        sentences.append(candidate.strip())
                if len(sentences) >= 4:
                    break
            return " ".join(sentences[:4]).strip()
        summary = final_note["summary"] or final_note["result"] or final_note["prompt"]
        return f"{summary} {final_note['decision']}".strip()

    def truncate_merge_text(self, text: str, limit: int) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: max(limit - 3, 1)].rstrip()}..."

    def extract_message_item_text(self, item: dict[str, Any]) -> str:
        text = str(item.get("text", "")).strip()
        if text:
            return text
        content = item.get("content", [])
        if isinstance(content, list):
            joined = "\n".join(
                str(part.get("text", "")).strip()
                for part in content
                if isinstance(part, dict) and part.get("text")
            ).strip()
            if joined:
                return joined
        return ""

    def extract_preview_text_from_items(self, items: list[dict[str, Any]]) -> str:
        messages = [
            self.extract_message_item_text(item)
            for item in items
            if isinstance(item, dict) and item.get("type") == "agentMessage"
        ]
        messages = [message for message in messages if message]
        return messages[-1] if messages else ""

    def extract_final_agent_text(self, thread_id: str, turn_id: str) -> str:
        chunks: list[str] = []
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type == "item/agentMessage/delta":
                chunks.append(str(event.payload.get("delta", "")))
                continue
            if event.type == "item/completed":
                item = event.payload.get("item", {})
                if item.get("type") == "agentMessage" and item.get("text"):
                    return str(item["text"])
        return "".join(chunks).strip()

    def extract_reasoning_summary(self, thread_id: str, turn_id: str) -> str:
        turn = self.db.get_turn(thread_id, turn_id)
        items = turn.metadata.get("items", []) if turn else []
        for item in items:
            if item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n".join(str(part).strip() for part in summary if str(part).strip()).strip()
                if text:
                    return text
            text = str(item.get("text", "")).strip()
            if text:
                return text
        chunks: list[str] = []
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type == "item/reasoning/summaryTextDelta":
                chunks.append(str(event.payload.get("delta", "")))
                continue
            if event.type != "item/completed":
                continue
            item = event.payload.get("item", {})
            if item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            if isinstance(summary, list):
                text = "\n".join(str(part).strip() for part in summary if str(part).strip()).strip()
                if text:
                    return text
            text = str(item.get("text", "")).strip()
            if text:
                return text
        return "".join(chunks).strip()

    def extract_command_summaries(self, thread_id: str, turn_id: str) -> list[str]:
        summaries: list[str] = []
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type != "item/completed":
                continue
            item = event.payload.get("item", {})
            if item.get("type") != "commandExecution":
                continue
            summaries.append(f"{item.get('command', '')} [{item.get('status', 'unknown')}] exit={item.get('exitCode')}")
        return summaries

    def extract_decision_summary(self, thread_id: str, turn_id: str, turn_status: str) -> str:
        approvals = self.db.list_approvals(thread_id=thread_id, turn_id=turn_id)
        decisions = [approval for approval in approvals if approval.status in {"approve", "deny"}]
        if decisions:
            latest = decisions[-1]
            if latest.status == "approve":
                return "Approval granted for the requested action."
            return "Approval denied for the requested action."
        if turn_status == "error":
            return "The turn failed before it produced a stable result."
        if turn_status == "running":
            return "The turn is still running."
        if turn_status == "interrupted":
            return "The turn was interrupted before completion."
        if turn_status == "completed":
            return "Completed without an explicit approval decision."
        return f"Turn status: {turn_status}"

    def detect_suspected_secrets(self, text: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        patterns = [
            ("Possible OpenAI key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
            ("Possible GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
            ("Possible AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
        ]
        for label, pattern in patterns:
            for match in pattern.finditer(text):
                findings.append({"label": label, "start": match.start(), "end": match.end()})
        for match in re.finditer(r"[A-Za-z0-9_\\-]{24,}", text):
            token = match.group(0)
            if self.looks_high_entropy(token):
                findings.append({"label": "High-entropy token-like string", "start": match.start(), "end": match.end()})
        return findings

    def looks_high_entropy(self, token: str) -> bool:
        if len(token) < 24:
            return False
        normalized = token.replace("-", "")
        if len(normalized) >= 24 and re.fullmatch(r"[0-9a-fA-F]+", normalized):
            return False
        alphabet = set(token)
        if len(alphabet) < 8:
            return False
        probabilities = [token.count(char) / len(token) for char in alphabet]
        entropy = -sum(prob * math.log2(prob) for prob in probabilities)
        return entropy > 3.5
