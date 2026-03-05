from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from .db import Database
from .models import ThreadRecord
from .ws import WebSocketHub


EnsureThreadFn = Callable[[str], Awaitable[ThreadRecord]]
RetireSessionFn = Callable[[Any], Awaitable[None]]


class ConversationService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        sessions: dict[str, Any],
        session_lock: Any,
        *,
        ensure_thread: EnsureThreadFn,
        retire_session: RetireSessionFn,
    ) -> None:
        self.db = db
        self.ws = ws
        self.sessions = sessions
        self._session_lock = session_lock
        self._ensure_thread = ensure_thread
        self._retire_session = retire_session

    async def delete_conversation(self, thread_id: str) -> dict[str, Any]:
        await self._ensure_thread(thread_id)
        conversation_id = self.conversation_root_id(thread_id)
        thread_ids = self.db.list_branch_thread_ids(conversation_id)
        return await self._delete_threads(conversation_id, thread_ids)

    async def rename_thread(self, thread_id: str, title: str) -> ThreadRecord:
        clean_title = title.strip()
        if not clean_title:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_request", "message": "Title cannot be empty", "details": {}}},
            )
        await self._ensure_thread(thread_id)
        updated = self.db.update_thread_title(thread_id, clean_title)
        if not updated:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "thread_not_found", "message": f"Unknown thread: {thread_id}", "details": {}}},
            )
        await self.ws.emit_thread_updated(updated)
        return updated

    async def delete_branch(self, thread_id: str) -> dict[str, Any]:
        thread = await self._ensure_thread(thread_id)
        if not thread.parentThreadId:
            return await self.delete_conversation(thread_id)
        conversation_id = self.conversation_root_id(thread_id)
        thread_ids = self.db.list_branch_thread_ids(thread_id)
        return await self._delete_threads(conversation_id, thread_ids)

    def conversation_root_id(self, thread_id: str) -> str:
        current = self.db.get_thread(thread_id)
        if not current:
            return thread_id
        while current.parentThreadId:
            parent = self.db.get_thread(current.parentThreadId)
            if not parent:
                break
            current = parent
        return current.threadId

    async def _delete_threads(self, conversation_id: str, thread_ids: list[str]) -> dict[str, Any]:
        if not thread_ids:
            return {"conversationId": conversation_id, "deletedThreadIds": []}
        async with self._session_lock:
            sessions = [self.sessions.get(item) for item in thread_ids]
        for session in sessions:
            if session is not None:
                await self._retire_session(session)
        self.db.delete_threads(thread_ids)
        for deleted_thread_id in thread_ids:
            await self.ws.emit_thread_deleted(deleted_thread_id, conversation_id)
        return {"conversationId": conversation_id, "deletedThreadIds": thread_ids}
