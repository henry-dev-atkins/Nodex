from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from backend.app.db import Database
from backend.app.import_service import ImportService
from backend.app.models import ImportPreviewRecord, ThreadRecord, TurnRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


class FakeWs:
    def __init__(self) -> None:
        self.turn_updates: list[TurnRecord] = []

    async def emit_turn_updated(self, turn: TurnRecord) -> None:
        self.turn_updates.append(turn)


def test_create_import_preview_rejects_unknown_destination_turn() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "imports.db")
    ws = FakeWs()
    try:
        now = utc_now()
        source = ThreadRecord(threadId="source", title="Source", createdAt=now, updatedAt=now)
        dest = ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now)
        db.upsert_thread(source)
        db.upsert_thread(dest)

        async def ensure_thread(thread_id: str) -> ThreadRecord:
            thread = db.get_thread(thread_id)
            assert thread is not None
            return thread

        service = ImportService(
            db,
            ws,  # type: ignore[arg-type]
            ensure_thread=ensure_thread,
            normalize_merge_mode=lambda mode: mode or "verbose",
            resolve_branch_scope=lambda _thread_id, _turn_id: [],
            build_merge_transfer_blob=lambda *_args, **_kwargs: asyncio.sleep(0, result="blob"),
            detect_suspected_secrets=lambda _blob: [],
            plus_seconds=lambda _seconds: utc_now(),
            branch_from_turn=lambda *_args, **_kwargs: asyncio.sleep(0),
            start_turn=lambda *_args, **_kwargs: asyncio.sleep(0),
            annotate_imported_turn=lambda turn, _preview: turn,
            import_preview_ttl_s=900,
        )

        try:
            asyncio.run(service.create_import_preview("source", "turn-1", "dest", dest_turn_id="missing"))
            raise AssertionError("Expected destination turn validation to fail")
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail["error"]["code"] == "turn_not_found"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_create_import_preview_persists_preview_with_normalized_mode() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "imports.db")
    ws = FakeWs()
    try:
        now = utc_now()
        source = ThreadRecord(threadId="source", title="Source", createdAt=now, updatedAt=now)
        dest = ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now)
        db.upsert_thread(source)
        db.upsert_thread(dest)
        db.upsert_turn(TurnRecord(turnId="turn-1", threadId="source", idx=1, userText="prompt", status="completed", startedAt=now))
        calls = {"mode": None, "blob": None}

        async def ensure_thread(thread_id: str) -> ThreadRecord:
            thread = db.get_thread(thread_id)
            assert thread is not None
            return thread

        async def build_blob(source_thread_id: str, source_turn_id: str, source_nodes: list[dict[str, str]], merge_mode: str) -> str:
            calls["mode"] = merge_mode
            calls["blob"] = (source_thread_id, source_turn_id, source_nodes)
            return "Transfer blob"

        service = ImportService(
            db,
            ws,  # type: ignore[arg-type]
            ensure_thread=ensure_thread,
            normalize_merge_mode=lambda _mode: "analysis",
            resolve_branch_scope=lambda _thread_id, _turn_id: [{"threadId": "source", "turnId": "turn-1"}],
            build_merge_transfer_blob=build_blob,
            detect_suspected_secrets=lambda blob: [{"label": "x", "start": 0, "end": len(blob)}],
            plus_seconds=lambda _seconds: "2099-01-01T00:00:00Z",
            branch_from_turn=lambda *_args, **_kwargs: asyncio.sleep(0),
            start_turn=lambda *_args, **_kwargs: asyncio.sleep(0),
            annotate_imported_turn=lambda turn, _preview: turn,
            import_preview_ttl_s=900,
        )

        preview = asyncio.run(service.create_import_preview("source", "turn-1", "dest", merge_mode="summary"))
        persisted = db.get_import_preview(preview.previewId)

        assert calls["mode"] == "analysis"
        assert preview.mergeMode == "analysis"
        assert persisted is not None
        assert persisted.transferBlob == "Transfer blob"
        assert persisted.suspectedSecrets == [{"label": "x", "start": 0, "end": len("Transfer blob")}]
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_commit_import_preview_branches_when_destination_turn_is_not_head() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "imports.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now))
        db.upsert_turn(TurnRecord(turnId="dest-2", threadId="dest", idx=2, userText="old", status="completed", startedAt=now))
        db.upsert_turn(TurnRecord(turnId="dest-3", threadId="dest", idx=3, userText="head", status="completed", startedAt=now))
        preview = ImportPreviewRecord(
            previewId="preview-1",
            destThreadId="dest",
            destTurnId="dest-2",
            sourceThreadId="source",
            sourceAnchorTurnId="source-1",
            sourceNodes=[{"threadId": "source", "turnId": "source-1"}],
            mergeMode="verbose",
            suspectedSecrets=[],
            transferBlob="blob",
            expiresAt=utc_now(),
        )
        db.save_import_preview(preview)
        branch_calls: list[tuple[str, str]] = []

        async def ensure_thread(_thread_id: str) -> ThreadRecord:
            raise AssertionError("ensure_thread is not expected during commit")

        async def branch_from_turn(thread_id: str, turn_id: str) -> ThreadRecord:
            branch_calls.append((thread_id, turn_id))
            child = ThreadRecord(threadId="child", title="Child", createdAt=utc_now(), updatedAt=utc_now(), parentThreadId="dest", forkedFromTurnId=turn_id)
            db.upsert_thread(child)
            return child

        async def start_turn(thread_id: str, text: str) -> TurnRecord:
            turn = TurnRecord(turnId="child-3", threadId=thread_id, idx=3, userText=text, status="running", startedAt=utc_now())
            db.upsert_turn(turn)
            return turn

        def annotate(turn: TurnRecord, _preview: ImportPreviewRecord) -> TurnRecord:
            return turn

        service = ImportService(
            db,
            ws,  # type: ignore[arg-type]
            ensure_thread=ensure_thread,
            normalize_merge_mode=lambda mode: mode or "verbose",
            resolve_branch_scope=lambda _thread_id, _turn_id: [],
            build_merge_transfer_blob=lambda *_args, **_kwargs: asyncio.sleep(0, result="blob"),
            detect_suspected_secrets=lambda _blob: [],
            plus_seconds=lambda _seconds: utc_now(),
            branch_from_turn=branch_from_turn,
            start_turn=start_turn,
            annotate_imported_turn=annotate,
            import_preview_ttl_s=900,
        )

        result = asyncio.run(service.commit_import_preview("preview-1", True, "merged"))

        assert branch_calls == [("dest", "dest-2")]
        assert result["thread"]["threadId"] == "child"
        assert result["turn"]["threadId"] == "child"
        assert db.get_import_preview("preview-1") is None
        assert len(ws.turn_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_commit_import_preview_continues_head_without_branching() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "imports.db")
    ws = FakeWs()
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now))
        db.upsert_turn(TurnRecord(turnId="dest-6", threadId="dest", idx=6, userText="head", status="completed", startedAt=now))
        preview = ImportPreviewRecord(
            previewId="preview-2",
            destThreadId="dest",
            destTurnId="dest-6",
            sourceThreadId="source",
            sourceAnchorTurnId="source-1",
            sourceNodes=[{"threadId": "source", "turnId": "source-1"}],
            mergeMode="summary",
            suspectedSecrets=[],
            transferBlob="blob",
            expiresAt=utc_now(),
        )
        db.save_import_preview(preview)

        async def ensure_thread(_thread_id: str) -> ThreadRecord:
            raise AssertionError("ensure_thread is not expected during commit")

        async def unexpected_branch(_thread_id: str, _turn_id: str) -> ThreadRecord:
            raise AssertionError("branch_from_turn should not be called when destination turn is head")

        async def start_turn(thread_id: str, text: str) -> TurnRecord:
            turn = TurnRecord(turnId="dest-7", threadId=thread_id, idx=7, userText=text, status="running", startedAt=utc_now())
            db.upsert_turn(turn)
            return turn

        service = ImportService(
            db,
            ws,  # type: ignore[arg-type]
            ensure_thread=ensure_thread,
            normalize_merge_mode=lambda mode: mode or "verbose",
            resolve_branch_scope=lambda _thread_id, _turn_id: [],
            build_merge_transfer_blob=lambda *_args, **_kwargs: asyncio.sleep(0, result="blob"),
            detect_suspected_secrets=lambda _blob: [],
            plus_seconds=lambda _seconds: utc_now(),
            branch_from_turn=unexpected_branch,
            start_turn=start_turn,
            annotate_imported_turn=lambda turn, _preview: turn,
            import_preview_ttl_s=900,
        )

        result = asyncio.run(service.commit_import_preview("preview-2", True, "merged"))

        assert result["thread"] is None
        assert result["turn"]["threadId"] == "dest"
        assert db.get_import_preview("preview-2") is None
        assert len(ws.turn_updates) == 1
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
