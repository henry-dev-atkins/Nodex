from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


APP_NAME = "codex-ui-wrapper"
APP_VERSION = "0.1.0"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def split_command(command: str) -> list[str]:
    return shlex.split(command, posix=os.name != "nt")


def resolve_subprocess_command(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    program = argv[0]
    resolved = shutil.which(program) if not Path(program).exists() else program
    if not resolved:
        return argv
    resolved_args = [resolved, *argv[1:]]
    if os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
        comspec = os.environ.get("COMSPEC", r"C:\WINDOWS\System32\cmd.exe")
        return [comspec, "/d", "/c", resolved, *argv[1:]]
    return resolved_args


def parse_codex_version(raw: str) -> str | None:
    match = re.search(r"(\d+\.\d+\.\d+)", raw)
    return match.group(1) if match else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def open_browser(url: str) -> None:
    webbrowser.open(url, new=2)
