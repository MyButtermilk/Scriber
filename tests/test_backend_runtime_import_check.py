from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts import check_backend_runtime_imports as runtime_import_checks
from scripts.check_backend_runtime_imports import (
    REQUIRED_IMPORTS,
    REQUIRED_PACKAGE_VERSIONS,
    SENTENCE_SEGMENTATION_PROBES,
    check_imports,
    check_package_versions,
    check_runtime_requirements,
    check_sentence_segmentation,
)
from backend_runtime.contract import RUNTIME_CONTRACT_REVISION, RUNTIME_REQUIRED_IMPORTS


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
    assert "yt_dlp" in required_modules
    assert "yt_dlp_ejs" in required_modules
    assert "sherpa_onnx" not in required_modules
    assert "pipecat.audio.vad.silero" in required_modules
    assert "pipecat.pipeline.pipeline" in required_modules
    assert "pipecat.pipeline.task" in required_modules
    assert "pipecat.pipeline.runner" in required_modules
    assert "pipecat.services.stt_service" in required_modules
    assert "pipecat.transcriptions.language" in required_modules
    assert "pipecat.transports.base_input" in required_modules
    assert "pipecat.processors.audio.vad_processor" in required_modules
    assert "pipecat.turns.user_turn_processor" in required_modules
    assert "pipecat.turns.user_turn_strategies" in required_modules
    assert "pipecat.audio.turn.smart_turn.local_smart_turn_v3" in required_modules
    assert "pipecat.processors.user_idle_processor" not in required_modules
    assert "src.web_api" in required_modules
    assert "pipecat.services.soniox.stt" in required_modules
    assert "pipecat.services.assemblyai.stt" in required_modules
    assert "pipecat.services.deepgram.stt" in required_modules
    assert "pipecat.services.google.stt" in required_modules
    assert "pipecat.services.speechmatics.stt" in required_modules
    assert "src.azure_mai_stt" in required_modules
    assert "src.microphone" in required_modules
    assert "src.audio_file_input" in required_modules
    assert "src.pipeline" in required_modules
    assert ("pipecat-ai", "1.5.0") in REQUIRED_PACKAGE_VERSIONS
    assert ("yt-dlp", "2026.7.4") in REQUIRED_PACKAGE_VERSIONS
    assert ("yt-dlp-ejs", "0.8.0") in REQUIRED_PACKAGE_VERSIONS


def test_frozen_runtime_contract_covers_direct_pipecat_pipeline_imports():
    frozen_modules = {module for module, _reason in RUNTIME_REQUIRED_IMPORTS}

    assert RUNTIME_CONTRACT_REVISION == 4
    assert {
        "pipecat.pipeline.pipeline",
        "pipecat.pipeline.task",
        "pipecat.pipeline.runner",
        "pipecat.processors.frame_processor",
        "pipecat.services.ai_service",
        "pipecat.services.settings",
        "pipecat.services.stt_service",
        "pipecat.transcriptions.language",
        "pipecat.transports.base_input",
        "pipecat.transports.base_transport",
        "pipecat.utils.time",
        "pipecat.audio.vad.vad_analyzer",
        "pipecat.turns.user_start",
        "pipecat.turns.user_stop",
    } <= frozen_modules


def test_runtime_build_and_cache_validators_read_the_contract_revision_from_source():
    repo_root = Path(__file__).resolve().parents[1]
    scripts = (
        repo_root / "scripts" / "build_tauri_backend_sidecar.ps1",
        repo_root / "scripts" / "ci" / "validate_backend_runtime_cache.ps1",
        repo_root / "scripts" / "ci" / "validate_backend_sidecar_cache.ps1",
    )

    for script in scripts:
        content = script.read_text(encoding="utf-8")
        assert "RUNTIME_CONTRACT_REVISION" in content
        assert "runtimeContract.revision -eq 1" not in content
        assert "runtimeContract.revision -ne 1" not in content


def test_backend_spec_removes_only_the_duplicate_punkt_tab_archive():
    spec = (
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "scriber-backend.spec"
    ).read_text(encoding="utf-8")
    exact_filter = '''a.datas = [
    entry
    for entry in a.datas
    if str(entry[0]).replace("\\\\", "/")
    != "nltk_data/tokenizers/punkt_tab.zip"
]'''

    assert spec.count(exact_filter) == 1
    assert spec.count('"nltk_data/tokenizers/punkt_tab.zip"') == 1
    assert spec.index("a = Analysis(") < spec.index(exact_filter) < spec.index(
        "pyz = PYZ(a.pure)"
    )


def test_backend_runtime_import_check_rejects_stale_pipecat():
    mismatches = check_package_versions(
        requirements=(("pipecat-ai", "1.5.0"),),
        version_for=lambda _package: "0.0.95",
    )

    assert mismatches == [
        {
            "module": "distribution:pipecat-ai",
            "reason": "required package version 1.5.0",
            "error": "VersionMismatch: installed 0.0.95",
        }
    ]


