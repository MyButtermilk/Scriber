"""
Local ONNX-based Speech-to-Text using onnx-asr library.
Supports automatic model download from Hugging Face.

Supported Models:
- nemo-canary-1b-v2: NVIDIA Canary 1B v2 (multilingual: en, de, fr, es)
- nemo-parakeet-tdt-0.6b-v3: NVIDIA Parakeet TDT v3 (25 European languages)
"""
import asyncio
import io
import os
import threading
import time
import wave
from fnmatch import fnmatch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional
from loguru import logger

# Lazy imports to avoid startup delay if onnx-asr not installed
_onnx_asr = None
_model_cache: dict[str, Any] = {}
_download_lock = threading.Lock()
_download_state_lock = threading.Lock()
_download_state: dict[str, dict[str, Any]] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="onnx_stt")

# =============================================================================
# Supported Models Configuration
# =============================================================================

ONNX_MODELS = {
    "nemo-canary-1b-v2": {
        "name": "Canary 1B v2",
        "description": "NVIDIA Canary - Best accuracy, multilingual (25 European languages)",
        "languages": [
            "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de",
            "el", "hu", "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk",
            "sl", "es", "sv", "ru", "uk"
        ],
        # Approx sizes from HF repo file list (int8 vs full precision)
        "size_mb": 1030,
        "size_mb_by_quantization": {
            "int8": 1030,
            "fp32": 3961,
        },
        "supported_quantizations": ["int8", "fp32"],
        "supports_timestamps": True,
        "supports_language_param": True,
        "hf_repo": "istupakov/canary-1b-v2-onnx",
    },
    "nemo-parakeet-tdt-0.6b-v3": {
        "name": "Parakeet TDT v3",
        "description": "NVIDIA Parakeet - Fast, 25 European languages incl. German",
        "languages": [
            "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de",
            "el", "hu", "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk",
            "sl", "es", "sv", "ru", "uk"
        ],
        # Approx sizes from HF repo file list (int8 vs full precision)
        "size_mb": 670,
        "size_mb_by_quantization": {
            "int8": 670,
            "fp16": 1280,
            "fp32": 2555,
        },
        "supported_quantizations": ["int8", "fp16", "fp32"],
        "supports_timestamps": True,
        "supports_language_param": False,  # Parakeet auto-detects language
        "hf_repo": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "hf_repo_by_quantization": {
            "fp16": "grikdotnet/parakeet-tdt-0.6b-fp16",
        },
    },
}

DEFAULT_MODEL = "nemo-parakeet-tdt-0.6b-v3"


def get_model_cache_dir() -> Path:
    """Get or create the model cache directory."""
    cache_env = os.getenv("SCRIBER_MODEL_CACHE", "")
    if cache_env:
        cache_dir = Path(cache_env).expanduser()
        # Ensure huggingface_hub uses the same cache location
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


def _get_onnx_asr():
    """Lazy import onnx_asr to avoid startup delay."""
    global _onnx_asr
    if _onnx_asr is None:
        try:
            import onnx_asr
            _onnx_asr = onnx_asr
            logger.debug("onnx-asr library loaded successfully")
        except ImportError as e:
            logger.error(f"onnx-asr not installed: {e}")
            raise ImportError(
                "onnx-asr library not installed. "
                "Install with: pip install onnx-asr[cpu,hub]"
            ) from e
    return _onnx_asr


def is_onnx_available() -> bool:
    """Check if onnx-asr library is available."""
    try:
        _get_onnx_asr()
        return True
    except ImportError:
        return False


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


def _normalize_quantization(quantization: Optional[str]) -> tuple[Optional[str], str]:
    """Return (onnx_quantization, label) where fp32 maps to None for onnx-asr."""
    if not quantization:
        return None, "int8"
    q = str(quantization).strip().lower()
    if q in ("fp32", "float32", "full"):
        return None, "fp32"
    return q, q


def _get_supported_quantizations(model_name: str) -> list[str]:
    info = ONNX_MODELS.get(model_name, {})
    return list(info.get("supported_quantizations") or ["int8", "fp32"])


