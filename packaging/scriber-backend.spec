# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import dis
import importlib.metadata
import importlib.util
import inspect
import os
import sys
from types import CodeType

repo_root = Path(os.environ.get("SCRIBER_REPO_ROOT", Path.cwd())).resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

numpy_version = "2.4.6+scriber.noblas.1"
expected_numpy_wheel = (
    repo_root
    / "packaging"
    / "wheels"
    / f"numpy-{numpy_version}-cp313-cp313-win_amd64.whl"
).resolve()
numpy_wheel_value = os.environ.get("SCRIBER_NUMPY_WHEEL_PATH")
numpy_overlay_value = os.environ.get("SCRIBER_NUMPY_OVERLAY_ROOT")
if not numpy_wheel_value or not numpy_overlay_value:
    raise RuntimeError(
        "PyInstaller requires the validated NumPy no-BLAS wheel and overlay inputs"
    )
numpy_wheel_path = Path(numpy_wheel_value).resolve()
numpy_overlay_root = Path(numpy_overlay_value).resolve()
if numpy_wheel_path != expected_numpy_wheel or not numpy_wheel_path.is_file():
    raise RuntimeError("PyInstaller NumPy wheel does not originate from the locked repository path")
if not numpy_overlay_root.is_dir():
    raise RuntimeError("PyInstaller NumPy overlay is missing")
expected_numpy_package = numpy_overlay_root / "numpy" / "__init__.py"
expected_numpy_dist_info = numpy_overlay_root / f"numpy-{numpy_version}.dist-info"
numpy_dist_info_dirs = sorted(
    path.name
    for path in numpy_overlay_root.glob("numpy-*.dist-info")
    if path.is_dir()
)
if (
    not expected_numpy_package.is_file()
    or not (expected_numpy_dist_info / "METADATA").is_file()
    or numpy_dist_info_dirs != [expected_numpy_dist_info.name]
):
    raise RuntimeError("PyInstaller NumPy overlay has unexpected package metadata")

# The source/build venv deliberately retains the public NumPy wheel. Only the
# PyInstaller child receives this extracted, locked product overlay.
sys.path.insert(0, str(numpy_overlay_root))
numpy_spec = importlib.util.find_spec("numpy")
if numpy_spec is None or not numpy_spec.origin:
    raise RuntimeError("PyInstaller cannot resolve NumPy from the product overlay")
numpy_origin = Path(numpy_spec.origin).resolve()
if not numpy_origin.is_relative_to(numpy_overlay_root):
    raise RuntimeError("PyInstaller resolved NumPy outside the validated product overlay")
numpy_distribution = importlib.metadata.distribution("numpy")
if numpy_distribution.version != numpy_version:
    raise RuntimeError(
        f"PyInstaller resolved NumPy {numpy_distribution.version}, expected {numpy_version}"
    )
numpy_distribution_root = Path(numpy_distribution.locate_file("")).resolve()
if not numpy_distribution_root.is_relative_to(numpy_overlay_root):
    raise RuntimeError("PyInstaller resolved NumPy metadata outside the validated product overlay")

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

stdlib_export_compat_root = repo_root / "packaging" / "stdlib_export_compat"
pyinstaller_hook_root = repo_root / "packaging" / "pyinstaller_hooks"

from backend_runtime.contract import RUNTIME_REQUIRED_IMPORTS
from backend_runtime.huggingface_hub_policy import (
    HUGGINGFACE_HUB_EXCLUDED_MODULES,
    HUGGINGFACE_HUB_REQUIRED_HIDDEN_IMPORTS,
)
from backend_runtime.yt_dlp_policy import partition_yt_dlp_modules


def exclude_datas(datas, excluded_destination_prefixes):
    excluded = tuple(prefix.replace("\\", "/").rstrip("/") + "/" for prefix in excluded_destination_prefixes)
    filtered = []
    for source, destination in datas:
        normalized_destination = str(destination).replace("\\", "/").rstrip("/") + "/"
        if normalized_destination.startswith(excluded):
            continue
        filtered.append((source, destination))
    return filtered


def exclude_pure_modules(pure, excluded_module_prefixes):
    excluded = tuple(str(prefix).rstrip(".") for prefix in excluded_module_prefixes)
    filtered = []
    for entry in pure:
        module_name = str(entry[0])
        if any(
            module_name == prefix or module_name.startswith(prefix + ".")
            for prefix in excluded
        ):
            continue
        filtered.append(entry)
    return filtered


