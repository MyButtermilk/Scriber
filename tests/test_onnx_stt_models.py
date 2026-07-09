import pytest

import src.onnx_stt as onnx_stt


def test_primeline_model_advertises_uploaded_quantizations():
    info = onnx_stt.get_model_info("parakeet-primeline")

    assert info is not None
    assert info["hf_repo"] == "Buttermilk03/parakeet-primeline-onnx"
    assert info["source_hf_repo"] == "primeline/parakeet-primeline"
    assert info["supported_quantizations"] == ["int8", "fp32"]
    assert info["size_mb_by_quantization"]["int8"] < info["size_mb_by_quantization"]["fp32"]


def test_primeline_download_patterns_keep_fp32_snapshot_complete():
    assert onnx_stt._build_allow_patterns("parakeet-primeline", "fp32") == []

    int8_patterns = onnx_stt._build_allow_patterns("parakeet-primeline", "int8")
    assert "config.json" in int8_patterns
    assert "vocab.txt" in int8_patterns
    assert any(pattern.startswith("encoder-model") for pattern in int8_patterns)
    assert any(pattern.startswith("decoder_joint-model") for pattern in int8_patterns)


def test_primeline_load_uses_uploaded_snapshot(monkeypatch, tmp_path):
    calls = {}

    class FakeOnnxAsr:
        @staticmethod
        def load_model(model, path=None, quantization=None):
            calls["model"] = model
            calls["path"] = path
            calls["quantization"] = quantization
            return object()

    monkeypatch.setattr(onnx_stt, "_get_onnx_asr", lambda: FakeOnnxAsr)
    monkeypatch.setattr(onnx_stt, "_snapshot_model_path", lambda model, quantization: tmp_path)
    onnx_stt.unload_model()

    onnx_stt.load_model("parakeet-primeline", quantization="int8", use_vad=False)

    assert calls == {
        "model": "Buttermilk03/parakeet-primeline-onnx",
        "path": tmp_path,
        "quantization": "int8",
    }


def test_primeline_rejects_unpublished_quantization(monkeypatch):
    monkeypatch.setattr(onnx_stt, "_get_onnx_asr", lambda: None)
    onnx_stt.unload_model()

    with pytest.raises(ValueError, match="Quantization not supported"):
        onnx_stt.load_model("parakeet-primeline", quantization="fp16", use_vad=False)