def _candidate_cache_dirs() -> list[Path | None]:
    """Return possible HF cache dirs (including default None)."""
    candidates: list[Path] = []
    try:
        candidates.append(get_model_cache_dir())
    except Exception:
        pass
    env_cache = os.getenv("HF_HUB_CACHE", "")
    if env_cache:
        candidates.append(Path(env_cache).expanduser())
    env_home = os.getenv("HF_HOME", "")
    if env_home:
        candidates.append(Path(env_home).expanduser() / "hub")
    candidates.append(Path.home() / ".cache" / "huggingface" / "hub")

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)

    # Filter to existing dirs; add None to allow default cache resolution
    existing = [p for p in uniq if p.exists()]
    return existing + [None]


def _resolve_repo_id(model_name: str, quantization_label: str) -> Optional[str]:
    info = ONNX_MODELS.get(model_name, {})
    repo_map = info.get("hf_repo_by_quantization") or {}
    return repo_map.get(quantization_label, info.get("hf_repo"))


def _build_allow_patterns(model_name: str, quantization_label: str) -> list[str]:
    """Build allow_patterns for snapshot_download based on model + quantization."""
    patterns: list[str] = ["config.json", "vocab.txt"]

    if quantization_label == "fp32":
        if model_name == "nemo-canary-1b-v2":
            patterns += ["encoder-model.onnx", "decoder-model.onnx", "encoder-model.onnx.data"]
        elif model_name == "nemo-parakeet-tdt-0.6b-v3":
            patterns += ["encoder-model.onnx", "decoder_joint-model.onnx", "encoder-model.onnx.data"]
        return patterns

    if model_name == "nemo-canary-1b-v2":
        patterns += [
            f"encoder-model*{quantization_label}*.onnx",
            f"decoder-model*{quantization_label}*.onnx",
        ]
    elif model_name == "nemo-parakeet-tdt-0.6b-v3":
        patterns += [
            f"encoder-model*{quantization_label}*.onnx",
            f"decoder_joint-model*{quantization_label}*.onnx",
        ]
    else:
        # Fallback: allow full repo if model is unknown
        patterns = []

    return patterns


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 ** 2:
        return f"{value / 1024:.1f} KB"
    if value < 1024 ** 3:
        return f"{value / (1024 ** 2):.1f} MB"
    return f"{value / (1024 ** 3):.1f} GB"


def _list_repo_files(repo_id: str, allow_patterns: list[str]) -> list[dict[str, Any]]:
    """Return repo files with sizes filtered by allow_patterns."""
    try:
        from huggingface_hub import HfApi
    except Exception as exc:
        logger.warning(f"HuggingFace Hub not available: {exc}")
        return []

    try:
        info = HfApi().model_info(repo_id, files_metadata=True)
    except Exception as exc:
        logger.warning(f"Failed to fetch repo metadata for {repo_id}: {exc}")
        return []

    files: list[dict[str, Any]] = []
    for sibling in info.siblings or []:
        filename = sibling.rfilename
        if allow_patterns:
            if not any(fnmatch(filename, pattern) for pattern in allow_patterns):
                continue
        size = getattr(sibling, "size", None)
        if size is None and getattr(sibling, "lfs", None):
            size = getattr(sibling.lfs, "size", None)
        files.append({"filename": filename, "size": int(size or 0)})
    return files


def get_download_state(model_name: str) -> dict[str, Any]:
    with _download_state_lock:
        return dict(_download_state.get(model_name, {}))


def is_model_downloading(model_name: str) -> bool:
    state = get_download_state(model_name)
    return state.get("status") == "downloading"


def get_model_status(model_name: str, quantization: Optional[str] = None) -> dict[str, Any]:
    """Get download status + availability for a model."""
    state = get_download_state(model_name)
    status = state.get("status")
    progress = state.get("progress")
    message = state.get("message", "")

    if status == "downloading":
        return {
            "downloaded": False,
            "status": "downloading",
            "progress": progress if progress is not None else 0.0,
            "message": message,
        }

    if status == "error":
        return {
            "downloaded": False,
            "status": "error",
            "progress": progress if progress is not None else -1.0,
            "message": message,
        }

    downloaded = is_model_downloaded(model_name, quantization=quantization)
    if downloaded:
        return {
            "downloaded": True,
            "status": "ready",
            "progress": 100.0,
            "message": "Ready",
        }

    return {
        "downloaded": False,
        "status": "not_downloaded",
        "progress": 0.0,
        "message": "Not downloaded",
    }


