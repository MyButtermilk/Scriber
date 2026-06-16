# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

repo_root = Path(os.environ.get("SCRIBER_REPO_ROOT", Path.cwd())).resolve()


def exclude_datas(datas, excluded_destination_prefixes):
    excluded = tuple(prefix.replace("\\", "/").rstrip("/") + "/" for prefix in excluded_destination_prefixes)
    filtered = []
    for source, destination in datas:
        normalized_destination = str(destination).replace("\\", "/").rstrip("/") + "/"
        if normalized_destination.startswith(excluded):
            continue
        filtered.append((source, destination))
    return filtered


hiddenimports = [
    "src.assemblyai_async_stt",
    "src.azure_mai_stt",
    "src.mistral_stt",
    "src.smallest_stt",
    "scripts.check_backend_runtime_imports",
    "pyloudnorm",
    "pyloudnorm.meter",
    "pyloudnorm.normalize",
    "pyloudnorm.util",
    "onnxruntime",
    "pipecat.audio.vad.silero",
    "yt_dlp",
    "pipecat.services.soniox.stt",
    "pipecat.services.google.stt",
    "pipecat.services.deepgram.stt",
    "pipecat.services.openai.stt",
    "pipecat.services.azure.stt",
    "pipecat.services.gladia.stt",
    "pipecat.services.groq.stt",
    "pipecat.services.speechmatics.stt",
    "pipecat.services.elevenlabs.stt",
]

for package in (
    "sounddevice",
    "pycaw",
    "keyboard",
    "pyautogui",
    "PIL",
    "yt_dlp",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

binaries = []
for package in ("onnxruntime",):
    try:
        binaries += collect_dynamic_libs(package)
    except Exception:
        pass

datas = []
assets_dir = repo_root / "src" / "assets"
if assets_dir.exists():
    datas.append((str(assets_dir), "src/assets"))

for package in ("pipecat", "yt_dlp"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

datas = exclude_datas(datas, ("pipecat/services/aws",))

try:
    # ONNXRuntime runtime DLLs are handled by collect_dynamic_libs(). Keep legal
    # notices, but avoid bundling sample models and mobile-helper docs.
    datas += collect_data_files(
        "onnxruntime",
        includes=["LICENSE", "Privacy.md", "ThirdPartyNotices.txt"],
    )
except Exception:
    pass

a = Analysis(
    [str(repo_root / "src" / "backend_worker.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "_pytest",
        "IPython",
        "jedi",
        "parso",
        "matplotlib",
        "pandas",
        "sklearn",
        "scipy",
        "torch",
        "torchaudio",
        "torchvision",
        "transformers",
        "datasets",
        "pyarrow",
        "fsspec",
        "nltk",
        "sqlalchemy",
        "onnx",
        "numba",
        "llvmlite",
        "onnxruntime.quantization",
        "onnxruntime.tools",
        "onnxruntime.transformers",
        "PIL.AvifImagePlugin",
        "PIL._avif",
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "tkinter",
        "_tkinter",
        "tcl",
        "tk",
        "customtkinter",
        "pystray",
        "google.generativeai",
        "google.ai.generativelanguage",
        "google.cloud.texttospeech",
        "google.genai",
        "googleapiclient",
        "google_auth_httplib2",
        "httplib2",
        "groq",
        "aioboto3",
        "aiobotocore",
        "boto3",
        "botocore",
        "s3transfer",
        "pipecat.services.aws",
        "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
        "nemo",
        "nemo_toolkit",
        "onnx_asr",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="scriber-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="scriber-backend",
)
