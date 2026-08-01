from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import torch

from scriber_polishing.ast_codec import document_to_dict
from scriber_polishing.inference import (
    PredictionResult,
    _canonical_json,
    build_batch_resume_binding,
    generate_batch_predictions,
    generate_prediction,
    main,
    validate_batch_prediction_manifest,
    validate_batch_resume_journal,
    write_batch_predictions,
    write_resumable_batch_predictions,
)
from scriber_polishing.protected_spans import legacy_policy_evidence
from scriber_polishing.sst_parser import parse_sst


class _CpuBatchTokenizer:
    eos_token_id = 2
    pad_token_id = 0
    padding_side = "right"

    def __init__(self) -> None:
        self._inputs: dict[str, list[int]] = {}
        self._outputs = {
            101: "[DOC]\n[P]Kurz.[/P]\n[/DOC]",
            102: "[DOC]\n[P]Betrag ⟦SCRIBER_PROTECTED_0000⟧.[/P]\n[/DOC]",
            103: "kein SST",
            104: "[DOC]\n[P]Fehler ohne Platzhalter.[/P]\n[/DOC]",
        }

    def _input_ids(self, content: str) -> list[int]:
        if content not in self._inputs:
            marker = 100 + len(self._inputs) + 1
            self._inputs[content] = [11, marker] if marker != 102 else [11, 12, 13, marker]
        return self._inputs[content]

    def apply_chat_template(
        self,
        conversations: list[Any],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        return_dict: bool = False,
        return_tensors: str | None = None,
        padding: bool = False,
    ) -> Any:
        assert tokenize is True
        assert add_generation_prompt is True
        is_batch = bool(conversations and isinstance(conversations[0], list))
        rows = [
            self._input_ids(conversation[0]["content"])
            for conversation in (conversations if is_batch else [conversations])
        ]
        if not return_dict:
            return rows if is_batch else rows[0]
        assert return_tensors == "pt"
        width = max(len(row) for row in rows)
        padded: list[list[int]] = []
        masks: list[list[int]] = []
        for row in rows:
            missing = width - len(row)
            if self.padding_side == "left":
                padded.append([self.pad_token_id] * missing + row)
                masks.append([0] * missing + [1] * len(row))
            else:
                padded.append(row + [self.pad_token_id] * missing)
                masks.append([1] * len(row) + [0] * missing)
        return {
            "input_ids": torch.tensor(padded, dtype=torch.long),
            "attention_mask": torch.tensor(masks, dtype=torch.long),
        }

    def decode(self, tokens: list[int], *, skip_special_tokens: bool) -> str:
        assert skip_special_tokens is True
        content = [token for token in tokens if token not in {self.eos_token_id, self.pad_token_id}]
        if not content:
            return ""
        return self._outputs[content[0]]


