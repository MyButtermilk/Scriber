from __future__ import annotations

import pytest

from scriber_polishing.model_factory import BASE_MODEL_ID, assert_base_model, run_forward_smoke


class FakeModel:
    def __call__(self, **kwargs):
        return {"loss": 0.25, "received": kwargs}


def test_forward_smoke_passes_completion_only_labels_to_the_model() -> None:
    result = run_forward_smoke(FakeModel(), {"input_ids": [1, 2], "labels": [-100, 2]})

    assert result["loss"] == 0.25
    assert result["received"]["labels"] == [-100, 2]


def test_only_the_fixed_gemma_base_model_is_accepted() -> None:
    assert assert_base_model(BASE_MODEL_ID) == BASE_MODEL_ID
    with pytest.raises(ValueError, match="google/gemma-3-270m-it"):
        assert_base_model("somewhere/else")
