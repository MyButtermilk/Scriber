"""
Local NeMo-based Speech-to-Text using .nemo models from Hugging Face.
"""
from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

_model_cache: dict[str, Any] = {}
_download_lock = threading.Lock()
_download_state_lock = threading.Lock()
_download_state: dict[str, dict[str, Any]] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nemo_stt")

NEMO_MODELS: dict[str, dict[str, Any]] = {
    "parakeet-primeline": {
        "name": "Parakeet Primeline (DE)",
        "description": "German-focused Parakeet model (.nemo)",
        "languages": ["de"],
        "size_mb": 0,
        "supports_timestamps": False,
        "hf_repo": "primeline/parakeet-primeline",
        "hf_filename": "2_95_WER.nemo",
    }
}

DEFAULT_MODEL = "parakeet-primeline"


def _get_nemo():
    try:
        import torch  # noqa: F401
        import nemo.collections.asr as nemo_asr
    except Exception as exc:
        logger.error(f"NeMo toolkit not installed: {exc}")
        raise ImportError(
            "NeMo toolkit not installed. Install with: pip install nemo_toolkit[asr]"
        ) from exc
    return nemo_asr


def is_nemo_available() -> bool:
    try:
        _get_nemo()
        return True
    except Exception:
        return False


def get_model_cache_dir() -> Path:
    cache_env = os.getenv("SCRIBER_NEMO_CACHE", "") or os.getenv("SCRIBER_MODEL_CACHE", "")
    if cache_env:
        cache_dir = Path(cache_env).expanduser()
        os.environ["HF_HUB_CACHE"] = str(cache_dir)
    else:
        hf_hub_cache = os.getenv("HF_HUB_CACHE", "")
        if hf_hub_cache:
            cache_dir = Path(hf_hub_cache).expanduser()
        else:
            hf_home = os.getenv("HF_HOME", "")
            if hf_home:
                cache_dir = Path(hf_home).expanduser() / "hub"
            else:
                cache_dir = Path.home() / ".cache" / "huggingface" / "hub"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 ** 2:
        return f"{value / 1024:.1f} KB"
    if value < 1024 ** 3:
        return f"{value / (1024 ** 2):.1f} MB"
    return f"{value / (1024 ** 3):.1f} GB"


def _get_file_size(repo_id: str, filename: str) -> int:
    try:
        from huggingface_hub import HfApi
    except Exception:
        return 0
    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
    except Exception:
        return 0
    for sibling in info.siblings or []:
        if sibling.rfilename == filename:
            size = getattr(sibling, "size", None)
            if size is None and getattr(sibling, "lfs", None):
                size = getattr(sibling.lfs, "size", None)
            return int(size or 0)
    return 0


def _set_download_state(
    model_name: str,
    status: str,
    progress: Optional[float] = None,
    message: str = "",
) -> None:
    with _download_state_lock:
        _download_state[model_name] = {
            "status": status,
            "progress": progress,
            "message": message,
        }


def get_download_state(model_name: str) -> dict[str, Any]:
    with _download_state_lock:
        return dict(_download_state.get(model_name, {}))


def is_model_downloading(model_name: str) -> bool:
    state = get_download_state(model_name)
    return state.get("status") == "downloading"


def get_model_info(model_name: str) -> dict[str, Any] | None:
    return NEMO_MODELS.get(model_name)


def get_model_status(model_name: str) -> dict[str, Any]:
    state = get_download_state(model_name)
    status = state.get("status")
    progress = state.get("progress")
    message = state.get("message", "")

    if status == "downloading":
        return {
            "downloaded": False,
            "status": "downloading",
            "progress": progress or 0.0,
            "message": message,
        }
    if status == "error":
        return {
            "downloaded": False,
            "status": "error",
            "progress": progress or 0.0,
            "message": message or "Download failed",
        }

    downloaded = is_model_downloaded(model_name)
    if downloaded:
        return {
            "downloaded": True,
            "status": "ready",
            "progress": 100.0,
            "message": "Downloaded",
        }
    return {
        "downloaded": False,
        "status": "not_downloaded",
        "progress": 0.0,
        "message": "Not downloaded",
    }


