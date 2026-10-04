"""Narrow compatibility fixes for the pinned yt-dlp 2026.8.19 runtime."""

from __future__ import annotations

import json
from typing import Any


def _bounded_ejs_input(self: Any, player: str, preprocessed: bool, requests: list[Any], /) -> str:
    # EJS otherwise emits its entire preprocessed player (over 4 MiB for current
    # YouTube players). Keep the hardened wrapper's output limit intact and ask
    # only for challenge responses. This mirrors the pinned upstream protocol.
    data: dict[str, Any] = {
        "requests": [{"type": request.type.value, "challenges": request.input.challenges} for request in requests],
    }
    if preprocessed:
        data.update(type="preprocessed", preprocessed_player=player)
    else:
        data.update(type="player", player=player, output_preprocessed=False)
    return (
        f"{self._lib_script.code}\nObject.assign(globalThis, lib);\n"
        f"{self._core_script.code}\nconsole.log(JSON.stringify(jsc({json.dumps(data)})));\n"
    )


def apply_compatibility_fixes() -> None:
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    from yt_dlp.extractor.youtube.jsc._builtin.quickjs import QuickJSJCP
    from yt_dlp.version import __version__

    if __version__ != "2026.08.19":
        raise RuntimeError("Review YouTube compatibility fixes when updating the pinned yt-dlp version")

    # Upstream yt-dlp/yt-dlp#17723: an incomplete TV identity receives
    # UNPLAYABLE even with a valid authenticated session. Keep default client
    # selection; only correct the existing tv_downgraded client identity.
    INNERTUBE_CLIENTS["tv_downgraded"]["INNERTUBE_CONTEXT"]["client"].update(
        deviceMake="Samsung",
        deviceModel="SmartTV",
        userAgent="Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/2.4.0 TV Safari/538.1",
        osName="Tizen",
        osVersion="2.4.0",
    )
    QuickJSJCP._construct_stdin = _bounded_ejs_input
