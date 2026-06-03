"""Small pyloudnorm-compatible surface used by Pipecat.

The upstream package depends on SciPy for ``scipy.signal.lfilter``. Scriber only
needs ``Meter.integrated_loudness`` through Pipecat's audio-volume helper, so this
local package keeps that runtime behavior without pulling SciPy into the Windows
sidecar.
"""

from . import normalize, util
from .meter import IIRfilter, Meter

__all__ = ["IIRfilter", "Meter", "normalize", "util"]
