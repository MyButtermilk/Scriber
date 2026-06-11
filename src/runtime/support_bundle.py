from __future__ import annotations

import json
import os
import platform
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.runtime.log_clear_state import clear_offset_for_path, load_clear_offsets
from src.runtime.paths import data_dir, logs_dir, repo_root, settings_path, support_bundles_dir
from src.version import app_version


_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|credential|authorization|cookie|session)",
    re.IGNORECASE,
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("?[A-Za-z0-9_-]*(?:apiKey|api_key|token|secret|password|credential|authorization|cookie|session)'
    r'[A-Za-z0-9_-]*"?\s*:\s*)"[^"]*"'
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_SECRET_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{8,})")
_SHELL_IPC_PIPE_RE = re.compile(
    r"(?:\\\\){1,2}\.(?:\\){1,2}pipe(?:\\){1,2}scriber-shell-[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)
_NATIVE_AUDIO_ENDPOINT_RE = re.compile(
    r"SWD(?:\\+|#)+MMDEVAPI(?:\\+|#)+[^\s\"',;<>]+",
    re.IGNORECASE,
)
_MAX_LOG_BYTES = 750_000


def is_sensitive_key(key: str) -> bool:
    key_str = str(key)
    if key_str.casefold() in {"endpointid", "prewarmid", "prewarm_id"}:
        return True
    return bool(_SENSITIVE_KEY_RE.search(key_str))


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in mapping.items():
        key_str = str(key)
        if is_sensitive_key(key_str):
            redacted[key_str] = "[REDACTED]"
        else:
            redacted[key_str] = redact_value(value)
    return redacted


def redact_text(text: str) -> str:
    redacted = str(text).replace("\x00", "")
    redacted = _JSON_SECRET_RE.sub(r'\1"[REDACTED]"', redacted)
    redacted = _ASSIGNMENT_SECRET_RE.sub(r"\1=[REDACTED]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _OPENAI_STYLE_SECRET_RE.sub("[REDACTED]", redacted)
    redacted = _SHELL_IPC_PIPE_RE.sub("[REDACTED_PIPE]", redacted)
    redacted = _NATIVE_AUDIO_ENDPOINT_RE.sub("[REDACTED_ENDPOINT_ID]", redacted)
    return redacted


def _read_tail(path: Path, *, max_bytes: int = _MAX_LOG_BYTES, start_offset: int = 0) -> str:
    size = path.stat().st_size
    start_offset = max(0, min(start_offset, size))
    readable_size = size - start_offset
    with path.open("rb") as handle:
        if readable_size > max_bytes:
            handle.seek(size - max_bytes)
            data = handle.read(max_bytes)
            data = b"[truncated to last bytes]\n" + data
        else:
            if start_offset:
                handle.seek(start_offset)
            data = handle.read(readable_size)
    return redact_text(data.decode("utf-8", errors="replace"))


def _write_json(zf: zipfile.ZipFile, name: str, value: dict[str, Any]) -> None:
    zf.writestr(name, json.dumps(redact_mapping(value), indent=2, sort_keys=True))


def _include_text_file(zf: zipfile.ZipFile, source: Path, archive_name: str) -> None:
    if source.is_file():
        zf.writestr(archive_name, _read_tail(source))


def _redacted_environment() -> dict[str, Any]:
    relevant_prefixes = (
        "SCRIBER_",
        "OPENAI_",
        "SONIOX_",
        "ASSEMBLYAI_",
        "DEEPGRAM_",
        "AZURE_",
        "GOOGLE_",
        "GROQ_",
    )
    selected = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(relevant_prefixes) or is_sensitive_key(key)
    }
    return redact_mapping(dict(sorted(selected.items())))


def _safe_state(state: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "listening",
        "status",
        "inputWarning",
        "inputWarningCode",
        "backgroundProcessing",
        "recordingState",
        "transcribing",
        "sessionId",
    }
    return {key: state.get(key) for key in sorted(allowed) if key in state}


def _write_runtime_files(
    zf: zipfile.ZipFile,
    *,
    runtime_info: dict[str, Any],
    app_state: dict[str, Any],
    audio_diagnostics: dict[str, Any] | None = None,
) -> None:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _write_json(
        zf,
        "manifest.json",
        {
            "generatedAt": generated_at,
            "appVersion": app_version(),
            "apiVersion": runtime_info.get("apiVersion"),
            "runtimeMode": runtime_info.get("runtimeMode"),
            "launchKind": runtime_info.get("launchKind"),
            "pid": runtime_info.get("pid"),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        },
    )
    _write_json(zf, "runtime.json", runtime_info)
    _write_json(zf, "state.redacted.json", _safe_state(app_state))
    if audio_diagnostics is not None:
        _write_json(zf, "audio-diagnostics.redacted.json", audio_diagnostics)
    _write_json(zf, "environment.redacted.json", _redacted_environment())


def _write_config_files(zf: zipfile.ZipFile) -> None:
    current_settings_path = settings_path()
    if current_settings_path.is_file():
        try:
            settings_payload = json.loads(current_settings_path.read_text(encoding="utf-8"))
            if isinstance(settings_payload, dict):
                _write_json(zf, "config/settings.redacted.json", settings_payload)
            else:
                zf.writestr("config/settings.redacted.txt", redact_text(str(settings_payload)))
        except Exception:
            _include_text_file(zf, current_settings_path, "config/settings.redacted.txt")

    env_path = data_dir() / ".env"
    if not env_path.is_file() and (repo_root() / ".env").is_file():
        env_path = repo_root() / ".env"
    if env_path.is_file():
        _include_text_file(zf, env_path, "config/env.redacted.txt")


def _write_log_files(zf: zipfile.ZipFile) -> None:
    seen: set[Path] = set()
    archive_names: set[str] = set()
    candidates: list[Path] = []
    clear_offsets = load_clear_offsets()
    for directory in (logs_dir(), data_dir() / "logs", repo_root()):
        if not directory.exists():
            continue
        for pattern in ("*.log", "*.jsonl", "*crash*.json", "*crash*.jsonl"):
            candidates.extend(directory.glob(pattern))

    for path in sorted({candidate.resolve() for candidate in candidates if candidate.is_file()}):
        if path in seen:
            continue
        seen.add(path)
        try:
            archive_name = f"logs/{path.name}"
            if archive_name in archive_names:
                archive_name = f"logs/{path.parent.name}-{path.name}"
            suffix = 2
            while archive_name in archive_names:
                archive_name = f"logs/{path.parent.name}-{suffix}-{path.name}"
                suffix += 1
            archive_names.add(archive_name)
            zf.writestr(archive_name, _read_tail(path, start_offset=clear_offset_for_path(path, clear_offsets)))
        except OSError:
            continue


def create_support_bundle(
    *,
    runtime_info: dict[str, Any],
    app_state: dict[str, Any],
    audio_diagnostics: dict[str, Any] | None = None,
    output_dir: Path | None = None,
) -> Path:
    target_dir = output_dir or support_bundles_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle_path = target_dir / f"scriber-support-{stamp}-{os.getpid()}.zip"

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_runtime_files(
            zf,
            runtime_info=runtime_info,
            app_state=app_state,
            audio_diagnostics=audio_diagnostics,
        )
        _write_config_files(zf)
        _write_log_files(zf)

    return bundle_path
