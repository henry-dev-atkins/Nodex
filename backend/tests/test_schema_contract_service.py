from __future__ import annotations

from pathlib import Path
import shutil
import uuid

from backend.app.schema_contract_service import SchemaContractService


def make_temp_root() -> Path:
    root = Path.cwd() / ".tmp_test_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    temp_root = root / uuid.uuid4().hex
    temp_root.mkdir(parents=True, exist_ok=False)
    return temp_root


def write_schema_set(root: Path, *, include_server_request: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ClientRequest.json").write_text(
        "\n".join(
            [
                "initialize",
                "thread/start",
                "thread/resume",
                "thread/fork",
                "thread/list",
                "thread/read",
                "turn/start",
            ]
        ),
        encoding="utf-8",
    )
    (root / "ServerNotification.json").write_text(
        "\n".join(
            [
                "thread/started",
                "thread/status/changed",
                "turn/started",
                "turn/completed",
                "item/started",
                "item/completed",
                "item/agentMessage/delta",
            ]
        ),
        encoding="utf-8",
    )
    requests = ["item/commandExecution/requestApproval"]
    if include_server_request:
        requests.append("item/fileChange/requestApproval")
    (root / "ServerRequest.json").write_text("\n".join(requests), encoding="utf-8")


def test_verify_schema_files_accepts_required_contracts() -> None:
    temp_root = make_temp_root()
    try:
        schema_dir = temp_root / "schema"
        write_schema_set(schema_dir, include_server_request=True)
        service = SchemaContractService(schema_dir)
        service.verify_schema_files()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_verify_schema_files_rejects_missing_method() -> None:
    temp_root = make_temp_root()
    try:
        schema_dir = temp_root / "schema"
        write_schema_set(schema_dir, include_server_request=False)
        service = SchemaContractService(schema_dir)
        try:
            service.verify_schema_files()
            raise AssertionError("Expected schema verification to fail")
        except RuntimeError as exc:
            assert "Missing required server request" in str(exc)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