def is_model_downloaded(model_name: str) -> bool:
    if model_name not in NEMO_MODELS:
        return False
    repo_id = NEMO_MODELS[model_name]["hf_repo"]
    filename = NEMO_MODELS[model_name]["hf_filename"]
    try:
        from huggingface_hub import hf_hub_download
        from huggingface_hub.utils import LocalEntryNotFoundError
    except Exception:
        return False

    try:
        path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            cache_dir=get_model_cache_dir(),
            local_files_only=True,
        )
        return bool(path and Path(path).exists())
    except LocalEntryNotFoundError:
        return False
    except Exception:
        return False


def list_available_models() -> list[dict[str, Any]]:
    models = []
    for model_id, info in NEMO_MODELS.items():
        status = get_model_status(model_id)
        size_mb = info.get("size_mb", 0)
        if not size_mb:
            repo_id = info.get("hf_repo")
            filename = info.get("hf_filename")
            if repo_id and filename:
                size_bytes = _get_file_size(repo_id, filename)
                if size_bytes:
                    size_mb = int(round(size_bytes / (1024 * 1024)))
        models.append(
            {
                "id": model_id,
                "name": info.get("name", model_id),
                "description": info.get("description", ""),
                "languages": info.get("languages", []),
                "sizeMb": size_mb or 0,
                "supportsTimestamps": info.get("supports_timestamps", False),
                "downloaded": status.get("downloaded"),
                "status": status.get("status"),
                "progress": status.get("progress"),
                "message": status.get("message"),
            }
        )
    return models


async def download_model(
    model_name: str,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> bool:
    if model_name not in NEMO_MODELS:
        logger.error(f"Unknown model: {model_name}")
        return False

    if is_model_downloaded(model_name):
        _set_download_state(model_name, "ready", 100.0, "Already downloaded")
        if on_progress:
            on_progress(100.0, "Already downloaded")
        return True

    if is_model_downloading(model_name):
        logger.info(f"Download already in progress for {model_name}")
        return False

    repo_id = NEMO_MODELS[model_name]["hf_repo"]
    filename = NEMO_MODELS[model_name]["hf_filename"]
    def _download():
        with _download_lock:
            try:
                import importlib
                from huggingface_hub import hf_hub_download

                cache_dir = get_model_cache_dir()
                start_msg = f"Downloading {filename}..."
                _set_download_state(model_name, "downloading", 0.0, start_msg)
                if on_progress:
                    on_progress(0.0, start_msg)

                hf_utils = importlib.import_module("huggingface_hub.utils")
                hf_utils_tqdm = importlib.import_module("huggingface_hub.utils.tqdm")
                hf_file_download = importlib.import_module("huggingface_hub.file_download")
                prev_tqdm = hf_utils_tqdm.tqdm

                class ProgressTqdm(prev_tqdm):
                    def __init__(self, *args, **kwargs):
                        kwargs.setdefault("disable", True)
                        super().__init__(*args, **kwargs)

                    def update(self, n=1):
                        result = super().update(n)
                        if on_progress and self.total:
                            percent = min(99.9, (self.n / self.total) * 100.0)
                            message = (
                                f"Downloading {filename} "
                                f"({_format_bytes(int(self.n))}/{_format_bytes(int(self.total))})"
                            )
                            _set_download_state(model_name, "downloading", percent, message)
                            on_progress(percent, message)
                        return result

                hf_utils.tqdm = ProgressTqdm
                hf_utils_tqdm.tqdm = ProgressTqdm
                hf_file_download.tqdm = ProgressTqdm

                try:
                    hf_hub_download(
                        repo_id=repo_id,
                        filename=filename,
                        cache_dir=cache_dir,
                        local_files_only=False,
                    )
                finally:
                    hf_utils.tqdm = prev_tqdm
                    hf_utils_tqdm.tqdm = prev_tqdm
                    hf_file_download.tqdm = prev_tqdm

                _set_download_state(model_name, "ready", 100.0, "Download complete")
                if on_progress:
                    on_progress(100.0, "Download complete!")
                logger.info(f"Model downloaded to cache: {cache_dir}")
                return True
            except Exception as exc:
                logger.error(f"Failed to download model {model_name}: {exc}")
                _set_download_state(model_name, "error", -1.0, str(exc))
                if on_progress:
                    on_progress(-1.0, f"Download failed: {exc}")
                return False

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _download)


