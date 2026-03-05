from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from fastapi import HTTPException

from backend.app.db import Database
from backend.app.merge_context_service import MergeContextService
from backend.app.models import ApprovalRecord, ImportPreviewRecord, ThreadRecord, TurnRecord
from backend.app.util import utc_now


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


def test_resolve_scope_and_build_verbose_blob() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "merge.db")
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="root", title="Root", createdAt=now, updatedAt=now))
        db.upsert_thread(
            ThreadRecord(
                threadId="child",
                title="Child",
                createdAt=now,
                updatedAt=now,
                parentThreadId="root",
                forkedFromTurnId="turn-root-1",
            )
        )
        db.upsert_turn(TurnRecord(turnId="turn-root-1", threadId="root", idx=1, userText="root prompt", status="completed", startedAt=now))
        db.upsert_turn(TurnRecord(turnId="turn-child-1", threadId="child", idx=1, userText="child prompt", status="completed", startedAt=now))
        db.append_event("root", "turn-root-1", 1, "item/completed", {"item": {"type": "agentMessage", "text": "root result"}})
        db.append_event(
            "child",
            "turn-child-1",
            1,
            "item/completed",
            {"item": {"type": "commandExecution", "command": "pytest -q", "status": "success", "exitCode": 0}},
        )
        db.append_event("child", "turn-child-1", 2, "item/completed", {"item": {"type": "agentMessage", "text": "child result"}})

        service = MergeContextService(db)
        nodes = service.resolve_branch_scope("child", "turn-child-1")
        blob = service.build_verbose_transfer_blob("child", "turn-child-1", nodes)

        assert nodes == [
            {"threadId": "root", "turnId": "turn-root-1"},
            {"threadId": "child", "turnId": "turn-child-1"},
        ]
        assert "Source branch: Child" in blob
        assert "root prompt" in blob
        assert "child prompt" in blob
        assert "root result" in blob
        assert "child result" in blob
        assert "pytest -q [success] exit=0" in blob
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_normalize_merge_mode_rejects_unsupported_value() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "merge.db")
    try:
        service = MergeContextService(db)
        try:
            service.normalize_merge_mode("invalid")
            raise AssertionError("Expected unsupported merge mode to fail")
        except HTTPException as exc:
            assert exc.status_code == 400
            assert exc.detail["error"]["code"] == "invalid_request"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_extract_decision_summary_prefers_latest_approval_decision() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "merge.db")
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="Thread", createdAt=now, updatedAt=now))
        db.upsert_turn(TurnRecord(turnId="turn-1", threadId="thread-1", idx=1, userText="prompt", status="completed", startedAt=now))
        db.upsert_approval(
            ApprovalRecord(
                approvalId="approval-1",
                threadId="thread-1",
                turnId="turn-1",
                itemId=None,
                requestId="req-1",
                requestMethod="item/commandExecution/requestApproval",
                status="approve",
                details={},
                createdAt=now,
                updatedAt=now,
            )
        )
        service = MergeContextService(db)

        assert service.extract_decision_summary("thread-1", "turn-1", "completed") == "Approval granted for the requested action."
        assert service.extract_decision_summary("thread-1", "turn-missing", "error") == "The turn failed before it produced a stable result."
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_detect_suspected_secrets_captures_known_and_high_entropy_tokens() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "merge.db")
    try:
        service = MergeContextService(db)
        findings = service.detect_suspected_secrets(
            "openai sk-abcdefghijklmnopqrstuvwxyz1234 entropy AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        )
        labels = {finding["label"] for finding in findings}
        assert "Possible OpenAI key" in labels
        assert "High-entropy token-like string" in labels
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_annotate_imported_turn_appends_context_link() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "merge.db")
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="dest", title="Dest", createdAt=now, updatedAt=now))
        db.upsert_turn(
            TurnRecord(
                turnId="dest-1",
                threadId="dest",
                idx=1,
                userText="prompt",
                status="running",
                startedAt=now,
                metadata={"contextLinks": [{"kind": "existing"}], "other": "value"},
            )
        )
        preview = ImportPreviewRecord(
            previewId="preview-1",
            destThreadId="dest",
            destTurnId=None,
            sourceThreadId="source",
            sourceAnchorTurnId="source-2",
            sourceNodes=[{"threadId": "source", "turnId": "source-1"}, {"threadId": "source", "turnId": "source-2"}],
            mergeMode="summary",
            suspectedSecrets=[],
            transferBlob="blob",
            expiresAt=now,
        )
        service = MergeContextService(db)
        turn = db.get_turn("dest", "dest-1")
        assert turn is not None

        updated = service.annotate_imported_turn(turn, preview)

        assert updated.metadata["other"] == "value"
        assert len(updated.metadata["contextLinks"]) == 2
        assert updated.metadata["contextLinks"][1]["kind"] == "contextImport"
        assert updated.metadata["contextLinks"][1]["previewId"] == "preview-1"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def test_extract_reasoning_summary_falls_back_to_event_deltas() -> None:
    temp_root = make_temp_root()
    db = Database(temp_root / "merge.db")
    try:
        now = utc_now()
        db.upsert_thread(ThreadRecord(threadId="thread-1", title="Thread", createdAt=now, updatedAt=now))
        db.upsert_turn(TurnRecord(turnId="turn-1", threadId="thread-1", idx=1, userText="prompt", status="completed", startedAt=now))
        db.append_event("thread-1", "turn-1", 1, "item/reasoning/summaryTextDelta", {"delta": "First part "})
        db.append_event("thread-1", "turn-1", 2, "item/reasoning/summaryTextDelta", {"delta": "Second part"})
        service = MergeContextService(db)

        assert service.extract_reasoning_summary("thread-1", "turn-1") == "First part Second part"
    finally:
        db.close()
        shutil.rmtree(temp_root, ignore_errors=True)
