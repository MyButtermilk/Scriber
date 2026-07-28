from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend_runtime import installer_youtube_holdout_probe as probe
from backend_runtime import launcher


@pytest.fixture(autouse=True)
def _disable_external_yt_dlp_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YTDLP_NO_PLUGINS", "1")


def _runtime_root(tmp_path: Path, name: str = "deno.exe") -> tuple[Path, Path]:
    root = tmp_path / "backend"
    tools = root / "tools" / "ffmpeg"
    tools.mkdir(parents=True)
    runtime = tools / name
    runtime.write_bytes(b"runtime")
    return root, runtime


def _request(runtime: Path, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "requestContract": probe.PROBE_CONTRACT,
        "schemaVersion": 1,
        "caseId": "player-signature",
        "family": "signature-challenge",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "expectedVideoId": "abcdefghijk",
        "runtimeKind": "deno",
        "runtimePath": str(runtime),
        "cacheMode": "cold",
    }
    value.update(updates)
    return value


class _FakeYdl:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    def __enter__(self) -> _FakeYdl:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        assert url == "https://www.youtube.com/watch?v=abcdefghijk"
        assert download is False
        logger = self.options["logger"]
        logger.debug("[youtube] Downloading player 123-main")
        logger.debug("[youtube] [jsc:deno] Solving JS challenges using deno")
        return {
            "id": "abcdefghijk",
            "extractor_key": "Youtube",
            "formats": [
                {
                    "acodec": "opus",
                    "url": "https://media.example.test/audio?sig=signed",
                }
            ],
        }


def test_frozen_probe_parses_exact_cli_policy_and_redacts_media_urls(
    tmp_path: Path,
) -> None:
    root, runtime = _runtime_root(tmp_path)
    observed_args: list[str] = []

    def parse_options(args: list[str]) -> SimpleNamespace:
        observed_args.extend(args)
        return SimpleNamespace(
            options=SimpleNamespace(plugin_dirs=[]),
            ydl_opts={
                "js_runtimes": {"deno": {"path": str(runtime)}},
                "remote_components": [],
                "cachedir": False,
            },
        )

    exit_code, response = probe.execute_probe(
        _request(runtime),
        runtime_root=root,
        parse_options=parse_options,
        ydl_factory=_FakeYdl,
    )

    assert exit_code == 0
    assert response["status"] == "pass"
    assert response["videoId"] == "abcdefghijk"
    assert response["policy"] == {
        "configDiscovery": False,
        "externalPlugins": False,
        "remoteComponents": False,
        "download": False,
        "explicitSingleRuntime": True,
    }
    assert {
        "metadata",
        "audio-format-url",
        "player-js",
        "js-challenge-runtime",
        "signature",
        "js-challenge-solved",
    }.issubset(response["observedCapabilities"])
    assert "--no-config" in observed_args
    assert "--no-plugin-dirs" in observed_args
    assert "--no-js-runtimes" in observed_args
    assert "--no-remote-components" in observed_args
    assert "--no-cache-dir" in observed_args
    encoded = json.dumps(response)
    assert "media.example.test" not in encoded
    assert "youtube.com/watch" not in encoded
    assert str(runtime) not in encoded


def test_frozen_probe_uses_the_pinned_yt_dlp_cli_parser(tmp_path: Path) -> None:
    root, runtime = _runtime_root(tmp_path)

    exit_code, response = probe.execute_probe(
        _request(runtime),
        runtime_root=root,
        ydl_factory=_FakeYdl,
    )

    assert exit_code == 0
    assert response["ytDlpVersion"] == "2026.7.4"
    assert response["ejsVersion"] == "0.8.0"
    assert response["policy"]["remoteComponents"] is False


def test_frozen_probe_format_selection_mode_uses_production_selectors_in_order(tmp_path: Path) -> None:
    root, runtime = _runtime_root(tmp_path)
    observed_selectors: list[str] = []

    class FormatYdl(_FakeYdl):
        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            selector = str(self.options["format"])
            observed_selectors.append(selector)
            if len(observed_selectors) == 1:
                raise RuntimeError("requested format is not available")
            return {
                "id": "abcdefghijk",
                "extractor_key": "Youtube",
                "formats": [],
                "duration": 634,
                "ext": "webm",
                "container": "webm_dash",
                "acodec": "opus",
                "protocol": "https",
                "format_id": "251",
            }

    exit_code, response = probe.execute_probe(
        _request(runtime, formatSelectionProbe=True),
        runtime_root=root,
        ydl_factory=FormatYdl,
    )

    from src.youtube_download import _FORMAT_SELECTORS

    assert exit_code == 0
    assert observed_selectors == list(_FORMAT_SELECTORS[:2])
    assert response["selectedFormat"] == {
        "routeKind": "audio-format",
        "selectorIndex": 2,
        "selector": _FORMAT_SELECTORS[1],
        "ext": "webm",
        "container": "webm_dash",
        "acodec": "opus",
        "protocol": "https",
        "formatId": "251",
        "durationSeconds": 634,
    }
    assert "youtube.com/watch" not in json.dumps(response)


