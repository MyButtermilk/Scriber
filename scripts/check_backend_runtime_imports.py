from __future__ import annotations

import importlib
import importlib.metadata
import io
import json
import re
import sys
import zipfile
import zlib
from collections.abc import Callable, Iterable
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.runtime.provider_dependencies import STANDARD_PROVIDER_RUNTIME_IMPORTS


CORE_RUNTIME_IMPORTS: tuple[tuple[str, str], ...] = (
    ("pyloudnorm", "local Pipecat loudness compatibility dependency"),
    ("onnxruntime", "Silero VAD native runtime dependency"),
    ("onnx_asr", "bundled local ONNX speech-to-text runtime dependency"),
    ("yt_dlp", "YouTube media extraction dependency"),
    ("yt_dlp_ejs", "YouTube external JavaScript challenge scripts"),
    ("pipecat.frames.frames", "Pipecat startup dependency"),
    ("pipecat.pipeline.pipeline", "Pipecat pipeline graph dependency"),
    ("pipecat.pipeline.task", "Pipecat pipeline task dependency"),
    ("pipecat.pipeline.runner", "Pipecat pipeline runner dependency"),
    ("pipecat.processors.frame_processor", "Pipecat frame processor dependency"),
    ("pipecat.services.ai_service", "Pipecat AI service dependency"),
    ("pipecat.services.settings", "Pipecat STT settings dependency"),
    ("pipecat.services.stt_service", "Pipecat STT base dependency"),
    ("pipecat.transcriptions.language", "Pipecat language dependency"),
    ("pipecat.transports.base_input", "Pipecat audio input transport dependency"),
    ("pipecat.transports.base_transport", "Pipecat transport dependency"),
    ("pipecat.utils.time", "Pipecat timestamp dependency"),
    ("pipecat.audio.vad.vad_analyzer", "Pipecat VAD startup dependency"),
    ("pipecat.audio.vad.silero", "Silero VAD startup dependency"),
    ("pipecat.processors.audio.vad_processor", "Pipecat VAD processor dependency"),
    ("pipecat.turns.user_start", "Pipecat user-turn start dependency"),
    ("pipecat.turns.user_stop", "Pipecat user-turn stop dependency"),
    ("pipecat.turns.user_turn_processor", "Pipecat user-turn processor dependency"),
    ("pipecat.turns.user_turn_strategies", "Pipecat user-turn strategy dependency"),
    (
        "pipecat.audio.turn.smart_turn.local_smart_turn_v3",
        "Pipecat 1.5 local Smart Turn startup dependency",
    ),
    ("src.microphone", "live microphone application runtime"),
    ("src.audio_file_input", "file audio application runtime"),
    ("src.pipeline", "transcription pipeline application runtime"),
    ("src.web_api", "backend API entry point"),
)
REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    *CORE_RUNTIME_IMPORTS,
    *STANDARD_PROVIDER_RUNTIME_IMPORTS,
)
REQUIRED_PACKAGE_VERSIONS: tuple[tuple[str, str], ...] = (
    ("pipecat-ai", "1.5.0"),
    ("yt-dlp", "2026.7.4"),
    ("yt-dlp-ejs", "0.8.0"),
)

BLOCKED_FROZEN_YT_DLP_IMPORTS: tuple[str, ...] = (
    "yt_dlp.extractor._extractors",
    "yt_dlp.extractor.generic",
    "yt_dlp.extractor.vimeo",
)

REQUIRED_FROZEN_EXPORT_IMPORTS: tuple[str, ...] = (
    "src.export",
)

FROZEN_EXPORT_COMPAT_IMPORTS: tuple[str, ...] = (
    "PIL",
    "docx",
    "reportlab.platypus",
)

BLOCKED_FROZEN_EXPORT_IMPORTS: tuple[str, ...] = (
    "lxml",
)

BLOCKED_FROZEN_UNUSED_PROVIDER_IMPORTS: tuple[str, ...] = (
    "deepgram.agent",
    "grpc._channel",
    "openai.types.realtime",
)

_WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _decode_pdf_literal(value: bytes) -> str:
    decoded = bytearray()
    index = 0
    while index < len(value):
        byte = value[index]
        if byte != 0x5C:
            decoded.append(byte)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        escaped = value[index]
        replacements = {
            ord("n"): ord("\n"),
            ord("r"): ord("\r"),
            ord("t"): ord("\t"),
            ord("b"): ord("\b"),
            ord("f"): ord("\f"),
            ord("("): ord("("),
            ord(")"): ord(")"),
            ord("\\"): ord("\\"),
        }
        if escaped in replacements:
            decoded.append(replacements[escaped])
            index += 1
            continue
        if ord("0") <= escaped <= ord("7"):
            end = index + 1
            while end < min(index + 3, len(value)) and ord("0") <= value[end] <= ord("7"):
                end += 1
            decoded.append(int(value[index:end], 8))
            index = end
            continue
        decoded.append(escaped)
        index += 1
    return decoded.decode("cp1252")


def _pdf_text_and_page_count(payload: bytes) -> tuple[str, int]:
    streams: list[bytes] = []
    for match in re.finditer(
        rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\n?endstream",
        payload,
        flags=re.DOTALL,
    ):
        dictionary, data = match.groups()
        if b"/FlateDecode" not in dictionary:
            continue
        streams.append(zlib.decompress(data))
    text_parts = [
        _decode_pdf_literal(literal)
        for stream in streams
        for literal in re.findall(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", stream)
    ]
    return " ".join(text_parts), len(re.findall(rb"/Type\s*/Page\b", payload))


def _docx_text_and_paragraph_count(payload: bytes) -> tuple[str, int, str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        required_parts = {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/styles.xml",
            "word/numbering.xml",
            "word/_rels/document.xml.rels",
        }
        if not required_parts <= set(archive.namelist()):
            raise RuntimeError("DOCX package parts are incomplete")
        for part in required_parts:
            ElementTree.fromstring(archive.read(part))
        document_xml = archive.read("word/document.xml").decode("utf-8")
    root = ElementTree.fromstring(document_xml)
    text = " ".join(node.text or "" for node in root.iter(f"{_WORD_NAMESPACE}t"))
    paragraphs = sum(1 for _ in root.iter(f"{_WORD_NAMESPACE}p"))
    return text, paragraphs, document_xml

REQUIRED_FROZEN_BUILD_PRUNE_IMPORTS: tuple[tuple[str, str], ...] = (
    ("cffi", "Google authentication CFFI runtime"),
    ("_cffi_backend", "Google authentication native CFFI backend"),
    ("pycparser", "CFFI declaration parser runtime"),
    ("google.oauth2.service_account", "Google service-account credential runtime"),
)

BLOCKED_FROZEN_BUILD_ONLY_IMPORTS: tuple[str, ...] = (
    "PyInstaller",
    "_distutils_hack",
    "altgraph",
    "keyboard._keyboard_tests",
    "keyboard._mouse_tests",
    "numpy.testing",
    "pefile",
    "pygments",
    "setuptools",
    "win32ctypes",
    "yt_dlp.__pyinstaller",
)

PUNKT_TAB_RETAINED_LANGUAGES: tuple[str, ...] = ("english", "german")
PUNKT_TAB_REQUIRED_FILES: frozenset[str] = frozenset(
    {
        "abbrev_types.txt",
        "collocations.tab",
        "ortho_context.tab",
        "sent_starters.txt",
    }
)
PUNKT_TAB_LANGUAGE_PROBES: tuple[
    tuple[str, str, str, tuple[str, ...]], ...
] = (
    (
        "english-punkt-tab",
        "english",
        "Dr. Smith arrived. The meeting started.",
        ("Dr. Smith arrived.", "The meeting started."),
    ),
    (
        "german-punkt-tab",
        "german",
        "Dr. Müller kommt. Danach geht es weiter.",
        ("Dr. Müller kommt.", "Danach geht es weiter."),
    ),
)

SENTENCE_SEGMENTATION_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "english_abbreviation",
        "Dr. Smith arrived. The meeting started.",
        "Dr. Smith arrived.",
    ),
    (
        "decimal",
        "The value is 3.14. Next item.",
        "The value is 3.14.",
    ),
    (
        "german_abbreviation",
        "Dr. Müller kommt. Danach geht es weiter.",
        "Dr. Müller kommt.",
    ),
    ("cjk_punctuation", "これは文です。次です", "これは文です。"),
    ("arabic_punctuation", "مرحبا؟التالي", "مرحبا؟"),
)


