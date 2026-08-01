from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

from scriber_polishing.model_factory import BASE_MODEL_ID, BASE_MODEL_REVISION
from scriber_polishing.token_length_audit import (
    TOKENIZER_ARTIFACT_FILENAMES,
    audit_token_lengths,
    build_tokenizer_evidence,
)


class _FakeGemmaTokenizer:
    name_or_path = BASE_MODEL_ID
    is_fast = True
    vocab_size = 128
    chat_template = "fake-gemma-chat-template-v1"
    bos_token_id = 1
    eos_token_id = 2
    pad_token_id = 0
    unk_token_id = 3
    padding_side = "right"

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert tokenize is True
        marker = re.search(r"AUDIT_PROMPT=(\d+);AUDIT_TOTAL=(\d+)", messages[0]["content"])
        assert marker is not None
        prompt_length, total_length = (int(value) for value in marker.groups())
        prompt = list(range(prompt_length))
        if len(messages) == 1:
            assert add_generation_prompt is True
            return prompt
        assert add_generation_prompt is False
        return prompt + list(range(prompt_length, total_length))


def _tokenizer_evidence(tmp_path: Path, tokenizer: _FakeGemmaTokenizer) -> dict[str, object]:
    files: dict[str, Path] = {}
    for index, filename in enumerate(TOKENIZER_ARTIFACT_FILENAMES):
        path = tmp_path / "tokenizer" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"tokenizer-artifact-{index}\n".encode())
        files[filename] = path
    return build_tokenizer_evidence(
        tokenizer,
        tokenizer_files=files,
        transformers_version="4.test",
    )


