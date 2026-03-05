from __future__ import annotations

from backend.app.thread_params_service import ThreadParamsService


def test_thread_start_params_defaults() -> None:
    service = ThreadParamsService(
        workspace_dir="C:/repo",
        approval_policy="on-request",
        service_name="CodexWrapper",
    )

    params = service.thread_start_params()

    assert params == {
        "cwd": "C:/repo",
        "approvalPolicy": "on-request",
        "ephemeral": False,
        "experimentalRawEvents": False,
        "persistExtendedHistory": True,
        "serviceName": "CodexWrapper",
    }


def test_thread_resume_params_with_history() -> None:
    service = ThreadParamsService(
        workspace_dir="C:/repo",
        approval_policy="never",
        service_name="CodexWrapper",
    )

    params = service.thread_resume_params("thread-1", history=[{"type": "message", "role": "user", "content": []}])

    assert params["threadId"] == "thread-1"
    assert params["cwd"] == "C:/repo"
    assert params["approvalPolicy"] == "never"
    assert params["persistExtendedHistory"] is True
    assert params["history"] == [{"type": "message", "role": "user", "content": []}]
