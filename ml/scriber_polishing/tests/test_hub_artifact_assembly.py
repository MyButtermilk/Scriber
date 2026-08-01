from __future__ import annotations

from pathlib import Path

import pytest

from scriber_polishing.hub_artifact_assembly import (
    HubArtifactAssemblyError,
    _link_or_copy,
    _prepare_root,
    _safe_destination,
    _write_immutable,
    assemble_private_hub_artifacts,
)


def test_publication_destination_rejects_absolute_and_parent_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "publication"

    assert _safe_destination(root, "runtime/inference.py") == (
        root / "runtime" / "inference.py"
    )
    for unsafe in ("../escape", "runtime/../escape", "/absolute/path"):
        with pytest.raises(HubArtifactAssemblyError, match="unsafe publication path"):
            _safe_destination(root, unsafe)


def test_publication_files_are_idempotent_but_never_replaced(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "tree" / "artifact.bin"
    source.write_bytes(b"verified artifact")

    _link_or_copy(source, destination)
    _link_or_copy(source, destination)
    assert destination.read_bytes() == b"verified artifact"

    changed = tmp_path / "changed.bin"
    changed.write_bytes(b"different artifact")
    with pytest.raises(HubArtifactAssemblyError, match="refusing to replace"):
        _link_or_copy(changed, destination)

    generated = tmp_path / "tree" / "NOTICE"
    _write_immutable(generated, b"bound notice\n")
    _write_immutable(generated, b"bound notice\n")
    with pytest.raises(HubArtifactAssemblyError, match="refusing to replace"):
        _write_immutable(generated, b"changed notice\n")


def test_publication_root_must_be_empty(tmp_path: Path) -> None:
    root = tmp_path / "publication"
    _prepare_root(root)
    _prepare_root(root)
    (root / "unexpected.txt").write_text("occupied", encoding="utf-8")

    with pytest.raises(HubArtifactAssemblyError, match="root is not empty"):
        _prepare_root(root)


def test_assembly_rejects_unknown_variant_before_reading_inputs(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(HubArtifactAssemblyError, match="supported publication candidate"):
        assemble_private_hub_artifacts(
            model_source=missing,
            dataset_source=missing,
            ai_gold_source=missing,
            evaluation_root=missing,
            completed_training_report=missing,
            variant_matrix_report=missing,
            benchmark_report=missing,
            terra_judge_report=missing,
            sol_judge_report=missing,
            regression_suite=missing,
            publication_config=missing,
            gemma_license=missing,
            apache_license=missing,
            output_root=missing,
            selected_variant="int4",
        )
