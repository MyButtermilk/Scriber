"""Narrow compatibility fixes for the pinned yt-dlp 2026.8.19 runtime."""

from __future__ import annotations

import contextlib
import json
import os
import ssl
import sys
from collections.abc import Callable
from typing import Any

_ORIGINAL_MAKE_SSL_CONTEXT: Callable[..., Any] | None = None


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


def _make_native_ssl_context(
    verify: bool = True,
    client_certificate: str | None = None,
    client_certificate_key: str | None = None,
    client_certificate_password: Any = None,
    legacy_support: bool = False,
    use_certifi: bool = True,
) -> Any:
    assert _ORIGINAL_MAKE_SSL_CONTEXT is not None
    if not verify or sys.platform != "win32":
        return _ORIGINAL_MAKE_SSL_CONTEXT(
            verify=verify,
            client_certificate=client_certificate,
            client_certificate_key=client_certificate_key,
            client_certificate_password=client_certificate_password,
            legacy_support=legacy_support,
            use_certifi=use_certifi,
        )
    try:
        import truststore
    except ImportError:
        return _ORIGINAL_MAKE_SSL_CONTEXT(
            verify=verify,
            client_certificate=client_certificate,
            client_certificate_key=client_certificate_key,
            client_certificate_password=client_certificate_password,
            legacy_support=legacy_support,
            use_certifi=use_certifi,
        )

    from yt_dlp.networking.exceptions import RequestError

    # Use the OS certificate policy, including managed Windows trust roots,
    # while preserving yt-dlp's ALPN, cipher suite, and TLS 1.2 baseline.
    context: Any = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    if hasattr(context, "keylog_filename"):
        context.keylog_filename = os.environ.get("SSLKEYLOGFILE") or None
    with contextlib.suppress(NotImplementedError):
        context.set_alpn_protocols(["http/1.1"])
    if legacy_support:
        context.options |= 4  # SSL_OP_LEGACY_SERVER_CONNECT
        context.set_ciphers("DEFAULT")
    elif ssl.OPENSSL_VERSION_INFO >= (1, 1, 1) and not ssl.OPENSSL_VERSION.startswith("LibreSSL"):
        context.set_ciphers("@SECLEVEL=2:ECDH+AESGCM:ECDH+CHACHA20:ECDH+AES:DHE+AES:!aNULL:!eNULL:!aDSS:!SHA1:!AESCCM")
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client_certificate:
        try:
            context.load_cert_chain(
                client_certificate,
                keyfile=client_certificate_key,
                password=client_certificate_password,
            )
        except ssl.SSLError as exc:
            raise RequestError("Unable to load client certificate") from exc
        if getattr(context, "post_handshake_auth", None) is not None:
            context.post_handshake_auth = True
    return context


def apply_compatibility_fixes() -> None:
    global _ORIGINAL_MAKE_SSL_CONTEXT
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    from yt_dlp.extractor.youtube._video import YoutubeIE
    from yt_dlp.extractor.youtube.jsc._builtin.quickjs import QuickJSJCP
    from yt_dlp.networking import _helper as net_helper
    from yt_dlp.networking import common as net_common
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
    # Upstream yt-dlp/yt-dlp#18124 (fixes #18123): YouTube's November 2026
    # `main` player variants trigger a preprocessing TypeError in EJS 0.8.0
    # (`a.addOnDisposeCallback is not a function`), leaving zero playable
    # formats. Defaulting `player_js_variant` to `tv` restores n-challenge
    # solving without changing the pinned runtime or QuickJS wrapper bounds.
    YoutubeIE._DEFAULT_PLAYER_JS_VARIANT = "tv"
    # Upstream yt-dlp/yt-dlp#18051: prioritize the TV client ahead of
    # `web_embedded` for authenticated sessions.
    YoutubeIE._DEFAULT_AUTHED_CLIENTS = ("tv_downgraded", "web_embedded", "web")
    QuickJSJCP._construct_stdin = _bounded_ejs_input

    if _ORIGINAL_MAKE_SSL_CONTEXT is None:
        _ORIGINAL_MAKE_SSL_CONTEXT = net_helper.make_ssl_context
    net_helper.make_ssl_context = _make_native_ssl_context
    net_common.make_ssl_context = _make_native_ssl_context