DEEPGRAM_REQUIRED_MODULES = frozenset(
    {
        "deepgram",
        "deepgram._secure_logging",
        "deepgram.base_client",
        "deepgram.client",
        "deepgram.environment",
        "deepgram.transport",
        "deepgram.transport_interface",
        "deepgram.core",
        "deepgram.core.api_error",
        "deepgram.core.client_wrapper",
        "deepgram.core.datetime_utils",
        "deepgram.core.events",
        "deepgram.core.file",
        "deepgram.core.force_multipart",
        "deepgram.core.http_client",
        "deepgram.core.jsonable_encoder",
        "deepgram.core.logging",
        "deepgram.core.pydantic_utilities",
        "deepgram.core.query_encoder",
        "deepgram.core.remove_none_from_dict",
        "deepgram.core.request_options",
        "deepgram.core.serialization",
        "deepgram.core.unchecked_base_model",
        "deepgram.core.websocket_compat",
        "deepgram.listen",
        "deepgram.listen.client",
        "deepgram.listen.raw_client",
        "deepgram.listen.v1",
        "deepgram.listen.v1.client",
        "deepgram.listen.v1.raw_client",
        "deepgram.listen.v1.socket_client",
        "deepgram.listen.v1.types",
        "deepgram.types",
    }
)
DEEPGRAM_REQUIRED_PREFIXES = (
    "deepgram.listen.v1.types.",
    "deepgram.types.listen_v1",
)


def retain_deepgram_runtime_modules(pure):
    """Keep only the Deepgram listen.v1 graph used by Scriber."""

    filtered = []
    for entry in pure:
        module_name = str(entry[0])
        if not module_name.startswith("deepgram."):
            filtered.append(entry)
            continue
        if module_name in DEEPGRAM_REQUIRED_MODULES or any(
            module_name.startswith(prefix) for prefix in DEEPGRAM_REQUIRED_PREFIXES
        ):
            filtered.append(entry)
    return filtered


def strip_runtime_docstrings(code: CodeType) -> tuple[CodeType, int]:
    """Delete compiler-owned docstrings while preserving executable constants."""

    constants = list(code.co_consts)
    changed = False
    stripped_count = 0

    for index, value in enumerate(constants):
        if isinstance(value, CodeType):
            replacement, nested_count = strip_runtime_docstrings(value)
            if replacement is not value:
                constants[index] = replacement
                changed = True
            stripped_count += nested_count

    instructions = list(dis.get_instructions(code))
    constant_load_counts = {}
    for instruction in instructions:
        if (
            instruction.opname in {"LOAD_CONST", "RETURN_CONST"}
            and isinstance(instruction.arg, int)
        ):
            constant_load_counts[instruction.arg] = (
                constant_load_counts.get(instruction.arg, 0) + 1
            )

    doc_index = None
    if code.co_flags & inspect.CO_OPTIMIZED:
        if (
            constants
            and isinstance(constants[0], str)
            and constant_load_counts.get(0, 0) == 0
        ):
            doc_index = 0
    else:
        for position, instruction in enumerate(instructions):
            if instruction.opname != "STORE_NAME" or instruction.argval != "__doc__":
                continue
            if position == 0:
                break
            previous = instructions[position - 1]
            stores_before = [
                item.argval
                for item in instructions[:position]
                if item.opname == "STORE_NAME"
            ]
            compiler_prefix = (
                (code.co_name == "<module>" and not stores_before)
                or (
                    "__module__" in stores_before
                    and "__qualname__" in stores_before
                    and all(
                        name in {"__module__", "__qualname__", "__firstlineno__"}
                        for name in stores_before
                    )
                )
            )
            if (
                compiler_prefix
                and previous.opname == "LOAD_CONST"
                and isinstance(previous.arg, int)
                and previous.arg < len(constants)
                and isinstance(constants[previous.arg], str)
                and constant_load_counts.get(previous.arg, 0) == 1
            ):
                doc_index = previous.arg
            break

    if doc_index is not None:
        constants[doc_index] = None
        changed = True
        stripped_count += 1

    if not changed:
        return code, stripped_count
    return code.replace(co_consts=tuple(constants)), stripped_count


def retain_punkt_tab_languages(datas, retained_languages):
    prefix = "nltk_data/tokenizers/punkt_tab/"
    retained = frozenset(str(language).casefold() for language in retained_languages)
    filtered = []
    for entry in datas:
        destination = str(entry[0]).replace("\\", "/")
        if destination.startswith(prefix):
            relative = destination[len(prefix) :]
            path_parts = relative.split("/", 1)
            if len(path_parts) == 2 and path_parts[0].casefold() not in retained:
                continue
        filtered.append(entry)
    return filtered


hiddenimports = [module for module, _reason in RUNTIME_REQUIRED_IMPORTS]
hiddenimports += [
    "backend_runtime.docstring_prune_probe",
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
]
hiddenimports += list(HUGGINGFACE_HUB_REQUIRED_HIDDEN_IMPORTS)

# PDF and DOCX export use only Python's standard library. The frozen runtime's
# protected revision-4 import contract still names docx and reportlab.platypus,
# while Pipecat and PyAutoGUI import Pillow for video/screenshot paths that
# Scriber does not expose. Analysis resolves those names to tiny packaging-only
# compatibility modules. lxml, Pillow binaries, and the real document libraries
# and their data stay out of the installer.

yt_dlp_modules, excluded_yt_dlp_extractor_modules = partition_yt_dlp_modules(
    collect_submodules("yt_dlp")
)
hiddenimports += list(yt_dlp_modules)

