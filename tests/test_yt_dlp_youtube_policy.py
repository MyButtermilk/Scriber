from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend_runtime.yt_dlp_policy import (
    EXPECTED_EXCLUDED_EXTRACTOR_MODULE_COUNT,
    EXPECTED_NON_EXTRACTOR_MODULE_COUNT,
    EXPECTED_RETAINED_EXTRACTOR_MODULE_COUNT,
    EXPECTED_YT_DLP_MODULE_COUNT,
    YOUTUBE_EXTRACTOR_CLASS_NAMES,
    is_retained_yt_dlp_module,
    partition_yt_dlp_modules,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pinned_yt_dlp_inventory_keeps_only_youtube_extractors() -> None:
    pytest.importorskip("PyInstaller")
    from PyInstaller.utils.hooks import collect_submodules

    retained, excluded = partition_yt_dlp_modules(collect_submodules("yt_dlp"))

    assert len(retained) + len(excluded) == EXPECTED_YT_DLP_MODULE_COUNT
    assert len(excluded) == EXPECTED_EXCLUDED_EXTRACTOR_MODULE_COUNT
    assert sum(name.startswith("yt_dlp.extractor") for name in retained) == (
        EXPECTED_RETAINED_EXTRACTOR_MODULE_COUNT
    )
    assert sum(not name.startswith("yt_dlp.extractor") for name in retained) == (
        EXPECTED_NON_EXTRACTOR_MODULE_COUNT
    )
    assert "yt_dlp.extractor.youtube._video" in retained
    assert "yt_dlp.extractor.generic" in excluded
    assert "yt_dlp.extractor.vimeo" in excluded
    assert all(is_retained_yt_dlp_module(name) for name in retained)
    assert all(not is_retained_yt_dlp_module(name) for name in excluded)


def test_youtube_only_runtime_policy_installs_exact_lazy_registry() -> None:
    code = """
import json
import os
from backend_runtime.yt_dlp_policy import apply_youtube_only_runtime_policy
apply_youtube_only_runtime_policy()
import yt_dlp
from yt_dlp import globals as g
from yt_dlp import extractor
ydl = yt_dlp.YoutubeDL({
    'quiet': True,
    'allowed_extractors': [r'youtube.*'],
    'remote_components': [],
})
print(json.dumps({
    'classes': list(extractor._extractors_context.value),
    'lazy': g.LAZY_EXTRACTORS.value,
    'pluginDirs': g.plugin_dirs.value,
    'pluginIEs': list(g.plugin_ies.value),
    'environment': os.environ.get('YTDLP_NO_PLUGINS'),
    'youtubeDLClasses': list(ydl._ies),
}))
ydl.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert tuple(payload["classes"]) == YOUTUBE_EXTRACTOR_CLASS_NAMES
    assert payload["lazy"] is True
    assert payload["pluginDirs"] == []
    assert payload["pluginIEs"] == []
    assert payload["environment"] == "1"
    assert len(payload["youtubeDLClasses"]) == 20
    assert payload["youtubeDLClasses"] == [name[:-2] for name in YOUTUBE_EXTRACTOR_CLASS_NAMES]


def test_runtime_hook_does_not_import_yt_dlp_during_normal_startup() -> None:
    code = """
import json
import os
import runpy
import sys
sys.argv = ['scriber-backend.exe']
runpy.run_path('backend_runtime/pyinstaller_yt_dlp_runtime_hook.py')
print(json.dumps({
    'loaded': any(name == 'yt_dlp' or name.startswith('yt_dlp.') for name in sys.modules),
    'environment': os.environ.get('YTDLP_NO_PLUGINS'),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"loaded": False, "environment": "1"}


def test_spec_wires_fail_closed_pruning_and_runtime_hook() -> None:
    spec = (REPO_ROOT / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )

    assert spec.count('collect_submodules("yt_dlp")') == 1
    assert "partition_yt_dlp_modules" in spec
    assert "*excluded_yt_dlp_extractor_modules" in spec
    assert "pyinstaller_yt_dlp_runtime_hook.py" in spec
    generic_collection_loop = spec.split("for package in (", 1)[1].split(
        "):\n    try:\n        hiddenimports += collect_submodules(package)", 1
    )[0]
    assert '"yt_dlp",' not in generic_collection_loop
