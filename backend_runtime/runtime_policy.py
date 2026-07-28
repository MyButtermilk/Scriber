"""Fail-closed product policy for the packaged CPython runtime."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from backend_runtime.contract import (
    PYTHON_RUNTIME_LOCK_SHA256_ENV,
    PYTHON_RUNTIME_POLICY_ENV,
    PYTHON_RUNTIME_POLICY_MANIFEST_NAME,
    PYTHON_RUNTIME_POLICY_NAME,
    PYTHON_RUNTIME_POLICY_SCHEMA_VERSION,
)

TARGET_PYTHON_VERSION = "3.14.6"
TARGET_CACHE_TAG = "cpython-314"
RUNTIME_FLAVORS = frozenset({"Official", "ClangPgo", "ClangPgoTail"})
RUNTIME_VARIANTS = {
    ("Official", False): "O0",
    ("Official", True): "O1",
    ("ClangPgo", False): "C0",
    ("ClangPgo", True): "C1",
    ("ClangPgoTail", False): "T0",
    ("ClangPgoTail", True): "T1",
}
JIT_ENV = "PYTHON_JIT"
INCOMPATIBLE_RUNTIME_ENV = ("PYTHON_GIL", "PYTHON_TLBC")


class RuntimePolicyError(RuntimeError):
    """Raised when the packaged Python runtime differs from its locked policy."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimePolicyError("Python runtime policy is missing or invalid.") from exc
    if not isinstance(value, dict):
        raise RuntimePolicyError("Python runtime policy must be a JSON object.")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimePolicyError(f"{label} must be a boolean.")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimePolicyError(f"{label} is invalid.")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RuntimePolicyError(f"{label} is invalid.") from exc
    return value


def _jit_state() -> tuple[bool, bool]:
    jit = getattr(sys, "_jit", None)
    if jit is None:
        return False, False
    try:
        return bool(jit.is_available()), bool(jit.is_enabled())
    except Exception as exc:
        raise RuntimePolicyError("CPython JIT state could not be queried.") from exc


