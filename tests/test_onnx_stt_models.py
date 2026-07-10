import asyncio
import hashlib
import json
import sys
import threading
import types
import zipfile

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


def test_snapshot_validation_requires_every_nonempty_model_artifact(tmp_path):
    patterns = onnx_stt._build_allow_patterns("parakeet-primeline", "int8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "vocab.txt").write_text("token", encoding="utf-8")
    (tmp_path / "encoder-model-int8.onnx").write_bytes(b"encoder")

    assert onnx_stt._snapshot_has_required_files(tmp_path, patterns) is False

    (tmp_path / "decoder_joint-model-int8.onnx").write_bytes(b"decoder")
    assert onnx_stt._snapshot_has_required_files(tmp_path, patterns) is True


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


def _install_archive_fixture(monkeypatch, tmp_path, *, sidecar_sha: str | None = None):
    info = dict(onnx_stt.ONNX_MODELS["parakeet-primeline"])
    required = onnx_stt._archive_required_files("parakeet-primeline", "fp32")
    archive_path = tmp_path / info["archive"]
    with zipfile.ZipFile(archive_path, "w") as archive:
        for filename in required:
            archive.writestr(filename, f"fixture:{filename}")
    archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    info["sha256"] = archive_sha
    monkeypatch.setitem(onnx_stt.ONNX_MODELS, "parakeet-primeline", info)

    manifest_path = tmp_path / info["manifest"]
    manifest_path.write_text(
        json.dumps(
            {
                "archive": info["archive"],
                "sha256": archive_sha,
                "size": archive_path.stat().st_size,
                "required_files": required,
            }
        ),
        encoding="utf-8",
    )
    sha_path = tmp_path / info["sha256_file"]
    sha_path.write_text(
        f"{sidecar_sha or archive_sha}  {info['archive']}\n",
        encoding="utf-8",
    )
    files = {
        info["archive"]: archive_path,
        info["manifest"]: manifest_path,
        info["sha256_file"]: sha_path,
    }
    hub = types.ModuleType("huggingface_hub")
    hub.hf_hub_download = lambda **kwargs: str(files[kwargs["filename"]])
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(onnx_stt, "get_model_cache_dir", lambda: tmp_path / "cache")
    return required


def test_archive_model_path_verifies_manifest_sidecar_and_payload(monkeypatch, tmp_path):
    required = _install_archive_fixture(monkeypatch, tmp_path)

    extracted = onnx_stt._archive_model_path("parakeet-primeline", "fp32")

    assert all((extracted / filename).is_file() for filename in required)


def test_archive_model_path_rejects_conflicting_sha_sidecar(monkeypatch, tmp_path):
    _install_archive_fixture(monkeypatch, tmp_path, sidecar_sha="0" * 64)

    with pytest.raises(ValueError, match="Conflicting SHA-256 metadata"):
        onnx_stt._archive_model_path("parakeet-primeline", "fp32")


@pytest.mark.asyncio
async def test_download_rechecks_cache_after_acquiring_worker_lock(monkeypatch):
    cache_checks = iter([False, True])
    progress = []
    monkeypatch.setattr(
        onnx_stt,
        "is_model_downloaded",
        lambda *_args, **_kwargs: next(cache_checks),
    )

    result = await onnx_stt.download_model(
        "parakeet-primeline",
        quantization="int8",
        on_progress=lambda value, message: progress.append((value, message)),
    )

    assert result is True
    assert progress == [
        (0.0, "Downloading model files (int8). This can take a while..."),
        (100.0, "Already downloaded"),
    ]


@pytest.mark.asyncio
async def test_concurrent_download_requests_claim_model_once(monkeypatch):
    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()

    preflight_barrier = threading.Barrier(2)
    worker_check_started = threading.Event()
    release_worker = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def _downloaded(*_args, **_kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number <= 2:
            preflight_barrier.wait(timeout=2)
            return False
        worker_check_started.set()
        assert release_worker.wait(timeout=2)
        return True

    monkeypatch.setattr(onnx_stt, "is_model_downloaded", _downloaded)

    first = asyncio.create_task(onnx_stt.download_model("parakeet-primeline", "int8"))
    second = asyncio.create_task(onnx_stt.download_model("parakeet-primeline", "int8"))
    assert await asyncio.to_thread(worker_check_started.wait, 1)
    await asyncio.sleep(0)
    release_worker.set()
    results = await asyncio.gather(first, second)

    assert sorted(results) == [False, True]
    assert calls == 3

    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()


@pytest.mark.asyncio
async def test_progress_callback_failure_does_not_abort_download(monkeypatch):
    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()
    cache_checks = iter([False, True])
    monkeypatch.setattr(
        onnx_stt,
        "is_model_downloaded",
        lambda *_args, **_kwargs: next(cache_checks),
    )

    result = await onnx_stt.download_model(
        "parakeet-primeline",
        "int8",
        on_progress=lambda *_args: (_ for _ in ()).throw(RuntimeError("UI closed")),
    )

    assert result is True
    assert onnx_stt.get_download_state("parakeet-primeline", "int8")["status"] == "ready"
    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()


@pytest.mark.asyncio
async def test_cancelled_http_waiter_does_not_cancel_download_worker(monkeypatch):
    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()
    worker_started = threading.Event()
    release_worker = threading.Event()
    calls = 0

    def _downloaded(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False
        worker_started.set()
        assert release_worker.wait(timeout=2)
        return True

    monkeypatch.setattr(onnx_stt, "is_model_downloaded", _downloaded)

    task = asyncio.create_task(onnx_stt.download_model("parakeet-primeline", "int8"))
    assert await asyncio.to_thread(worker_started.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release_worker.set()
    for _ in range(100):
        if onnx_stt.get_download_state("parakeet-primeline", "int8").get("status") == "ready":
            break
        await asyncio.sleep(0.01)

    assert onnx_stt.get_download_state("parakeet-primeline", "int8")["status"] == "ready"
    with onnx_stt._download_state_lock:
        onnx_stt._download_state.clear()


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


def test_delete_model_rejects_unsupported_quantization_before_unload(monkeypatch):
    monkeypatch.setattr(
        onnx_stt,
        "unload_model",
        lambda *_args, **_kwargs: pytest.fail("invalid selection unloaded the active model"),
    )

    with pytest.raises(ValueError, match="Quantization not supported"):
        onnx_stt.delete_model("parakeet-primeline", quantization="fp16")
