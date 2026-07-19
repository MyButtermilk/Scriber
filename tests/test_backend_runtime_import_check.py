from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts import check_backend_runtime_imports as runtime_import_checks
from scripts.check_backend_runtime_imports import (
    BLOCKED_FROZEN_BUILD_ONLY_IMPORTS,
    BLOCKED_FROZEN_YT_DLP_IMPORTS,
    PUNKT_TAB_LANGUAGE_PROBES,
    PUNKT_TAB_REQUIRED_FILES,
    PUNKT_TAB_RETAINED_LANGUAGES,
    REQUIRED_IMPORTS,
    REQUIRED_FROZEN_BUILD_PRUNE_IMPORTS,
    REQUIRED_PACKAGE_VERSIONS,
    SENTENCE_SEGMENTATION_PROBES,
    check_frozen_build_tool_pruning,
    check_imports,
    check_frozen_youtube_only_yt_dlp,
    check_package_versions,
    check_provider_initialization_matrix,
    check_runtime_requirements,
    check_sentence_segmentation,
)
from backend_runtime.contract import RUNTIME_CONTRACT_REVISION, RUNTIME_REQUIRED_IMPORTS


PUNKT_TAB_PRUNED_LANGUAGES = (
    "czech",
    "danish",
    "dutch",
    "estonian",
    "finnish",
    "french",
    "greek",
    "italian",
    "malayalam",
    "norwegian",
    "polish",
    "portuguese",
    "russian",
    "slovene",
    "spanish",
    "swedish",
    "turkish",
)


def _write_complete_punkt_tab(
    root: Path,
    *,
    languages: tuple[str, ...] = PUNKT_TAB_RETAINED_LANGUAGES,
) -> Path:
    nltk_data = root / "nltk_data"
    punkt_tab = nltk_data / "tokenizers" / "punkt_tab"
    for language in languages:
        language_root = punkt_tab / language
        language_root.mkdir(parents=True, exist_ok=True)
        for filename in PUNKT_TAB_REQUIRED_FILES:
            (language_root / filename).write_text("runtime-probe", encoding="utf-8")
    return nltk_data


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


def test_sidecar_spec_prunes_exact_build_and_test_pyz_prefixes():
    repo_root = Path(__file__).resolve().parents[1]
    spec = (repo_root / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )
    expected_prefixes = tuple(BLOCKED_FROZEN_BUILD_ONLY_IMPORTS)
    filter_call = spec.split("a.pure[:] = exclude_pure_modules(", 1)[1].split(
        "\n)\n\npyz = PYZ(a.pure)", 1
    )[0]

    assert "def exclude_pure_modules(" in spec
    assert 'module_name == prefix or module_name.startswith(prefix + ".")' in spec
    assert spec.count("a.pure[:] = exclude_pure_modules(") == 1
    assert spec.index("a.pure[:] = exclude_pure_modules(") < spec.index(
        "pyz = PYZ(a.pure)"
    )
    assert 'startswith("setuptools/")' in spec
    assert spec.index('startswith("setuptools/")') < spec.index(
        "a.pure[:] = exclude_pure_modules("
    )
    for prefix in expected_prefixes:
        assert f'"{prefix}"' in filter_call
    for retained_prefix in (
        "_cffi_backend",
        "cffi",
        "cryptography",
        "google.oauth2.service_account",
        "httpx",
        "packaging",
        "pycparser",
        "yt_dlp",
    ):
        assert f'"{retained_prefix}"' not in filter_call


def test_frozen_build_tool_gate_requires_runtime_dependencies_and_absence():
    required = {module for module, _reason in REQUIRED_FROZEN_BUILD_PRUNE_IMPORTS}
    imported: list[str] = []

    def fake_import(module_name: str) -> object:
        imported.append(module_name)
        if module_name in BLOCKED_FROZEN_BUILD_ONLY_IMPORTS:
            exc = ModuleNotFoundError(module_name)
            exc.name = module_name
            raise exc
        return object()

    assert check_frozen_build_tool_pruning(
        frozen=True,
        import_module=fake_import,
    ) == []
    assert required == {
        "_cffi_backend",
        "cffi",
        "google.oauth2.service_account",
        "pycparser",
    }
    assert set(imported) == required | set(BLOCKED_FROZEN_BUILD_ONLY_IMPORTS)


