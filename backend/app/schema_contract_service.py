from __future__ import annotations

from pathlib import Path


class SchemaContractService:
    def __init__(self, schema_cache_dir: Path) -> None:
        self._schema_cache_dir = schema_cache_dir

    def verify_schema_files(self) -> None:
        required_client_methods = {
            "initialize",
            "thread/start",
            "thread/resume",
            "thread/fork",
            "thread/list",
            "thread/read",
            "turn/start",
        }
        required_notifications = {
            "thread/started",
            "thread/status/changed",
            "turn/started",
            "turn/completed",
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
        }
        required_server_requests = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }
        client_schema = (self._schema_cache_dir / "ClientRequest.json").read_text(encoding="utf-8")
        notification_schema = (self._schema_cache_dir / "ServerNotification.json").read_text(encoding="utf-8")
        request_schema = (self._schema_cache_dir / "ServerRequest.json").read_text(encoding="utf-8")
        for method in required_client_methods:
            if method not in client_schema:
                raise RuntimeError(f"Missing required client method in schema: {method}")
        for method in required_notifications:
            if method not in notification_schema:
                raise RuntimeError(f"Missing required notification in schema: {method}")
        for method in required_server_requests:
            if method not in request_schema:
                raise RuntimeError(f"Missing required server request in schema: {method}")
