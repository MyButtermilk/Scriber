import pytest

import src.onnx_stt as onnx_stt


def test_primeline_model_advertises_uploaded_quantizations():
    info = onnx_stt.get_model_info("parakeet-primeline")

    assert info is not None
    assert info["hf_repo"] == "geier/deskscribe-parakeet-primeline-onnx"
    assert info["hf_repo_by_quantization"]["int8"] == "Buttermilk03/parakeet-primeline-onnx"
    assert info["source_hf_repo"] == "primeline/parakeet-primeline"
    assert info["supported_quantizations"] == ["int8", "fp32"]
    assert info["load_from_archive"] is True
    assert info["archive_quantizations"] == ["fp32"]
    assert info["load_from_snapshot_quantizations"] == ["int8"]
    assert info["archive"] == "parakeet-primeline-onnx-v1.zip"
    assert info["manifest"] == "parakeet-primeline-onnx-v1.manifest.json"
    assert info["sha256"] == "a75a87c815f8cd6cb66de1d9462db6b719b97070e6e6a5716408bf4b5c1c46aa"
    assert "mel_fbanks_nemo128.bin" in info["archive_common_files"]


def test_primeline_download_patterns_fetch_deskscribe_archive_package():
    assert onnx_stt._build_allow_patterns("parakeet-primeline", "fp32") == [
        "parakeet-primeline-onnx-v1.zip",
        "parakeet-primeline-onnx-v1.manifest.json",
        "parakeet-primeline-onnx-v1.zip.sha256",
    ]


def test_primeline_int8_download_patterns_fetch_buttermilk_files():
    assert onnx_stt._resolve_repo_id("parakeet-primeline", "int8") == "Buttermilk03/parakeet-primeline-onnx"

    int8_patterns = onnx_stt._build_allow_patterns("parakeet-primeline", "int8")
    assert "config.json" in int8_patterns
    assert "vocab.txt" in int8_patterns
    assert "encoder-model*int8*.onnx" in int8_patterns
    assert "decoder_joint-model*int8*.onnx" in int8_patterns


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
    monkeypatch.setattr(onnx_stt, "_archive_model_path", lambda model, quantization: tmp_path)
    onnx_stt.unload_model()

    onnx_stt.load_model("parakeet-primeline", quantization="fp32", use_vad=False)

    assert calls == {
        "model": "geier/deskscribe-parakeet-primeline-onnx",
        "path": tmp_path,
        "quantization": None,
    }


def test_primeline_rejects_unpublished_quantization(monkeypatch):
    monkeypatch.setattr(onnx_stt, "_get_onnx_asr", lambda: None)
    onnx_stt.unload_model()

    with pytest.raises(ValueError, match="Quantization not supported"):
        onnx_stt.load_model("parakeet-primeline", quantization="fp16", use_vad=False)


def test_primeline_int8_load_uses_buttermilk_snapshot(monkeypatch, tmp_path):
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


def test_primeline_archive_download_status_requires_extracted_files(monkeypatch, tmp_path):
    monkeypatch.setattr(onnx_stt, "get_model_cache_dir", lambda: tmp_path)
    assert onnx_stt.is_model_downloaded("parakeet-primeline", quantization="fp32") is False

    extract_dir = onnx_stt._archive_extract_dir("parakeet-primeline", "fp32")
    extract_dir.mkdir(parents=True)
    for filename in onnx_stt._archive_required_files("parakeet-primeline", "fp32"):
        (extract_dir / filename).write_text("test", encoding="utf-8")

    assert onnx_stt.is_model_downloaded("parakeet-primeline", quantization="fp32") is True


@pytest.mark.asyncio
async def test_download_rechecks_cache_after_acquiring_worker_lock(monkeypatch):
    cache_checks = iter([False, True])
    progress = []
    monkeypatch.setattr(
        onnx_stt,
        "is_model_downloaded",
        lambda *_args, **_kwargs: next(cache_checks),
    )
    monkeypatch.setattr(onnx_stt, "is_model_downloading", lambda _model: False)

    result = await onnx_stt.download_model(
        "parakeet-primeline",
        quantization="int8",
        on_progress=lambda value, message: progress.append((value, message)),
    )

    assert result is True
    assert progress == [(100.0, "Already downloaded")]


def test_delete_model_refuses_active_download(monkeypatch):
    monkeypatch.setattr(onnx_stt, "is_model_downloading", lambda _model: True)
    monkeypatch.setattr(
        onnx_stt,
        "unload_model",
        lambda *_args, **_kwargs: pytest.fail("active model was unloaded"),
    )

    assert onnx_stt.delete_model("parakeet-primeline", quantization="int8") is False


def test_download_status_is_isolated_by_quantization(monkeypatch):
    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()


def test_delete_model_rejects_unsupported_quantization_before_unload(monkeypatch):
    monkeypatch.setattr(
        onnx_stt,
        "unload_model",
        lambda *_args, **_kwargs: pytest.fail("invalid selection unloaded the active model"),
    )

    with pytest.raises(ValueError, match="Quantization not supported"):
        onnx_stt.delete_model("parakeet-primeline", quantization="fp16")
    monkeypatch.setattr(onnx_stt, "is_model_downloaded", lambda *_args, **_kwargs: False)

    onnx_stt._set_download_state(
        "parakeet-primeline",
        "error",
        -1.0,
        "fp32 failed",
        quantization="fp32",
    )
    onnx_stt._set_download_state(
        "parakeet-primeline",
        "downloading",
        42.0,
        "int8 active",
        quantization="int8",
    )

    assert onnx_stt.get_model_status("parakeet-primeline", "fp32")["status"] == "error"
    int8_status = onnx_stt.get_model_status("parakeet-primeline", "int8")
    assert int8_status["status"] == "downloading"
    assert int8_status["progress"] == 42.0
    assert onnx_stt.is_model_downloading("parakeet-primeline") is True
    assert onnx_stt.is_model_downloading("parakeet-primeline", "fp32") is False

    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()