def test_frozen_build_tool_gate_rejects_present_setuptools():
    def fake_import(module_name: str) -> object:
        if module_name == "setuptools":
            return object()
        if module_name in BLOCKED_FROZEN_BUILD_ONLY_IMPORTS:
            exc = ModuleNotFoundError(module_name)
            exc.name = module_name
            raise exc
        return object()

    assert check_frozen_build_tool_pruning(
        frozen=True,
        import_module=fake_import,
    ) == [
        {
            "module": "frozen-build-tool-pruning:setuptools",
            "reason": "minimal frozen build/test dependency graph",
            "error": "RuntimeError: frozen build-tool pruning check failed",
        }
    ]


def test_provider_initialization_matrix_preserves_size_sensitive_runtimes():
    assert check_provider_initialization_matrix() == []


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


def test_backend_spec_keeps_only_english_and_german_punkt_tab_models():
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
    assert "def retain_punkt_tab_languages(" in spec
    assert 'prefix = "nltk_data/tokenizers/punkt_tab/"' in spec
    assert 'path_parts = relative.split("/", 1)' in spec
    exact_language_filter = (
        'a.datas = retain_punkt_tab_languages(a.datas, ("english", "german"))'
    )
    assert spec.count(exact_language_filter) == 1
    assert spec.index("a = Analysis(") < spec.index(exact_filter) < spec.index(
        exact_language_filter
    ) < spec.index(
        "pyz = PYZ(a.pure)"
    )