def test_backend_runtime_import_check_exercises_sentence_segmentation():
    assert {label for label, _text, _expected in SENTENCE_SEGMENTATION_PROBES} == {
        "english_abbreviation",
        "decimal",
        "german_abbreviation",
        "cjk_punctuation",
        "arabic_punctuation",
    }
    assert check_sentence_segmentation(frozen=False) == []


def test_frozen_sentence_check_uses_only_bundled_nltk_data(tmp_path):
    bundled_nltk_data = tmp_path / "nltk_data"
    bundled_nltk_data.mkdir()
    original_download_calls: list[str] = []

    def original_download(name: str) -> bool:
        original_download_calls.append(name)
        return True

    fake_nltk = SimpleNamespace(
        data=SimpleNamespace(path=["user-cache", "developer-cache"]),
        download=original_download,
    )
    expected_ends = {
        text: len(expected)
        for _label, text, expected in SENTENCE_SEGMENTATION_PROBES
    }
    observed_paths: list[list[str]] = []

    def fake_import(module_name: str) -> object:
        if module_name == "nltk":
            return fake_nltk
        if module_name == "pipecat.utils.string":
            observed_paths.append(list(fake_nltk.data.path))
            assert fake_nltk.download is not original_download
            return SimpleNamespace(
                match_endofsentence=lambda text: expected_ends[text]
            )
        raise AssertionError(f"unexpected import: {module_name}")

    assert (
        check_sentence_segmentation(
            import_module=fake_import,
            frozen=True,
            frozen_root=tmp_path,
        )
        == []
    )
    assert observed_paths == [[str(bundled_nltk_data)]]
    assert fake_nltk.data.path == [str(bundled_nltk_data)]
    assert fake_nltk.download is original_download
    assert original_download_calls == []


def test_frozen_sentence_check_rejects_download_fallback(tmp_path):
    bundled_nltk_data = tmp_path / "nltk_data"
    bundled_nltk_data.mkdir()
    original_download_calls: list[str] = []

    def original_download(name: str) -> bool:
        original_download_calls.append(name)
        return True

    fake_nltk = SimpleNamespace(
        data=SimpleNamespace(path=["user-cache"]),
        download=original_download,
    )

    def fake_import(module_name: str) -> object:
        if module_name == "nltk":
            return fake_nltk
        if module_name == "pipecat.utils.string":
            fake_nltk.download("punkt_tab")
        raise AssertionError(f"unexpected import: {module_name}")

    failures = check_sentence_segmentation(
        import_module=fake_import,
        frozen=True,
        frozen_root=tmp_path,
    )

    assert failures == [
        {
            "module": "pipecat.utils.string.match_endofsentence",
            "reason": "bundled NLTK/Pipecat sentence segmentation runtime",
            "error": "RuntimeError: sentence segmentation probe failed",
        }
    ]
    assert fake_nltk.data.path == [str(bundled_nltk_data)]
    assert fake_nltk.download is original_download
    assert original_download_calls == []


def test_runtime_requirements_stop_before_pipecat_imports_when_sentence_gate_fails(
    monkeypatch,
):
    failure = {
        "module": "pipecat.utils.string.match_endofsentence",
        "reason": "bundled NLTK/Pipecat sentence segmentation runtime",
        "error": "LookupError: sentence segmentation probe failed",
    }
    monkeypatch.setattr(
        runtime_import_checks,
        "check_sentence_segmentation",
        lambda: [failure],
    )
    monkeypatch.setattr(
        runtime_import_checks,
        "check_imports",
        lambda: (_ for _ in ()).throw(AssertionError("imports must not run")),
    )
    monkeypatch.setattr(
        runtime_import_checks,
        "check_package_versions",
        lambda: [],
    )

    assert check_runtime_requirements() == [failure]


def test_standard_requirements_include_audio_runtime_dependencies():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements-base.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "scipy" not in requirements
    assert "onnxruntime" in requirements
    assert "onnx-asr[cpu,hub]>=0.10.2,<0.11" in requirements
    assert "sherpa-onnx==1.13.4" not in requirements
    assert "pipecat-ai[silero]==1.5.0" in requirements
    assert "yt-dlp[default,deno]==2026.7.4" in requirements
    assert "deno==2.9.2" in requirements
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

    local_requirements = (
        Path(__file__).resolve().parents[1] / "requirements-local-asr.txt"
    ).read_text(encoding="utf-8").splitlines()
    assert "onnx-asr[cpu,hub]>=0.10.2,<0.11" in local_requirements
    assert all("pipecat-ai" not in line for line in local_requirements)