def is_model_downloaded(model_name: str, quantization: Optional[str] = None) -> bool:
    """
    Check if a model is already downloaded in the HuggingFace cache.
    """
    if model_name not in ONNX_MODELS:
        return False
    
    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub.utils import LocalEntryNotFoundError

        _, q_label = _normalize_quantization(quantization)
        supported = _get_supported_quantizations(model_name)
        if q_label not in supported:
            return False

        repo_id = _resolve_repo_id(model_name, q_label)
        if not repo_id:
            return False
        allow_patterns = _build_allow_patterns(model_name, q_label)

        for cache_dir in _candidate_cache_dirs():
            try:
                snapshot_download(
                    repo_id=repo_id,
                    cache_dir=cache_dir,
                    local_files_only=True,
                    allow_patterns=allow_patterns or None,
                )
                return True
            except LocalEntryNotFoundError:
                continue
        return False
    except LocalEntryNotFoundError:
        return False
    except Exception as e:
        logger.debug(f"Could not check model cache status: {e}")
        return False


def get_model_info(model_name: str) -> Optional[dict]:
    """Get metadata for a model."""
    return ONNX_MODELS.get(model_name)


def list_available_models(quantization: Optional[str] = None) -> list[dict]:
    """List all available models with their download status."""
    models = []
    for model_id, info in ONNX_MODELS.items():
        status = get_model_status(model_id, quantization=quantization)
        models.append({
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "languages": info["languages"],
            "sizeMb": info["size_mb"],
            "sizeMbByQuantization": info.get("size_mb_by_quantization", {}),
            "supportedQuantizations": info.get("supported_quantizations", ["int8", "fp32"]),
            "supportsTimestamps": info["supports_timestamps"],
            "downloaded": status["downloaded"],
            "status": status["status"],
            "progress": status["progress"],
            "message": status["message"],
        })
    return models


