from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.inference import (
    PredictionResult,
    _canonical_json,
    build_batch_resume_binding,
    main,
    validate_batch_resume_journal,
    write_batch_predictions,
    write_resumable_batch_predictions,
)
from scriber_polishing.protected_spans import legacy_policy_evidence
from scriber_polishing.sst_parser import parse_sst


def _reference(case_id: str, source_text: str) -> dict[str, object]:
    target_sst = f"[DOC]\n[P]{source_text}.[/P]\n[/DOC]"
    return {
        "schema_version": 1,
        "case_id": case_id,
        "split": "test",
        "source_text": source_text,
        "target_sst": target_sst,
        "target_plain_text": f"{source_text}.",
        "target_ast": document_to_dict(parse_sst(target_sst)),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": f"Der Inhalt lautet {source_text}.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Der Resume-Test bleibt deterministisch.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
            ],
            "entities": [],
            "structure": {
                "has_subject": False,
                "has_salutation": False,
                "paragraph_topics": ["Resume-Test"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [],
        "slice_tags": ["resume_test"],
        "is_identity": False,
    }


def _write_references(path: Path, references: list[dict[str, object]]) -> None:
    path.write_bytes(b"".join(_canonical_json(reference) for reference in references))


def _model_fixture(path: Path) -> Path:
    path.mkdir()
    (path / "config.json").write_text(
        '{"dtype":"bfloat16","model_type":"gemma3_text"}\n',
        encoding="utf-8",
    )
    (path / "model.safetensors").write_bytes(b"deterministic-model-fixture")
    return path


def _result(source: str) -> PredictionResult:
    index = int(source.rsplit(" ", maxsplit=1)[-1])
    return PredictionResult(
        prediction_sst=f"[DOC]\n[P]Ergebnis {index}.[/P]\n[/DOC]",
        valid_sst=index % 2 == 1,
        restoration_error=None if index % 3 else "fixture restoration error",
        generation_seconds=index / 10,
        input_tokens=index + 10,
        output_tokens=index + 20,
    )


def _binding(
    *,
    references_path: Path,
    references: list[dict[str, object]],
    model_path: Path,
    predictions_path: Path,
    manifest_path: Path,
    candidate: str = "pilot",
    quantization: str = "none",
    device: str = "cuda",
    max_new_tokens: int = 512,
    policy: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_batch_resume_binding(
        references_path=references_path,
        references=references,
        candidate=candidate,
        model_reference=str(model_path),
        revision=None,
        tokenizer=SimpleNamespace(),
        model=SimpleNamespace(config=SimpleNamespace()),
        quantization=quantization,
        device=device,
        max_new_tokens=max_new_tokens,
        protection_policy=policy or legacy_policy_evidence(),
        protection_binding=None,
        predictions_output=predictions_path,
        manifest_output=manifest_path,
    )


def _manifest_fields(references_path: Path, model_path: Path) -> dict[str, object]:
    return {
        "model": str(model_path),
        "revision": None,
        "reference_sha256": hashlib.sha256(references_path.read_bytes()).hexdigest(),
        "quantization": "none",
        "device": "cuda",
    }


@pytest.fixture
def resume_fixture(
    tmp_path: Path,
) -> tuple[
    list[dict[str, object]],
    Path,
    Path,
    Path,
    Path,
    Path,
    dict[str, object],
]:
    references = [_reference(f"resume_case_{index:03d}", f"Quelle {index}") for index in range(1, 5)]
    references_path = tmp_path / "references.jsonl"
    _write_references(references_path, references)
    model_path = _model_fixture(tmp_path / "model")
    predictions_path = tmp_path / "predictions.jsonl"
    manifest_path = tmp_path / "manifest.json"
    journal_path = tmp_path / "resume.journal.json"
    binding = _binding(
        references_path=references_path,
        references=references,
        model_path=model_path,
        predictions_path=predictions_path,
        manifest_path=manifest_path,
    )
    return (
        references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        binding,
    )


def _interrupt_after_two(source: str) -> PredictionResult:
    if source == "Quelle 3":
        raise RuntimeError("simulated interruption")
    return _result(source)


def test_interrupt_then_resume_only_exact_suffix_and_matches_fresh_run(
    resume_fixture,
    tmp_path: Path,
) -> None:
    (
        references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        binding,
    ) = resume_fixture
    fields = _manifest_fields(references_path, model_path)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_interrupt_after_two,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    assert journal_path.is_file()
    assert not predictions_path.exists()
    assert not manifest_path.exists()
    interrupted_journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert [entry["case_id"] for entry in interrupted_journal["entries"]] == [
        "resume_case_001",
        "resume_case_002",
    ]
    assert interrupted_journal["entries"][1]["evidence"]["input_tokens"] == 12

    resumed_sources: list[str] = []

    def resumed_predictor(source: str) -> PredictionResult:
        resumed_sources.append(source)
        return _result(source)

    resumed_manifest = write_resumable_batch_predictions(
        references,
        predictions_path,
        manifest_path,
        predictor=resumed_predictor,
        candidate="pilot",
        resume_journal=journal_path,
        resume_binding=binding,
        manifest_fields=fields,
    )
    assert resumed_sources == ["Quelle 3", "Quelle 4"]
    assert not journal_path.exists()

    fresh_predictions = tmp_path / "fresh.predictions.jsonl"
    fresh_manifest = write_batch_predictions(
        references,
        fresh_predictions,
        predictor=lambda source: _result(source),
        candidate="pilot",
    )
    fresh_manifest.update(fields)
    expected_manifest_payload = (
        json.dumps(
            fresh_manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    assert predictions_path.read_bytes() == fresh_predictions.read_bytes()
    assert manifest_path.read_bytes() == expected_manifest_payload
    assert resumed_manifest == fresh_manifest


def test_binding_mismatch_fails_without_generating_more_cases(
    resume_fixture,
) -> None:
    (
        references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        binding,
    ) = resume_fixture
    fields = _manifest_fields(references_path, model_path)
    with pytest.raises(RuntimeError):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_interrupt_after_two,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    mismatched = deepcopy(binding)
    mismatched["max_new_tokens"] = 256
    calls: list[str] = []
    with pytest.raises(ValueError, match="binding differs"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=lambda source: calls.append(source) or _result(source),
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=mismatched,
            manifest_fields=fields,
        )
    assert calls == []


def test_changed_model_tree_produces_a_binding_mismatch(
    resume_fixture,
) -> None:
    (
        references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        binding,
    ) = resume_fixture
    fields = _manifest_fields(references_path, model_path)
    with pytest.raises(RuntimeError):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_interrupt_after_two,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    (model_path / "model.safetensors").write_bytes(b"changed-model-fixture")
    changed_binding = _binding(
        references_path=references_path,
        references=references,
        model_path=model_path,
        predictions_path=predictions_path,
        manifest_path=manifest_path,
    )
    assert changed_binding["model"]["tree_sha256"] != binding["model"]["tree_sha256"]
    with pytest.raises(ValueError, match="binding differs"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_result,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=changed_binding,
            manifest_fields=fields,
        )


@pytest.mark.parametrize("corruption", ["middle_prefix", "duplicate", "invalid_json"])
def test_corrupt_or_non_prefix_journal_fails_closed(
    resume_fixture,
    corruption: str,
) -> None:
    (
        references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        binding,
    ) = resume_fixture
    fields = _manifest_fields(references_path, model_path)
    with pytest.raises(RuntimeError):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_interrupt_after_two,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    if corruption == "invalid_json":
        journal_path.write_bytes(b'{"truncated":')
    else:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if corruption == "middle_prefix":
            entry = journal["entries"][0]
            entry["case_id"] = "resume_case_003"
            entry["prediction"]["case_id"] = "resume_case_003"
            entry["evidence"]["case_id"] = "resume_case_003"
            unhashed = {key: value for key, value in entry.items() if key != "entry_sha256"}
            entry["entry_sha256"] = "sha256:" + hashlib.sha256(_canonical_json(unhashed)).hexdigest()
        else:
            journal["entries"][1] = deepcopy(journal["entries"][0])
        journal_path.write_bytes(_canonical_json(journal))
    with pytest.raises(ValueError):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_result,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    assert not predictions_path.exists()
    assert not manifest_path.exists()


def test_completed_journal_recovers_if_manifest_finalization_was_interrupted(
    resume_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        binding,
    ) = resume_fixture
    fields = _manifest_fields(references_path, model_path)
    import scriber_polishing.inference as inference

    real_write_manifest = inference._write_manifest
    attempts = 0

    def interrupted_manifest_write(path: Path, value) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("manifest finalization interrupted")
        real_write_manifest(path, value)

    monkeypatch.setattr(inference, "_write_manifest", interrupted_manifest_write)
    with pytest.raises(RuntimeError, match="finalization interrupted"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_result,
            candidate="pilot",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    assert predictions_path.is_file()
    assert not manifest_path.exists()
    assert journal_path.is_file()
    assert len(json.loads(journal_path.read_text(encoding="utf-8"))["entries"]) == 4

    calls: list[str] = []
    write_resumable_batch_predictions(
        references,
        predictions_path,
        manifest_path,
        predictor=lambda source: calls.append(source) or _result(source),
        candidate="pilot",
        resume_journal=journal_path,
        resume_binding=binding,
        manifest_fields=fields,
    )
    assert calls == []
    assert manifest_path.is_file()
    assert not journal_path.exists()


def test_validator_rejects_recomputed_binding_hash_with_stale_binding(
    resume_fixture,
) -> None:
    references, _refs, _model, _pred, _manifest, _journal, binding = resume_fixture
    journal = {
        "schema_version": 1,
        "kind": "scriber_batch_inference_resume_journal",
        "status": "in_progress",
        "binding_sha256": "sha256:" + hashlib.sha256(_canonical_json(binding)).hexdigest(),
        "binding": binding,
        "entries": [],
    }
    assert (
        validate_batch_resume_journal(
            journal,
            references=references,
            expected_binding=binding,
        )
        == journal
    )
    changed = deepcopy(binding)
    changed["device"] = "cpu"
    with pytest.raises(ValueError, match="binding differs"):
        validate_batch_resume_journal(
            journal,
            references=references,
            expected_binding=changed,
        )


def test_cli_resume_journal_restarts_only_missing_suffix(
    resume_fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _references,
        references_path,
        model_path,
        predictions_path,
        manifest_path,
        journal_path,
        _binding_value,
    ) = resume_fixture
    monkeypatch.setattr(
        "scriber_polishing.inference.load_transformers_runtime",
        lambda **_kwargs: (
            SimpleNamespace(),
            SimpleNamespace(config=SimpleNamespace()),
        ),
    )
    first_sources: list[str] = []

    def first_generate(source: str, **_kwargs) -> PredictionResult:
        first_sources.append(source)
        return _interrupt_after_two(source)

    monkeypatch.setattr(
        "scriber_polishing.inference.generate_prediction",
        first_generate,
    )
    arguments = [
        "--model",
        str(model_path),
        "--references",
        str(references_path),
        "--predictions-output",
        str(predictions_path),
        "--manifest-output",
        str(manifest_path),
        "--candidate",
        "pilot",
        "--resume-journal",
        str(journal_path),
    ]
    with pytest.raises(RuntimeError, match="simulated interruption"):
        main(arguments)
    assert first_sources == ["Quelle 1", "Quelle 2", "Quelle 3"]
    assert journal_path.is_file()
    assert not predictions_path.exists()

    resumed_sources: list[str] = []

    def resumed_generate(source: str, **_kwargs) -> PredictionResult:
        resumed_sources.append(source)
        return _result(source)

    monkeypatch.setattr(
        "scriber_polishing.inference.generate_prediction",
        resumed_generate,
    )
    assert main(arguments) == 0
    assert resumed_sources == ["Quelle 3", "Quelle 4"]
    assert predictions_path.is_file()
    assert manifest_path.is_file()
    assert not journal_path.exists()


def test_hub_resume_requires_and_binds_an_exact_resolved_revision(
    tmp_path: Path,
) -> None:
    references = [_reference("hub_resume_case", "Quelle 1")]
    references_path = tmp_path / "references.jsonl"
    _write_references(references_path, references)
    revision = "a" * 40
    binding = build_batch_resume_binding(
        references_path=references_path,
        references=references,
        candidate="base_model",
        model_reference="example/private-model",
        revision=revision,
        tokenizer=SimpleNamespace(init_kwargs={"_commit_hash": revision}),
        model=SimpleNamespace(config=SimpleNamespace(_commit_hash=revision)),
        quantization="none",
        device="cuda",
        max_new_tokens=512,
        protection_policy=legacy_policy_evidence(),
        protection_binding=None,
        predictions_output=tmp_path / "predictions.jsonl",
        manifest_output=tmp_path / "manifest.json",
    )
    assert binding["model"]["kind"] == "hub_revision"
    assert binding["model"]["resolved_revision"] == revision
    assert binding["model"]["tree_sha256"] is None

    with pytest.raises(ValueError, match="exact resolved"):
        build_batch_resume_binding(
            references_path=references_path,
            references=references,
            candidate="base_model",
            model_reference="example/private-model",
            revision=None,
            tokenizer=SimpleNamespace(init_kwargs={}),
            model=SimpleNamespace(config=SimpleNamespace()),
            quantization="none",
            device="cuda",
            max_new_tokens=512,
            protection_policy=legacy_policy_evidence(),
            protection_binding=None,
            predictions_output=tmp_path / "other.predictions.jsonl",
            manifest_output=tmp_path / "other.manifest.json",
        )
