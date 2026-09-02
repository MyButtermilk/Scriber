from pathlib import Path

from src.omnilingual_asr_stt import omnilingual_asr_available, omnilingual_language_code, transcribe_omnilingual_file


def test_omnilingual_language_code_maps_known_ui_languages_without_guessing():
    assert omnilingual_language_code("de-DE") == "deu_Latn"
    assert omnilingual_language_code("uk") == "ukr_Cyrl"
    assert omnilingual_language_code("auto") is None
    assert omnilingual_language_code("unknown-language") is None


def test_omnilingual_runtime_probe_is_boolean():
    assert isinstance(omnilingual_asr_available(), bool)


def test_file_inference_uses_official_pipeline_arguments(monkeypatch, tmp_path: Path):
    captured = {}

    class Pipeline:
        def transcribe(self, inputs, *, lang, batch_size):
            captured.update(inputs=inputs, lang=lang, batch_size=batch_size)
            return ["  Ergebnis  "]

    monkeypatch.setattr("src.omnilingual_asr_stt._load_pipeline", lambda _model: Pipeline())
    audio = tmp_path / "sample.wav"

    assert transcribe_omnilingual_file(audio, model_card="omniASR_LLM_300M_v2", language="deu_Latn") == "Ergebnis"
    assert captured == {"inputs": [audio], "lang": ["deu_Latn"], "batch_size": 1}
