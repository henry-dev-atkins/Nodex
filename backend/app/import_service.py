from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from .db import Database
from .models import ImportPreviewRecord, ThreadRecord, TurnRecord
from .ws import WebSocketHub


EnsureThreadFn = Callable[[str], Awaitable[ThreadRecord]]
NormalizeMergeModeFn = Callable[[str | None], str]
ResolveBranchScopeFn = Callable[[str, str], list[dict[str, str]]]
BuildMergeTransferBlobFn = Callable[[str, str, list[dict[str, str]], str], Awaitable[str]]
DetectSecretsFn = Callable[[str], list[dict[str, Any]]]
PlusSecondsFn = Callable[[int], str]
BranchFromTurnFn = Callable[[str, str], Awaitable[ThreadRecord]]
StartTurnFn = Callable[[str, str], Awaitable[TurnRecord]]
AnnotateImportedTurnFn = Callable[[TurnRecord, ImportPreviewRecord], TurnRecord]


class ImportService:
    def __init__(
        self,
        db: Database,
        ws: WebSocketHub,
        *,
        ensure_thread: EnsureThreadFn,
        normalize_merge_mode: NormalizeMergeModeFn,
        resolve_branch_scope: ResolveBranchScopeFn,
        build_merge_transfer_blob: BuildMergeTransferBlobFn,
        detect_suspected_secrets: DetectSecretsFn,
        plus_seconds: PlusSecondsFn,
        branch_from_turn: BranchFromTurnFn,
        start_turn: StartTurnFn,
        annotate_imported_turn: AnnotateImportedTurnFn,
        import_preview_ttl_s: int,
    ) -> None:
        self.db = db
        self.ws = ws
        self._ensure_thread = ensure_thread
        self._normalize_merge_mode = normalize_merge_mode
        self._resolve_branch_scope = resolve_branch_scope
        self._build_merge_transfer_blob = build_merge_transfer_blob
        self._detect_suspected_secrets = detect_suspected_secrets
        self._plus_seconds = plus_seconds
        self._branch_from_turn = branch_from_turn
        self._start_turn = start_turn
        self._annotate_imported_turn = annotate_imported_turn
        self._import_preview_ttl_s = import_preview_ttl_s

    async def create_import_preview(
        self,
        source_thread_id: str,
        source_turn_id: str,
        dest_thread_id: str,
        dest_turn_id: str | None = None,
        merge_mode: str = "verbose",
    ) -> ImportPreviewRecord:
        await self._ensure_thread(source_thread_id)
        await self._ensure_thread(dest_thread_id)
        merge_mode = self._normalize_merge_mode(merge_mode)
        if dest_turn_id:
            dest_turn = self.db.get_turn(dest_thread_id, dest_turn_id)
            if not dest_turn:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": {
                            "code": "turn_not_found",
                            "message": f"Unknown destination turn: {dest_turn_id}",
                            "details": {},
                        }
                    },
                )
        source_nodes = self._resolve_branch_scope(source_thread_id, source_turn_id)
        blob = await self._build_merge_transfer_blob(source_thread_id, source_turn_id, source_nodes, merge_mode)
        preview = ImportPreviewRecord(
            previewId=f"imp_prev_{uuid.uuid4().hex}",
            destThreadId=dest_thread_id,
            destTurnId=dest_turn_id,
            sourceThreadId=source_thread_id,
            sourceAnchorTurnId=source_turn_id,
            sourceNodes=source_nodes,
            mergeMode=merge_mode,
            suspectedSecrets=self._detect_suspected_secrets(blob),
            transferBlob=blob,
            expiresAt=self._plus_seconds(self._import_preview_ttl_s),
        )
        self.db.save_import_preview(preview)
        return preview

    async def commit_import_preview(self, preview_id: str, confirmed: bool, edited_transfer_blob: str) -> dict[str, Any]:
        preview = self.db.get_import_preview(preview_id)
        if not preview:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "invalid_request", "message": "Unknown import preview", "details": {}}},
            )
        if not confirmed:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "import_preview_required", "message": "Import must be explicitly confirmed", "details": {}}},
            )
        created_thread: ThreadRecord | None = None
        destination_thread_id = preview.destThreadId
        if preview.destTurnId:
            head_turn_id = self.db.get_last_turn_id(preview.destThreadId)
            if head_turn_id != preview.destTurnId:
                created_thread = await self._branch_from_turn(preview.destThreadId, preview.destTurnId)
                destination_thread_id = created_thread.threadId
        turn = await self._start_turn(destination_thread_id, edited_transfer_blob)
        turn = self._annotate_imported_turn(turn, preview)
        await self.ws.emit_turn_updated(turn)
        self.db.delete_import_preview(preview_id)
        branch_turns = self.db.list_turns(created_thread.threadId) if created_thread else None
        return {
            "importedIntoTurnId": turn.turnId,
            "destThreadId": turn.threadId,
            "status": turn.status,
            "turn": turn.model_dump(),
            "thread": created_thread.model_dump() if created_thread else None,
            "turns": [item.model_dump() for item in branch_turns] if branch_turns else None,
        }
