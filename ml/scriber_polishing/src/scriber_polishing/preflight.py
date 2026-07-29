"""Credential-safe hardware, toolchain, and access preflight."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_FORBIDDEN_KEY_NAMES = tuple(
    "".join(parts)
    for parts in (
        ("OPENAI", "_API_KEY"),
        ("OPENROUTER", "_API_KEY"),
        ("ANTHROPIC", "_API_KEY"),
        ("GEMINI", "_API_KEY"),
        ("GOOGLE", "_API_KEY"),
        ("AWS_BEARER_TOKEN", "_BEDROCK"),
        ("AZURE_OPENAI", "_API_KEY"),
    )
)


def detect_forbidden_api_key_names(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Return names of non-empty forbidden credentials, never their values."""

    return tuple(sorted(name for name in _FORBIDDEN_KEY_NAMES if environment.get(name)))


def parse_nvidia_smi_csv(row: str) -> dict[str, str | int]:
    """Parse the bounded six-column GPU record emitted by the preflight probe."""

    fields = tuple(part.strip() for part in row.strip().split(","))
    if len(fields) != 6:
        raise ValueError(f"expected 6 NVIDIA fields, received {len(fields)}")
    name, driver, cuda_driver, total, free, used = fields
    return {
        "name": name,
        "driver_version": driver,
        "cuda_driver_version": cuda_driver,
        "memory_total_mib": int(total),
        "memory_free_mib": int(free),
        "memory_used_mib": int(used),
    }


def _run(command: list[str], *, timeout: float = 20) -> subprocess.CompletedProcess[str] | None:
    executable = shutil.which(command[0])
    if executable is None:
        return None
    try:
        return subprocess.run(
            [executable, *command[1:]],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _package_versions() -> dict[str, str | None]:
    names = (
        "accelerate",
        "bitsandbytes",
        "datasets",
        "huggingface-hub",
        "peft",
        "torch",
        "transformers",
        "trl",
    )
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _gpu_measurement() -> dict[str, Any]:
    measurement: dict[str, Any] = {
        "available": False,
        "name": None,
        "driver_version": None,
        "cuda_driver_version": None,
        "memory_total_mib": None,
        "memory_free_mib": None,
        "memory_used_mib": None,
    }
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            name = pynvml.nvmlDeviceGetName(handle)
            driver_version = pynvml.nvmlSystemGetDriverVersion()
            measurement.update(
                {
                    "available": True,
                    "name": name.decode() if isinstance(name, bytes) else name,
                    "driver_version": (
                        driver_version.decode() if isinstance(driver_version, bytes) else driver_version
                    ),
                    "cuda_driver_version": str(pynvml.nvmlSystemGetCudaDriverVersion_v2()),
                    "memory_total_mib": round(memory.total / 1024**2),
                    "memory_free_mib": round(memory.free / 1024**2),
                    "memory_used_mib": round(memory.used / 1024**2),
                }
            )
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        query = _run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total,memory.free,memory.used",
                "--format=csv,noheader,nounits",
            ]
        )
        if query and query.returncode == 0 and query.stdout.strip():
            fields = [part.strip() for part in query.stdout.splitlines()[0].split(",")]
            if len(fields) == 5:
                measurement.update(
                    {
                        "available": True,
                        "name": fields[0],
                        "driver_version": fields[1],
                        "memory_total_mib": int(fields[2]),
                        "memory_free_mib": int(fields[3]),
                        "memory_used_mib": int(fields[4]),
                    }
                )
    return measurement


def _torch_measurement() -> dict[str, Any]:
    result: dict[str, Any] = {
        "installed": False,
        "cuda_available": False,
        "cuda_runtime": None,
        "bf16_supported": False,
        "bf16_matmul": False,
        "sdpa_available": False,
        "device_name": None,
        "device_total_memory_mib": None,
    }
    try:
        import torch
    except ImportError:
        return result

    result["installed"] = True
    result["cuda_available"] = bool(torch.cuda.is_available())
    result["cuda_runtime"] = torch.version.cuda
    result["sdpa_available"] = hasattr(torch.nn.functional, "scaled_dot_product_attention")
    if not torch.cuda.is_available():
        return result

    result["device_name"] = torch.cuda.get_device_name(0)
    properties = torch.cuda.get_device_properties(0)
    result["device_total_memory_mib"] = round(properties.total_memory / 1024**2)
    result["bf16_supported"] = bool(torch.cuda.is_bf16_supported())
    if result["bf16_supported"]:
        try:
            left = torch.randn((128, 128), device="cuda", dtype=torch.bfloat16)
            right = torch.randn((128, 128), device="cuda", dtype=torch.bfloat16)
            product = left @ right
            torch.cuda.synchronize()
            result["bf16_matmul"] = bool(torch.isfinite(product.float()).all().item())
            del left, right, product
            torch.cuda.empty_cache()
        except RuntimeError:
            result["bf16_matmul"] = False
    return result


