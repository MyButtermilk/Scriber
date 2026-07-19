# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

repo_root = Path(os.environ.get("SCRIBER_REPO_ROOT", Path.cwd())).resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend_runtime.contract import RUNTIME_REQUIRED_IMPORTS


def exclude_datas(datas, excluded_destination_prefixes):
    excluded = tuple(prefix.replace("\\", "/").rstrip("/") + "/" for prefix in excluded_destination_prefixes)
    filtered = []
    for source, destination in datas:
        normalized_destination = str(destination).replace("\\", "/").rstrip("/") + "/"
        if normalized_destination.startswith(excluded):
            continue
        filtered.append((source, destination))
    return filtered


hiddenimports = [module for module, _reason in RUNTIME_REQUIRED_IMPORTS]
hiddenimports += [
    "pyloudnorm",
    "pyloudnorm.meter",
    "pyloudnorm.normalize",
    "pyloudnorm.util",
    "onnx_asr",
    "onnxruntime",
    "pipecat.audio.vad.silero",
    "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
    "yt_dlp",
    "yt_dlp_ejs",
    "pipecat.services.soniox.stt",
    "pipecat.services.assemblyai.stt",
    "pipecat.services.google.stt",
    "pipecat.services.deepgram.stt",
    "pipecat.services.openai.stt",
    "pipecat.services.gladia.stt",
    "pipecat.services.groq.stt",
    "pipecat.services.speechmatics.stt",
    "pipecat.services.elevenlabs.stt",
    "huggingface_hub.file_download",
    "huggingface_hub.utils.tqdm",
]

for package in (
    "sounddevice",
    "pycaw",
    "keyboard",
    "pyautogui",
    "PIL",
    "yt_dlp",
    "yt_dlp_ejs",
    "huggingface_hub",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

def collect_required_dynamic_libs(package):
    libs = collect_dynamic_libs(package)
    if not libs:
        raise RuntimeError(f"No dynamic libraries collected for required package: {package}")
    return libs


binaries = collect_required_dynamic_libs("onnxruntime")

datas = []
datas += copy_metadata("pipecat-ai")
datas += copy_metadata("onnx-asr")
datas += copy_metadata("huggingface-hub")
datas += copy_metadata("yt-dlp")
datas += copy_metadata("yt-dlp-ejs")

for package in ("pipecat", "yt_dlp", "yt_dlp_ejs"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

datas = exclude_datas(datas, ("pipecat/services/aws",))

try:
    # onnx-asr loads small package-local preprocessor ONNX files at runtime
    # for models such as Parakeet TDT. Model weights stay in the user cache.
    datas += collect_data_files(
        "onnx_asr",
        includes=[
            "preprocessors/*.onnx",
            "preprocessors/*.py",
        ],
    )
except Exception:
    pass

try:
    # ONNXRuntime runtime DLLs are handled by collect_dynamic_libs(). Keep legal
    # notices, but avoid bundling sample models and mobile-helper docs.
    datas += collect_data_files(
        "onnxruntime",
        includes=["LICENSE", "Privacy.md", "ThirdPartyNotices.txt"],
    )
except Exception:
    pass

datas = exclude_datas(datas, ("tzdata",))

a = Analysis(
    [str(repo_root / "backend_runtime" / "launcher.py")],
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
        "sqlalchemy",
        "tzdata",
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
        "googleapiclient",
        "google_auth_httplib2",
        "httplib2",
        "aioboto3",
        "aiobotocore",
        "boto3",
        "botocore",
        "s3transfer",
        "pipecat.services.aws",
        # First-party application modules are staged as a checksummed physical
        # overlay after the stable frozen runtime has been restored or built.
        "src",
        "scripts",
        "nemo",
        "nemo_toolkit",
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
    upx=False,
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
