from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any


VERSION = "0.106.0"
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SOURCE = ROOT / "backend" / "tests" / "fixtures" / "schema"


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print(f"codex-cli {VERSION}")
        return 0
    if argv[:3] == ["app-server", "generate-json-schema", "--out"] and len(argv) == 4:
        out_dir = Path(argv[3])
        _copy_schema(out_dir)
        return 0
    if argv == ["app-server"]:
        FakeAppServer().run()
        return 0
    print(f"Unsupported fake codex invocation: {argv}", file=sys.stderr)
    return 2


def _copy_schema(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in SCHEMA_SOURCE.iterdir():
        dest = out_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)


class FakeAppServer:
    def __init__(self) -> None:
        now = int(time.time())
        self._next_thread = 1
        self._next_turn = 1
        self._next_item = 1
        self._next_server_request = 0
        self.threads: dict[str, dict[str, Any]] = {}
        self.pending_approvals: dict[int, dict[str, Any]] = {}
        self.started_at = now

    def run(self) -> None:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "method" in msg:
                self._handle_request(msg)
                continue
            self._handle_response(msg)

    def _handle_request(self, msg: dict[str, Any]) -> None:
        method = msg["method"]
        params = msg.get("params", {})
        request_id = msg.get("id")
        if method == "initialize":
            self._send_response(request_id, {"serverInfo": {"name": "fake-codex", "version": VERSION}})
            return
        if method == "initialized":
            return
        if method == "thread/start":
            thread = self._new_thread()
            self._send_response(request_id, {"thread": self._thread_payload(thread)})
            return
        if method == "thread/resume":
            thread = self.threads[params["threadId"]]
            self._send_response(request_id, {"thread": self._thread_payload(thread)})
            return
        if method == "thread/fork":
            parent = self.threads[params["threadId"]]
            child = self._fork_thread(parent)
            self._send_response(request_id, {"thread": self._thread_payload(child)})
            return
        if method == "thread/list":
            self._send_response(request_id, {"threads": [self._thread_payload(thread) for thread in self.threads.values()]})
            return
        if method == "thread/read":
            thread = self.threads[params["threadId"]]
            self._send_response(request_id, {"thread": self._thread_payload(thread)})
            return
        if method == "turn/start":
            self._start_turn(request_id, params)
            return
        self._send_error(request_id, -32601, f"Unsupported method: {method}")

    def _handle_response(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("id")
        if request_id not in self.pending_approvals:
            return
        pending = self.pending_approvals.pop(int(request_id))
        decision = str(msg.get("result", {}).get("decision", "decline"))
        if decision == "accept":
            self._notify(
                "item/completed",
                {
                    "threadId": pending["threadId"],
                    "turnId": pending["turnId"],
                    "item": {
                        "id": pending["itemId"],
                        "type": "fileChange",
                        "status": "completed",
                        "changes": pending["changes"],
                    },
                },
            )
            self._complete_turn(
                pending["threadId"],
                pending["turnId"],
                "Approved fake file change and completed the turn.",
            )
            return
        self._notify(
            "item/completed",
            {
                "threadId": pending["threadId"],
                "turnId": pending["turnId"],
                "item": {
                    "id": pending["itemId"],
                    "type": "fileChange",
                    "status": "denied",
                    "changes": pending["changes"],
                },
            },
        )
        self._complete_turn(
            pending["threadId"],
            pending["turnId"],
            "Denied fake file change and completed the turn without writing.",
        )

    def _start_turn(self, request_id: Any, params: dict[str, Any]) -> None:
        thread = self.threads[params["threadId"]]
        turn_id = f"fake-turn-{self._next_turn:04d}"
        self._next_turn += 1
        user_text = self._extract_user_text(params.get("input", []))
        turn = {
            "id": turn_id,
            "status": "inProgress",
            "items": [
                {
                    "id": "item-1",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": user_text, "text_elements": []}],
                }
            ],
            "userText": user_text,
        }
        thread["turns"].append(turn)
        thread["updatedAt"] = int(time.time())
        self._send_response(request_id, {"turn": {"id": turn_id, "status": "inProgress"}})
        self._notify("thread/status/changed", {"threadId": thread["id"], "status": {"type": "active"}})
        self._notify("turn/started", {"threadId": thread["id"], "turnId": turn_id, "turn": {"id": turn_id, "status": "inProgress"}})
        if "approval" in user_text.lower():
            self._request_file_change_approval(thread["id"], turn_id)
            return
        self._complete_turn(thread["id"], turn_id, f"Fake Codex handled: {user_text}")

    def _request_file_change_approval(self, thread_id: str, turn_id: str) -> None:
        item_id = f"call_fake_{self._next_item:04d}"
        self._next_item += 1
        changes = [
            {
                "path": str(ROOT / "fake_approval_output.txt"),
                "kind": {"type": "add"},
                "diff": "FAKE_APPROVAL_CONTENT\n",
            }
        ]
        self._notify(
            "item/started",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {"id": item_id, "type": "fileChange", "status": "inProgress", "changes": changes},
            },
        )
        self._notify(
            "thread/status/changed",
            {
                "threadId": thread_id,
                "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            },
        )
        server_request_id = self._next_server_request
        self._next_server_request += 1
        self.pending_approvals[server_request_id] = {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": item_id,
            "changes": changes,
        }
        self._send(
            {
                "jsonrpc": "2.0",
                "id": server_request_id,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "reason": "Fake approval gate"},
            }
        )

    def _complete_turn(self, thread_id: str, turn_id: str, message: str) -> None:
        thread = self.threads[thread_id]
        turn = next(item for item in thread["turns"] if item["id"] == turn_id)
        item_id = f"msg_fake_{self._next_item:04d}"
        self._next_item += 1
        self._notify("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id, "itemId": item_id, "delta": message})
        self._notify(
            "item/completed",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {"id": item_id, "type": "agentMessage", "phase": "final_answer", "text": message},
            },
        )
        turn["status"] = "completed"
        thread["updatedAt"] = int(time.time())
        self._notify("thread/status/changed", {"threadId": thread_id, "status": {"type": "idle"}})
        self._notify("turn/completed", {"threadId": thread_id, "turnId": turn_id, "turn": {"id": turn_id, "status": "completed", "items": []}})

    def _new_thread(self) -> dict[str, Any]:
        thread_id = f"fake-thread-{self._next_thread:04d}"
        self._next_thread += 1
        now = int(time.time())
        thread = {
            "id": thread_id,
            "name": f"Fake thread {thread_id}",
            "preview": "",
            "cwd": str(ROOT),
            "path": str(ROOT / ".tmp" / f"{thread_id}.jsonl"),
            "cliVersion": VERSION,
            "modelProvider": "fake-openai",
            "source": "fake",
            "status": {"type": "idle"},
            "createdAt": now,
            "updatedAt": now,
            "turns": [],
        }
        self.threads[thread_id] = thread
        return thread

    def _fork_thread(self, parent: dict[str, Any]) -> dict[str, Any]:
        child = self._new_thread()
        child["turns"] = json.loads(json.dumps(parent["turns"]))
        child["preview"] = parent["preview"]
        child["updatedAt"] = int(time.time())
        return child

    def _thread_payload(self, thread: dict[str, Any]) -> dict[str, Any]:
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
            "turns": [
                {
                    "id": turn["id"],
                    "status": turn["status"],
                    "items": turn["items"],
                }
                for turn in thread["turns"]
            ],
        }

    def _extract_user_text(self, items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in items:
            if item.get("type") != "text":
                continue
            parts.append(str(item.get("text", "")))
        return "".join(parts).strip()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send_response(self, request_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _send(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
