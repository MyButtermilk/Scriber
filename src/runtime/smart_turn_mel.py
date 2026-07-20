from __future__ import annotations

import hashlib
import importlib
import threading
from pathlib import Path
from typing import Any


_MODEL_NAME = "smart-turn-mel-matmul.onnx"
_MODEL_SHA256 = "64568feef60b122cd072163ba5c55e5018e5ffb22f6534ec2620ca3d33545f15"
_MEL_FILTERS_SHA256 = "abbc49377876704082b17d436c19d91e2a1874fa73f5e2aa8a38df3943f73798"
_EXPECTED_FILTER_SHAPE = (201, 80)
_INSTALL_LOCK = threading.Lock()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _TransposeMatmul:
    def __init__(self, session: Any):
        self._session = session

    def __matmul__(self, magnitudes: Any) -> Any:
        import numpy as np

        contiguous = np.ascontiguousarray(magnitudes, dtype=np.float64)
        if contiguous.ndim != 2 or contiguous.shape[0] != _EXPECTED_FILTER_SHAPE[0]:
            raise ValueError(f"Unexpected SmartTurn magnitude shape: {contiguous.shape}")
        return self._session.run(None, {"magnitudes": contiguous})[0]


class _MelFiltersProxy:
    def __init__(self, original: Any, session: Any):
        self._original = original
        self._transpose = _TransposeMatmul(session)

    @property
    def T(self) -> _TransposeMatmul:
        return self._transpose

    @property
    def shape(self) -> tuple[int, int]:
        return self._original.shape

    def __array__(self, dtype: Any = None, copy: bool | None = None) -> Any:
        import numpy as np

        return np.array(self._original, dtype=dtype, copy=copy)


def install_smart_turn_mel_acceleration(*, force: bool = False) -> bool:
    """Route Pipecat's fixed mel projection through the bundled ONNX Runtime.

    Scriber's compact NumPy build intentionally has no general-purpose BLAS.
    SmartTurn's one material matrix multiplication is fixed at 80x201, so a
    tiny attested ONNX graph can reuse ONNX Runtime's existing MLAS kernels
    without shipping a second 20 MB linear-algebra runtime.
    """

    import numpy as np
    import onnxruntime as ort
    _whisper_features = importlib.import_module(
        "pipecat.audio.turn.smart_turn._whisper_features"
    )

    if not force and "+scriber.noblas." not in np.__version__:
        return False

    with _INSTALL_LOCK:
        current = _whisper_features._MEL_FILTERS
        if isinstance(current, _MelFiltersProxy):
            return False

        filters = np.asarray(current, dtype=np.float64)
        if filters.shape != _EXPECTED_FILTER_SHAPE:
            raise RuntimeError(f"Unsupported Pipecat mel-filter shape: {filters.shape}")
        matrix = np.ascontiguousarray(filters.T)
        if _sha256_bytes(matrix.tobytes()) != _MEL_FILTERS_SHA256:
            raise RuntimeError("Pipecat mel filters do not match the attested SmartTurn model")

        model_path = Path(__file__).with_name(_MODEL_NAME)
        model_bytes = model_path.read_bytes()
        if _sha256_bytes(model_bytes) != _MODEL_SHA256:
            raise RuntimeError("SmartTurn mel model does not match its attested SHA-256")

        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(model_bytes, sess_options=options)

        probe = np.arange(201, dtype=np.float64).reshape(201, 1) / 201.0
        expected = matrix @ probe
        actual = session.run(None, {"magnitudes": probe})[0]
        if not np.allclose(actual, expected, rtol=1e-12, atol=1e-12):
            raise RuntimeError("SmartTurn mel model failed its numerical self-check")

        _whisper_features._MEL_FILTERS = _MelFiltersProxy(current, session)
        return True
