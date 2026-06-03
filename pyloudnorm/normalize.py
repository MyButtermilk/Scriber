from __future__ import annotations

import warnings

import numpy as np


def peak(data: np.ndarray, target: float) -> np.ndarray:
    current_peak = np.max(np.abs(data))
    if current_peak == 0:
        return data.copy()
    output = (np.power(10.0, target / 20.0) / current_peak) * data
    if np.max(np.abs(output)) >= 1.0:
        warnings.warn("Possible clipped samples in output.", stacklevel=2)
    return output


def loudness(data: np.ndarray, input_loudness: float, target_loudness: float) -> np.ndarray:
    output = np.power(10.0, (target_loudness - input_loudness) / 20.0) * data
    if np.max(np.abs(output)) >= 1.0:
        warnings.warn("Possible clipped samples in output.", stacklevel=2)
    return output
