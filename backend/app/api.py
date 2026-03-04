from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from .codex_manager import CodexManager
from .db import Database
from .models import (
    ApprovalDecisionRequest,
    BranchThreadRequest,
    CreateThreadRequest,
    ForkThreadRequest,
    ImportCommitRequest,
    ImportPreviewRequest,
    RenameThreadRequest,
    StartTurnRequest,
)
from .util import APP_NAME, APP_VERSION, utc_now


def build_api_router(db: Database, manager: CodexManager, require_token):
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/bootstrap")
    async def bootstrap(afterEventId: int | None = Query(default=None)) -> dict[str, Any]:
        threads = db.list_threads()
        turns = [turn for thread in threads for turn in db.list_turns(thread.threadId)]
        events = db.list_events(after_event_id=afterEventId, limit=5000)
        return {
            "serverTime": utc_now(),
            "snapshot": {
                "threads": [thread.model_dump() for thread in threads],
                "turns": [turn.model_dump() for turn in turns],
                "approvals": [approval.model_dump() for approval in db.list_approvals()],
                "pendingApprovals": [approval.model_dump() for approval in db.list_pending_approvals()],
            },
            "events": [event.model_dump() for event in events],
            "lastEventId": db.last_event_id(),
        }

    @router.get("/threads")
    async def list_threads() -> dict[str, Any]:
        return {"threads": [thread.model_dump() for thread in await manager.list_threads()]}

    @router.post("/threads")
    async def create_thread(payload: CreateThreadRequest) -> dict[str, Any]:
        thread = await manager.start_thread(title=payload.title)
        return {"thread": thread.model_dump()}

    @router.get("/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, Any]:
        thread = await manager.get_thread(thread_id)
        turns = db.list_turns(thread_id)
        return {"thread": thread.model_dump(), "turns": [turn.model_dump() for turn in turns]}

    @router.get("/threads/{thread_id}/events")
    async def get_thread_events(
        thread_id: str,
        afterEventId: int | None = Query(default=None),
        limit: int = Query(default=500, le=5000),
    ) -> dict[str, Any]:
        await manager.get_thread(thread_id)
        events = db.list_events(after_event_id=afterEventId, thread_id=thread_id, limit=limit)
        return {"events": [event.model_dump() for event in events], "lastEventId": db.last_event_id()}

    @router.post("/threads/{thread_id}/turns")
    async def post_turn(thread_id: str, payload: StartTurnRequest) -> dict[str, Any]:
        turn = await manager.start_turn(thread_id, payload.text)
        return {"turn": turn.model_dump()}

    @router.post("/threads/{thread_id}/fork")
    async def post_fork(thread_id: str, payload: ForkThreadRequest) -> dict[str, Any]:
        thread = await manager.fork_thread(thread_id, title=payload.title)
        turns = db.list_turns(thread.threadId)
        return {"thread": thread.model_dump(), "turns": [turn.model_dump() for turn in turns]}

    @router.post("/threads/{thread_id}/branch")
    async def post_branch(thread_id: str, payload: BranchThreadRequest) -> dict[str, Any]:
        thread = await manager.branch_from_turn(thread_id, payload.turnId, title=payload.title)
        turns = db.list_turns(thread.threadId)
        return {"thread": thread.model_dump(), "turns": [turn.model_dump() for turn in turns]}

    @router.post("/threads/{thread_id}/rename")
    async def rename_thread(thread_id: str, payload: RenameThreadRequest) -> dict[str, Any]:
        thread = await manager.rename_thread(thread_id, payload.title)
        return {"thread": thread.model_dump()}

    @router.delete("/threads/{thread_id}")
    async def delete_thread_branch(thread_id: str) -> dict[str, Any]:
        return await manager.delete_branch(thread_id)

    @router.delete("/conversations/{thread_id}")
    async def delete_conversation(thread_id: str) -> dict[str, Any]:
        return await manager.delete_conversation(thread_id)

    @router.post("/approvals/{approval_id}")
    async def respond_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict[str, Any]:
        approval = await manager.respond_approval(approval_id, payload.decision)
        return {"approvalId": approval.approvalId, "decision": payload.decision, "status": "submitted"}

    @router.post("/import/preview")
    async def import_preview(payload: ImportPreviewRequest) -> dict[str, Any]:
        preview = await manager.create_import_preview(
            payload.sourceThreadId,
            payload.sourceTurnIds,
            payload.destThreadId,
            dest_turn_id=payload.destTurnId,
        )
        return preview.model_dump()

    @router.post("/import/commit")
    async def import_commit(payload: ImportCommitRequest) -> dict[str, Any]:
        return await manager.commit_import_preview(payload.previewId, payload.confirmed, payload.editedTransferBlob)

    @router.get("/meta")
    async def meta() -> dict[str, Any]:
        return {"service": APP_NAME, "version": APP_VERSION}

    return router