def _load_model(model_name: str):
    if model_name in _model_cache:
        return _model_cache[model_name]

    if model_name not in NEMO_MODELS:
        raise ValueError(f"Unknown model: {model_name}")

    nemo_asr = _get_nemo()
    import torch
    repo_id = NEMO_MODELS[model_name]["hf_repo"]
    filename = NEMO_MODELS[model_name]["hf_filename"]

    from huggingface_hub import hf_hub_download

    model_path = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        cache_dir=get_model_cache_dir(),
        local_files_only=False,
    )

    def _load_on_device(device: str):
        logger.info(f"Loading NeMo model: {model_name} (device={device})")
        model = nemo_asr.models.ASRModel.restore_from(restore_path=model_path, map_location=device)
        model.eval()
        if device == "cuda":
            try:
                model.to(torch.device("cuda"))
            except Exception:
                # If moving fails, keep original device and let caller retry on CPU
                pass
        return model

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda":
        try:
            model = _load_on_device("cuda")
        except Exception as exc:
            logger.warning(f"CUDA load failed for {model_name}, falling back to CPU: {exc}")
            model = _load_on_device("cpu")
    else:
        model = _load_on_device("cpu")

    _model_cache[model_name] = model
    return model


async def transcribe_audio_bytes(
    audio_bytes: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    model_name: str = DEFAULT_MODEL,
) -> str:
    import numpy as np

    model = _load_model(model_name)

    def _extract_text(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, dict):
            text_val = result.get("text")
            if isinstance(text_val, list) and text_val:
                return str(text_val[0] or "").strip()
            if text_val is not None:
                return str(text_val).strip()
        if hasattr(result, "text"):
            return str(getattr(result, "text") or "").strip()
        return str(result).strip()

    def _transcribe():
        # Prefer numpy array transcription to avoid dataloader/manifest issues.
        samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if channels and channels > 1 and samples.size % channels == 0:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if samples.size == 0:
            return ""
        try:
            results = model.transcribe(
                samples,
                batch_size=1,
                return_hypotheses=False,
                num_workers=0,
                verbose=False,
            )
            if isinstance(results, tuple) and results:
                results = results[0]
            if isinstance(results, list) and results:
                return _extract_text(results[0])
            return _extract_text(results)
        except Exception as exc:
            logger.warning(f"NeMo numpy transcribe failed: {exc}")
            return ""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _transcribe)


def delete_model(model_name: str) -> bool:
    if model_name not in NEMO_MODELS:
        return False

    repo_id = NEMO_MODELS[model_name]["hf_repo"]
    try:
        from huggingface_hub import scan_cache_dir
    except Exception:
        return False

    deleted = False
    for cache_dir in [get_model_cache_dir(), None]:
        try:
            cache_info = scan_cache_dir(cache_dir=cache_dir)
        except Exception:
            continue
        for repo in cache_info.repos:
            if repo.repo_id == repo_id:
                try:
                    cache_info.delete_revisions(repo.revisions).execute()
                    deleted = True
                except Exception as exc:
                    logger.warning(f"Failed to delete repo cache for {repo_id}: {exc}")
    if deleted:
        _set_download_state(model_name, "not_downloaded", 0.0, "Deleted")
    return deleted
