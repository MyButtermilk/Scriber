from __future__ import annotations

import warnings

import numpy as np

from . import util


class IIRfilter:
    """Biquad IIR filter compatible with the subset used by pyloudnorm."""

    def __init__(
        self,
        G: float,
        Q: float,
        fc: float,
        rate: float,
        filter_type: str,
        passband_gain: float = 1.0,
    ) -> None:
        self.G = G
        self.Q = Q
        self.fc = fc
        self.rate = rate
        self.filter_type = filter_type
        self.passband_gain = passband_gain

    def generate_coefficients(self) -> tuple[np.ndarray, np.ndarray]:
        amplitude = 10 ** (self.G / 40.0)
        omega = 2.0 * np.pi * (self.fc / self.rate)
        alpha = np.sin(omega) / (2.0 * self.Q)
        cos_omega = np.cos(omega)

        if self.filter_type == "high_shelf":
            root_a = np.sqrt(amplitude)
            b0 = amplitude * ((amplitude + 1) + (amplitude - 1) * cos_omega + 2 * root_a * alpha)
            b1 = -2 * amplitude * ((amplitude - 1) + (amplitude + 1) * cos_omega)
            b2 = amplitude * ((amplitude + 1) + (amplitude - 1) * cos_omega - 2 * root_a * alpha)
            a0 = (amplitude + 1) - (amplitude - 1) * cos_omega + 2 * root_a * alpha
            a1 = 2 * ((amplitude - 1) - (amplitude + 1) * cos_omega)
            a2 = (amplitude + 1) - (amplitude - 1) * cos_omega - 2 * root_a * alpha
        elif self.filter_type == "low_shelf":
            root_a = np.sqrt(amplitude)
            b0 = amplitude * ((amplitude + 1) - (amplitude - 1) * cos_omega + 2 * root_a * alpha)
            b1 = 2 * amplitude * ((amplitude - 1) - (amplitude + 1) * cos_omega)
            b2 = amplitude * ((amplitude + 1) - (amplitude - 1) * cos_omega - 2 * root_a * alpha)
            a0 = (amplitude + 1) + (amplitude - 1) * cos_omega + 2 * root_a * alpha
            a1 = -2 * ((amplitude - 1) + (amplitude + 1) * cos_omega)
            a2 = (amplitude + 1) + (amplitude - 1) * cos_omega - 2 * root_a * alpha
        elif self.filter_type == "high_pass":
            b0 = (1 + cos_omega) / 2
            b1 = -(1 + cos_omega)
            b2 = (1 + cos_omega) / 2
            a0 = 1 + alpha
            a1 = -2 * cos_omega
            a2 = 1 - alpha
        elif self.filter_type == "low_pass":
            b0 = (1 - cos_omega) / 2
            b1 = 1 - cos_omega
            b2 = (1 - cos_omega) / 2
            a0 = 1 + alpha
            a1 = -2 * cos_omega
            a2 = 1 - alpha
        elif self.filter_type == "peaking":
            b0 = 1 + alpha * amplitude
            b1 = -2 * cos_omega
            b2 = 1 - alpha * amplitude
            a0 = 1 + alpha / amplitude
            a1 = -2 * cos_omega
            a2 = 1 - alpha / amplitude
        elif self.filter_type == "notch":
            b0 = 1
            b1 = -2 * cos_omega
            b2 = 1
            a0 = 1 + alpha
            a1 = -2 * cos_omega
            a2 = 1 - alpha
        elif self.filter_type == "high_shelf_DeMan":
            k = np.tan(np.pi * self.fc / self.rate)
            vh = np.power(10.0, self.G / 20.0)
            vb = np.power(vh, 0.499666774155)
            denominator = 1.0 + k / self.Q + k * k
            b0 = (vh + vb * k / self.Q + k * k) / denominator
            b1 = 2.0 * (k * k - vh) / denominator
            b2 = (vh - vb * k / self.Q + k * k) / denominator
            a0 = 1.0
            a1 = 2.0 * (k * k - 1.0) / denominator
            a2 = (1.0 - k / self.Q + k * k) / denominator
        elif self.filter_type == "high_pass_DeMan":
            k = np.tan(np.pi * self.fc / self.rate)
            denominator = 1.0 + k / self.Q + k * k
            b0 = 1.0
            b1 = -2.0
            b2 = 1.0
            a0 = 1.0
            a1 = 2.0 * (k * k - 1.0) / denominator
            a2 = (1.0 - k / self.Q + k * k) / denominator
        else:
            raise ValueError(f"Invalid filter type: {self.filter_type}")

        return np.array([b0, b1, b2], dtype=np.float64) / a0, np.array(
            [a0, a1, a2], dtype=np.float64
        ) / a0

    def apply_filter(self, data: np.ndarray) -> np.ndarray:
        return self.passband_gain * _lfilter(self.b, self.a, data)

    @property
    def a(self) -> np.ndarray:
        return self.generate_coefficients()[1]

    @property
    def b(self) -> np.ndarray:
        return self.generate_coefficients()[0]


