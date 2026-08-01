from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_marker_repair.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "verify_marker_repair_script",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
verify_marker_repair = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify_marker_repair)


def test_local_tokenizer_is_loaded_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tokenizer_path = tmp_path / "parent-checkpoint"
    tokenizer_path.mkdir()
    calls: list[tuple[object, dict[str, object]]] = []
    identity_calls: list[tuple[object, dict[str, object]]] = []
    expected = object()

    def fake_from_pretrained(
        source: object,
        **kwargs: object,
    ) -> object:
        calls.append((source, kwargs))
        return expected

    monkeypatch.setattr(
        verify_marker_repair.AutoTokenizer,
        "from_pretrained",
        fake_from_pretrained,
    )
    monkeypatch.setattr(
        verify_marker_repair,
        "capture_tokenizer_identity",
        lambda tokenizer, **kwargs: identity_calls.append(
            (tokenizer, kwargs)
        ),
        raising=False,
    )

    tokenizer = verify_marker_repair._load_tokenizer(tokenizer_path)

    assert tokenizer is expected
    assert calls == [
        (
            tokenizer_path.resolve(),
                {
                    "use_fast": True,
                    "local_files_only": True,
                    "fix_mistral_regex": False,
                },
        )
    ]
    assert identity_calls == [
        (
            expected,
            {
                "model_id": verify_marker_repair.BASE_MODEL_ID,
                "revision": verify_marker_repair.BASE_MODEL_REVISION,
                "local_source_path": tokenizer_path.resolve(),
            },
        )
    ]