def test_pipeline_uses_pipecat_1_5_smart_turn_import_without_removed_processor():
    pipeline_source = (
        Path(__file__).resolve().parents[1] / "src" / "pipeline.py"
    ).read_text(encoding="utf-8")

    assert "local_smart_turn_v3 import LocalSmartTurnAnalyzerV3" in pipeline_source
    assert "UserIdleProcessor" not in pipeline_source
    assert "pipecat.audio.streams.input" not in pipeline_source


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
    assert "Invoke-FrozenBackendRuntimeLayerCheck" in build_script
    assert "--runtime-import-check" in build_script
    assert "--runtime-layer-check" in build_script
    assert 'repo_root / "backend_runtime" / "launcher.py"' in spec
    assert "scripts.check_backend_runtime_imports" not in spec


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
    assert 'copy_metadata("yt-dlp")' in spec
    assert 'copy_metadata("yt-dlp-ejs")' in spec
    assert '"yt_dlp_ejs"' in spec
    assert 'copy_metadata("sherpa-onnx")' not in spec
    assert 'collect_data_files(\n        "onnx_asr",' in spec
    assert '"preprocessors/*.onnx"' in spec
    assert '"preprocessors/*.py"' in spec
    assert "collect_data_files(" in spec
    assert '"onnxruntime",' in spec
    hidden_imports = spec.split("hiddenimports += [", 1)[1].split(
        "]\n\nfor package in (", 1
    )[0]
    excluded_imports = spec.split("excludes=[", 1)[1].split(
        "],\n    noarchive", 1
    )[0]
    smart_turn_module = '"pipecat.audio.turn.smart_turn.local_smart_turn_v3"'
    assert smart_turn_module in hidden_imports
    assert smart_turn_module not in excluded_imports
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
    hiddenimports_block = spec.split("hiddenimports += [", 1)[1].split("]", 1)[0]
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
    assert 'Resolve-PythonInstalledTool -Names @("deno.exe", "deno")' in build_script
    assert "$pythonCommand = Get-Command $Python -ErrorAction SilentlyContinue" in build_script
    assert "if ($pythonDir)" in build_script
    runtime_manifest_source = build_script.split(
        "function Get-BackendRuntimeInputManifest", 1
    )[1].split("function Get-BackendApplicationInputManifest", 1)[0]
    assert 'Resolve-PythonInstalledTool -Names @("deno.exe", "deno")' not in runtime_manifest_source
    assert 'Resolve-MediaTool -Names @("yt-dlp.exe", "yt-dlp")' not in runtime_manifest_source
    assert '"requirements-base.txt"' in runtime_manifest_source
    assert "Get-PythonFileEntries" in runtime_manifest_source
    sidecar_manifest_source = build_script.split(
        "function Get-SidecarInputManifest", 1
    )[1].split("function Get-BackendRuntimeFileIdentityEntries", 1)[0]
    assert 'Get-ToolMetadataEntry -Path $resolvedDeno -Name "deno"' in sidecar_manifest_source
    assert 'Get-ToolMetadataEntry -Path $resolvedYtDlp -Name "yt-dlp"' in sidecar_manifest_source
    assert '$ErrorActionPreference = "Continue"' in build_script
    assert '& $Python -c "import PyInstaller" *> $null' in build_script
    assert 'Test-MediaToolExecutable -Path $copiedDeno -Name "deno"' in build_script


def test_backend_media_resolution_supports_fresh_full_and_runtime_cache_hits():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (
        repo_root / "scripts" / "build_tauri_backend_sidecar.ps1"
    ).read_text(encoding="utf-8")
    resolver = build_script.split(
        "function Resolve-BackendStableMediaTool", 1
    )[1].split("function Initialize-BackendRuntimeStableMediaTools", 1)[0]

    assert "Resolve-BackendSidecarCandidateMediaTool" in resolver
    assert resolver.index("Resolve-BackendSidecarCandidateMediaTool") < resolver.index(
        'Join-Path $RuntimeCacheRoot "media-tools"'
    )
    assert resolver.index('Join-Path $RuntimeCacheRoot "media-tools"') < resolver.index(
        "Resolve-PythonInstalledTool"
    )
    assert '[string]$manifest.cacheKey -ne $ExpectedRuntimeCacheKey' in resolver
    assert "Test-BackendStableMediaFiles" in resolver
    candidate = build_script.split(
        "function Resolve-BackendSidecarCandidateMediaTool", 1
    )[1].split("function Resolve-BackendStableMediaTool", 1)[0]
    assert '[string]$manifest.inputManifest.runtimeCacheKey -ne $ExpectedRuntimeCacheKey' in candidate
    assert "Test-BackendMediaFiles" in candidate
    assert "Full-sidecar cache generations disagree" in candidate


def test_local_stt_services_do_not_override_pipecat_settings_object():
    repo_root = Path(__file__).resolve().parents[1]
    onnx_service = (repo_root / "src" / "onnx_local_service.py").read_text(encoding="utf-8")

    assert "self._local_settings" in onnx_service
    assert "self._settings = {" not in onnx_service
    assert "AIService.process_frame(self, frame, direction)" in onnx_service
    assert "frame.delta" in onnx_service
    assert "frame.settings" not in onnx_service
    assert "super(STTService, self)" not in onnx_service


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