def _validate_frozen_punkt_tab_data(bundled_nltk_data: Path) -> None:
    punkt_tab_root = bundled_nltk_data / "tokenizers" / "punkt_tab"
    if not punkt_tab_root.is_dir():
        raise RuntimeError("bundled punkt_tab directory is missing")
    actual_languages = tuple(
        sorted(path.name for path in punkt_tab_root.iterdir() if path.is_dir())
    )
    if actual_languages != tuple(sorted(PUNKT_TAB_RETAINED_LANGUAGES)):
        raise RuntimeError("bundled punkt_tab language set is not exactly English/German")
    for language in PUNKT_TAB_RETAINED_LANGUAGES:
        language_root = punkt_tab_root / language
        actual_files = {
            path.name for path in language_root.iterdir() if path.is_file()
        }
        if actual_files != PUNKT_TAB_REQUIRED_FILES:
            raise RuntimeError(f"bundled {language} punkt_tab model is incomplete")


def check_sentence_segmentation(
    *,
    import_module: Callable[[str], object] = importlib.import_module,
    frozen: bool | None = None,
    frozen_root: Path | None = None,
) -> list[dict[str, str]]:
    """Exercise the bundled NLTK data through Pipecat without network fallback."""

    nltk_module: object | None = None
    original_download: object | None = None
    try:
        nltk_module = import_module("nltk")
        nltk_data = getattr(nltk_module, "data")
        nltk_data_path = getattr(nltk_data, "path")
        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        if is_frozen:
            root = frozen_root
            if root is None:
                raw_root = getattr(sys, "_MEIPASS", None)
                if not isinstance(raw_root, (str, bytes)):
                    raise RuntimeError("frozen runtime has no bundled data root")
                root = Path(raw_root)
            bundled_nltk_data = root / "nltk_data"
            if not bundled_nltk_data.is_dir():
                raise RuntimeError("bundled nltk_data directory is missing")
            _validate_frozen_punkt_tab_data(bundled_nltk_data)
            # Replace, rather than prepend to, the search list so a developer or
            # user cache can never make an incomplete frozen bundle look valid.
            nltk_data_path[:] = [str(bundled_nltk_data)]

        original_download = getattr(nltk_module, "download")
        if not callable(original_download):
            raise RuntimeError("NLTK downloader boundary is unavailable")

        def reject_download(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("NLTK download fallback was attempted")

        setattr(nltk_module, "download", reject_download)
        nltk_tokenize = import_module("nltk.tokenize")
        sent_tokenize = getattr(nltk_tokenize, "sent_tokenize")
        tokenizer_factory = getattr(nltk_tokenize, "_get_punkt_tokenizer", None)
        cache_clear = getattr(tokenizer_factory, "cache_clear", None)
        if not callable(sent_tokenize) or not callable(cache_clear):
            raise RuntimeError("NLTK Punkt tokenizer boundary is unavailable")
        # Never let a tokenizer cached before the frozen search-path lock make a
        # missing bundled language look valid.
        cache_clear()
        for label, language, text, expected_sentences in PUNKT_TAB_LANGUAGE_PROBES:
            actual_sentences = tuple(sent_tokenize(text, language=language))
            if actual_sentences != expected_sentences:
                raise RuntimeError(
                    f"sentence tokenizer probe {label} returned {actual_sentences!r}; "
                    f"expected {expected_sentences!r}"
                )

        pipecat_string = import_module("pipecat.utils.string")
        match_endofsentence = getattr(pipecat_string, "match_endofsentence")
        if not callable(match_endofsentence):
            raise RuntimeError("Pipecat sentence segmenter is unavailable")

        for label, text, expected_first_sentence in SENTENCE_SEGMENTATION_PROBES:
            expected_end = len(expected_first_sentence)
            actual_end = match_endofsentence(text)
            if actual_end != expected_end:
                raise RuntimeError(
                    f"sentence segmentation probe {label} returned {actual_end!r}; "
                    f"expected {expected_end}"
                )
    except Exception as exc:
        return [
            {
                "module": "pipecat.utils.string.match_endofsentence",
                "reason": "bundled NLTK/Pipecat sentence segmentation runtime",
                # Keep frozen diagnostics independent of install paths and user data.
                "error": f"{type(exc).__name__}: sentence segmentation probe failed",
            }
        ]
    finally:
        if nltk_module is not None and callable(original_download):
            setattr(nltk_module, "download", original_download)

    return []


def check_imports(
    imports: Iterable[tuple[str, str]] = REQUIRED_IMPORTS,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for module_name, reason in imports:
        try:
            import_module(module_name)
        except Exception as exc:
            missing.append(
                {
                    "module": module_name,
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return missing


def check_package_versions(
    requirements: Iterable[tuple[str, str]] = REQUIRED_PACKAGE_VERSIONS,
    version_for: Callable[[str], str] = importlib.metadata.version,
) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for package_name, expected_version in requirements:
        try:
            installed_version = version_for(package_name)
        except Exception as exc:
            mismatches.append(
                {
                    "module": f"distribution:{package_name}",
                    "reason": f"required package version {expected_version}",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if installed_version != expected_version:
            mismatches.append(
                {
                    "module": f"distribution:{package_name}",
                    "reason": f"required package version {expected_version}",
                    "error": f"VersionMismatch: installed {installed_version}",
                }
            )
    return mismatches


def check_frozen_youtube_only_yt_dlp(
    *,
    frozen: bool | None = None,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    """Verify the pruned yt-dlp registry and representative blocked imports."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return []
    try:
        policy = import_module("backend_runtime.yt_dlp_policy")
        apply_policy = getattr(policy, "apply_youtube_only_runtime_policy")
        expected_names = tuple(getattr(policy, "YOUTUBE_EXTRACTOR_CLASS_NAMES"))
        apply_policy()

        globals_module = import_module("yt_dlp.globals")
        extractor_module = import_module("yt_dlp.extractor")
        if getattr(globals_module, "LAZY_EXTRACTORS").value is not True:
            raise RuntimeError("lazy extractor mode is not active")
        if getattr(globals_module, "plugin_dirs").value != []:
            raise RuntimeError("external plugin directories are active")
        registry = getattr(extractor_module, "_extractors_context").value
        if tuple(registry) != expected_names or len(registry) != 20:
            raise RuntimeError("extractor registry is not the exact YouTube policy")
        for module_name in BLOCKED_FROZEN_YT_DLP_IMPORTS:
            try:
                import_module(module_name)
            except ModuleNotFoundError:
                continue
            raise RuntimeError(f"blocked extractor import succeeded: {module_name}")
    except Exception as exc:
        return [
            {
                "module": "yt_dlp.extractor",
                "reason": "frozen YouTube-only yt-dlp runtime policy",
                "error": f"{type(exc).__name__}: YouTube-only policy check failed",
            }
        ]
    return []


def check_frozen_text_export_graph(
    *,
    frozen: bool | None = None,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    """Render localized documents and reject every legacy export package."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return []
    try:
        export_module = import_module(REQUIRED_FROZEN_EXPORT_IMPORTS[0])
        for module_name in FROZEN_EXPORT_COMPAT_IMPORTS:
            compat_module = import_module(module_name)
            if getattr(compat_module, "SCRIBER_STDLIB_EXPORT_COMPAT", False) is not True:
                raise RuntimeError(f"legacy export package resolved: {module_name}")
        export_to_pdf = getattr(export_module, "export_to_pdf")
        export_to_docx = getattr(export_module, "export_to_docx")
        for language, title, marker, labels, unicode_marker in (
            (
                "de",
                "Planungsprüfung München",
                "Prüfpunkt Größe Straße café",
                {
                    "date": "Datum",
                    "duration": "Dauer",
                    "summary": "Zusammenfassung",
                    "transcript": "Transkript",
                },
                "Vollständig: – „Deutsch“ € 漢字 🙂",
            ),
            (
                "en",
                "Planning review London",
                "Review point size café",
                {
                    "date": "Date",
                    "duration": "Duration",
                    "summary": "Summary",
                    "transcript": "Transcript",
                },
                "Complete: – “English” € 漢字 🙂",
            ),
        ):
            filler = (
                "verlässlicher deutscher Exporttext. "
                if language == "de"
                else "reliable English export text. "
            )
            content = "\n\n".join(
                f"[Speaker {index + 1}]: {marker} {index:03d}: "
                + (filler * 5)
                + (f" {unicode_marker}" if index == 0 else "")
                for index in range(140)
            )
            summary = (
                f"# {labels['summary']}\n"
                f"- **{marker}**\n"
                f"*{'Kursiv' if language == 'de' else 'Italic'}* `export-code`"
            )
            pdf = export_to_pdf(
                title,
                content,
                summary=summary,
                date="2026-07-19",
                duration="9:20",
                labels=labels,
            )
            docx = export_to_docx(
                title,
                content,
                summary=summary,
                date="2026-07-19",
                duration="9:20",
                labels=labels,
            )
            if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
                raise RuntimeError(f"{language} PDF render failed")
            if not isinstance(docx, bytes) or not docx.startswith(b"PK"):
                raise RuntimeError(f"{language} DOCX render failed")
            pdf_text, page_count = _pdf_text_and_page_count(pdf)
            if page_count < 5 or not all(
                value in pdf_text
                for value in (title, labels["summary"], labels["transcript"], marker)
            ):
                raise RuntimeError(f"{language} PDF content or pagination failed")
            docx_text, paragraph_count, document_xml = _docx_text_and_paragraph_count(docx)
            if paragraph_count < 140 or not all(
                value in docx_text
                for value in (
                    title,
                    labels["summary"],
                    labels["transcript"],
                    marker,
                    unicode_marker,
                )
            ):
                raise RuntimeError(f"{language} DOCX text was not preserved")
            for formatting_marker in ("<w:b/>", "<w:i/>", "<w:numPr>", 'w:ascii="Consolas"'):
                if formatting_marker not in document_xml:
                    raise RuntimeError(f"{language} DOCX formatting was not preserved")
        for module_name in BLOCKED_FROZEN_EXPORT_IMPORTS:
            try:
                import_module(module_name)
            except ModuleNotFoundError as exc:
                if exc.name == module_name:
                    continue
                raise
            raise RuntimeError(f"blocked export import succeeded: {module_name}")
    except Exception as exc:
        return [
            {
                "module": "text-export-runtime-graph",
                "reason": "minimal frozen PDF/DOCX dependency graph",
                "error": f"{type(exc).__name__}: text export runtime graph check failed",
            }
        ]
    return []


def check_provider_initialization_matrix(
    *,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    """Initialize credential-free representatives of size-sensitive providers."""

    def google_service_account_cffi() -> None:
        cffi_module = import_module("cffi")
        ffi = getattr(cffi_module, "FFI")()
        probe_buffer = ffi.new("unsigned char[]", b"scriber")
        if bytes(ffi.buffer(probe_buffer, 7)) != b"scriber":
            raise RuntimeError("CFFI buffer round trip failed")

        rsa = import_module("cryptography.hazmat.primitives.asymmetric.rsa")
        serialization = import_module("cryptography.hazmat.primitives.serialization")
        service_account = import_module("google.oauth2.service_account")
        private_key = getattr(rsa, "generate_private_key")(
            public_exponent=65537,
            key_size=2048,
        )
        private_key_pem = private_key.private_bytes(
            getattr(serialization, "Encoding").PEM,
            getattr(serialization, "PrivateFormat").PKCS8,
            getattr(serialization, "NoEncryption")(),
        ).decode("ascii")
        info = {
            "type": "service_account",
            "project_id": "scriber-runtime-probe",
            "private_key_id": "0" * 40,
            "private_key": private_key_pem,
            "client_email": (
                "runtime-probe@scriber-runtime-probe.iam.gserviceaccount.com"
            ),
            "client_id": "1",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": (
                "https://www.googleapis.com/oauth2/v1/certs"
            ),
            "client_x509_cert_url": (
                "https://www.googleapis.com/robot/v1/metadata/x509/"
                "runtime-probe"
            ),
        }
        credentials_type = getattr(service_account, "Credentials")
        credentials = credentials_type.from_service_account_info(info)
        if not credentials.sign_bytes(b"scriber-provider-probe"):
            raise RuntimeError("Google service-account signing probe failed")

    def initialize_service(module_name: str, class_name: str) -> None:
        module = import_module(module_name)
        service_type = getattr(module, class_name)
        service = service_type(api_key="scriber-runtime-probe", sample_rate=16000)
        if service.__class__.__name__ != class_name:
            raise RuntimeError("provider constructor returned an unexpected type")

    probes: tuple[tuple[str, Callable[[], None]], ...] = (
        ("google-service-account-cffi", google_service_account_cffi),
        (
            "openai-realtime",
            lambda: initialize_service(
                "pipecat.services.openai.stt",
                "OpenAIRealtimeSTTService",
            ),
        ),
        (
            "deepgram-realtime",
            lambda: initialize_service(
                "pipecat.services.deepgram.stt",
                "DeepgramSTTService",
            ),
        ),
        (
            "elevenlabs-realtime",
            lambda: initialize_service(
                "pipecat.services.elevenlabs.stt",
                "ElevenLabsRealtimeSTTService",
            ),
        ),
        (
            "speechmatics-realtime",
            lambda: initialize_service(
                "pipecat.services.speechmatics.stt",
                "SpeechmaticsSTTService",
            ),
        ),
    )
    failures: list[dict[str, str]] = []
    for label, probe in probes:
        try:
            probe()
        except Exception as exc:
            failures.append(
                {
                    "module": label,
                    "reason": "provider runtime initialization matrix",
                    "error": f"{type(exc).__name__}: provider initialization probe failed",
                }
            )
    return failures


def check_frozen_build_tool_pruning(
    *,
    frozen: bool | None = None,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    """Require runtime dependencies and reject build/test modules in frozen PYZ."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return []
    active_module = "unknown"
    try:
        for module_name, _reason in REQUIRED_FROZEN_BUILD_PRUNE_IMPORTS:
            active_module = module_name
            import_module(module_name)
        for module_name in BLOCKED_FROZEN_BUILD_ONLY_IMPORTS:
            active_module = module_name
            try:
                import_module(module_name)
            except ModuleNotFoundError as exc:
                missing_name = exc.name
                if isinstance(missing_name, str) and (
                    missing_name == module_name
                    or module_name.startswith(missing_name + ".")
                ):
                    continue
                raise
            raise RuntimeError(f"blocked build-only import succeeded: {module_name}")
    except Exception as exc:
        return [
            {
                "module": f"frozen-build-tool-pruning:{active_module}",
                "reason": "minimal frozen build/test dependency graph",
                "error": f"{type(exc).__name__}: frozen build-tool pruning check failed",
            }
        ]
    return []


def check_frozen_docstring_pruning(
    *,
    frozen: bool | None = None,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    """Prove that recursive docstrings are gone without disabling assertions."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return []
    try:
        if not __debug__:
            raise RuntimeError("frozen runtime disabled __debug__")
        module = import_module("backend_runtime.docstring_prune_probe")
        probe_type = getattr(module, "DocstringPruneProbe")
        assertions_enabled = getattr(module, "assertions_enabled")
        docstrings = (
            getattr(module, "__doc__", None),
            getattr(probe_type, "__doc__", None),
            getattr(probe_type.method, "__doc__", None),
            getattr(assertions_enabled, "__doc__", None),
        )
        if any(value is not None for value in docstrings):
            raise RuntimeError("frozen runtime retained sentinel docstrings")
        if assertions_enabled() is not True:
            raise RuntimeError("frozen runtime stripped assertion bytecode")
        if probe_type().method() is not True:
            raise RuntimeError("frozen nested code sentinel failed")
    except Exception as exc:
        return [
            {
                "module": "frozen-docstring-pruning",
                "reason": "docstring deletion with active assertions",
                "error": f"{type(exc).__name__}: frozen docstring pruning check failed",
            }
        ]
    return []


def check_frozen_provider_pruning(
    *,
    frozen: bool | None = None,
    import_module: Callable[[str], object] = importlib.import_module,
) -> list[dict[str, str]]:
    """Reject representative unused SDK branches in the frozen runtime."""

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return []
    active_module = "unknown"
    try:
        for module_name in BLOCKED_FROZEN_UNUSED_PROVIDER_IMPORTS:
            active_module = module_name
            try:
                import_module(module_name)
            except ModuleNotFoundError as exc:
                missing_name = exc.name
                if isinstance(missing_name, str) and (
                    missing_name == module_name
                    or module_name.startswith(missing_name + ".")
                ):
                    continue
                raise
            raise RuntimeError(f"unused provider import succeeded: {module_name}")
    except Exception as exc:
        return [
            {
                "module": f"frozen-provider-pruning:{active_module}",
                "reason": "minimal frozen provider SDK graph",
                "error": f"{type(exc).__name__}: frozen provider pruning check failed",
            }
        ]
    return []


def check_runtime_requirements() -> list[dict[str, str]]:
    segmentation_failures = check_sentence_segmentation()
    version_failures = check_package_versions()
    if segmentation_failures:
        # Pipecat's module import attempts an NLTK download when punkt data is
        # absent. Do not run the broader imports after the no-network gate has
        # failed, or that fallback could mask a broken frozen bundle.
        return [*segmentation_failures, *version_failures]
    return [
        *check_imports(),
        *version_failures,
        *check_frozen_youtube_only_yt_dlp(),
        *check_frozen_text_export_graph(),
        *check_provider_initialization_matrix(),
        *check_frozen_build_tool_pruning(),
        *check_frozen_docstring_pruning(),
        *check_frozen_provider_pruning(),
    ]


def main() -> int:
    missing = check_runtime_requirements()
    result = {"ok": not missing, "missing": missing}
    print(json.dumps(result, separators=(",", ":")))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
