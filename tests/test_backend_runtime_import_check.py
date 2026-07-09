from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_backend_runtime_imports import REQUIRED_IMPORTS, check_imports


def test_backend_worker_startup_timeout_simulation_is_once(monkeypatch, tmp_path):
    from src import backend_worker

    marker_path = tmp_path / "startup-timeout.marker"
    monkeypatch.setenv("SCRIBER_SIMULATE_STARTUP_TIMEOUT_ONCE", "1")
    monkeypatch.setenv("SCRIBER_SIMULATE_STARTUP_TIMEOUT_MARKER", str(marker_path))

    assert backend_worker.should_simulate_startup_timeout_once() is True
    assert marker_path.exists()
    assert backend_worker.should_simulate_startup_timeout_once() is False


def test_backend_runtime_import_check_covers_audio_startup_dependencies():
    required_modules = {module for module, _reason in REQUIRED_IMPORTS}

    assert "pyloudnorm" in required_modules
    assert "onnxruntime" in required_modules
    assert "onnx_asr" in required_modules
    assert "sherpa_onnx" not in required_modules
    assert "pipecat.audio.vad.silero" in required_modules
    assert "src.web_api" in required_modules
    assert "pipecat.services.soniox.stt" in required_modules
    assert "pipecat.services.assemblyai.stt" in required_modules
    assert "pipecat.services.deepgram.stt" in required_modules
    assert "pipecat.services.google.stt" in required_modules
    assert "pipecat.services.speechmatics.stt" in required_modules
    assert "src.azure_mai_stt" in required_modules


def test_standard_requirements_include_audio_runtime_dependencies():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements-base.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "scipy" not in requirements
    assert "onnxruntime" in requirements
    assert "onnx-asr[cpu,hub]>=0.10.2,<0.11" in requirements
    assert "sherpa-onnx==1.13.4" not in requirements
    assert "pipecat-ai[silero]==1.5.0" in requirements
    assert "deepgram-sdk==7.4.0" in requirements
    assert "google-cloud-speech<3,>=2.33.0" in requirements
    assert "google-genai<3,>=1.68.0" in requirements
    assert "groq~=0.23.0" in requirements
    assert "nltk<4,>=3.9.4" in requirements
    assert "openai<3,>=1.74.0" in requirements
    assert "speechmatics-rt==1.1.0" in requirements
    assert "speechmatics-voice==0.2.8" in requirements
    assert "speechmatics-python" not in requirements
    assert all("transformers" not in line for line in requirements)
    assert "google-generativeai" not in requirements
    assert "azure-cognitiveservices-speech~=1.42.0" not in requirements
    assert "PySide6-Essentials" not in requirements
    assert "customtkinter" not in requirements
    assert "pystray" not in requirements
    assert all("aws" not in line for line in requirements)
    assert all("boto" not in line for line in requirements)


def test_backend_worker_import_does_not_eagerly_import_web_api():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.backend_worker; print('src.web_api' in sys.modules)",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


def test_backend_worker_runtime_import_check_entrypoint():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "src.backend_worker", "--runtime-import-check"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"ok": True, "missing": []}