def test_nsis_upgrade_hook_removes_only_obsolete_frozen_runtime_files_and_directories():
    repo_root = Path(__file__).resolve().parents[1]
    config = json.loads(
        (repo_root / "Frontend" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    hook_relative = config["bundle"]["windows"]["nsis"]["installerHooks"]
    hook_path = repo_root / "Frontend" / "src-tauri" / hook_relative
    hook = hook_path.read_text(encoding="utf-8")
    punkt_tab_files = {
        "abbrev_types.txt",
        "collocations.tab",
        "ortho_context.tab",
        "sent_starters.txt",
    }
    obsolete_paths = {
        "nltk_data/tokenizers/punkt_tab.zip",
        "hf_xet/hf_xet.pyd",
        "lxml/builder.cp313-win_amd64.pyd",
        "lxml/html/_difflib.cp313-win_amd64.pyd",
        "lxml/html/diff.cp313-win_amd64.pyd",
        "lxml/isoschematron/resources/rng/iso-schematron.rng",
        "lxml/isoschematron/resources/xsl/iso-schematron-xslt1/iso_abstract_expand.xsl",
        "lxml/isoschematron/resources/xsl/iso-schematron-xslt1/iso_dsdl_include.xsl",
        "lxml/isoschematron/resources/xsl/iso-schematron-xslt1/iso_schematron_message.xsl",
        "lxml/isoschematron/resources/xsl/iso-schematron-xslt1/iso_schematron_skeleton_for_xslt1.xsl",
        "lxml/isoschematron/resources/xsl/iso-schematron-xslt1/iso_svrl_for_xslt1.xsl",
        "lxml/isoschematron/resources/xsl/iso-schematron-xslt1/readme.txt",
        "lxml/isoschematron/resources/xsl/RNG2Schtrn.xsl",
        "lxml/isoschematron/resources/xsl/XSD2Schtrn.xsl",
        "lxml/objectify.cp313-win_amd64.pyd",
        "lxml/sax.cp313-win_amd64.pyd",
        "PIL/_imagingcms.cp313-win_amd64.pyd",
        "PIL/_imagingmath.cp313-win_amd64.pyd",
        "PIL/_imagingmorph.cp313-win_amd64.pyd",
        "PIL/_imagingtk.cp313-win_amd64.pyd",
        "PIL/_webp.cp313-win_amd64.pyd",
        "PIL/_imaging.cp313-win_amd64.pyd",
        "PIL/_imagingft.cp313-win_amd64.pyd",
        "docx/py.typed",
        "docx/templates/default-comments.xml",
        "docx/templates/default-docx-template/[Content_Types].xml",
        "docx/templates/default-docx-template/_rels/.rels",
        "docx/templates/default-docx-template/customXml/_rels/item1.xml.rels",
        "docx/templates/default-docx-template/customXml/item1.xml",
        "docx/templates/default-docx-template/customXml/itemProps1.xml",
        "docx/templates/default-docx-template/docProps/app.xml",
        "docx/templates/default-docx-template/docProps/core.xml",
        "docx/templates/default-docx-template/docProps/thumbnail.jpeg",
        "docx/templates/default-docx-template/word/_rels/document.xml.rels",
        "docx/templates/default-docx-template/word/document.xml",
        "docx/templates/default-docx-template/word/fontTable.xml",
        "docx/templates/default-docx-template/word/numbering.xml",
        "docx/templates/default-docx-template/word/settings.xml",
        "docx/templates/default-docx-template/word/styles.xml",
        "docx/templates/default-docx-template/word/stylesWithEffects.xml",
        "docx/templates/default-docx-template/word/theme/theme1.xml",
        "docx/templates/default-docx-template/word/webSettings.xml",
        "docx/templates/default-footer.xml",
        "docx/templates/default-header.xml",
        "docx/templates/default-settings.xml",
        "docx/templates/default-styles.xml",
        "docx/templates/default.docx",
        "lxml/_elementpath.cp313-win_amd64.pyd",
        "lxml/etree.cp313-win_amd64.pyd",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/INSTALLER",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/METADATA",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/RECORD",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/REQUESTED",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/WHEEL",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/licenses/LICENSE",
        "setuptools/_vendor/importlib_metadata-8.7.1.dist-info/top_level.txt",
        "setuptools/_vendor/jaraco/text/Lorem ipsum.txt",
    } | {
        f"nltk_data/tokenizers/punkt_tab/{language}/{filename}"
        for language in PUNKT_TAB_PRUNED_LANGUAGES
        for filename in punkt_tab_files
    }
    obsolete_installed_paths = {
        "backend/tools/ffmpeg/deno.exe",
        *(f"backend/_internal/{path}" for path in obsolete_paths),
    }
    delete_prefix = '  Delete "$INSTDIR\\'
    deleted_installed_paths = {
        line.removeprefix(delete_prefix).removesuffix('"').replace("\\", "/")
        for line in hook.splitlines()
        if line.startswith(delete_prefix)
    }
    obsolete_directories = (
        *(
            f"backend/_internal/nltk_data/tokenizers/punkt_tab/{language}"
            for language in PUNKT_TAB_PRUNED_LANGUAGES
        ),
        "backend/_internal/PIL",
        "backend/_internal/docx/templates/default-docx-template/_rels",
        "backend/_internal/docx/templates/default-docx-template/customXml/_rels",
        "backend/_internal/docx/templates/default-docx-template/customXml",
        "backend/_internal/docx/templates/default-docx-template/docProps",
        "backend/_internal/docx/templates/default-docx-template/word/_rels",
        "backend/_internal/docx/templates/default-docx-template/word/theme",
        "backend/_internal/docx/templates/default-docx-template/word",
        "backend/_internal/docx/templates/default-docx-template",
        "backend/_internal/docx/templates",
        "backend/_internal/docx",
        "backend/_internal/lxml/isoschematron/resources/xsl/iso-schematron-xslt1",
        "backend/_internal/lxml/isoschematron/resources/xsl",
        "backend/_internal/lxml/isoschematron/resources/rng",
        "backend/_internal/lxml/isoschematron/resources",
        "backend/_internal/lxml/isoschematron",
        "backend/_internal/lxml/html",
        "backend/_internal/lxml",
        "backend/_internal/reportlab",
        "backend/_internal/hf_xet",
        "backend/_internal/setuptools/_vendor/importlib_metadata-8.7.1.dist-info/licenses",
        "backend/_internal/setuptools/_vendor/importlib_metadata-8.7.1.dist-info",
        "backend/_internal/setuptools/_vendor/jaraco/text",
        "backend/_internal/setuptools/_vendor/jaraco",
        "backend/_internal/setuptools/_vendor",
        "backend/_internal/setuptools",
        "backend/_internal",
        "backend",
    )
    rmdir_prefix = '  RMDir "$INSTDIR\\'
    removed_directories = tuple(
        line.removeprefix(rmdir_prefix).removesuffix('"').replace("\\", "/")
        for line in hook.splitlines()
        if line.startswith(rmdir_prefix)
    )

    assert hook_relative == "./windows/installer-hooks.nsh"
    assert hook.count("!macro NSIS_HOOK_POSTINSTALL") == 1
    assert "NSIS_HOOK_PREINSTALL" not in hook
    assert deleted_installed_paths == obsolete_installed_paths
    assert hook.count("  Delete ") == len(obsolete_installed_paths)
    assert removed_directories == obsolete_directories
    assert hook.count("  RMDir ") == len(obsolete_directories)
    for retained_language in PUNKT_TAB_RETAINED_LANGUAGES:
        retained_prefix = (
            f"backend/_internal/nltk_data/tokenizers/punkt_tab/"
            f"{retained_language}/"
        )
        assert not any(
            path.startswith(retained_prefix) for path in deleted_installed_paths
        )
        assert (
            f"backend/_internal/nltk_data/tokenizers/punkt_tab/{retained_language}"
            not in removed_directories
        )
    first_rmdir_path = obsolete_directories[0].replace("/", "\\")
    first_rmdir = f'  RMDir "$INSTDIR\\{first_rmdir_path}"'
    assert hook.index(first_rmdir) > hook.rindex("  Delete ")
    for child_index, child in enumerate(removed_directories):
        for parent_index, parent in enumerate(removed_directories):
            if child.startswith(f"{parent}/"):
                assert child_index < parent_index
    assert "RMDir /r" not in hook
    assert "/REBOOTOK" not in hook
    assert "*" not in hook


def test_nsis_upgrade_hook_tombstones_only_the_exact_obsolete_deno_runtime_file():
    hook = (
        Path(__file__).resolve().parents[1]
        / "Frontend"
        / "src-tauri"
        / "windows"
        / "installer-hooks.nsh"
    ).read_text(encoding="utf-8")
    media_tool_prefix = '  Delete "$INSTDIR\\backend\\tools\\ffmpeg\\'
    media_tool_deletes = tuple(
        line.removeprefix(media_tool_prefix).removesuffix('"')
        for line in hook.splitlines()
        if line.startswith(media_tool_prefix)
    )

    assert media_tool_deletes == ("deno.exe",)
    assert 'Delete "$INSTDIR\\backend\\tools\\ffmpeg\\qjs.exe"' not in hook
    assert 'Delete "$INSTDIR\\backend\\tools\\ffmpeg\\qjs-engine.exe"' not in hook
    assert 'RMDir "$INSTDIR\\backend\\tools\\ffmpeg"' not in hook
    assert 'RMDir "$INSTDIR\\backend\\tools"' not in hook
    assert "RMDir /r" not in hook
    assert "*" not in hook


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
    assert tuple(
        language for _label, language, _text, _expected in PUNKT_TAB_LANGUAGE_PROBES
    ) == PUNKT_TAB_RETAINED_LANGUAGES
    assert {
        label for label, _language, _text, _expected in PUNKT_TAB_LANGUAGE_PROBES
    } == {"english-punkt-tab", "german-punkt-tab"}
    assert check_sentence_segmentation(frozen=False) == []


def test_pinned_pipecat_and_scriber_sentence_paths_do_not_request_other_languages():
    pipecat_spec = importlib.util.find_spec("pipecat")
    assert pipecat_spec is not None
    assert pipecat_spec.submodule_search_locations is not None
    pipecat_root = Path(next(iter(pipecat_spec.submodule_search_locations)))
    string_source = (pipecat_root / "utils" / "string.py").read_text(encoding="utf-8")
    assert string_source.count("sent_tokenize(text)") == 1
    assert "sent_tokenize(text," not in string_source

    expected_callers = {
        "processors/aggregators/sentence.py": 1,
        "processors/frameworks/rtvi/observer.py": 1,
        "services/google/gemini_live/llm.py": 1,
        "utils/text/simple_text_aggregator.py": 1,
    }
    actual_callers: dict[str, int] = {}
    for source_path in pipecat_root.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        call_count = source.count("match_endofsentence(") - source.count(
            "def match_endofsentence("
        )
        if call_count:
            actual_callers[source_path.relative_to(pipecat_root).as_posix()] = call_count
    assert actual_callers == expected_callers

    repo_root = Path(__file__).resolve().parents[1]
    application_sources = sorted((repo_root / "src").rglob("*.py"))
    assert application_sources
    for source_path in application_sources:
        source = source_path.read_text(encoding="utf-8")
        assert "sent_tokenize" not in source
        assert "match_endofsentence" not in source


def test_frozen_sentence_check_uses_only_bundled_nltk_data(tmp_path):
    bundled_nltk_data = _write_complete_punkt_tab(tmp_path)
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
    expected_tokenizations = {
        (language, text): expected
        for _label, language, text, expected in PUNKT_TAB_LANGUAGE_PROBES
    }
    observed_paths: list[list[str]] = []
    tokenizer_calls: list[tuple[str, str]] = []
    cache_clear_calls: list[bool] = []

    def fake_sent_tokenize(text: str, *, language: str) -> tuple[str, ...]:
        tokenizer_calls.append((language, text))
        return expected_tokenizations[(language, text)]

    def fake_cache_clear() -> None:
        cache_clear_calls.append(True)

    tokenizer_factory = SimpleNamespace(cache_clear=fake_cache_clear)

    def fake_import(module_name: str) -> object:
        if module_name == "nltk":
            return fake_nltk
        if module_name == "nltk.tokenize":
            assert fake_nltk.download is not original_download
            return SimpleNamespace(
                sent_tokenize=fake_sent_tokenize,
                _get_punkt_tokenizer=tokenizer_factory,
            )
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
    assert cache_clear_calls == [True]
    assert tokenizer_calls == [
        (language, text)
        for _label, language, text, _expected in PUNKT_TAB_LANGUAGE_PROBES
    ]


def test_frozen_sentence_check_rejects_download_fallback(tmp_path):
    bundled_nltk_data = _write_complete_punkt_tab(tmp_path)
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
        if module_name == "nltk.tokenize":
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


def test_frozen_sentence_check_rejects_any_third_punkt_tab_language(tmp_path):
    bundled_nltk_data = _write_complete_punkt_tab(
        tmp_path,
        languages=("english", "german", "spanish"),
    )
    original_download = lambda *_args, **_kwargs: True
    fake_nltk = SimpleNamespace(
        data=SimpleNamespace(path=["user-cache"]),
        download=original_download,
    )

    def fake_import(module_name: str) -> object:
        if module_name == "nltk":
            return fake_nltk
        raise AssertionError(f"unexpected import: {module_name}")

    assert check_sentence_segmentation(
        import_module=fake_import,
        frozen=True,
        frozen_root=tmp_path,
    ) == [
        {
            "module": "pipecat.utils.string.match_endofsentence",
            "reason": "bundled NLTK/Pipecat sentence segmentation runtime",
            "error": "RuntimeError: sentence segmentation probe failed",
        }
    ]
    assert fake_nltk.data.path == ["user-cache"]
    assert fake_nltk.download is original_download
    assert bundled_nltk_data.is_dir()


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


def test_frozen_youtube_only_runtime_check_requires_registry_and_blocked_imports():
    expected_names = tuple(f"Youtube{index}IE" for index in range(20))
    apply_calls: list[bool] = []
    policy = SimpleNamespace(
        apply_youtube_only_runtime_policy=lambda: apply_calls.append(True),
        YOUTUBE_EXTRACTOR_CLASS_NAMES=expected_names,
    )
    globals_module = SimpleNamespace(
        LAZY_EXTRACTORS=SimpleNamespace(value=True),
        plugin_dirs=SimpleNamespace(value=[]),
    )
    extractor_module = SimpleNamespace(
        _extractors_context=SimpleNamespace(value=dict.fromkeys(expected_names))
    )

    def fake_import(module_name: str) -> object:
        if module_name == "backend_runtime.yt_dlp_policy":
            return policy
        if module_name == "yt_dlp.globals":
            return globals_module
        if module_name == "yt_dlp.extractor":
            return extractor_module
        if module_name in BLOCKED_FROZEN_YT_DLP_IMPORTS:
            raise ModuleNotFoundError(module_name)
        raise AssertionError(module_name)

    assert check_frozen_youtube_only_yt_dlp(frozen=True, import_module=fake_import) == []
    assert apply_calls == [True]


def test_frozen_youtube_only_runtime_check_rejects_present_foreign_extractor():
    expected_names = tuple(f"Youtube{index}IE" for index in range(20))
    policy = SimpleNamespace(
        apply_youtube_only_runtime_policy=lambda: None,
        YOUTUBE_EXTRACTOR_CLASS_NAMES=expected_names,
    )
    globals_module = SimpleNamespace(
        LAZY_EXTRACTORS=SimpleNamespace(value=True),
        plugin_dirs=SimpleNamespace(value=[]),
    )
    extractor_module = SimpleNamespace(
        _extractors_context=SimpleNamespace(value=dict.fromkeys(expected_names))
    )

    def fake_import(module_name: str) -> object:
        return {
            "backend_runtime.yt_dlp_policy": policy,
            "yt_dlp.globals": globals_module,
            "yt_dlp.extractor": extractor_module,
        }.get(module_name, object())

    failures = check_frozen_youtube_only_yt_dlp(
        frozen=True, import_module=fake_import
    )

    assert failures == [
        {
            "module": "yt_dlp.extractor",
            "reason": "frozen YouTube-only yt-dlp runtime policy",
            "error": "RuntimeError: YouTube-only policy check failed",
        }
    ]


def test_standard_requirements_include_audio_runtime_dependencies():
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements-base.txt"
    ).read_text(encoding="utf-8").splitlines()

    assert "scipy" not in requirements
    assert "onnxruntime" in requirements
    assert "onnx-asr[cpu,hub]>=0.10.2,<0.11" in requirements
    assert "sherpa-onnx==1.13.4" not in requirements
    assert "pipecat-ai[silero]==1.5.0" in requirements
    assert "yt-dlp[default]==2026.7.4" in requirements
    assert all(not line.startswith("deno==") for line in requirements)
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
    excluded_runtime = spec.split("excludes=[", 1)[1]
    assert '"lxml",' in excluded_runtime
    assert '"PIL",' not in excluded_runtime
    assert '"docx",' not in excluded_runtime
    assert '"reportlab",' not in excluded_runtime
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
    assert 'Resolve-BackendStableMediaTool -Names @("qjs.exe")' in build_script
    assert "$pythonCommand = Get-Command $Python -ErrorAction SilentlyContinue" in build_script
    assert "if ($pythonDir)" in build_script
    runtime_manifest_source = build_script.split(
        "function Get-BackendRuntimeInputManifest", 1
    )[1].split("function Get-BackendApplicationInputManifest", 1)[0]
    assert '"packaging\\quickjs-youtube-runtime-lock-v1.json"' in runtime_manifest_source
    assert '"scripts\\build_quickjs_youtube_runtime.py"' in runtime_manifest_source
    assert 'Resolve-MediaTool -Names @("yt-dlp.exe", "yt-dlp")' not in runtime_manifest_source
    assert '"requirements-base.txt"' in runtime_manifest_source
    assert "Get-PythonFileEntries" in runtime_manifest_source
    sidecar_manifest_source = build_script.split(
        "function Get-SidecarInputManifest", 1
    )[1].split("function Get-BackendRuntimeFileIdentityEntries", 1)[0]
    assert "Get-QuickJsYoutubeRuntimeLockedFileMetadata" in sidecar_manifest_source
    assert 'Get-ToolMetadataEntry -Path $resolvedYtDlp -Name "yt-dlp"' in sidecar_manifest_source
    assert '$ErrorActionPreference = "Continue"' in build_script
    assert '& $Python -c "import PyInstaller" *> $null' in build_script
    assert "Invoke-QuickJsYoutubeRuntimeBuild" in build_script
    assert '"--quickjs-engine", $env:SCRIBER_QUICKJS_ENGINE_BIN' in build_script
    assert '"--quickjs-license", $env:SCRIBER_QUICKJS_LICENSE_FILE' in build_script
    assert "DENORT_BIN" not in build_script


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
