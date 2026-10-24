from __future__ import annotations

from pathlib import Path

import pytest

from scriber_polishing.v2_local_compact_evaluation import (
    V2LocalCompactEvaluationError,
    canonicalize_packet_json_file,
    extract_runner_python_stage,
    reserve_local_evaluation_paths,
)


def test_local_wrapper_reserves_new_sibling_work_and_result_paths(tmp_path: Path) -> None:
    output = tmp_path / "v2-compact-result"

    reservation = reserve_local_evaluation_paths(output)

    assert reservation.output_root == output.resolve()
    assert reservation.work_root == (tmp_path / "v2-compact-result.work").resolve()
    assert reservation.work_root.is_dir()
    assert not reservation.output_root.exists()

    with pytest.raises(V2LocalCompactEvaluationError, match="work directory already exists"):
        reserve_local_evaluation_paths(output)

    second_output = tmp_path / "existing-result"
    second_output.mkdir()
    with pytest.raises(V2LocalCompactEvaluationError, match="output already exists"):
        reserve_local_evaluation_paths(second_output)


def test_local_wrapper_extracts_one_valid_stage_and_rejects_missing_ambiguous_or_invalid_stages() -> None:
    selected_source = "result = 'selected stage'"
    selected = f"python - <<'PY'\n{selected_source}\nPY\n"
    unrelated = "python - <<'PY'\nresult = 'other stage'\nPY\n"

    assert extract_runner_python_stage(unrelated + selected, "selected stage") == selected_source

    for runner in (unrelated, selected + selected):
        with pytest.raises(V2LocalCompactEvaluationError, match="exactly one Python stage"):
            extract_runner_python_stage(runner, "selected stage")

    with pytest.raises(V2LocalCompactEvaluationError, match="does not compile"):
        extract_runner_python_stage("python - <<'PY'\nresult = 'selected stage\nPY\n", "selected stage")


def test_local_wrapper_canonicalizes_windows_packet_json_without_changing_values(tmp_path: Path) -> None:
    path = tmp_path / "selection.json"
    path.write_bytes(b'{"suite":"ai_gold","selected_record_count":100}\r\n')

    binding = canonicalize_packet_json_file(path)

    assert path.read_bytes() == b'{"selected_record_count":100,"suite":"ai_gold"}\n'
    assert binding == {
        "bytes": 48,
        "sha256": "sha256:31172b098dea8ddd53a9fec99401f0be0b0c805cab8c66b05e7251637c9c24c2",
    }
