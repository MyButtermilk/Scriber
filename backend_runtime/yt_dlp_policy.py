"""Pinned YouTube-only yt-dlp packaging and runtime policy.

Scriber uses yt-dlp only for YouTube URLs.  Keep the module inventory and the
runtime extractor registry in one place so the PyInstaller build and the
application cannot drift independently.
"""

from __future__ import annotations

import os
from collections.abc import Iterable


EXPECTED_YT_DLP_MODULE_COUNT = 1046
EXPECTED_EXTRACTOR_MODULE_COUNT = 972
EXPECTED_NON_EXTRACTOR_MODULE_COUNT = 74
EXPECTED_RETAINED_EXTRACTOR_MODULE_COUNT = 38
EXPECTED_EXCLUDED_EXTRACTOR_MODULE_COUNT = 934

_EXACT_RETAINED_EXTRACTOR_MODULES = frozenset(
    {
        "yt_dlp.extractor",
        "yt_dlp.extractor.adobepass",
        "yt_dlp.extractor.afreecatv",
        "yt_dlp.extractor.common",
        "yt_dlp.extractor.extractors",
        "yt_dlp.extractor.lazy_extractors",
        "yt_dlp.extractor.openload",
    }
)
_YOUTUBE_EXTRACTOR_MODULE_PREFIX = "yt_dlp.extractor.youtube"

YOUTUBE_EXTRACTOR_CLASS_NAMES = (
    "YoutubeClipIE",
    "YoutubeConsentRedirectIE",
    "YoutubeFavouritesIE",
    "YoutubeHistoryIE",
    "YoutubeIE",
    "YoutubeLivestreamEmbedIE",
    "YoutubeMusicSearchURLIE",
    "YoutubeNotificationsIE",
    "YoutubePlaylistIE",
    "YoutubeRecommendedIE",
    "YoutubeSearchIE",
    "YoutubeSearchURLIE",
    "YoutubeShortsAudioPivotIE",
    "YoutubeSubscriptionsIE",
    "YoutubeTabIE",
    "YoutubeTruncatedIDIE",
    "YoutubeTruncatedURLIE",
    "YoutubeWatchLaterIE",
    "YoutubeYtBeIE",
    "YoutubeYtUserIE",
)


def is_retained_yt_dlp_module(module_name: str) -> bool:
    """Return whether a discovered yt-dlp module belongs in Scriber."""

    if not module_name.startswith("yt_dlp.extractor"):
        return True
    return module_name in _EXACT_RETAINED_EXTRACTOR_MODULES or (
        module_name == _YOUTUBE_EXTRACTOR_MODULE_PREFIX
        or module_name.startswith(f"{_YOUTUBE_EXTRACTOR_MODULE_PREFIX}.")
    )


def partition_yt_dlp_modules(
    discovered_modules: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate the pinned 2026.7.4 inventory and return keep/exclude sets."""

    discovered = tuple(sorted(discovered_modules))
    if len(discovered) != len(set(discovered)):
        raise RuntimeError("yt-dlp module discovery returned duplicates")
    extractor_modules = tuple(
        name
        for name in discovered
        if name == "yt_dlp.extractor" or name.startswith("yt_dlp.extractor.")
    )
    extractor_module_set = set(extractor_modules)
    non_extractor_modules = tuple(
        name for name in discovered if name not in extractor_module_set
    )
    retained = tuple(name for name in discovered if is_retained_yt_dlp_module(name))
    excluded = tuple(name for name in discovered if not is_retained_yt_dlp_module(name))
    retained_module_set = set(retained)
    retained_extractors = tuple(
        name for name in extractor_modules if name in retained_module_set
    )

    actual_counts = (
        len(discovered),
        len(extractor_modules),
        len(non_extractor_modules),
        len(retained_extractors),
        len(excluded),
    )
    expected_counts = (
        EXPECTED_YT_DLP_MODULE_COUNT,
        EXPECTED_EXTRACTOR_MODULE_COUNT,
        EXPECTED_NON_EXTRACTOR_MODULE_COUNT,
        EXPECTED_RETAINED_EXTRACTOR_MODULE_COUNT,
        EXPECTED_EXCLUDED_EXTRACTOR_MODULE_COUNT,
    )
    if actual_counts != expected_counts:
        raise RuntimeError(
            "yt-dlp 2026.7.4 module inventory drifted: "
            f"expected {expected_counts}, got {actual_counts}"
        )
    if set(non_extractor_modules) - set(retained):
        raise RuntimeError("yt-dlp non-extractor modules must not be pruned")
    return retained, excluded


def disable_external_yt_dlp_plugins() -> None:
    """Apply the process-global plugin boundary without importing extractors."""

    os.environ["YTDLP_NO_PLUGINS"] = "1"
    from yt_dlp.globals import plugin_dirs

    plugin_dirs.value = []
    if plugin_dirs.value != []:
        raise RuntimeError("yt-dlp plugin directories were not disabled")


def apply_youtube_only_runtime_policy() -> None:
    """Install the exact 20-class lazy YouTube extractor registry."""

    disable_external_yt_dlp_plugins()

    from yt_dlp import extractor
    from yt_dlp.extractor import lazy_extractors
    from yt_dlp.globals import LAZY_EXTRACTORS, extractors, plugin_ies, plugin_ies_overrides

    extractor.import_extractors()
    lookup = getattr(lazy_extractors, "_CLASS_LOOKUP", None)
    if not isinstance(lookup, dict):
        raise RuntimeError("yt-dlp lazy extractor lookup is unavailable")
    if len(YOUTUBE_EXTRACTOR_CLASS_NAMES) != 20:
        raise RuntimeError("Scriber YouTube extractor policy must contain 20 classes")
    missing = set(YOUTUBE_EXTRACTOR_CLASS_NAMES) - set(lookup)
    if missing:
        raise RuntimeError("yt-dlp YouTube lazy extractor registry drifted")

    selected = {name: lookup[name] for name in YOUTUBE_EXTRACTOR_CLASS_NAMES}
    extractors.value = selected
    plugin_ies.value = {}
    plugin_ies_overrides.value.clear()

    if LAZY_EXTRACTORS.value is not True:
        raise RuntimeError("yt-dlp lazy extractors are required by the pruned runtime")
    if tuple(extractors.value) != YOUTUBE_EXTRACTOR_CLASS_NAMES:
        raise RuntimeError("yt-dlp extractor registry is not YouTube-only")
    if len(extractors.value) != 20 or plugin_ies.value:
        raise RuntimeError("yt-dlp extractor registry policy was not applied exactly")
