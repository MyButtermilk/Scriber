from __future__ import annotations

import importlib.util
from pathlib import Path


def _module():
    script = Path(__file__).parents[1] / "scripts" / "probe_bitsandbytes_failure.py"
    spec = importlib.util.spec_from_file_location("probe_bitsandbytes_failure", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failure_reason_classification_is_conservative() -> None:
    module = _module()

    assert module._reason_code("generated an empty completion") == "empty_completion"
    assert module._reason_code("did not produce valid SST") == "invalid_sst"
    assert module._reason_code("CUDA allocation failed") == "materialization_failed"
