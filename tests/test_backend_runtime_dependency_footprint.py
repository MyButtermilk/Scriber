from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.analyze_backend_runtime_dependencies import build_report


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_fake_sidecar(root: Path) -> Path:
    internal = root / "_internal"
    internal_files = {
        "onnxruntime/capi/onnxruntime.dll": b"o" * 4096,
        "onnxruntime/capi/onnxruntime_pybind11_state.pyd": b"p" * 4096,
        "google/__init__.py": b"",
        "grpc/_cython/cygrpc.pyd": b"r" * 1024,
        "PIL/Image.py": b"i" * 1024,
        "PIL/ImageDraw.py": b"d" * 1024,
        "azure/cognitiveservices/speech/Microsoft.CognitiveServices.Speech.core.dll": b"a" * 1024,
    }
    sidecar_files = {
        "tools/ffmpeg/ffmpeg.exe": b"f" * 20480,
        "tools/ffmpeg/ffprobe.exe": b"p" * 20480,
    }
    for relative, content in internal_files.items():
        path = internal / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for relative, content in sidecar_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def test_runtime_dependency_footprint_reports_required_groups(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")

    report = build_report(sidecar)

    assert report["apiVersion"] == "1"
    assert report["ok"] is True
    assert report["summary"]["totalMb"] > 0
    assert report["summary"]["missingRequiredPaths"] == []
    assert report["summary"]["componentMissingRequiredPaths"] == []
    assert set(report["dependencies"]) == {
        "scipy",
        "onnxruntime",
        "awsSdk",
        "pythonGuiRuntime",
        "unusedProviderSdks",
    }
    assert set(report["components"]) == {
        "backend",
        "internal",
        "mediaTools",
        "azureSpeech",
        "pyside6",
        "pythonGuiRuntime",
        "googleGrpc",
        "pillow",
    }
    assert report["dependencies"]["scipy"]["expectedPresent"] is False
    assert report["dependencies"]["scipy"]["totalMb"] == 0
    assert report["dependencies"]["scipy"]["missingRequiredPaths"] == []
    assert report["dependencies"]["awsSdk"]["expectedPresent"] is False
    assert report["dependencies"]["awsSdk"]["totalMb"] == 0
    assert report["dependencies"]["awsSdk"]["missingRequiredPaths"] == []
    assert report["dependencies"]["pythonGuiRuntime"]["expectedPresent"] is False
    assert report["dependencies"]["pythonGuiRuntime"]["totalMb"] == 0
    assert report["dependencies"]["pythonGuiRuntime"]["missingRequiredPaths"] == []
    assert report["dependencies"]["unusedProviderSdks"]["expectedPresent"] is False
    assert report["dependencies"]["unusedProviderSdks"]["totalMb"] == 0
    assert report["dependencies"]["unusedProviderSdks"]["missingRequiredPaths"] == []
    assert report["dependencies"]["onnxruntime"]["missingRequiredPaths"] == []
    assert report["dependencies"]["onnxruntime"]["topFiles"][0]["path"].startswith(
        "onnxruntime"
    )
    assert report["components"]["mediaTools"]["missingRequiredPaths"] == []
    assert report["components"]["azureSpeech"]["missingRequiredPaths"] == []
    assert report["components"]["pyside6"]["missingRequiredPaths"] == []
    assert report["components"]["pyside6"]["disallowedPaths"] == []
    assert report["components"]["pythonGuiRuntime"]["missingRequiredPaths"] == []
    assert report["components"]["pythonGuiRuntime"]["disallowedPaths"] == []
    assert report["components"]["googleGrpc"]["missingRequiredPaths"] == []
    assert report["components"]["pillow"]["missingRequiredPaths"] == []
    assert report["components"]["pillow"]["disallowedPaths"] == []


def test_runtime_dependency_footprint_fails_when_budget_is_exceeded(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")

    report = build_report(sidecar, max_total_mb=0.001)

    assert report["ok"] is False
    assert "total" in report["summary"]["budgetFailures"]


def test_runtime_dependency_footprint_fails_when_component_budget_is_exceeded(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")

    report = build_report(sidecar, max_media_tools_mb=0.001)

    assert report["ok"] is False
    assert "mediaTools" in report["summary"]["componentBudgetFailures"]


def test_runtime_dependency_footprint_fails_when_standard_component_is_missing(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    (sidecar / "tools" / "ffmpeg" / "ffprobe.exe").unlink()
    (sidecar / "_internal" / "azure" / "cognitiveservices" / "speech" / "Microsoft.CognitiveServices.Speech.core.dll").unlink()

    report = build_report(sidecar)

    assert report["ok"] is False
    assert "mediaTools:tools/ffmpeg/ffprobe.exe" in report["summary"]["componentMissingRequiredPaths"]
    assert (
        "azureSpeech:_internal/azure/cognitiveservices/speech/Microsoft.CognitiveServices.Speech.core.dll"
        in report["summary"]["componentMissingRequiredPaths"]
    )


def test_runtime_dependency_footprint_fails_when_unused_pillow_avif_binary_is_bundled(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    avif = sidecar / "_internal" / "PIL" / "_avif.cp313-win_amd64.pyd"
    avif.write_bytes(b"a" * 1024)

    report = build_report(sidecar)

    assert report["ok"] is False
    assert "pillow:_internal\\PIL\\_avif.cp313-win_amd64.pyd" in report["summary"]["componentDisallowedPaths"]


def test_runtime_dependency_footprint_fails_when_scipy_is_bundled(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    scipy_path = sidecar / "_internal" / "scipy" / "signal" / "_sigtools.pyd"
    scipy_path.parent.mkdir(parents=True, exist_ok=True)
    scipy_path.write_bytes(b"s" * 1024)

    report = build_report(sidecar)

    assert report["ok"] is False
    assert report["dependencies"]["scipy"]["unexpectedPresent"] is True
    assert "scipy" in report["summary"]["unexpectedPresentDependencies"]


def test_runtime_dependency_footprint_fails_when_aws_sdk_is_bundled(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    botocore_path = sidecar / "_internal" / "botocore" / "auth.py"
    botocore_path.parent.mkdir(parents=True, exist_ok=True)
    botocore_path.write_bytes(b"b" * 1024)

    report = build_report(sidecar)

    assert report["ok"] is False
    assert report["dependencies"]["awsSdk"]["unexpectedPresent"] is True
    assert "awsSdk" in report["summary"]["unexpectedPresentDependencies"]


def test_runtime_dependency_footprint_fails_when_legacy_gui_runtime_is_bundled(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    pyside = sidecar / "_internal" / "PySide6" / "QtWidgets.pyd"
    pyside.parent.mkdir(parents=True, exist_ok=True)
    pyside.write_bytes(b"q" * 1024)
    customtkinter = sidecar / "_internal" / "customtkinter" / "__init__.py"
    customtkinter.parent.mkdir(parents=True, exist_ok=True)
    customtkinter.write_bytes(b"")

    report = build_report(sidecar)

    assert report["ok"] is False
    assert report["dependencies"]["pythonGuiRuntime"]["unexpectedPresent"] is True
    assert "pythonGuiRuntime" in report["summary"]["unexpectedPresentDependencies"]
    assert "pyside6:_internal\\PySide6" in report["summary"]["componentDisallowedPaths"]
    assert (
        "pythonGuiRuntime:_internal\\customtkinter"
        in report["summary"]["componentDisallowedPaths"]
    )


def test_runtime_dependency_footprint_fails_when_unused_provider_sdk_is_bundled(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    gemini = sidecar / "_internal" / "google" / "generativeai" / "__init__.py"
    gemini.parent.mkdir(parents=True, exist_ok=True)
    gemini.write_bytes(b"g")

    report = build_report(sidecar)

    assert report["ok"] is False
    assert report["dependencies"]["unusedProviderSdks"]["unexpectedPresent"] is True
    assert "unusedProviderSdks" in report["summary"]["unexpectedPresentDependencies"]
    assert "unusedProviderSdks:google\\generativeai" in report["summary"]["disallowedPaths"]


def test_runtime_dependency_footprint_fails_on_prunable_onnxruntime_data(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    sample_model = sidecar / "_internal" / "onnxruntime" / "datasets" / "sample.onnx"
    sample_model.parent.mkdir(parents=True, exist_ok=True)
    sample_model.write_bytes(b"model")

    report = build_report(sidecar)

    assert report["ok"] is False
    assert report["summary"]["disallowedPaths"] == ["onnxruntime:onnxruntime\\datasets"]


def test_runtime_dependency_footprint_cli_writes_report(tmp_path: Path) -> None:
    sidecar = write_fake_sidecar(tmp_path / "scriber-backend")
    output = tmp_path / "runtime-dependency-footprint.json"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/analyze_backend_runtime_dependencies.py",
            "--sidecar-dir",
            str(sidecar),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    stdout = json.loads(completed.stdout)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert stdout["ok"] is True
    assert stdout["output"] == str(output.resolve())
    assert written["ok"] is True


def test_windows_build_and_release_workflow_can_emit_runtime_dependency_footprint() -> None:
    build = (REPO_ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "release-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "[switch]$RunRuntimeDependencyFootprint" in build
    assert "[double]$MaxScipyRuntimeDependencyMB = 0" in build
    assert "[double]$MaxOnnxRuntimeDependencyMB = 0" in build
    assert "[double]$MaxPythonRuntimeDependencyMB = 0" in build
    assert "[double]$MaxBackendRuntimeDependencyMB = 0" in build
    assert "[double]$MaxInternalRuntimeDependencyMB = 0" in build
    assert "[double]$MaxMediaToolsRuntimeDependencyMB = 0" in build
    assert "[double]$MaxPySide6RuntimeDependencyMB = 0" in build
    assert "[double]$MaxGoogleGrpcRuntimeDependencyMB = 0" in build
    assert "[double]$MaxPillowRuntimeDependencyMB = 0" in build
    assert "[switch]$FastLocalInstaller" in build
    assert "scripts\\analyze_backend_runtime_dependencies.py" in build
    assert "runtime-dependency-footprint.json" in build
    assert "--max-scipy-mb" in build
    assert "--max-onnxruntime-mb" in build
    assert "--max-total-mb" in build
    assert "--max-backend-mb" in build
    assert "--max-internal-mb" in build
    assert "--max-media-tools-mb" in build
    assert "--max-pyside6-mb" in build
    assert "--max-google-grpc-mb" in build
    assert "--max-pillow-mb" in build
    assert "if ($FastLocalInstaller)" in build
    assert "PrunePySide6" not in build
    assert "$MaxPySide6RuntimeDependencyMB = 65" not in build
    assert "$MaxPillowRuntimeDependencyMB = 6" in build
    assert "$runtimeDependencyFootprint[\"path\"] = $runtimeDependencyFootprintPath" in build
    assert "runtimeDependencyFootprint = $runtimeDependencyFootprint" in build

    assert '"-RunRuntimeDependencyFootprint"' in workflow
    assert "PrunePySide6" not in workflow
    assert '"-MaxScipyRuntimeDependencyMB"' in workflow
    assert '"-MaxOnnxRuntimeDependencyMB"' in workflow
    assert '"-MaxPythonRuntimeDependencyMB"' in workflow
    assert '"-MaxBackendRuntimeDependencyMB"' in workflow
    assert '"-MaxInternalRuntimeDependencyMB"' in workflow
    assert '"-MaxMediaToolsRuntimeDependencyMB"' in workflow
    assert '"-MaxPySide6RuntimeDependencyMB"' not in workflow
    assert '"-MaxGoogleGrpcRuntimeDependencyMB"' in workflow
    assert '"-MaxPillowRuntimeDependencyMB"' in workflow
