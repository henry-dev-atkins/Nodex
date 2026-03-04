from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CamelModel(BaseModel):
    model_config = {"populate_by_name": True}


class ThreadRecord(CamelModel):
    threadId: str
    title: str | None = None
    createdAt: str
    updatedAt: str
    parentThreadId: str | None = None
    forkedFromTurnId: str | None = None
    status: str = "idle"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnRecord(CamelModel):
    turnId: str
    threadId: str
    idx: int
    userText: str
    status: str
    startedAt: str
    completedAt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventRecord(CamelModel):
    eventId: int
    threadId: str
    turnId: str | None = None
    seq: int
    type: str
    ts: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalRecord(CamelModel):
    approvalId: str
    threadId: str
    turnId: str | None = None
    itemId: str | None = None
    requestId: str
    requestMethod: str
    status: Literal["pending", "approve", "deny", "expired"] = "pending"
    details: dict[str, Any] = Field(default_factory=dict)
    createdAt: str
    updatedAt: str


class ImportPreviewRecord(CamelModel):
    previewId: str
    destThreadId: str
    destTurnId: str | None = None
    sourceThreadId: str
    sourceAnchorTurnId: str
    sourceNodes: list[dict[str, str]]
    mergeMode: Literal["verbose", "summary", "decision", "analysis"] = "verbose"
    suspectedSecrets: list[dict[str, Any]]
    transferBlob: str
    expiresAt: str


class CreateThreadRequest(CamelModel):
    title: str | None = None


class StartTurnRequest(CamelModel):
    text: str = Field(min_length=1)
    clientRequestId: str | None = None


class ForkThreadRequest(CamelModel):
    title: str | None = None


class BranchThreadRequest(CamelModel):
    turnId: str
    title: str | None = None


class RenameThreadRequest(CamelModel):
    title: str = Field(min_length=1)


class ApprovalDecisionRequest(CamelModel):
    decision: Literal["approve", "deny"]


class ImportPreviewRequest(CamelModel):
    sourceThreadId: str
    sourceTurnId: str
    destThreadId: str
    destTurnId: str | None = None
    mergeMode: Literal["verbose", "summary", "decision", "analysis"] = "verbose"


class ImportCommitRequest(CamelModel):
    previewId: str
    confirmed: bool
    editedTransferBlob: str
