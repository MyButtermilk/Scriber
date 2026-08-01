from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from scriber_polishing.hf_v2_final_evaluation import v2_final_evaluation_runner_payload
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


def test_local_wrapper_extracts_hash_bound_namespaced_packet_stages(tmp_path: Path) -> None:
    runner = v2_final_evaluation_runner_payload().decode("utf-8")

    combined = extract_runner_python_stage(runner, "combined case coverage differs")
    final = extract_runner_python_stage(runner, "Terra source count differs")

    assert 'suite+"__"+row["case_id"]' in combined
    assert 'suite+"__"+case_id' in final
    assert "len({row[\"case_id\"] for row in terra})!=300" in final
    for index, source in enumerate((combined, final)):
        path = tmp_path / f"stage-{index}.py"
        path.write_text(source + "\n", encoding="utf-8", newline="\n")
        completed = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert completed.returncode == 0, completed.stderr
        assert re.search(r"case_id", source)


def test_local_wrapper_canonicalizes_windows_packet_json_without_changing_values(tmp_path: Path) -> None:
    path = tmp_path / "selection.json"
    path.write_bytes(b'{"suite":"ai_gold","selected_record_count":100}\r\n')

    binding = canonicalize_packet_json_file(path)

    assert path.read_bytes() == b'{"selected_record_count":100,"suite":"ai_gold"}\n'
    assert binding == {
        "bytes": 48,
        "sha256": "sha256:31172b098dea8ddd53a9fec99401f0be0b0c805cab8c66b05e7251637c9c24c2",
    }