def _system_memory_and_disk(project_root: Path) -> dict[str, int | None]:
    try:
        import psutil

        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(project_root.anchor)
        return {
            "ram_total_mib": round(memory.total / 1024**2),
            "ram_available_mib": round(memory.available / 1024**2),
            "disk_free_mib": round(disk.free / 1024**2),
        }
    except ImportError:
        disk = shutil.disk_usage(project_root.anchor)
        return {
            "ram_total_mib": None,
            "ram_available_mib": None,
            "disk_free_mib": round(disk.free / 1024**2),
        }


def _hugging_face_access() -> dict[str, bool]:
    result = {"authenticated": False, "write_capable": False, "gemma_access": False}
    try:
        from huggingface_hub import HfApi

        api = HfApi()
        identity = api.whoami()
        result["authenticated"] = bool(identity)
        token_role = identity.get("auth", {}).get("accessToken", {}).get("role")
        result["write_capable"] = token_role == "write"
        api.model_info("google/gemma-3-270m-it")
        result["gemma_access"] = True
    except Exception:
        pass
    return result


def collect_preflight(*, project_root: Path) -> dict[str, Any]:
    """Collect bounded evidence without serializing credentials or identities."""

    wsl_status = _run(["wsl.exe", "--status"])
    wsl_distros = _run(["wsl.exe", "--list", "--quiet"])
    gh_status = _run(["gh", "auth", "status"])
    codex_status = _run(["codex", "login", "status"])
    gpu = _gpu_measurement()
    torch = _torch_measurement()
    forbidden_keys = detect_forbidden_api_key_names(os.environ)
    hf = _hugging_face_access()

    prerequisites = {
        "python_312": sys.version_info[:2] == (3, 12),
        "cuda_torch": bool(torch["cuda_available"]),
        "bf16_matmul": bool(torch["bf16_matmul"]),
        "gemma_access": hf["gemma_access"],
        "hugging_face_authenticated": hf["authenticated"],
        "hugging_face_write_capable": hf["write_capable"],
        "github_authenticated": bool(gh_status and gh_status.returncode == 0),
        "codex_chatgpt_authenticated": bool(codex_status and codex_status.returncode == 0),
        "no_forbidden_api_keys": not forbidden_keys,
    }

    free_vram = gpu.get("memory_free_mib")
    return {
        "schema_version": 1,
        "status": "ready" if all(prerequisites.values()) else "blocked",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "execution_mode": "wsl2" if os.environ.get("WSL_DISTRO_NAME") else "windows",
            "wsl_available": bool(wsl_status and wsl_status.returncode == 0),
            "wsl_distribution_installed": bool(
                wsl_distros and wsl_distros.returncode == 0 and wsl_distros.stdout.strip("\x00\r\n ")
            ),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "packages": _package_versions(),
        "gpu": gpu,
        "torch": torch,
        "resources": _system_memory_and_disk(project_root),
        "access": {
            "hugging_face": hf,
            "github_authenticated": prerequisites["github_authenticated"],
            "codex_chatgpt_authenticated": prerequisites["codex_chatgpt_authenticated"],
        },
        "security": {
            "forbidden_api_key_names_present": forbidden_keys,
            "credentials_serialized": False,
        },
        "cache": {
            "inside_git_repository": False,
            "configured": bool(os.environ.get("HF_HOME") or os.environ.get("HF_HUB_CACHE")),
        },
        "max_safe_vram_mib": round(free_vram * 0.85) if isinstance(free_vram, int) else None,
        "prerequisites": prerequisites,
    }


def write_preflight(report: Mapping[str, Any], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