def test_sidecar_build_runs_frozen_runtime_import_check():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (
        repo_root / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    spec = (repo_root / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )

    assert "Invoke-FrozenBackendRuntimeImportCheck" in build_script
    assert "--runtime-import-check" in build_script
    assert "scripts.check_backend_runtime_imports" in spec


def test_sidecar_spec_bundles_silero_vad_runtime_dependency():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (
        repo_root / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    spec = (repo_root / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )

    assert "collect_dynamic_libs" in spec
    assert '"onnxruntime"' in spec
    assert '"onnx_asr"' in spec
    assert '"sherpa_onnx"' not in spec
    assert '"azure.cognitiveservices.speech"' not in spec
    assert "collect_required_dynamic_libs" in spec
    assert "upx=False" in spec
    assert '"pipecat.audio.vad.silero"' in spec
    assert '"pipecat.services.aws.stt"' not in spec
    assert '"pipecat.services.azure.stt"' not in spec
    assert '"pipecat.services.soniox.stt"' in spec
    assert '"pipecat.services.assemblyai.stt"' in spec
    assert '"pyloudnorm.meter"' in spec
    assert '"scipy",' in spec
    assert '"scipy.signal"' not in spec
    collect_submodules_packages = spec.split("for package in (", 1)[1].split(
        "):\n    try:\n        hiddenimports += collect_submodules(package)",
        1,
    )[0]
    assert '"onnxruntime"' not in collect_submodules_packages
    assert "copy_metadata" in spec
    assert 'copy_metadata("pipecat-ai")' in spec
    assert 'copy_metadata("onnx-asr")' in spec
    assert 'copy_metadata("sherpa-onnx")' not in spec
    assert 'collect_data_files(\n        "onnx_asr",' in spec
    assert '"preprocessors/*.onnx"' in spec
    assert '"preprocessors/*.py"' in spec
    assert "collect_data_files(" in spec
    assert '"onnxruntime",' in spec
    assert "includes=[" in spec
    assert "ThirdPartyNotices.txt" in spec
    assert '"onnxruntime",' not in spec.split("excludes=[", 1)[1]
    assert '"onnx",' in spec
    assert '"numba",' in spec
    assert '"llvmlite",' in spec
    assert '"scipy",' in spec.split("excludes=[", 1)[1]
    assert '"tzdata",' in spec.split("excludes=[", 1)[1]
    assert 'exclude_datas(datas, ("tzdata",))' in spec
    assert '"PIL.AvifImagePlugin",' in spec.split("excludes=[", 1)[1]
    assert '"PIL._avif",' in spec.split("excludes=[", 1)[1]
    hiddenimports_block = spec.split("hiddenimports = [", 1)[1].split("]", 1)[0]
    assert '"PySide6.QtCore"' not in hiddenimports_block
    assert '"PySide6",' in spec.split("excludes=[", 1)[1]
    assert '"tkinter",' in spec.split("excludes=[", 1)[1]
    assert '"customtkinter",' in spec.split("excludes=[", 1)[1]
    assert '"pystray",' in spec.split("excludes=[", 1)[1]
    assert '"google.generativeai",' in spec.split("excludes=[", 1)[1]
    assert '"google.cloud.texttospeech",' in spec.split("excludes=[", 1)[1]
    assert '"boto3",' in spec.split("excludes=[", 1)[1]
    assert '"botocore",' in spec.split("excludes=[", 1)[1]
    assert '"s3transfer",' in spec.split("excludes=[", 1)[1]
    assert '"pipecat.services.aws",' in spec.split("excludes=[", 1)[1]
    assert 'exclude_datas(datas, ("pipecat/services/aws",))' in spec
    assert "_internal\\onnxruntime" in build_script
    assert "_internal\\onnxruntime\\capi" in build_script
    assert "_internal\\scipy" not in build_script


def test_local_stt_services_do_not_override_pipecat_settings_object():
    repo_root = Path(__file__).resolve().parents[1]
    onnx_service = (repo_root / "src" / "onnx_local_service.py").read_text(encoding="utf-8")

    assert "self._local_settings" in onnx_service
    assert "self._settings = {" not in onnx_service


def test_backend_runtime_import_check_reports_missing_modules():
    def fake_import(module_name: str) -> object:
        if module_name == "missing_module":
            raise ModuleNotFoundError("No module named 'missing_module'")
        return object()

    missing = check_imports(
        [
            ("present_module", "test present"),
            ("missing_module", "test missing"),
        ],
        import_module=fake_import,
    )

    assert missing == [
        {
            "module": "missing_module",
            "reason": "test missing",
            "error": "ModuleNotFoundError: No module named 'missing_module'",
        }
    ]
