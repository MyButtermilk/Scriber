"""Application-layer YouTube player/client compatibility overrides for yt-dlp.

Security policy, plugin disabling, the 20-class YouTube extractor allowlist,
QuickJS wrapper bounds, and Windows native truststore TLS remain frozen inside
``backend_runtime/``. Only frequently changing YouTube player/client defaults
live in this checksummed application module so compatibility-only fixes can
recompose the application layer without triggering a PyInstaller runtime freeze.
"""

from __future__ import annotations

PINNED_YT_DLP_VERSION = "2026.08.19"


def apply_youtube_client_compat_overrides() -> None:
    """Apply validated YouTube player/client defaults for the pinned yt-dlp."""

    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    from yt_dlp.extractor.youtube._video import YoutubeIE
    from yt_dlp.version import __version__

    if __version__ != PINNED_YT_DLP_VERSION:
        raise RuntimeError("Review YouTube compatibility fixes when updating the pinned yt-dlp version")

    # Upstream yt-dlp/yt-dlp#17723: an incomplete TV identity receives
    # UNPLAYABLE even with a valid authenticated session. Keep default client
    # selection; only correct the existing tv_downgraded client identity.
    INNERTUBE_CLIENTS["tv_downgraded"]["INNERTUBE_CONTEXT"]["client"].update(
        deviceMake="Samsung",
        deviceModel="SmartTV",
        userAgent=(
            "Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 "
            "(KHTML, like Gecko) Version/2.4.0 TV Safari/538.1"
        ),
        osName="Tizen",
        osVersion="2.4.0",
    )
    # Upstream yt-dlp/yt-dlp#18124 (fixes #18123): YouTube's November 2026
    # `main` player variants trigger a preprocessing TypeError in EJS 0.8.0
    # (`a.addOnDisposeCallback is not a function`), leaving zero playable
    # formats. Defaulting `player_js_variant` to `tv` restores n-challenge
    # solving without changing the pinned runtime or QuickJS wrapper bounds.
    YoutubeIE._DEFAULT_PLAYER_JS_VARIANT = "tv"
    # Upstream yt-dlp/yt-dlp#18051: prioritize the TV client ahead of
    # `web_embedded` for authenticated sessions.
    YoutubeIE._DEFAULT_AUTHED_CLIENTS = ("tv_downgraded", "web_embedded", "web")