class _CpuGreedyModel:
    device = torch.device("cpu")

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []
        self.config = SimpleNamespace()
        self.generation_config = SimpleNamespace(eos_token_id=2)

    def to(self, _device: str) -> _CpuGreedyModel:
        return self

    def eval(self) -> None:
        return None

    def generate(
        self,
        input_ids: Any,
        *,
        attention_mask: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        assert kwargs["do_sample"] is False
        assert kwargs["num_beams"] == 1
        rows = input_ids.tolist()
        masks = attention_mask.tolist() if attention_mask is not None else [[1] * len(row) for row in rows]
        self.batch_sizes.append(len(rows))
        completions: list[list[int]] = []
        for row, mask in zip(rows, masks, strict=True):
            marker = [token for token, active in zip(row, mask, strict=True) if active][-1]
            filler = list(range(70, 70 + marker - 100))
            completion = [marker, *filler, self._eos(marker)]
            completions.append(completion)
        width = max(len(completion) for completion in completions)
        generated = [
            [*row, *completion, *([2] * (width - len(completion)))]
            for row, completion in zip(rows, completions, strict=True)
        ]
        return torch.tensor(generated, dtype=torch.long)

    @staticmethod
    def _eos(marker: int) -> int:
        return 2


def _stable(result: PredictionResult) -> PredictionResult:
    return replace(result, generation_seconds=0.0)


def test_cpu_greedy_batch_matches_individual_outputs_for_mixed_lengths_and_placeholders() -> None:
    sources = [
        "kurz",
        "Der Betrag ist 1.234,56 €",
        "ungueltig",
        "Fehler 99,00 €",
    ]
    tokenizer = _CpuBatchTokenizer()
    single_model = _CpuGreedyModel()
    expected = [_stable(generate_prediction(source, tokenizer=tokenizer, model=single_model)) for source in sources]
    batch_model = _CpuGreedyModel()

    actual = [
        _stable(result)
        for result in generate_batch_predictions(
            sources,
            tokenizer=tokenizer,
            model=batch_model,
        )
    ]

    assert actual == expected
    assert actual[1].prediction_sst == "[DOC]\n[P]Betrag 1.234,56 €.[/P]\n[/DOC]"
    assert actual[2].valid_sst is False
    assert actual[3].restoration_error is not None
    assert single_model.batch_sizes == [1, 1, 1, 1]
    assert batch_model.batch_sizes == [4]
    assert tokenizer.padding_side == "right"


def _reference(case_id: str, source_text: str) -> dict[str, object]:
    target_sst = "[DOC]\n[P]Referenz.[/P]\n[/DOC]"
    return {
        "schema_version": 1,
        "case_id": case_id,
        "split": "test",
        "source_text": source_text,
        "target_sst": target_sst,
        "target_plain_text": "Referenz.",
        "target_ast": document_to_dict(parse_sst(target_sst)),
        "semantic_plan": {
            "facts": [
                {
                    "fact_id": "f1",
                    "order": 1,
                    "text": "Der Inhalt ist eine Referenz.",
                    "polarity": "positive",
                    "modality": "fact",
                    "condition": None,
                    "protected_values": [],
                },
                {
                    "fact_id": "f2",
                    "order": 2,
                    "text": "Die Inferenz bleibt deterministisch.",
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
                "paragraph_topics": ["Referenz"],
                "heading_levels": [],
                "list_kind": "none",
                "list_items": [],
                "has_closing": False,
                "signature_lines": [],
            },
        },
        "protected_spans": [],
        "slice_tags": ["batch_test"],
        "is_identity": False,
    }


def _write_references(path: Path) -> None:
    references = [
        _reference("batch_case_001", "kurz"),
        _reference("batch_case_002", "Der Betrag ist 1.234,56 €"),
        _reference("batch_case_003", "ungueltig"),
    ]
    path.write_text(
        "".join(
            json.dumps(
                reference,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for reference in references
        ),
        encoding="utf-8",
    )


def _install_cpu_fake_transformers(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_CpuGreedyModel]:
    loaded_models: list[_CpuGreedyModel] = []

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(*_args: Any, **_kwargs: Any) -> _CpuBatchTokenizer:
            return _CpuBatchTokenizer()

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(*_args: Any, **_kwargs: Any) -> _CpuGreedyModel:
            model = _CpuGreedyModel()
            loaded_models.append(model)
            return model

    transformers = ModuleType("transformers")
    transformers.AutoModelForCausalLM = AutoModelForCausalLM
    transformers.AutoTokenizer = AutoTokenizer
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    return loaded_models


def _run_reference_cli(
    *,
    references: Path,
    output_root: Path,
    batch_size: int | None,
) -> None:
    arguments = [
        "--model",
        "cpu-fake-model",
        "--references",
        str(references),
        "--predictions-output",
        str(output_root / "predictions.jsonl"),
        "--manifest-output",
        str(output_root / "manifest.json"),
        "--candidate",
        "candidate_a",
        "--device",
        "cpu",
    ]
    if batch_size is not None:
        arguments.extend(["--batch-size", str(batch_size)])
    assert main(arguments) == 0


def test_reference_cli_batch_preserves_prediction_bytes_and_deterministic_manifest_counters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = tmp_path / "references.jsonl"
    _write_references(references)
    loaded_models = _install_cpu_fake_transformers(monkeypatch)
    default_root = tmp_path / "default"
    batch_root = tmp_path / "batch"

    _run_reference_cli(
        references=references,
        output_root=default_root,
        batch_size=None,
    )
    _run_reference_cli(
        references=references,
        output_root=batch_root,
        batch_size=3,
    )

    assert (batch_root / "predictions.jsonl").read_bytes() == (default_root / "predictions.jsonl").read_bytes()
    default_manifest = json.loads((default_root / "manifest.json").read_text(encoding="utf-8"))
    batch_manifest = json.loads((batch_root / "manifest.json").read_text(encoding="utf-8"))
    assert default_manifest["batch_size"] == 1
    assert batch_manifest["batch_size"] == 3
    for field in (
        "prediction_sha256",
        "record_count",
        "valid_sst_count",
        "restoration_error_count",
        "total_input_tokens",
        "total_output_tokens",
    ):
        assert batch_manifest[field] == default_manifest[field]
    assert loaded_models[0].batch_sizes == [1, 1, 1]
    assert loaded_models[1].batch_sizes == [3]


def _fixed_result(source: str) -> PredictionResult:
    return PredictionResult(
        prediction_sst=f"[DOC]\n[P]{source}.[/P]\n[/DOC]",
        valid_sst=True,
        restoration_error=None,
        generation_seconds=0.25,
        input_tokens=len(source),
        output_tokens=5,
    )


def _model_fixture(path: Path) -> None:
    path.mkdir()
    (path / "config.json").write_text(
        '{"model_type":"gemma3_text"}\n',
        encoding="utf-8",
    )
    (path / "model.safetensors").write_bytes(b"cpu-fake-model")


def test_resumable_batches_commit_only_complete_results_and_reject_batch_size_changes(
    tmp_path: Path,
) -> None:
    references = [_reference(f"resume_batch_{index:03d}", f"Quelle {index}") for index in range(1, 6)]
    references_path = tmp_path / "references.jsonl"
    references_path.write_bytes(b"".join(_canonical_json(reference) for reference in references))
    model_path = tmp_path / "model"
    _model_fixture(model_path)
    predictions_path = tmp_path / "predictions.jsonl"
    manifest_path = tmp_path / "manifest.json"
    journal_path = tmp_path / "resume.json"
    binding = build_batch_resume_binding(
        references_path=references_path,
        references=references,
        candidate="candidate_a",
        model_reference=str(model_path),
        revision=None,
        tokenizer=SimpleNamespace(),
        model=SimpleNamespace(config=SimpleNamespace()),
        quantization="none",
        device="cpu",
        max_new_tokens=512,
        batch_size=2,
        protection_policy=legacy_policy_evidence(),
        protection_binding=None,
        predictions_output=predictions_path,
        manifest_output=manifest_path,
    )
    fields = {
        "model": str(model_path),
        "revision": None,
        "reference_sha256": binding["reference_sha256"].removeprefix("sha256:"),
        "quantization": "none",
        "device": "cpu",
    }
    calls = 0

    def interrupted_batch(sources: list[str]) -> list[PredictionResult]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated batch interruption")
        return [_fixed_result(source) for source in sources]

    with pytest.raises(RuntimeError, match="simulated batch interruption"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_fixed_result,
            batch_predictor=interrupted_batch,
            batch_size=2,
            candidate="candidate_a",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert [entry["case_id"] for entry in journal["entries"]] == [
        "resume_batch_001",
        "resume_batch_002",
    ]
    with pytest.raises(ValueError, match="binding"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_fixed_result,
            batch_size=1,
            candidate="candidate_a",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    assert json.loads(journal_path.read_text(encoding="utf-8")) == journal

    resumed_batches: list[list[str]] = []

    def resumed_batch(sources: list[str]) -> list[PredictionResult]:
        resumed_batches.append(list(sources))
        return [_fixed_result(source) for source in sources]

    resumed_manifest = write_resumable_batch_predictions(
        references,
        predictions_path,
        manifest_path,
        predictor=_fixed_result,
        batch_predictor=resumed_batch,
        batch_size=2,
        candidate="candidate_a",
        resume_journal=journal_path,
        resume_binding=binding,
        manifest_fields=fields,
    )
    fresh_path = tmp_path / "fresh.jsonl"
    fresh_manifest = write_batch_predictions(
        references,
        fresh_path,
        predictor=_fixed_result,
        batch_predictor=lambda sources: [_fixed_result(source) for source in sources],
        batch_size=2,
        candidate="candidate_a",
    )

    assert resumed_batches == [["Quelle 3", "Quelle 4"], ["Quelle 5"]]
    assert predictions_path.read_bytes() == fresh_path.read_bytes()
    for field in fresh_manifest:
        assert resumed_manifest[field] == fresh_manifest[field]
    assert not journal_path.exists()


def test_resumable_batch_progress_is_reported_only_after_one_durable_batch_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scriber_polishing.inference as inference

    references = [_reference(f"durable_batch_{index:03d}", f"Quelle {index}") for index in range(1, 6)]
    references_path = tmp_path / "references.jsonl"
    references_path.write_bytes(b"".join(_canonical_json(reference) for reference in references))
    model_path = tmp_path / "model"
    _model_fixture(model_path)
    predictions_path = tmp_path / "predictions.jsonl"
    manifest_path = tmp_path / "manifest.json"
    journal_path = tmp_path / "resume.json"
    binding = build_batch_resume_binding(
        references_path=references_path,
        references=references,
        candidate="candidate_a",
        model_reference=str(model_path),
        revision=None,
        tokenizer=SimpleNamespace(),
        model=SimpleNamespace(config=SimpleNamespace()),
        quantization="none",
        device="cpu",
        max_new_tokens=512,
        batch_size=2,
        protection_policy=legacy_policy_evidence(),
        protection_binding=None,
        predictions_output=predictions_path,
        manifest_output=manifest_path,
    )
    fields = {
        "model": str(model_path),
        "revision": None,
        "reference_sha256": binding["reference_sha256"].removeprefix("sha256:"),
        "quantization": "none",
        "device": "cpu",
    }
    real_write = inference._write_resume_journal
    journal_writes = 0

    def interrupt_second_batch_commit(path: Path, journal: dict[str, Any]) -> None:
        nonlocal journal_writes
        journal_writes += 1
        if journal_writes == 3:
            raise RuntimeError("simulated durable journal interruption")
        real_write(path, journal)

    monkeypatch.setattr(inference, "_write_resume_journal", interrupt_second_batch_commit)
    progress: list[int] = []
    with pytest.raises(RuntimeError, match="durable journal interruption"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_fixed_result,
            batch_predictor=lambda sources: [_fixed_result(source) for source in sources],
            batch_size=2,
            candidate="candidate_a",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
            progress=lambda completed, _total: progress.append(completed),
        )
    durable = json.loads(journal_path.read_text(encoding="utf-8"))
    assert [entry["case_id"] for entry in durable["entries"]] == [
        "durable_batch_001",
        "durable_batch_002",
    ]
    assert progress == [1, 2]
    assert journal_writes == 3

    monkeypatch.setattr(inference, "_write_resume_journal", real_write)
    resumed_sources: list[str] = []

    def resume_batch(sources: list[str]) -> list[PredictionResult]:
        resumed_sources.extend(sources)
        return [_fixed_result(source) for source in sources]

    resumed = write_resumable_batch_predictions(
        references,
        predictions_path,
        manifest_path,
        predictor=_fixed_result,
        batch_predictor=resume_batch,
        batch_size=2,
        candidate="candidate_a",
        resume_journal=journal_path,
        resume_binding=binding,
        manifest_fields=fields,
    )
    fresh_path = tmp_path / "fresh.jsonl"
    fresh = write_batch_predictions(
        references,
        fresh_path,
        predictor=_fixed_result,
        batch_predictor=lambda sources: [_fixed_result(source) for source in sources],
        batch_size=2,
        candidate="candidate_a",
    )
    assert resumed_sources == ["Quelle 3", "Quelle 4", "Quelle 5"]
    assert predictions_path.read_bytes() == fresh_path.read_bytes()
    assert all(resumed[key] == fresh[key] for key in fresh)


def test_batch_durable_resume_accepts_a_legacy_per_record_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scriber_polishing.inference as inference

    references = [_reference(f"legacy_prefix_{index:03d}", f"Quelle {index}") for index in range(1, 5)]
    references_path = tmp_path / "references.jsonl"
    references_path.write_bytes(b"".join(_canonical_json(reference) for reference in references))
    model_path = tmp_path / "model"
    _model_fixture(model_path)
    predictions_path = tmp_path / "predictions.jsonl"
    manifest_path = tmp_path / "manifest.json"
    journal_path = tmp_path / "resume.json"
    binding = build_batch_resume_binding(
        references_path=references_path,
        references=references,
        candidate="candidate_a",
        model_reference=str(model_path),
        revision=None,
        tokenizer=SimpleNamespace(),
        model=SimpleNamespace(config=SimpleNamespace()),
        quantization="none",
        device="cpu",
        max_new_tokens=512,
        batch_size=2,
        protection_policy=legacy_policy_evidence(),
        protection_binding=None,
        predictions_output=predictions_path,
        manifest_output=manifest_path,
    )
    fields = {
        "model": str(model_path),
        "revision": None,
        "reference_sha256": binding["reference_sha256"].removeprefix("sha256:"),
        "quantization": "none",
        "device": "cpu",
    }
    real_write = inference._write_resume_journal
    writes = 0

    def stop_after_first_batch(path: Path, journal: dict[str, Any]) -> None:
        nonlocal writes
        writes += 1
        real_write(path, journal)
        if writes == 2:
            raise RuntimeError("stop after first durable batch")

    monkeypatch.setattr(inference, "_write_resume_journal", stop_after_first_batch)
    with pytest.raises(RuntimeError, match="first durable batch"):
        write_resumable_batch_predictions(
            references,
            predictions_path,
            manifest_path,
            predictor=_fixed_result,
            batch_predictor=lambda sources: [_fixed_result(source) for source in sources],
            batch_size=2,
            candidate="candidate_a",
            resume_journal=journal_path,
            resume_binding=binding,
            manifest_fields=fields,
        )
    legacy = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy["entries"] = legacy["entries"][:1]
    journal_path.write_bytes(_canonical_json(legacy))
    monkeypatch.setattr(inference, "_write_resume_journal", real_write)
    resumed_sources: list[str] = []

    write_resumable_batch_predictions(
        references,
        predictions_path,
        manifest_path,
        predictor=_fixed_result,
        batch_predictor=lambda sources: (
            resumed_sources.extend(sources) or [_fixed_result(source) for source in sources]
        ),
        batch_size=2,
        candidate="candidate_a",
        resume_journal=journal_path,
        resume_binding=binding,
        manifest_fields=fields,
    )
    assert resumed_sources == ["Quelle 2", "Quelle 3", "Quelle 4"]


def test_partial_batch_result_fails_without_publishing_predictions(
    tmp_path: Path,
) -> None:
    references = [
        _reference("partial_batch_001", "Quelle 1"),
        _reference("partial_batch_002", "Quelle 2"),
    ]
    output = tmp_path / "predictions.jsonl"

    with pytest.raises(ValueError, match="wrong number"):
        write_batch_predictions(
            references,
            output,
            predictor=_fixed_result,
            batch_predictor=lambda sources: [_fixed_result(sources[0])],
            batch_size=2,
            candidate="candidate_a",
        )

    assert not output.exists()


def test_current_manifest_and_resume_schema_fail_closed_without_batch_provenance(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": 1,
        "candidate": "candidate_a",
        "record_count": 1,
        "valid_sst_count": 1,
        "restoration_error_count": 0,
        "prediction_sha256": "a" * 64,
        "total_generation_seconds": 0.1,
        "total_input_tokens": 2,
        "total_output_tokens": 3,
    }
    with pytest.raises(ValueError, match="batch_size"):
        validate_batch_prediction_manifest(manifest)

    references = [_reference("legacy_resume_001", "Quelle")]
    legacy_binding = {
        "reference_sha256": "sha256:" + "a" * 64,
        "reference_case_ids_sha256": "sha256:" + "b" * 64,
        "reference_count": 1,
        "candidate": "candidate_a",
        "model": {
            "kind": "hub_revision",
            "model_reference": "example/model",
            "resolved_path": None,
            "requested_revision": "c" * 40,
            "resolved_revision": "c" * 40,
            "tree_sha256": None,
            "config_sha256": None,
            "file_count": None,
            "total_bytes": None,
            "identity_sha256": "sha256:" + "d" * 64,
        },
        "quantization": "none",
        "device": "cpu",
        "max_new_tokens": 512,
        "protection_policy": {
            "policy_id": "legacy",
            "policy_sha256": "sha256:" + "e" * 64,
            "evidence_sha256": "sha256:" + "f" * 64,
            "source": "legacy",
            "artifact_sha256": None,
        },
        "predictions_output_path": str(tmp_path / "predictions.jsonl"),
        "manifest_output_path": str(tmp_path / "manifest.json"),
    }
    journal = {
        "schema_version": 1,
        "kind": "scriber_batch_inference_resume_journal",
        "status": "in_progress",
        "binding_sha256": "sha256:" + "0" * 64,
        "binding": legacy_binding,
        "entries": [],
    }
    with pytest.raises(ValueError, match="batch_size"):
        validate_batch_resume_journal(
            journal,
            references=references,
            expected_binding=legacy_binding,
        )


@pytest.mark.parametrize(
    "arguments, message",
    [
        (
            ["--model", "unused", "--text", "roh", "--batch-size", "2"],
            "--batch-size greater than one requires --references",
        ),
        (
            [
                "--model",
                "unused",
                "--references",
                "unused.jsonl",
                "--predictions-output",
                "unused.predictions.jsonl",
                "--manifest-output",
                "unused.manifest.json",
                "--candidate",
                "candidate_a",
                "--batch-size",
                "0",
            ],
            "--batch-size must be positive",
        ),
    ],
)
def test_cli_rejects_batching_outside_reference_mode_or_nonpositive_sizes(
    arguments: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    assert message in capsys.readouterr().err
