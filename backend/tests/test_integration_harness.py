from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.main import create_app


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


@contextmanager
def patched_env(overrides: dict[str, str]):
    original = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def wait_for(predicate, timeout_s: float = 5.0, interval_s: float = 0.05):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return predicate()


async def _noop(*_args, **_kwargs) -> None:
    return None


class FakeRpcHarness:
    next_thread = 1
    next_turn = 1
    next_item = 1
    next_request = 0
    threads: dict[str, dict[str, Any]] = {}

    def __init__(self, notification_handler, server_request_handler, exit_handler) -> None:
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        self.exit_handler = exit_handler
        self.thread: dict[str, Any] | None = None
        self.pending: dict[int, dict[str, str]] = {}

    @classmethod
    async def start(
        cls,
        command: list[str],
        notification_handler,
        server_request_handler,
        stderr_handler,
        exit_handler,
    ) -> "FakeRpcHarness":
        del command, stderr_handler
        return cls(notification_handler, server_request_handler, exit_handler)

    async def request(self, method: str, params: dict[str, Any], timeout_s: float = 60.0) -> Any:
        del timeout_s
        if method == "initialize":
            return {"serverInfo": {"name": "fake-codex", "version": "0.106.0"}}
        if method == "thread/start":
            thread_id = f"fake-thread-{self.__class__.next_thread:04d}"
            self.__class__.next_thread += 1
            now = int(time.time())
            self.thread = {
                "id": thread_id,
                "name": f"Fake thread {thread_id}",
                "preview": "",
                "cwd": str(Path.cwd()),
                "path": str(Path.cwd() / ".tmp" / f"{thread_id}.jsonl"),
                "cliVersion": "0.106.0",
                "modelProvider": "fake-openai",
                "source": "fake",
                "status": {"type": "idle"},
                "createdAt": now,
                "updatedAt": now,
                "turns": [],
            }
            self.__class__.threads[thread_id] = self.thread
            return {"thread": self._thread_payload()}
        if method == "thread/resume":
            history = params.get("history")
            if history:
                thread_id = f"fake-thread-{self.__class__.next_thread:04d}"
                self.__class__.next_thread += 1
                now = int(time.time())
                turns = self._turns_from_history(history)
                self.thread = {
                    "id": thread_id,
                    "name": f"Fake branch {thread_id}",
                    "preview": self._history_preview(history),
                    "cwd": str(Path.cwd()),
                    "path": str(Path.cwd() / ".tmp" / f"{thread_id}.jsonl"),
                    "cliVersion": "0.106.0",
                    "modelProvider": "fake-openai",
                    "source": "fake",
                    "status": {"type": "idle"},
                    "createdAt": now,
                    "updatedAt": now,
                    "turns": turns,
                }
                self.__class__.threads[thread_id] = self.thread
                return {"thread": self._thread_payload()}
            thread = self.__class__.threads[params["threadId"]]
            self.thread = thread
            return {"thread": self._thread_payload(thread)}
        if method == "thread/read":
            thread = self.__class__.threads[params["threadId"]]
            self.thread = thread
            return {"thread": self._thread_payload(thread)}
        if method == "thread/fork":
            parent = self.__class__.threads[params["threadId"]]
            thread_id = f"fake-thread-{self.__class__.next_thread:04d}"
            self.__class__.next_thread += 1
            now = int(time.time())
            child = {
                **json.loads(json.dumps(parent)),
                "id": thread_id,
                "name": f"Fake fork {thread_id}",
                "createdAt": now,
                "updatedAt": now,
                "status": {"type": "idle"},
            }
            self.__class__.threads[thread_id] = child
            return {"thread": self._thread_payload(child)}
        if method == "turn/start":
            assert self.thread is not None
            turn_id = f"fake-turn-{self.__class__.next_turn:04d}"
            self.__class__.next_turn += 1
            user_text = "".join(str(item.get("text", "")) for item in params.get("input", []) if item.get("type") == "text")
            self.thread["turns"].append(
                {
                    "id": turn_id,
                    "status": "inProgress",
                    "items": [
                        {
                            "id": "item-1",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": user_text, "text_elements": []}],
                        }
                    ],
                }
            )
            await self.notification_handler(
                {"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"threadId": self.thread["id"], "status": {"type": "active"}}}
            )
            await self.notification_handler(
                {
                    "jsonrpc": "2.0",
                    "method": "turn/started",
                    "params": {"threadId": self.thread["id"], "turnId": turn_id, "turn": {"id": turn_id, "status": "inProgress"}},
                }
            )
            item_id = f"call_fake_{self.__class__.next_item:04d}"
            self.__class__.next_item += 1
            await self.notification_handler(
                {
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "threadId": self.thread["id"],
                        "turnId": turn_id,
                        "item": {
                            "id": item_id,
                            "type": "fileChange",
                            "status": "inProgress",
                            "changes": [
                                {
                                    "path": str(Path.cwd() / "fake_approval_output.txt"),
                                    "kind": {"type": "add"},
                                    "diff": "FAKE_APPROVAL_CONTENT\n",
                                }
                            ],
                        },
                    },
                }
            )
            await self.notification_handler(
                {
                    "jsonrpc": "2.0",
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": self.thread["id"],
                        "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
                    },
                }
            )
            request_id = self.__class__.next_request
            self.__class__.next_request += 1
            self.pending[request_id] = {"threadId": self.thread["id"], "turnId": turn_id, "itemId": item_id}
            await self.server_request_handler(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "item/fileChange/requestApproval",
                    "params": {"threadId": self.thread["id"], "turnId": turn_id, "itemId": item_id, "reason": "Fake approval gate"},
                }
            )
            return {"turn": {"id": turn_id, "status": "inProgress"}}
        raise AssertionError(f"Unexpected fake RPC request: {method}")

    async def request_with_retry(self, method: str, params: dict[str, Any], timeout_s: float = 60.0) -> Any:
        return await self.request(method, params, timeout_s=timeout_s)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        del method, params

    async def send_response(self, request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        del error
        pending = self.pending.pop(int(request_id))
        decision = str((result or {}).get("decision", "decline"))
        denied = decision != "accept"
        message = "Denied fake file change and completed the turn without writing." if denied else "Approved fake file change."
        thread = self.__class__.threads[pending["threadId"]]
        turn = next(item for item in thread["turns"] if item["id"] == pending["turnId"])
        turn["status"] = "completed"
        turn["items"].append(
            {
                "id": pending["itemId"],
                "type": "fileChange",
                "status": "denied" if denied else "completed",
                "changes": [],
            }
        )
        await self.notification_handler(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": pending["threadId"],
                    "turnId": pending["turnId"],
                    "item": {"id": pending["itemId"], "type": "fileChange", "status": "denied" if denied else "completed", "changes": []},
                },
            }
        )
        item_id = f"msg_fake_{self.__class__.next_item:04d}"
        self.__class__.next_item += 1
        turn["items"].append({"id": item_id, "type": "agentMessage", "phase": "final_answer", "text": message})
        await self.notification_handler(
            {
                "jsonrpc": "2.0",
                "method": "item/agentMessage/delta",
                "params": {"threadId": pending["threadId"], "turnId": pending["turnId"], "itemId": item_id, "delta": message},
            }
        )
        await self.notification_handler(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "threadId": pending["threadId"],
                    "turnId": pending["turnId"],
                    "item": {"id": item_id, "type": "agentMessage", "phase": "final_answer", "text": message},
                },
            }
        )
        thread["updatedAt"] = int(time.time())
        await self.notification_handler(
            {"jsonrpc": "2.0", "method": "thread/status/changed", "params": {"threadId": pending["threadId"], "status": {"type": "idle"}}}
        )
        await self.notification_handler(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {
                    "threadId": pending["threadId"],
                    "turnId": pending["turnId"],
                    "turn": {"id": pending["turnId"], "status": "completed", "items": []},
                },
            }
        )

    async def close(self) -> None:
        await self.exit_handler(0)

    def _thread_payload_for(self, thread: dict[str, Any] | None) -> dict[str, Any]:
        assert thread is not None
        return {
            "id": thread["id"],
            "name": thread["name"],
            "preview": thread["preview"],
            "cwd": thread["cwd"],
            "path": thread["path"],
            "cliVersion": thread["cliVersion"],
            "modelProvider": thread["modelProvider"],
            "source": thread["source"],
            "status": thread["status"],
            "createdAt": thread["createdAt"],
            "updatedAt": thread["updatedAt"],
            "turns": list(thread["turns"]),
        }

    def _thread_payload(self, thread: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._thread_payload_for(thread or self.thread)

    def _turns_from_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for item in history:
            if item.get("type") == "message" and item.get("role") == "user":
                turn_id = f"fake-turn-{self.__class__.next_turn:04d}"
                self.__class__.next_turn += 1
                text = "\n".join(content.get("text", "") for content in item.get("content", []) if isinstance(content, dict))
                current = {
                    "id": turn_id,
                    "status": "completed",
                    "items": [
                        {
                            "id": f"user_fake_{self.__class__.next_item:04d}",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": text, "text_elements": []}],
                        }
                    ],
                }
                self.__class__.next_item += 1
                turns.append(current)
                continue
            if current is None:
                continue
            if item.get("type") == "message" and item.get("role") == "assistant":
                text = "\n".join(content.get("text", "") for content in item.get("content", []) if isinstance(content, dict))
                current["items"].append(
                    {
                        "id": f"msg_fake_{self.__class__.next_item:04d}",
                        "type": "agentMessage",
                        "phase": item.get("phase"),
                        "text": text,
                    }
                )
                self.__class__.next_item += 1
                continue
            if item.get("type") == "reasoning":
                current["items"].append(
                    {
                        "id": f"reason_fake_{self.__class__.next_item:04d}",
                        "type": "reasoning",
                        "summary": [summary.get("text", "") for summary in item.get("summary", [])],
                        "content": [content.get("text", "") for content in item.get("content", [])],
                    }
                )
                self.__class__.next_item += 1
                continue
            current["items"].append({"id": f"other_fake_{self.__class__.next_item:04d}", "type": "contextCompaction"})
            self.__class__.next_item += 1
        return turns

    def _history_preview(self, history: list[dict[str, Any]]) -> str:
        for item in history:
            if item.get("type") == "message" and item.get("role") == "user":
                text = "\n".join(content.get("text", "") for content in item.get("content", []) if isinstance(content, dict)).strip()
                if text:
                    return text
        return ""


def test_fake_codex_harness_covers_deny_round_trip() -> None:
    temp_root = make_temp_root()
    data_dir = temp_root / "data"
    env = {
        "CODEX_UI_DATA_DIR": str(data_dir),
        "CODEX_UI_APPROVAL_POLICY": "untrusted",
        "CODEX_UI_OPEN_BROWSER": "0",
        "CODEX_UI_WORKSPACE_DIR": str(temp_root),
    }
    FakeRpcHarness.next_thread = 1
    FakeRpcHarness.next_turn = 1
    FakeRpcHarness.next_item = 1
    FakeRpcHarness.next_request = 0
    FakeRpcHarness.threads = {}
    try:
        with patched_env(env):
            with patch("backend.app.codex_manager.CodexManager.verify_codex_installation", new=_noop), patch(
                "backend.app.codex_manager.CodexManager.ensure_schema", new=_noop
            ), patch("backend.app.codex_manager.CodexRpcClient.start", new=FakeRpcHarness.start):
                app = create_app()
                token = data_dir.joinpath("session_token.txt").read_text(encoding="utf-8").strip()
                headers = {"Authorization": f"Bearer {token}"}
                with TestClient(app) as client:
                    thread = client.post("/api/threads", headers=headers, json={"title": "Harness thread"}).json()["thread"]
                    turn = client.post(
                        f"/api/threads/{thread['threadId']}/turns",
                        headers=headers,
                        json={"text": "Please require approval for this fake file change."},
                    ).json()["turn"]

                    def pending_approval():
                        payload = client.get("/api/bootstrap", headers=headers).json()
                        approvals = [item for item in payload["snapshot"]["pendingApprovals"] if item["threadId"] == thread["threadId"]]
                        return approvals[0] if approvals else None

                    approval = wait_for(pending_approval)
                    assert approval is not None
                    assert approval["requestMethod"] == "item/fileChange/requestApproval"

                    response = client.post(
                        f"/api/approvals/{approval['approvalId']}",
                        headers=headers,
                        json={"decision": "deny"},
                    )
                    assert response.status_code == 200
                    assert response.json()["status"] == "submitted"

                    def completed_turn():
                        payload = client.get(f"/api/threads/{thread['threadId']}", headers=headers).json()
                        turns = [item for item in payload["turns"] if item["turnId"] == turn["turnId"] and item["status"] == "completed"]
                        return turns[0] if turns else None

                    final_turn = wait_for(completed_turn)
                    assert final_turn is not None
                    assert final_turn["status"] == "completed"

                    bootstrap = client.get("/api/bootstrap", headers=headers).json()
                    pending = [item for item in bootstrap["snapshot"]["pendingApprovals"] if item["threadId"] == thread["threadId"]]
                    assert pending == []
                    historical = [
                        item
                        for item in bootstrap["snapshot"]["approvals"]
                        if item["threadId"] == thread["threadId"] and item["turnId"] == turn["turnId"]
                    ]
                    assert historical
                    assert historical[-1]["status"] == "deny"

                    events = client.get(f"/api/threads/{thread['threadId']}/events", headers=headers).json()["events"]
                    agent_messages = [
                        event["payload"]["item"]["text"]
                        for event in events
                        if event["type"] == "item/completed" and event["payload"].get("item", {}).get("type") == "agentMessage"
                    ]
                    assert any("Denied fake file change" in message for message in agent_messages)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_branch_from_turn_and_delete_conversation() -> None:
    temp_root = make_temp_root()
    data_dir = temp_root / "data"
    env = {
        "CODEX_UI_DATA_DIR": str(data_dir),
        "CODEX_UI_APPROVAL_POLICY": "on-request",
        "CODEX_UI_OPEN_BROWSER": "0",
        "CODEX_UI_WORKSPACE_DIR": str(temp_root),
    }
    FakeRpcHarness.next_thread = 1
    FakeRpcHarness.next_turn = 1
    FakeRpcHarness.next_item = 1
    FakeRpcHarness.next_request = 0
    FakeRpcHarness.threads = {}
    try:
        with patched_env(env):
            with patch("backend.app.codex_manager.CodexManager.verify_codex_installation", new=_noop), patch(
                "backend.app.codex_manager.CodexManager.ensure_schema", new=_noop
            ), patch("backend.app.codex_manager.CodexRpcClient.start", new=FakeRpcHarness.start):
                app = create_app()
                token = data_dir.joinpath("session_token.txt").read_text(encoding="utf-8").strip()
                headers = {"Authorization": f"Bearer {token}"}
                with TestClient(app) as client:
                    thread = client.post("/api/threads", headers=headers, json={"title": "Root"}).json()["thread"]
                    first_turn = client.post(
                        f"/api/threads/{thread['threadId']}/turns",
                        headers=headers,
                        json={"text": "Please require approval for this fake file change."},
                    ).json()["turn"]

                    approval = wait_for(
                        lambda: next(
                            (
                                item
                                for item in client.get("/api/bootstrap", headers=headers).json()["snapshot"]["pendingApprovals"]
                                if item["threadId"] == thread["threadId"]
                            ),
                            None,
                        )
                    )
                    assert approval is not None
                    assert client.post(
                        f"/api/approvals/{approval['approvalId']}",
                        headers=headers,
                        json={"decision": "approve"},
                    ).status_code == 200

                    wait_for(
                        lambda: next(
                            (
                                item
                                for item in client.get(f"/api/threads/{thread['threadId']}", headers=headers).json()["turns"]
                                if item["turnId"] == first_turn["turnId"] and item["status"] == "completed"
                            ),
                            None,
                        )
                    )

                    second_turn = client.post(
                        f"/api/threads/{thread['threadId']}/turns",
                        headers=headers,
                        json={"text": "Please require approval for this fake file change again."},
                    ).json()["turn"]
                    approval = wait_for(
                        lambda: next(
                            (
                                item
                                for item in client.get("/api/bootstrap", headers=headers).json()["snapshot"]["pendingApprovals"]
                                if item["threadId"] == thread["threadId"]
                            ),
                            None,
                        )
                    )
                    assert approval is not None
                    assert client.post(
                        f"/api/approvals/{approval['approvalId']}",
                        headers=headers,
                        json={"decision": "deny"},
                    ).status_code == 200

                    wait_for(
                        lambda: next(
                            (
                                item
                                for item in client.get(f"/api/threads/{thread['threadId']}", headers=headers).json()["turns"]
                                if item["turnId"] == second_turn["turnId"] and item["status"] == "completed"
                            ),
                            None,
                        )
                    )

                    branch = client.post(
                        f"/api/threads/{thread['threadId']}/branch",
                        headers=headers,
                        json={"turnId": first_turn["turnId"], "title": "Branch from first"},
                    )
                    assert branch.status_code == 200
                    branch_payload = branch.json()
                    assert branch_payload["thread"]["parentThreadId"] == thread["threadId"]
                    assert branch_payload["thread"]["forkedFromTurnId"] == first_turn["turnId"]
                    assert [item["idx"] for item in branch_payload["turns"]] == [1]
                    assert branch_payload["turns"][0]["userText"] == "Please require approval for this fake file change."

                    deleted = client.delete(f"/api/conversations/{thread['threadId']}", headers=headers)
                    assert deleted.status_code == 200
                    deleted_ids = deleted.json()["deletedThreadIds"]
                    assert thread["threadId"] in deleted_ids
                    assert branch_payload["thread"]["threadId"] in deleted_ids

                    bootstrap = client.get("/api/bootstrap", headers=headers).json()
                    assert bootstrap["snapshot"]["threads"] == []
                    assert bootstrap["snapshot"]["turns"] == []
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