class Meter:
    """Integrated loudness meter for the pyloudnorm API used by Pipecat."""

    def __init__(self, rate: float, filter_class: str = "K-weighting", block_size: float = 0.400) -> None:
        self.rate = rate
        self.block_size = block_size
        self.filter_class = filter_class

    def integrated_loudness(self, data: np.ndarray) -> float:
        input_data = data.copy()
        util.valid_audio(input_data, self.rate, self.block_size)
        if input_data.ndim == 1:
            input_data = input_data.reshape((input_data.shape[0], 1))

        channel_count = input_data.shape[1]
        for filter_stage in self._filters.values():
            for channel in range(channel_count):
                input_data[:, channel] = filter_stage.apply_filter(input_data[:, channel])

        block_size = float(self.block_size)
        overlap = 0.75
        step = 1.0 - overlap
        duration = input_data.shape[0] / self.rate
        block_count = int(np.round(((duration - block_size) / (block_size * step))) + 1)
        if block_count <= 0:
            return float("-inf")

        energies = np.zeros((channel_count, block_count), dtype=np.float64)
        for block_index in range(block_count):
            lower = int(block_size * block_index * step * self.rate)
            upper = int(block_size * (block_index * step + 1) * self.rate)
            block = input_data[lower:upper, :]
            if block.size:
                energies[:, block_index] = np.mean(np.square(block), axis=0)

        channel_gains = np.array([1.0, 1.0, 1.0, 1.41, 1.41], dtype=np.float64)[:channel_count]
        weighted_energy = channel_gains @ energies
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            block_loudness = -0.691 + 10.0 * np.log10(weighted_energy)

        absolute_threshold = -70.0
        gated_indices = [idx for idx, value in enumerate(block_loudness) if value >= absolute_threshold]
        if not gated_indices:
            return float("-inf")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            gated_mean = np.array(
                [np.mean(energies[channel, gated_indices]) for channel in range(channel_count)],
                dtype=np.float64,
            )
            relative_threshold = -0.691 + 10.0 * np.log10(channel_gains @ gated_mean) - 10.0

        gated_indices = [
            idx
            for idx, value in enumerate(block_loudness)
            if value > relative_threshold and value > absolute_threshold
        ]
        if not gated_indices:
            return float("-inf")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            gated_mean = np.nan_to_num(
                np.array(
                    [np.mean(energies[channel, gated_indices]) for channel in range(channel_count)],
                    dtype=np.float64,
                )
            )
        with np.errstate(divide="ignore"):
            return float(-0.691 + 10.0 * np.log10(channel_gains @ gated_mean))

    @property
    def filter_class(self) -> str:
        return self._filter_class

    @filter_class.setter
    def filter_class(self, value: str) -> None:
        self._filter_class = value
        if value == "K-weighting":
            self._filters = {
                "high_shelf": IIRfilter(4.0, 1 / np.sqrt(2), 1500.0, self.rate, "high_shelf"),
                "high_pass": IIRfilter(0.0, 0.5, 38.0, self.rate, "high_pass"),
            }
        elif value == "Fenton/Lee 1":
            self._filters = {
                "high_shelf": IIRfilter(5.0, 1 / np.sqrt(2), 1500.0, self.rate, "high_shelf"),
                "high_pass": IIRfilter(0.0, 0.5, 130.0, self.rate, "high_pass"),
                "peaking": IIRfilter(0.0, 1 / np.sqrt(2), 500.0, self.rate, "peaking"),
            }
        elif value == "Fenton/Lee 2":
            self._filters = {
                "high_shelf": IIRfilter(4.0, 1 / np.sqrt(2), 1500.0, self.rate, "high_shelf"),
                "high_pass": IIRfilter(0.0, 0.5, 38.0, self.rate, "high_pass"),
            }
        elif value == "Dash et al.":
            self._filters = {
                "high_pass": IIRfilter(0.0, 0.375, 149.0, self.rate, "high_pass"),
                "peaking": IIRfilter(-2.93820927, 1.68878655, 1000.0, self.rate, "peaking"),
            }
        elif value == "DeMan":
            self._filters = {
                "high_shelf_DeMan": IIRfilter(
                    3.99984385397,
                    0.7071752369554193,
                    1681.9744509555319,
                    self.rate,
                    "high_shelf_DeMan",
                ),
                "high_pass_DeMan": IIRfilter(
                    0.0,
                    0.5003270373253953,
                    38.13547087613982,
                    self.rate,
                    "high_pass_DeMan",
                ),
            }
        elif value == "custom":
            self._filters = {}
        else:
            raise ValueError(f"Invalid filter class: {value}")


def _lfilter(b: np.ndarray, a: np.ndarray, data: np.ndarray) -> np.ndarray:
    values = np.asarray(data, dtype=np.float64)
    output = np.empty_like(values, dtype=np.float64)
    x1 = x2 = y1 = y2 = 0.0
    for index, x0 in enumerate(values):
        y0 = b[0] * x0 + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        output[index] = y0
        x2 = x1
        x1 = x0
        y2 = y1
        y1 = y0
    return output
