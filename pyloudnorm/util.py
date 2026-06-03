from __future__ import annotations

import numpy as np


def valid_audio(data: np.ndarray, rate: float, block_size: float) -> bool:
    if not isinstance(data, np.ndarray):
        raise ValueError("Data must be of type numpy.ndarray.")
    if not np.issubdtype(data.dtype, np.floating):
        raise ValueError("Data must be floating point.")
    if data.ndim == 2 and data.shape[1] > 5:
        raise ValueError("Audio must have five channels or less.")
    if data.shape[0] < block_size * rate:
        raise ValueError("Audio must have length greater than the block size.")
    return True