def validate_runtime_policy_payload(
    payload: dict[str, Any],
    runtime_root: Path,
    *,
    python_version: str | None = None,
    cache_tag: str | None = None,
    compiler: str | None = None,
    jit_available: bool | None = None,
    jit_active: bool | None = None,
    environ: dict[str, str] | os._Environ[str] | None = None,
    require_supervisor_environment: bool = False,
) -> dict[str, Any]:
    """Validate policy shape, runtime bytes, and the current interpreter state."""

    root = runtime_root.resolve()
    if payload.get("schemaVersion") != PYTHON_RUNTIME_POLICY_SCHEMA_VERSION:
        raise RuntimePolicyError("Python runtime policy schema is unsupported.")
    if payload.get("name") != PYTHON_RUNTIME_POLICY_NAME:
        raise RuntimePolicyError("Python runtime policy name is unsupported.")

    flavor = payload.get("runtimeFlavor")
    if flavor not in RUNTIME_FLAVORS:
        raise RuntimePolicyError("Python runtime flavor is unsupported.")
    tail_enabled = _bool(payload.get("tailCallInterpreter"), "Tail-call interpreter state")
    if tail_enabled != (flavor == "ClangPgoTail"):
        raise RuntimePolicyError("Python runtime flavor and tail-call state disagree.")

    expected_compiler = payload.get("compiler")
    if not isinstance(expected_compiler, str) or not expected_compiler.strip():
        raise RuntimePolicyError("Python compiler identity is missing.")
    actual_compiler = compiler if compiler is not None else platform.python_compiler()
    if expected_compiler != actual_compiler:
        raise RuntimePolicyError("Python compiler identity does not match its policy.")
    compiler_lower = actual_compiler.lower()
    if flavor == "Official" and "msc" not in compiler_lower:
        raise RuntimePolicyError("Official Python runtime is not an MSVC build.")
    if flavor != "Official" and "clang" not in compiler_lower:
        raise RuntimePolicyError("Custom Python runtime is not a Clang build.")

    python = payload.get("python")
    if not isinstance(python, dict):
        raise RuntimePolicyError("Python runtime identity is missing.")
    actual_version = python_version if python_version is not None else platform.python_version()
    actual_cache_tag = cache_tag if cache_tag is not None else sys.implementation.cache_tag
    if (
        python.get("version") != TARGET_PYTHON_VERSION
        or python.get("cacheTag") != TARGET_CACHE_TAG
        or actual_version != TARGET_PYTHON_VERSION
        or actual_cache_tag != TARGET_CACHE_TAG
    ):
        raise RuntimePolicyError("Packaged Python must be exactly CPython 3.14.6.")

    dll = python.get("dll")
    if not isinstance(dll, dict):
        raise RuntimePolicyError("Python DLL identity is missing.")
    if dll.get("path") != "_internal/python314.dll":
        raise RuntimePolicyError("Python DLL path is unsupported.")
    dll_path = root / "_internal" / "python314.dll"
    if not dll_path.is_file():
        raise RuntimePolicyError("python314.dll is missing from the packaged runtime.")
    expected_length = dll.get("length")
    if not isinstance(expected_length, int) or expected_length <= 0 or dll_path.stat().st_size != expected_length:
        raise RuntimePolicyError("python314.dll length does not match its policy.")
    if _sha(dll.get("sha256"), "Python DLL checksum") != _sha256(dll_path):
        raise RuntimePolicyError("python314.dll checksum does not match its policy.")

    runtime_lock_sha = _sha(payload.get("runtimeLockSha256"), "Runtime lock checksum")
    jit = payload.get("jit")
    if not isinstance(jit, dict):
        raise RuntimePolicyError("Python JIT policy is missing.")
    available = _bool(jit.get("available"), "JIT availability")
    expected = _bool(jit.get("expected"), "Expected JIT state")
    recorded_active = _bool(jit.get("active"), "Recorded JIT state")
    actual_available, actual_active = _jit_state()
    if jit_available is not None:
        actual_available = jit_available
    if jit_active is not None:
        actual_active = jit_active
    if available != actual_available:
        raise RuntimePolicyError("CPython JIT availability does not match its policy.")
    if expected and not available:
        raise RuntimePolicyError("The selected runtime requires an unavailable JIT.")
    if recorded_active != expected or actual_active != expected:
        raise RuntimePolicyError("CPython JIT state does not match its policy.")
    if payload.get("variantId") != RUNTIME_VARIANTS[(flavor, expected)]:
        raise RuntimePolicyError("Python runtime variant does not match its policy.")

    env = os.environ if environ is None else environ
    if require_supervisor_environment:
        if env.get(JIT_ENV) != ("1" if expected else "0"):
            raise RuntimePolicyError("The supervisor did not set PYTHON_JIT explicitly.")
        for name in INCOMPATIBLE_RUNTIME_ENV:
            if name in env:
                raise RuntimePolicyError(f"The supervisor did not remove inherited {name}.")
        if env.get(PYTHON_RUNTIME_LOCK_SHA256_ENV) != runtime_lock_sha:
            raise RuntimePolicyError("The supervisor runtime-lock identity does not match.")

    return payload


def validate_packaged_runtime_policy(
    runtime_root: Path,
    *,
    require_supervisor_environment: bool = False,
) -> dict[str, Any]:
    root = runtime_root.resolve()
    expected_path = (root / PYTHON_RUNTIME_POLICY_MANIFEST_NAME).resolve()
    configured_path = os.environ.get(PYTHON_RUNTIME_POLICY_ENV, "").strip()
    if configured_path and Path(configured_path).resolve() != expected_path:
        raise RuntimePolicyError("Python runtime policy path is not the packaged manifest.")
    return validate_runtime_policy_payload(
        _load_object(expected_path),
        root,
        require_supervisor_environment=require_supervisor_environment,
    )


def enforce_runtime_policy_if_configured() -> None:
    """Validate source launches only when a supervisor supplied a policy."""

    configured_path = os.environ.get(PYTHON_RUNTIME_POLICY_ENV, "").strip()
    if not configured_path:
        return
    path = Path(configured_path).resolve()
    validate_packaged_runtime_policy(
        path.parent,
        require_supervisor_environment=os.getenv("SCRIBER_RUNTIME_MODE") == "tauri-supervised",
    )