for package in (
    "sounddevice",
    "pycaw",
    "keyboard",
    "pyautogui",
    "yt_dlp_ejs",
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
numpy_metadata_datas = copy_metadata("numpy")
if len(numpy_metadata_datas) != 1:
    raise RuntimeError("PyInstaller NumPy metadata collection is ambiguous")
numpy_metadata_source, numpy_metadata_destination = numpy_metadata_datas[0]
if (
    Path(numpy_metadata_source).resolve() != expected_numpy_dist_info
    or Path(numpy_metadata_destination).as_posix() != expected_numpy_dist_info.name
):
    raise RuntimeError("PyInstaller NumPy metadata does not originate from the product overlay")
datas += numpy_metadata_datas
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
    pathex=[str(numpy_overlay_root), str(stdlib_export_compat_root), str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(pyinstaller_hook_root)],
    hooksconfig={},
    runtime_hooks=[
        str(repo_root / "backend_runtime" / "pyinstaller_huggingface_runtime_hook.py"),
        str(repo_root / "backend_runtime" / "pyinstaller_yt_dlp_runtime_hook.py"),
    ],
    excludes=[
        *excluded_yt_dlp_extractor_modules,
        "lxml",
        *HUGGINGFACE_HUB_EXCLUDED_MODULES,
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

analysis_data_destinations = {
    str(entry[0]).replace("\\", "/")
    for entry in a.datas
}
numpy_metadata_prefix = expected_numpy_dist_info.name + "/"
if (
    numpy_metadata_prefix + "METADATA" not in analysis_data_destinations
    or not any(
        destination.startswith(numpy_metadata_prefix + "licenses/")
        for destination in analysis_data_destinations
    )
):
    raise RuntimeError("Frozen runtime is missing custom NumPy metadata or licenses")
if any(
    destination == "numpy-2.4.6.dist-info"
    or destination.startswith("numpy-2.4.6.dist-info/")
    for destination in analysis_data_destinations
):
    raise RuntimeError("Frozen runtime retained official NumPy metadata")

# PyInstaller's NLTK hook includes both the complete expanded punkt_tab tree
# and its redundant source archive. Scriber and Pipecat 1.5 use English sentence
# segmentation, while the complete German model is an explicit product/runtime
# contract. Keep both languages plus root metadata and drop every other language.
a.datas = [
    entry
    for entry in a.datas
    if str(entry[0]).replace("\\", "/")
    != "nltk_data/tokenizers/punkt_tab.zip"
]
a.datas = retain_punkt_tab_languages(a.datas, ("english", "german"))

# The setuptools hook also stages vendored metadata/text files underneath a
# setuptools directory. With the code removed, that directory would form an
# importable but non-functional namespace package and retain build-only bytes.
a.datas = [
    entry
    for entry in a.datas
    if not (
        str(entry[0]).replace("\\", "/") == "setuptools"
        or str(entry[0]).replace("\\", "/").startswith("setuptools/")
    )
]

# Analysis hooks can pull build tooling and package-local test helpers into the
# frozen PYZ even though no Scriber runtime path imports them. Keep the filter
# after Analysis so it also covers modules introduced by third-party hooks.
# Mutate the TOC in place to preserve PyInstaller's code-cache association with
# this exact list object.
a.pure[:] = exclude_pure_modules(
    a.pure,
    (
        "PyInstaller",
        "_distutils_hack",
        "altgraph",
        "keyboard._keyboard_tests",
        "keyboard._mouse_tests",
        "openai.types.realtime",
        "openai.resources.realtime",
        "openai.types.conversations",
        "openai.resources.conversations",
        "openai.types.webhooks",
        "openai.resources.webhooks",
        "openai.lib._realtime",
        "grpc._channel",
        "grpc._server",
        "grpc._interceptor",
        "grpc._utilities",
        "grpc._auth",
        "numpy.testing",
        "pefile",
        "pygments",
        "setuptools",
        "win32ctypes",
        "yt_dlp.__pyinstaller",
    ),
)
a.pure[:] = retain_deepgram_runtime_modules(a.pure)

# Python's optimize=2 also deletes assertions. Instead, rewrite only
# compiler-owned module/class/function docstrings in the retained PYZ code
# objects. The physical first-party application overlay remains untouched.
code_cache = CONF.get("code_cache", {}).get(id(a.pure))
if not isinstance(code_cache, dict):
    raise RuntimeError(
        "PyInstaller code cache is unavailable; refusing an unverified docstring prune"
    )

retained_pure_names = {str(entry[0]) for entry in a.pure}
missing_cached_code = [
    str(name)
    for name, source, _typecode in a.pure
    if source not in (None, "-")
    and not isinstance(code_cache.get(str(name)), CodeType)
]
if missing_cached_code:
    raise RuntimeError(
        "PyInstaller code cache is incomplete for retained modules: "
        + ", ".join(sorted(missing_cached_code)[:20])
    )

total_stripped_docstrings = 0
for module_name in sorted(retained_pure_names):
    cached_code = code_cache.get(module_name)
    if not isinstance(cached_code, CodeType):
        continue
    stripped_code, stripped_count = strip_runtime_docstrings(cached_code)
    code_cache[module_name] = stripped_code
    total_stripped_docstrings += stripped_count
if total_stripped_docstrings == 0:
    raise RuntimeError("PyInstaller docstring prune stripped no code objects")

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