async def download_model(
    model_name: str,
    quantization: Optional[str] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> bool:
    """
    Download model from Hugging Face with progress callback.
    
    Args:
        model_name: Name of the model (e.g., "nemo-canary-1b-v2")
        on_progress: Callback(progress_percent, status_message)
    
    Returns:
        True on success, False on failure.
    """
    if model_name not in ONNX_MODELS:
        logger.error(f"Unknown model: {model_name}")
        return False

    quantization_onnx, q_label = _normalize_quantization(quantization)
    supported = _get_supported_quantizations(model_name)
    if q_label not in supported:
        raise ValueError(f"Quantization not supported for {model_name}: {q_label}")
    supported = _get_supported_quantizations(model_name)
    if q_label not in supported:
        _set_download_state(model_name, "error", -1.0, f"Quantization not supported: {q_label}")
        raise ValueError(f"Quantization not supported: {q_label}")

    if is_model_downloaded(model_name, quantization=q_label):
        _set_download_state(model_name, "ready", 100.0, "Already downloaded")
        if on_progress:
            on_progress(100.0, "Already downloaded")
        return True

    if is_model_downloading(model_name):
        logger.info(f"Download already in progress for {model_name}")
        return False

    repo_id = _resolve_repo_id(model_name, q_label)
    if not repo_id:
        _set_download_state(model_name, "error", -1.0, "Missing repo for model")
        return False
    allow_patterns = _build_allow_patterns(model_name, q_label)

    def _download():
        with _download_lock:
            try:
                import importlib
                from huggingface_hub import hf_hub_download, snapshot_download
                from huggingface_hub.utils import LocalEntryNotFoundError
                from tqdm import tqdm

                cache_dir = get_model_cache_dir()
                start_msg = f"Downloading model files ({q_label}). This can take a while..."
                _set_download_state(model_name, "downloading", 0.0, start_msg)

                if on_progress:
                    on_progress(0.0, start_msg)

                logger.info(f"Downloading ONNX model: {model_name} from {repo_id}")

                files = _list_repo_files(repo_id, allow_patterns)
                if not files:
                    # Fallback to snapshot_download with file-count progress
                    def _make_tqdm(on_progress_cb):
                        class ProgressTqdm(tqdm):
                            def __init__(self, *args, **kwargs):
                                kwargs.setdefault("disable", True)
                                super().__init__(*args, **kwargs)

                            def update(self, n=1):
                                result = super().update(n)
                                if on_progress_cb and self.total:
                                    percent = min(100.0, (self.n / self.total) * 100.0)
                                    message = f"Downloading files {self.n}/{self.total}..."
                                    _set_download_state(model_name, "downloading", percent, message)
                                    on_progress_cb(percent, message)
                                return result

                            def close(self):
                                super().close()
                        return ProgressTqdm

                    local_dir = snapshot_download(
                        repo_id=repo_id,
                        cache_dir=cache_dir,
                        resume_download=True,
                        allow_patterns=allow_patterns or None,
                        tqdm_class=_make_tqdm(on_progress),
                    )

                    _set_download_state(model_name, "ready", 100.0, "Download complete")
                    if on_progress:
                        on_progress(100.0, "Download complete!")

                    logger.info(f"Model downloaded to: {local_dir}")
                    return True

                total_bytes = sum(entry.get("size", 0) or 0 for entry in files)
                downloaded_bytes = 0
                downloaded_files = 0
                last_emit = 0.0
                current_file = ""

                def _emit(force: bool = False) -> None:
                    nonlocal last_emit
                    now = time.monotonic()
                    if not force and (now - last_emit) < 0.25:
                        return
                    last_emit = now
                    if total_bytes > 0:
                        percent = min(99.9, (downloaded_bytes / total_bytes) * 100.0)
                        message = f"Downloading {current_file} ({_format_bytes(downloaded_bytes)}/{_format_bytes(total_bytes)})"
                    else:
                        total_files = max(len(files), 1)
                        percent = min(99.9, (downloaded_files / total_files) * 100.0)
                        message = f"Downloading files {downloaded_files}/{total_files}..."
                    _set_download_state(model_name, "downloading", percent, message)
                    if on_progress:
                        on_progress(percent, message)

                def _add_bytes(delta: int) -> None:
                    nonlocal downloaded_bytes
                    if delta <= 0:
                        return
                    downloaded_bytes += delta
                    _emit()

                hf_utils = importlib.import_module("huggingface_hub.utils")
                hf_utils_tqdm = importlib.import_module("huggingface_hub.utils.tqdm")
                hf_file_download = importlib.import_module("huggingface_hub.file_download")
                prev_tqdm = hf_utils_tqdm.tqdm

                class ProgressTqdm(prev_tqdm):
                    def __init__(self, *args, **kwargs):
                        initial = int(kwargs.get("initial") or 0)
                        super().__init__(*args, **kwargs)
                        if initial:
                            _add_bytes(initial)

                    def update(self, n=1):
                        if n:
                            _add_bytes(int(n))
                        return super().update(n)

                hf_utils.tqdm = ProgressTqdm
                hf_utils_tqdm.tqdm = ProgressTqdm
                hf_file_download.tqdm = ProgressTqdm

                try:
                    for entry in files:
                        filename = entry.get("filename", "")
                        current_file = filename
                        size = int(entry.get("size", 0) or 0)

                        cached = False
                        try:
                            local = hf_hub_download(
                                repo_id=repo_id,
                                filename=filename,
                                cache_dir=cache_dir,
                                local_files_only=True,
                            )
                            if local and Path(local).exists():
                                cached = True
                        except LocalEntryNotFoundError:
                            cached = False

                        if cached:
                            downloaded_files += 1
                            if size:
                                downloaded_bytes += size
                            _emit(force=True)
                            continue

                        hf_hub_download(
                            repo_id=repo_id,
                            filename=filename,
                            cache_dir=cache_dir,
                            local_files_only=False,
                        )
                        downloaded_files += 1
                        _emit(force=True)

                finally:
                    hf_utils.tqdm = prev_tqdm
                    hf_utils_tqdm.tqdm = prev_tqdm
                    hf_file_download.tqdm = prev_tqdm

                _set_download_state(model_name, "ready", 100.0, "Download complete")
                if on_progress:
                    on_progress(100.0, "Download complete!")

                logger.info(f"Model downloaded to cache: {cache_dir}")
                return True

            except Exception as e:
                logger.error(f"Failed to download model {model_name}: {e}")
                _set_download_state(model_name, "error", -1.0, str(e))
                if on_progress:
                    on_progress(-1.0, f"Download failed: {e}")
                return False

    # Run download in thread pool to not block event loop
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _download)