def test_audit_reports_exact_completion_only_length_distribution_and_evidence(tmp_path: Path) -> None:
    tokenizer = _FakeGemmaTokenizer()
    lengths = ((4, 10), (4, 512), (4, 513), (600, 769), (4, 1025))
    rows = [
        {
            "example_id": f"train-{index}",
            "split": "train",
            "source_text": f"AUDIT_PROMPT={prompt};AUDIT_TOTAL={total}",
            "target_sst": f"[DOC]\n[P]Ziel {index}.[/P]\n[/DOC]",
        }
        for index, (prompt, total) in enumerate(lengths)
    ]
    train_path = tmp_path / "train.jsonl"
    train_payload = b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for row in rows
    )
    train_path.write_bytes(train_payload)
    output_path = tmp_path / "audit.json"

    report = audit_token_lengths(
        {"train": train_path},
        tokenizer=tokenizer,
        tokenizer_evidence=_tokenizer_evidence(tmp_path, tokenizer),
        output_path=output_path,
        command=("python", "scripts/audit_token_lengths.py", "--train", "train.jsonl"),
    )

    assert report["model"] == {
        "id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
    }
    assert report["inputs"]["train"]["count"] == 5
    assert report["inputs"]["train"]["sha256"] == "sha256:" + hashlib.sha256(train_payload).hexdigest()
    assert report["statistics"]["all"]["token_length"] == {
        "count": 5,
        "min": 10,
        "p50": 513,
        "p90": 1025,
        "p95": 1025,
        "p99": 1025,
        "max": 1025,
        "mean": 565.8,
    }
    assert report["statistics"]["all"]["counts_above"] == {
        "512": 3,
        "768": 2,
        "1024": 1,
    }
    assert report["statistics"]["all"]["truncation_estimates"]["512"] == {
        "examples_over_limit": 3,
        "fraction_over_limit": 0.6,
        "tokens_removed_by_right_truncation": 771,
        "completion_tokens_removed": 683,
        "examples_with_completion_partially_truncated": 2,
        "examples_with_completion_fully_truncated": 1,
        "examples_rejected_by_training_filter": 3,
    }
    assert report["tokenizer"]["files_sha256"].startswith("sha256:")
    assert report["evidence"]["effective_config"]["thresholds"] == [512, 768, 1024]
    assert report["evidence"]["command"]["argv"] == [
        "python",
        "scripts/audit_token_lengths.py",
        "--train",
        "train.jsonl",
    ]
    assert output_path.read_bytes() == (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def test_audit_rejects_runtime_tokenizer_that_differs_from_bound_evidence(tmp_path: Path) -> None:
    tokenizer = _FakeGemmaTokenizer()
    evidence = _tokenizer_evidence(tmp_path, tokenizer)
    tokenizer.chat_template = "changed-after-evidence-was-built"
    train_path = tmp_path / "train.jsonl"
    train_path.write_text(
        json.dumps(
            {
                "example_id": "train-1",
                "split": "train",
                "source_text": "AUDIT_PROMPT=4;AUDIT_TOTAL=10",
                "target_sst": "[DOC]\n[P]Ziel.[/P]\n[/DOC]",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime tokenizer"):
        audit_token_lengths(
            {"train": train_path},
            tokenizer=tokenizer,
            tokenizer_evidence=evidence,
            output_path=tmp_path / "audit.json",
            command=("audit",),
        )


def test_cli_writes_audit_with_exact_invocation_evidence(tmp_path: Path) -> None:
    script_path = Path(__file__).parents[1] / "scripts" / "audit_token_lengths.py"
    spec = importlib.util.spec_from_file_location(
        "scriber_polishing_audit_token_lengths_cli",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main = module.main

    tokenizer = _FakeGemmaTokenizer()
    evidence = _tokenizer_evidence(tmp_path, tokenizer)
    train_path = tmp_path / "train.jsonl"
    train_path.write_text(
        json.dumps(
            {
                "example_id": "train-1",
                "split": "train",
                "source_text": "AUDIT_PROMPT=4;AUDIT_TOTAL=10",
                "target_sst": "[DOC]\n[P]Ziel.[/P]\n[/DOC]",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "audit.json"
    argv = [
        "--train",
        str(train_path),
        "--output",
        str(output_path),
        "--local-files-only",
    ]

    def load_tokenizer(**kwargs):
        assert kwargs == {"cache_dir": None, "local_files_only": True}
        return tokenizer, evidence

    assert main(argv, tokenizer_loader=load_tokenizer) == 0
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["evidence"]["command"]["argv"][1:] == [
        "scripts/audit_token_lengths.py",
        *argv,
    ]
    assert set(report["evidence"]["implementation"]) == {"module", "script"}


def test_audit_requires_train_split_without_creating_output(tmp_path: Path) -> None:
    tokenizer = _FakeGemmaTokenizer()
    output_path = tmp_path / "audit.json"

    with pytest.raises(ValueError, match="requires the frozen train split"):
        audit_token_lengths(
            {"validation": tmp_path / "validation.jsonl"},
            tokenizer=tokenizer,
            tokenizer_evidence=_tokenizer_evidence(tmp_path, tokenizer),
            output_path=output_path,
            command=("audit",),
        )

    assert not output_path.exists()


@pytest.mark.parametrize(
    "payload, expected_error",
    [
        (b"{not-json}\n", "malformed JSON"),
        (b"[]\n", "must be an object"),
        (
            (
                json.dumps(
                    {
                        "example_id": "bad",
                        "split": "train",
                        "source_text": "",
                        "target_sst": "sst",
                    }
                )
                + "\n"
            ).encode(),
            "source_text",
        ),
        (
            (
                json.dumps(
                    {
                        "example_id": "bad",
                        "split": "validation",
                        "source_text": "source",
                        "target_sst": "sst",
                    }
                )
                + "\n"
            ).encode(),
            "declares split",
        ),
        (
            b"".join(
                (
                    json.dumps(
                        {
                            "example_id": "duplicate",
                            "split": "train",
                            "source_text": "AUDIT_PROMPT=4;AUDIT_TOTAL=10",
                            "target_sst": "sst",
                        }
                    )
                    + "\n"
                ).encode()
                for _ in range(2)
            ),
            "duplicate example_id",
        ),
    ],
)
def test_audit_fails_closed_on_malformed_frozen_rows_without_replacing_report(
    tmp_path: Path,
    payload: bytes,
    expected_error: str,
) -> None:
    tokenizer = _FakeGemmaTokenizer()
    train_path = tmp_path / "train.jsonl"
    train_path.write_bytes(payload)
    output_path = tmp_path / "audit.json"
    previous_report = b'{"previous":"evidence"}\n'
    output_path.write_bytes(previous_report)

    with pytest.raises(ValueError, match=expected_error):
        audit_token_lengths(
            {"train": train_path},
            tokenizer=tokenizer,
            tokenizer_evidence=_tokenizer_evidence(tmp_path, tokenizer),
            output_path=output_path,
            command=("audit",),
        )

    assert output_path.read_bytes() == previous_report


@pytest.mark.skipif(
    os.environ.get("SCRIBER_RUN_REAL_TOKENIZER_TEST") != "1",
    reason="set SCRIBER_RUN_REAL_TOKENIZER_TEST=1 to exercise the cached exact Gemma tokenizer",
)
def test_exact_cached_gemma_tokenizer_renders_one_completion_only_example(tmp_path: Path) -> None:
    from scriber_polishing.token_length_audit import load_exact_tokenizer_and_evidence

    cache_dir_value = os.environ.get("HF_HUB_CACHE")
    tokenizer, evidence = load_exact_tokenizer_and_evidence(
        cache_dir=Path(cache_dir_value) if cache_dir_value else None,
        local_files_only=True,
    )
    train_path = tmp_path / "train.jsonl"
    train_path.write_text(
        json.dumps(
            {
                "example_id": "real-tokenizer-1",
                "split": "train",
                "source_text": "also bitte den termin am montag bestätigen",
                "target_sst": "[DOC]\n[P]Bitte den Termin am Montag bestätigen.[/P]\n[/DOC]",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = audit_token_lengths(
        {"train": train_path},
        tokenizer=tokenizer,
        tokenizer_evidence=evidence,
        output_path=tmp_path / "real-tokenizer-audit.json",
        command=("pytest", "real-tokenizer-integration"),
    )

    statistics = report["statistics"]["all"]
    assert report["tokenizer"]["class"] == "GemmaTokenizerFast"
    assert len(report["tokenizer"]["files"]) == len(TOKENIZER_ARTIFACT_FILENAMES)
    assert statistics["token_length"]["count"] == 1
    assert statistics["token_length"]["max"] > statistics["completion_token_length"]["max"] > 0
