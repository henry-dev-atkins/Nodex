from __future__ import annotations

from typing import Any

from .db import Database
from .models import TurnRecord
from .response_history import ResponseHistoryProjector


class TurnHistoryService:
    def __init__(self, db: Database, projector: ResponseHistoryProjector) -> None:
        self.db = db
        self.projector = projector

    def lineage_turn_snapshots(
        self,
        thread_id: str,
        upto_turn_id: str | None = None,
        include_error_turns: bool = False,
    ) -> list[dict[str, Any]]:
        thread = self.db.get_thread(thread_id)
        if not thread:
            return []
        turns: list[dict[str, Any]] = []
        if thread.parentThreadId and thread.forkedFromTurnId:
            turns.extend(self.lineage_turn_snapshots(thread.parentThreadId, thread.forkedFromTurnId, include_error_turns=True))
        for turn in self.db.list_turns(thread_id):
            if turn.status in {"error", "running", "interrupted"} and not include_error_turns:
                if upto_turn_id and turn.turnId == upto_turn_id:
                    break
                continue
            turns.append({"id": turn.turnId, "items": self.items_for_history_from_turn(turn)})
            if upto_turn_id and turn.turnId == upto_turn_id:
                break
        return turns

    def history_from_turn_snapshots(
        self,
        turns: list[dict[str, Any]],
        include_tool_calls: bool = False,
    ) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for turn in turns:
            history.extend(
                self.projector.response_items_from_thread_items(
                    turn.get("items", []),
                    include_tool_calls=include_tool_calls,
                )
            )
        return history

    def items_for_history_from_turn(self, turn: TurnRecord) -> list[dict[str, Any]]:
        fallback_text = (turn.userText or "").strip()
        existing = turn.metadata.get("items", [])
        if isinstance(existing, list):
            normalized_existing = [item for item in existing if isinstance(item, dict)]
            if normalized_existing:
                return self._ensure_user_message_item(normalized_existing, fallback_text)
        recovered = self.items_from_turn_events(turn.threadId, turn.turnId)
        if recovered:
            return self._ensure_user_message_item(recovered, fallback_text)
        if not fallback_text:
            return []
        return [self._user_message_item(fallback_text)]

    def items_from_turn_events(self, thread_id: str, turn_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        index_by_id: dict[str, int] = {}
        for event in self.db.list_turn_events(thread_id, turn_id):
            if event.type != "item/completed":
                continue
            item = event.payload.get("item")
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in index_by_id:
                items[index_by_id[item_id]] = item
                continue
            if isinstance(item_id, str):
                index_by_id[item_id] = len(items)
            items.append(item)
        return items

    def persist_turn_items_from_events(self, turn: TurnRecord) -> TurnRecord:
        existing = turn.metadata.get("items", [])
        if isinstance(existing, list) and any(isinstance(item, dict) for item in existing):
            return turn
        recovered = self.items_from_turn_events(turn.threadId, turn.turnId)
        if not recovered:
            return turn
        metadata = dict(turn.metadata)
        metadata["items"] = recovered
        updated = self.db.update_turn_status(
            turn.threadId,
            turn.turnId,
            turn.status,
            completed_at=turn.completedAt,
            metadata=metadata,
        )
        return updated or turn

    def _user_message_item(self, text: str) -> dict[str, Any]:
        return {
            "type": "userMessage",
            "content": [{"type": "text", "text": text, "text_elements": []}],
        }

    def _ensure_user_message_item(self, items: list[dict[str, Any]], fallback_text: str) -> list[dict[str, Any]]:
        if not fallback_text:
            return items
        extracted = self.projector.extract_user_text_from_items(items).strip()
        if extracted:
            return items
        return [self._user_message_item(fallback_text), *items]