def load_model(
    model_name: str = DEFAULT_MODEL,
    quantization: str = "int8",
    use_vad: bool = True,
):
    """
    Load an ONNX model (downloads if not present).
    
    Args:
        model_name: Name of the model to load
        quantization: "int8" (fastest), "fp16", or "fp32" (most accurate)
        use_vad: Enable Voice Activity Detection for long audio
    
    Returns:
        Loaded onnx_asr model object
    """
    global _model_cache
    
    cache_key = f"{model_name}_{quantization}_{use_vad}"
    if cache_key in _model_cache:
        logger.debug(f"Returning cached model: {cache_key}")
        return _model_cache[cache_key]
    
    # Ensure cache directory is initialized for HuggingFace downloads
    get_model_cache_dir()

    onnx_asr = _get_onnx_asr()
    
    quantization_onnx, q_label = _normalize_quantization(quantization)
    model_arg = _resolve_repo_id(model_name, q_label) if q_label == "fp16" else model_name

    logger.info(f"Loading ONNX model: {model_name} (quantization={q_label})")
    
    try:
        model = onnx_asr.load_model(model_arg, quantization=quantization_onnx)
        
        if use_vad:
            # Load Silero VAD for handling long audio files
            try:
                vad = onnx_asr.load_vad("silero")
                model = model.with_vad(vad)
                logger.debug("Silero VAD enabled for long audio support")
            except Exception as vad_err:
                logger.warning(f"Could not load VAD, long audio may fail: {vad_err}")
        
        _model_cache[cache_key] = model
        logger.info(f"Model loaded successfully: {model_name}")
        return model
        
    except Exception as e:
        logger.error(f"Failed to load model {model_name}: {e}")
        raise


def unload_model(model_name: str = None) -> None:
    """
    Unload model(s) from cache to free memory.
    
    Args:
        model_name: Specific model to unload, or None to unload all
    """
    global _model_cache
    
    if model_name is None:
        _model_cache.clear()
        logger.info("All ONNX models unloaded from cache")
    else:
        keys_to_remove = [k for k in _model_cache if k.startswith(model_name)]
        for key in keys_to_remove:
            del _model_cache[key]
        if keys_to_remove:
            logger.info(f"Unloaded model: {model_name}")


