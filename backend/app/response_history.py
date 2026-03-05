from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .util import split_command


class ResponseHistoryProjector:
    def extract_user_text_from_items(self, items: list[dict[str, Any]]) -> str:
        for item in items:
            if item.get("type") != "userMessage":
                continue
            text_parts = [
                part.get("text", "")
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            text = "".join(text_parts).strip()
            if text:
                return text
        return ""

    def content_item_from_user_input(self, part: dict[str, Any]) -> dict[str, Any]:
        part_type = part.get("type")
        if part_type == "text":
            return {"type": "input_text", "text": part.get("text", "")}
        if part_type in {"image", "localImage"}:
            return {"type": "input_image", "image_url": part.get("url") or part.get("path") or ""}
        name = part.get("name") or part.get("path") or part.get("type") or "input"
        return {"type": "input_text", "text": str(name)}

    def local_shell_status(self, status: Any) -> str:
        if status in {"completed", "success"}:
            return "completed"
        if status in {"inProgress", "running"}:
            return "in_progress"
        return "incomplete"

    def sanitize_local_shell_action(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_action = item.get("action") if isinstance(item.get("action"), dict) else item
        command_value = raw_action.get("command")
        if isinstance(command_value, list):
            command = [str(part) for part in command_value if part is not None]
        else:
            command = split_command(str(command_value or item.get("command", "")))
        action: dict[str, Any] = {
            "type": "exec",
            "command": command,
        }
        working_directory = raw_action.get("cwd") or raw_action.get("working_directory") or item.get("cwd") or item.get("working_directory")
        if working_directory is not None:
            action["working_directory"] = str(working_directory)
        timeout_ms = raw_action.get("timeout_ms", item.get("timeout_ms"))
        if isinstance(timeout_ms, int) and timeout_ms >= 0:
            action["timeout_ms"] = timeout_ms
        user = raw_action.get("user", item.get("user"))
        if user is not None:
            action["user"] = str(user)
        env = raw_action.get("env", item.get("env"))
        if isinstance(env, dict):
            sanitized_env = {
                str(key): str(value)
                for key, value in env.items()
                if key is not None and value is not None
            }
            if sanitized_env:
                action["env"] = sanitized_env
        return action

    def sanitize_message_history_item(
        self,
        role: str,
        content: list[dict[str, Any]],
        phase: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": "message",
            "role": role,
            "content": content,
        }
        if phase is not None:
            item["phase"] = phase
        return item

    def sanitize_reasoning_history_item(self, item: dict[str, Any]) -> dict[str, Any]:
        history_item: dict[str, Any] = {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text} for text in item.get("summary", [])],
        }
        content = [{"type": "reasoning_text", "text": text} for text in item.get("content", [])]
        if content:
            history_item["content"] = content
        encrypted_content = item.get("encrypted_content")
        if encrypted_content is not None:
            history_item["encrypted_content"] = encrypted_content
        return history_item

    def sanitize_web_search_action(self, item: dict[str, Any]) -> dict[str, Any]:
        raw_action = item.get("action")
        if isinstance(raw_action, dict):
            return {
                str(key): value
                for key, value in raw_action.items()
                if key is not None and value is not None
            }
        action = {"type": "search"}
        query = item.get("query")
        if query is not None:
            action["query"] = str(query)
        return action

    def response_items_from_thread_items(
        self,
        items: list[dict[str, Any]],
        include_tool_calls: bool = True,
    ) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        for item in items:
            item_type = item.get("type")
            if item_type == "message":
                history.append(
                    self.sanitize_message_history_item(
                        str(item.get("role", "assistant")),
                        [part for part in item.get("content", []) if isinstance(part, dict)],
                        phase=item.get("phase"),
                    )
                )
                continue
            if item_type == "userMessage":
                history.append(
                    self.sanitize_message_history_item(
                        "user",
                        [self.content_item_from_user_input(part) for part in item.get("content", [])],
                    )
                )
                continue
            if item_type == "agentMessage":
                history.append(
                    self.sanitize_message_history_item(
                        "assistant",
                        [{"type": "output_text", "text": item.get("text", "")}],
                        phase=item.get("phase"),
                    )
                )
                continue
            if item_type == "plan":
                history.append(
                    self.sanitize_message_history_item(
                        "assistant",
                        [{"type": "output_text", "text": item.get("text", "")}],
                        phase="commentary",
                    )
                )
                continue
            if item_type == "reasoning":
                history.append(self.sanitize_reasoning_history_item(item))
                continue
            if item_type in {"commandExecution", "local_shell_call", "localShellCall"}:
                if not include_tool_calls:
                    continue
                history.append(
                    {
                        "type": "local_shell_call",
                        "call_id": item.get("id") or item.get("call_id"),
                        "status": self.local_shell_status(item.get("status")),
                        "action": self.sanitize_local_shell_action(item),
                    }
                )
                continue
            if item_type in {"webSearch", "web_search_call"}:
                if not include_tool_calls:
                    continue
                history.append(
                    {
                        "type": "web_search_call",
                        "status": self.local_shell_status(item.get("status")) if item_type == "web_search_call" else "completed",
                        "action": self.sanitize_web_search_action(item),
                    }
                )
                continue
        return history

    def build_response_history(
        self,
        codex_thread: dict[str, Any],
        turn_id: str,
        include_tool_calls: bool = False,
    ) -> list[dict[str, Any]]:
        history: list[dict[str, Any]] = []
        found = False
        for turn in codex_thread.get("turns", []):
            history.extend(
                self.response_items_from_thread_items(
                    turn.get("items", []),
                    include_tool_calls=include_tool_calls,
                )
            )
            if turn.get("id") == turn_id:
                found = True
                break
        if not found:
            raise HTTPException(
                status_code=404,
                detail={"error": {"code": "turn_not_found", "message": f"Unknown turn: {turn_id}", "details": {}}},
            )
        return history