def test_frozen_probe_caption_mode_requires_real_parseable_timed_cues(tmp_path: Path) -> None:
    root, runtime = _runtime_root(tmp_path)
    caption_payload = json.dumps(
        {
            "events": [
                {
                    "tStartMs": 100,
                    "dDurationMs": 900,
                    "segs": [{"utf8": "A real timed caption."}],
                },
                {
                    "tStartMs": 1100,
                    "dDurationMs": 800,
                    "segs": [{"utf8": "The parser must see this cue."}],
                },
            ]
        }
    ).encode()

    class CaptionResponse:
        def __enter__(self) -> CaptionResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit > len(caption_payload)
            return caption_payload

    class CaptionYdl(_FakeYdl):
        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            assert url == "https://www.youtube.com/watch?v=abcdefghijk"
            assert download is False
            return {
                "id": "abcdefghijk",
                "extractor_key": "Youtube",
                "duration": 42,
                "formats": [],
                "language": "en",
                "subtitles": {
                    "en": [
                        {
                            "ext": "json3",
                            "url": "https://captions.example.test/private-token",
                        }
                    ]
                },
            }

        def urlopen(self, _request: object) -> CaptionResponse:
            return CaptionResponse()

    exit_code, response = probe.execute_probe(
        _request(runtime, captionSelectionProbe=True),
        runtime_root=root,
        ydl_factory=CaptionYdl,
    )

    assert exit_code == 0
    assert response["selectedCaption"] == {
        "routeKind": "youtube-captions",
        "captionKind": "manual",
        "captionFormat": "json3",
        "language": "en",
        "cueCount": 2,
        "textChars": 51,
        "durationSeconds": 42,
    }
    encoded = json.dumps(response)
    assert "A real timed caption" not in encoded
    assert "captions.example.test" not in encoded
    assert "private-token" not in encoded


def test_frozen_probe_caption_mode_fails_when_caption_metadata_has_no_parseable_cues(tmp_path: Path) -> None:
    root, runtime = _runtime_root(tmp_path)

    class EmptyResponse:
        def __enter__(self) -> EmptyResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"events":[]}'

    class CaptionYdl(_FakeYdl):
        def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
            assert download is False
            return {
                "id": "abcdefghijk",
                "extractor_key": "Youtube",
                "duration": 42,
                "formats": [],
                "automatic_captions": {
                    "en": [
                        {
                            "ext": "json3",
                            "url": "https://captions.example.test/private-token",
                        }
                    ]
                },
            }

        def urlopen(self, _request: object) -> EmptyResponse:
            return EmptyResponse()

    exit_code, response = probe.execute_probe(
        _request(runtime, captionSelectionProbe=True),
        runtime_root=root,
        ydl_factory=CaptionYdl,
    )

    assert exit_code == 1
    assert response["status"] == "fail"
    assert "captions.example.test" not in json.dumps(response)


def test_frozen_probe_rejects_conflicting_selection_modes(tmp_path: Path) -> None:
    root, runtime = _runtime_root(tmp_path)
    with pytest.raises(probe.ProbeBoundaryError, match="fields"):
        probe.execute_probe(
            _request(runtime, formatSelectionProbe=True, captionSelectionProbe=True),
            runtime_root=root,
        )


def test_frozen_probe_rejects_runtime_outside_backend(tmp_path: Path) -> None:
    root, _runtime = _runtime_root(tmp_path)
    outside = tmp_path / "deno.exe"
    outside.write_bytes(b"outside")

    with pytest.raises(probe.ProbeBoundaryError, match="escaped"):
        probe.execute_probe(_request(outside), runtime_root=root)


def test_frozen_probe_request_contract_is_exact(tmp_path: Path) -> None:
    root, runtime = _runtime_root(tmp_path)
    request = _request(runtime)
    request["unexpected"] = True

    with pytest.raises(probe.ProbeBoundaryError, match="fields"):
        probe.execute_probe(request, runtime_root=root)


def test_frozen_probe_failure_is_bounded_and_does_not_echo_provider_text(
    tmp_path: Path,
) -> None:
    root, runtime = _runtime_root(tmp_path)

    def parse_options(_args: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            options=SimpleNamespace(plugin_dirs=[]),
            ydl_opts={
                "js_runtimes": {"deno": {"path": str(runtime)}},
                "remote_components": [],
                "cachedir": False,
            },
        )

    class FailingYdl(_FakeYdl):
        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            raise RuntimeError(f"HTTP Error 429 for secret URL {url}")

    exit_code, response = probe.execute_probe(
        _request(runtime),
        runtime_root=root,
        parse_options=parse_options,
        ydl_factory=FailingYdl,
    )

    assert exit_code == 1
    assert response["status"] == "fail"
    assert response["failureCode"] == "http_429"
    assert "secret" not in json.dumps(response).casefold()
    assert "youtube.com" not in json.dumps(response).casefold()


def test_frozen_probe_rejects_enabled_external_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, runtime = _runtime_root(tmp_path)
    monkeypatch.delenv("YTDLP_NO_PLUGINS", raising=False)

    with pytest.raises(probe.ProbeBoundaryError, match="plugins"):
        probe.execute_probe(_request(runtime), runtime_root=root)


def test_launcher_routes_only_exact_frozen_probe_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(sys, "argv", ["scriber-backend.exe", "--installer-youtube-holdout-probe"])
    monkeypatch.setattr(launcher, "_runtime_root", lambda: runtime_root)
    monkeypatch.setattr(
        launcher,
        "validate_runtime_layer",
        lambda root: calls.append(("validate", root)) or {},
    )
    monkeypatch.setattr(
        launcher,
        "_validate_python_runtime",
        lambda root, manifest: calls.append(("policy", root)),
    )
    monkeypatch.setattr(
        launcher,
        "_activate_application_layer",
        lambda root, manifest: calls.append(("application", root)),
    )
    monkeypatch.setattr(
        launcher,
        "run_frozen_probe",
        lambda root: calls.append(("probe", root)) or 17,
    )

    assert launcher.main() == 17
    assert calls == [
        ("validate", runtime_root),
        ("policy", runtime_root),
        ("application", runtime_root),
        ("probe", runtime_root),
    ]


def test_launcher_rejects_probe_payload_on_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["scriber-backend.exe", "--installer-youtube-holdout-probe", "https://example.test"],
    )

    assert launcher.main() == 78
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "backend_layer_validation_failed"
    assert "example.test" not in json.dumps(error)