async def transcribe_audio(
    audio_path: str,
    model_name: str = DEFAULT_MODEL,
    language: str = "auto",
    quantization: str = "int8",
    use_vad: bool = True,
    on_progress: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Transcribe audio file using local ONNX model.
    
    Args:
        audio_path: Path to audio file (WAV, MP3, etc.)
        model_name: ONNX model to use
        language: Language code (e.g., "en", "de") or "auto"
        quantization: Model quantization level
        use_vad: Enable VAD for long audio
        on_progress: Optional progress callback
    
    Returns:
        Transcribed text
    """
    if on_progress:
        on_progress("Loading model...")
    
    model = load_model(model_name, quantization, use_vad)
    model_info = ONNX_MODELS.get(model_name, {})
    
    if on_progress:
        on_progress("Transcribing...")
    
    def _transcribe():
        # Determine language parameter
        lang_param = None
        if model_info.get("supports_language_param") and language != "auto":
            lang_param = language
        
        # Handle VAD results (returns iterator for segmented audio)
        if use_vad:
            segments = []
            for result in model.recognize(audio_path, language=lang_param):
                if isinstance(result, str):
                    segments.append(result)
                elif hasattr(result, 'text'):
                    segments.append(result.text)
                else:
                    segments.append(str(result))
            return " ".join(segments)
        else:
            result = model.recognize(audio_path, language=lang_param)
            if isinstance(result, str):
                return result
            elif hasattr(result, 'text'):
                return result.text
            return str(result)
    
    # Run transcription in thread pool
    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(_executor, _transcribe)
    
    if on_progress:
        on_progress("Complete")
    
    logger.info(f"Transcription complete: {len(text)} characters")
    return text


async def transcribe_audio_bytes(
    audio_bytes: bytes,
    sample_rate: int = 16000,
    model_name: str = DEFAULT_MODEL,
    language: str = "auto",
    quantization: str = "int8",
    use_vad: bool = False,
) -> str:
    """
    Transcribe audio from bytes (PCM float32 or int16).
    
    Args:
        audio_bytes: Raw audio data
        sample_rate: Sample rate of the audio
        model_name: ONNX model to use
        language: Language code or "auto"
        quantization: Model quantization level
    
    Returns:
        Transcribed text
    """
    import numpy as np

    model = load_model(model_name, quantization, use_vad=use_vad)
    model_info = ONNX_MODELS.get(model_name, {})
    
    def _transcribe():
        # Convert bytes to numpy array
        sr = sample_rate
        if audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
            try:
                with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
                    sr = wav.getframerate()
                    channels = wav.getnchannels()
                    sampwidth = wav.getsampwidth()
                    frames = wav.readframes(wav.getnframes())
                if sampwidth == 1:
                    audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                elif sampwidth == 2:
                    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                elif sampwidth == 4:
                    audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
                else:
                    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

                if channels > 1:
                    audio = audio.reshape(-1, channels).mean(axis=1)
            except Exception as exc:
                logger.warning(f"Failed to decode WAV bytes ({exc}); falling back to raw PCM parsing")
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            # Assume raw PCM int16 (Scriber inputs are s16le); fallback to float32 if needed
            try:
                audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception:
                audio = np.frombuffer(audio_bytes, dtype=np.float32)
        
        # Determine language parameter
        lang_param = None
        if model_info.get("supports_language_param") and language != "auto":
            lang_param = language
        
        result = model.recognize(audio, sample_rate=sr, language=lang_param)
        
        if isinstance(result, str):
            return result
        elif hasattr(result, 'text'):
            return result.text
        return str(result)
    
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _transcribe)


def delete_model(model_name: str, quantization: Optional[str] = None) -> bool:
    """
    Delete a downloaded model from the cache.
    
    Args:
        model_name: Name of the model to delete
        quantization: Quantization to delete (int8/fp16/fp32). If None, delete all.
    
    Returns:
        True if deleted, False otherwise
    """
    if model_name not in ONNX_MODELS:
        return False
    
    # Unload from memory first
    unload_model(model_name)
    
    try:
        import shutil
        from huggingface_hub import scan_cache_dir, HFCacheInfo, constants
        
        repo_ids: list[str] = []
        if quantization:
            _, q_label = _normalize_quantization(quantization)
            repo_id = _resolve_repo_id(model_name, q_label)
            if repo_id:
                repo_ids.append(repo_id)
        else:
            repo_ids.append(ONNX_MODELS[model_name]["hf_repo"])
            for repo_id in (ONNX_MODELS[model_name].get("hf_repo_by_quantization") or {}).values():
                if repo_id not in repo_ids:
                    repo_ids.append(repo_id)

        deleted = False
        for cache_dir in _candidate_cache_dirs():
            try:
                cache_info = scan_cache_dir(cache_dir=cache_dir)
            except Exception:
                continue
            for repo in cache_info.repos:
                if repo.repo_id in repo_ids:
                    for revision in repo.revisions:
                        try:
                            cache_info.delete_revisions(revision.commit_hash).execute()
                            deleted = True
                        except Exception as exc:
                            logger.warning(f"Failed to delete revision {revision.commit_hash}: {exc}")

        if not deleted:
            # Fallback: delete repo folders directly if cache metadata isn't available.
            fallback_dirs: list[Path] = []
            for cache_dir in _candidate_cache_dirs():
                if cache_dir is None:
                    continue
                fallback_dirs.append(Path(cache_dir))
            try:
                fallback_dirs.append(Path(constants.HF_HUB_CACHE))
            except Exception:
                pass

            for repo_id in repo_ids:
                repo_folder_name = f"models--{repo_id.replace('/', '--')}"
                for root in fallback_dirs:
                    repo_path = root / repo_folder_name
                    if repo_path.exists():
                        try:
                            shutil.rmtree(repo_path, ignore_errors=False)
                            deleted = True
                        except Exception as exc:
                            logger.warning(f"Failed to delete repo folder {repo_path}: {exc}")

        if deleted:
            logger.info(f"Deleted model from cache: {model_name}")
            _set_download_state(model_name, "not_downloaded", 0.0, "Deleted")
        return deleted
        
    except Exception as e:
        logger.error(f"Failed to delete model {model_name}: {e}")
        return False
