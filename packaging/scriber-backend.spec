# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

repo_root = Path(os.environ.get("SCRIBER_REPO_ROOT", Path.cwd())).resolve()

hiddenimports = [
    "src.assemblyai_async_stt",
    "src.azure_mai_stt",
    "src.mistral_stt",
    "src.smallest_stt",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pipecat.services.google.stt",
    "pipecat.services.assemblyai.stt",
    "pipecat.services.deepgram.stt",
    "pipecat.services.openai.stt",
    "pipecat.services.azure.stt",
    "pipecat.services.gladia.stt",
    "pipecat.services.groq.stt",
    "pipecat.services.speechmatics.stt",
    "pipecat.services.aws.stt",
    "pipecat.services.elevenlabs.stt",
]

for package in (
    "sounddevice",
    "pycaw",
    "keyboard",
    "pyautogui",
    "PIL",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

datas = []
assets_dir = repo_root / "src" / "assets"
if assets_dir.exists():
    datas.append((str(assets_dir), "src/assets"))

frontend_dist = repo_root / "Frontend" / "dist" / "public"
if frontend_dist.exists():
    datas.append((str(frontend_dist), "Frontend/dist/public"))

for package in ("pipecat", "google"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

a = Analysis(
    [str(repo_root / "src" / "backend_worker.py")],
    pathex=[str(repo_root)],
    binaries=[],
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
        "torch",
        "torchaudio",
        "torchvision",
        "transformers",
        "datasets",
        "pyarrow",
        "fsspec",
        "nltk",
        "sqlalchemy",
        "onnxruntime",
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
