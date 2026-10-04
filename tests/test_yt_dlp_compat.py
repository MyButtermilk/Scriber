import json
from types import SimpleNamespace

from backend_runtime.yt_dlp_compat import _bounded_ejs_input, apply_compatibility_fixes


def test_current_player_does_not_emit_multi_megabyte_preprocessed_cache():
    provider = SimpleNamespace(_lib_script=SimpleNamespace(code="LIB"), _core_script=SimpleNamespace(code="CORE"))
    request = SimpleNamespace(type=SimpleNamespace(value="n"), input=SimpleNamespace(challenges=["test"]))
    player = 'player with "quotes" and output_preprocessed: true'
    script = _bounded_ejs_input(provider, player, False, [request])
    data = json.loads(script.split("console.log(JSON.stringify(jsc(")[1].removesuffix(")));\n"))
    assert data == {
        "type": "player",
        "player": player,
        "output_preprocessed": False,
        "requests": [{"type": "n", "challenges": ["test"]}],
    }
    assert "Object.assign(globalThis, lib)" in script


def test_existing_cached_player_and_default_client_selection_remain_supported():
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    from yt_dlp.extractor.youtube._video import YoutubeIE
    from yt_dlp.extractor.youtube.jsc._builtin.quickjs import QuickJSJCP

    defaults = YoutubeIE._DEFAULT_CLIENTS
    apply_compatibility_fixes()
    apply_compatibility_fixes()
    assert defaults == YoutubeIE._DEFAULT_CLIENTS
    client = INNERTUBE_CLIENTS["tv_downgraded"]["INNERTUBE_CONTEXT"]["client"]
    assert client["deviceMake"] == "Samsung"
    assert client["osName"] == "Tizen"
    assert QuickJSJCP._construct_stdin is _bounded_ejs_input
    provider = SimpleNamespace(_lib_script=SimpleNamespace(code=""), _core_script=SimpleNamespace(code=""))
    script = _bounded_ejs_input(provider, "cached", True, [])
    assert '"preprocessed_player": "cached"' in script
    assert '"output_preprocessed"' not in script
