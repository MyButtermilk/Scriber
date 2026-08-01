"""Immutable V2 final-evaluation packet and evidence verification.

This module is deliberately a preparation boundary.  It can copy and verify
already sealed inputs, produce an immutable packet, and validate later remote
evidence.  It never imports the Hugging Face client, mutates a Bucket, or
starts a Job.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .curriculum import canonical_json_bytes

V2_EVALUATION_ATTEMPT = "attempt14"
V2_EVALUATION_JOB_NAME = "scriber-v2-hardcase-replay-final-evaluation-v1"
V2_EVALUATION_OUTPUT_RELATIVE = f"{V2_EVALUATION_ATTEMPT}/{V2_EVALUATION_JOB_NAME}"
V2_EVALUATION_BUCKET = "hf://buckets/Buttermilk03/scriber-polishing-private-runs"
V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI = f"{V2_EVALUATION_BUCKET}/{V2_EVALUATION_OUTPUT_RELATIVE}"
V2_EVALUATION_OUTPUT_REMOTE_URI = f"{V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI}/final"
V2_EVALUATION_OUTPUT_PREFIX_CONTAINER = f"/outputs/{V2_EVALUATION_JOB_NAME}"
V2_EVALUATION_OUTPUT_CONTAINER = f"{V2_EVALUATION_OUTPUT_PREFIX_CONTAINER}/final"
V2_EVALUATION_PACKET_REMOTE_URI = (
    f"{V2_EVALUATION_BUCKET}/{V2_EVALUATION_ATTEMPT}/packets/{V2_EVALUATION_JOB_NAME}"
)
V2_EVALUATION_OUTPUT_MOUNT_URI = f"{V2_EVALUATION_BUCKET}/{V2_EVALUATION_ATTEMPT}"
V2_EVALUATION_IMAGE = (
    "pytorch/pytorch@sha256:eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
)
V2_EVALUATION_FLAVOR = "l4x1"
V2_EVALUATION_TIMEOUT_MINUTES = 45
V2_COMPACT_RECORDS_PER_SUITE = 100
V2_COMPACT_TOTAL_RECORDS = 300
V2_COMPACT_SELECTION_ALGORITHM = "sha256_case_id_rank_v1"
V2_COMPACT_SELECTION_SEED = "scriber-v2-compact-smoke-v1"

V2_TRAINING_JOB_ID = "6a6e05bf6b79c09949c1e5fd"
V2_TRAINING_PACKET_TREE_SHA256 = "sha256:3d11fbc9441ee4e563b46a87bf354a34fd9ff59d04a784325c725375f67ec5b6"
V2_PARENT_CHECKPOINT_TREE_SHA256 = "sha256:4cf99b2768c5af9c992ee8f6390822a548d6b546ad354368a8d6cfb8ae288ca4"
V2_RUN_FINGERPRINT = "sha256:7b23f23e70f60df9d58961808e529ff7738032de75a7a4a62c12529bc734a45c"
V2_MODEL_DIRECTORY = "final-7b23f23e70f60df9"
V2_MODEL_REMOTE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt13/"
    "scriber-v2-hardcase-replay-v1/training/final-7b23f23e70f60df9"
)
V2_TRAINING_REMOTE_RESULT_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
    "attempt13/scriber-v2-hardcase-replay-v1"
)
V2_MODEL_TREE_SHA256 = "sha256:4233c0084cacdd3ba88207cece0ce20d1e1f56cfbfa64e71182a2deb2934c743"
V2_MODEL_WEIGHT_SHA256 = "sha256:fd86d26f70e128fd1e8d4f11e69d4778ae930e628e374aa64595aca5bd0b9bb3"
V2_MODEL_WEIGHT_BYTES = 536_223_056
V2_POLICY_ARTIFACT_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"

V1_MODEL_TREE_SHA256 = "sha256:5f6caf237fc3e1c3092363d6c17d405463f1cf99a84b0bbbec1720cf125fe3e2"
V1_PACKET_MANIFEST_SHA256 = "sha256:3270d2c3507cfbc128f96001488cef60d9b22d463df8016fe6b3aedf4abb7b9a"
V1_RUNNER_SHA256 = "sha256:8320fd92e0e561a073bc20f837cd1460522167c6888b96f2da475c56284e8a60"
V1_EVALUATOR_SNAPSHOT_TREE_SHA256 = "sha256:75877a883963cdee4fef5d07f2ef92100d9ed843e29e48fc40b8aad74cb3960e"
V1_EVALUATOR_SNAPSHOT_FILE_COUNT = 167
V1_EVALUATOR_SNAPSHOT_BYTES = 2_840_121
V1_EVALUATION_CONFIG_SHA256 = "sha256:656558732668b72d1596b4558e651cd99278d97af3dd822b8e947b6f477fa998"
V1_COMPLETED_RESULT_SHA256 = "sha256:c6a05af6b2076c69c93c802f1b65c1079d1841d51c0cfc08609ee7283effea79"
V1_COMPLETED_OUTPUT_REMOTE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/"
    "attempt12/scriber-full-120k-v2-final-evaluation-v1"
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_PROJECT_ROOT = Path(__file__).parents[2]
_PACKET_SCHEMA = _PROJECT_ROOT / "contracts/hf_v2_final_evaluation_packet_manifest_schema.json"
_LAUNCH_SCHEMA = _PROJECT_ROOT / "contracts/hf_v2_final_evaluation_launch_contract_schema.json"
_RESULT_SCHEMA = _PROJECT_ROOT / "contracts/hf_v2_final_evaluation_result_schema.json"
_POSTFLIGHT_SCHEMA = _PROJECT_ROOT / "contracts/hf_v2_training_completion_model_binding_schema.json"
_TERRA_SOURCE_SCHEMA = _PROJECT_ROOT / "contracts/v2_compact_terra_source_case_schema.json"

_REFERENCES: dict[str, dict[str, object]] = {
    "ai_gold": {
        "records_filename": "ai_gold_automatic_test_challenge.references.jsonl",
        "manifest_filename": "ai_gold_automatic_test_challenge.references.manifest.json",
        "source_record_count": 1750,
        "selected_record_count": V2_COMPACT_RECORDS_PER_SUITE,
        "split_counts": {"challenge": 750, "test": 1000},
        "records_sha256": "sha256:e12c672e45fccf8beccc1fc06c36d8a45540c7ee7c0149bbce773a1136c6c664",
        "manifest_sha256": "sha256:20b375a4584ff91468f6482f074004f72cecd3c2b70c7dc140f3dacd25f9248a",
        "usage": "inference_and_evaluation_only",
    },
    "critical_regression": {
        "records_filename": "critical_regression_as_challenge.references.jsonl",
        "manifest_filename": "critical_regression_as_challenge.references.manifest.json",
        "source_record_count": 500,
        "selected_record_count": V2_COMPACT_RECORDS_PER_SUITE,
        "split_counts": {"challenge": 500},
        "records_sha256": "sha256:5b5bffe762e718b0f8d2dd0905a732d71f5ff21256c9c8d6cc06bb396ff2fd14",
        "manifest_sha256": "sha256:a980d4d84510c5de3d3cb647322f0af4d36e23b333b33940c7d41c7b399fd52f",
        "usage": "inference_and_evaluation_only",
    },
    "regular_test": {
        "records_filename": "regular_test_1000.references.jsonl",
        "manifest_filename": "regular_test_1000.references.manifest.json",
        "source_record_count": 1000,
        "selected_record_count": V2_COMPACT_RECORDS_PER_SUITE,
        "split_counts": {"test": 1000},
        "records_sha256": "sha256:01e92d42516a5356f1b3607e15fc7faee2e47ddf717d0781aca60c2933a276fb",
        "manifest_sha256": "sha256:ee6ee04a72234adfb336d7907a1e5e4143c58ddee3b78dc79a03d2b401af3ca2",
        "usage": "inference_and_evaluation_only",
    },
}

_V1_FINAL_REPORTS = {
    "ai_gold": {
        "path": "final/ai_gold/evaluation.json",
        "bytes": 11_618_677,
        "sha256": "sha256:e46bb211bf3b41bbcd6028b23fee1691bb94551eba293deab7b134a25c3460ee",
    },
    "critical_regression": {
        "path": "final/critical_regression/evaluation.json",
        "bytes": 3_559_245,
        "sha256": "sha256:392f76424c5690c62f430a6d47f06baf911bef4d3cc63f44c7fcf88ae8a5b4e8",
    },
    "regular_test": {
        "path": "final/regular_test/evaluation.json",
        "bytes": 7_755_172,
        "sha256": "sha256:169d85b6fdaaaf6551a45e4ac90a01fd7dc4ae5c6373c3b7ae18a9896eb702c1",
    },
}


class V2FinalEvaluationError(ValueError):
    """An immutable V2 evaluation input or result failed closed."""


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise V2FinalEvaluationError(f"{label} must be a regular file")
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2FinalEvaluationError(f"{label} changed while hashing")
    return payload


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2FinalEvaluationError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise V2FinalEvaluationError(f"{label} must contain an object")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise V2FinalEvaluationError(f"{label} must be an object")
    return value


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise V2FinalEvaluationError(f"{label} must be sha256:<64 lowercase hex>")
    return value


def _safe_relative(value: str) -> bool:
    candidate = Path(value)
    return bool(value) and not candidate.is_absolute() and ".." not in candidate.parts and "\\" not in value


def _validate_schema(path: Path, value: Mapping[str, object], label: str) -> None:
    schema = _json_object(_regular_bytes(path, f"{label} schema"), f"{label} schema")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(value)),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise V2FinalEvaluationError(f"{label} schema failed at {location}: {errors[0].message}")


def _validate_postflight_binding(value: Mapping[str, object]) -> dict[str, object]:
    _validate_schema(_POSTFLIGHT_SCHEMA, value, "V2 postflight binding")
    job = _mapping(value.get("job"), "postflight job")
    source = _mapping(value.get("source"), "postflight source")
    training = _mapping(value.get("training"), "postflight training")
    model = _mapping(value.get("model"), "postflight model")
    runtime = _mapping(value.get("runtime"), "postflight runtime")
    verification = _mapping(value.get("verification"), "postflight verification")
    evidence = _mapping(value.get("evidence"), "postflight evidence")
    weights = model.get("weight_files")
    expected_weight = {
        "path": "model.safetensors",
        "bytes": V2_MODEL_WEIGHT_BYTES,
        "sha256": V2_MODEL_WEIGHT_SHA256,
    }
    exact_checks = (
        (value.get("schema_version"), 1),
        (value.get("kind"), "scriber_hf_v2_training_completion_model_binding"),
        (value.get("status"), "complete"),
        (job.get("id"), V2_TRAINING_JOB_ID),
        (job.get("terminal_status"), "COMPLETED"),
        (job.get("remote_result_uri"), V2_TRAINING_REMOTE_RESULT_URI),
        (source.get("packet_tree_sha256"), V2_TRAINING_PACKET_TREE_SHA256),
        (source.get("parent_checkpoint_tree_sha256"), V2_PARENT_CHECKPOINT_TREE_SHA256),
        (training.get("completed_steps"), 640),
        (training.get("max_steps"), 640),
        (training.get("learning_rate"), 5e-6),
        (training.get("bf16"), True),
        (model.get("run_fingerprint"), V2_RUN_FINGERPRINT),
        (model.get("directory"), V2_MODEL_DIRECTORY),
        (model.get("remote_uri"), V2_MODEL_REMOTE_URI),
        (model.get("tree_sha256"), V2_MODEL_TREE_SHA256),
        (weights, [expected_weight]),
        (model.get("protection_policy_sha256"), V2_POLICY_ARTIFACT_SHA256),
        (model.get("reload_status"), "passed"),
        (runtime.get("cuda_available"), True),
        (runtime.get("bf16_supported"), True),
        (runtime.get("device_count"), 1),
        (verification.get("metadata_only"), True),
        (verification.get("model_downloaded"), False),
        (verification.get("external_mutation_performed"), False),
    )
    if any(actual != expected for actual, expected in exact_checks):
        raise V2FinalEvaluationError("postflight binding differs from the exact completed V2 training run")
    for container, keys in (
        (model, ("inventory_sha256", "reload_verification_sha256")),
        (runtime, ("verification_sha256",)),
        (evidence, ("training_report_sha256", "training_result_sha256", "remote_inventory_sha256")),
    ):
        for key in keys:
            _require_hash(container.get(key), f"postflight {key}")
    return json.loads(json.dumps(value))


def _file_record(root: Path, path: Path) -> dict[str, object]:
    payload = _regular_bytes(path, f"packet source {path}")
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(payload),
        "sha256": _sha256(payload),
    }


def _selected_v1_evaluator_files(root: Path) -> list[Path]:
    selected = [
        root / "pyproject.toml",
        root / "configs/evaluation.yaml",
        root / "scripts/evaluate_predictions.py",
    ]
    for directory in (root / "contracts", root / "src"):
        if directory.is_symlink() or not directory.is_dir():
            raise V2FinalEvaluationError("V1 evaluator snapshot directory is missing")
        selected.extend(path for path in sorted(directory.rglob("*")) if path.is_file())
    if any(path.is_symlink() for path in selected):
        raise V2FinalEvaluationError("V1 evaluator snapshot contains a symlink")
    return selected


def _verify_v1_evaluator_snapshot(packet_root: Path) -> tuple[Path, list[dict[str, object]]]:
    manifest_payload = _regular_bytes(packet_root / "job-manifest.json", "V1 evaluator packet manifest")
    runner_payload = _regular_bytes(packet_root / "run-final-evaluation.sh", "V1 evaluator runner")
    if _sha256(manifest_payload) != V1_PACKET_MANIFEST_SHA256 or _sha256(runner_payload) != V1_RUNNER_SHA256:
        raise V2FinalEvaluationError("V1 evaluator packet identity differs from the completed snapshot")
    manifest = _json_object(manifest_payload, "V1 evaluator packet manifest")
    if (
        manifest.get("kind") != "scriber_full_model_final_evaluation_job"
        or _mapping(manifest.get("final_model"), "V1 final model").get("tree_sha256") != V1_MODEL_TREE_SHA256
        or _mapping(manifest.get("output"), "V1 output").get("remote_uri") != V1_COMPLETED_OUTPUT_REMOTE_URI
        or manifest.get("runner_sha256") != V1_RUNNER_SHA256
    ):
        raise V2FinalEvaluationError("V1 evaluator packet metadata differs from the completed snapshot")
    project = packet_root / "repo/ml/scriber_polishing"
    selected = _selected_v1_evaluator_files(project)
    records = [_file_record(project, path) for path in selected]
    records.sort(key=lambda item: str(item["path"]))
    if (
        len(records) != V1_EVALUATOR_SNAPSHOT_FILE_COUNT
        or sum(int(item["bytes"]) for item in records) != V1_EVALUATOR_SNAPSHOT_BYTES
        or _sha256(canonical_json_bytes(records)) != V1_EVALUATOR_SNAPSHOT_TREE_SHA256
    ):
        raise V2FinalEvaluationError("V1 evaluator source snapshot differs from the completed packet")
    return project, records


def _verify_v1_result(path: Path) -> dict[str, object]:
    payload = _regular_bytes(path, "V1 completed evaluation result")
    if _sha256(payload) != V1_COMPLETED_RESULT_SHA256:
        raise V2FinalEvaluationError("V1 completed evaluation result SHA-256 differs")
    value = _json_object(payload, "V1 completed evaluation result")
    reports = value.get("reports")
    if (
        value.get("kind") != "scriber_full_model_final_evaluation_result"
        or value.get("model_tree_sha256") != V1_MODEL_TREE_SHA256
        or value.get("training_steps_executed") != 0
        or not isinstance(reports, list)
    ):
        raise V2FinalEvaluationError("V1 completed evaluation result metadata differs")
    observed = {str(item.get("path")): item for item in reports if isinstance(item, Mapping)}
    for expected in _V1_FINAL_REPORTS.values():
        if observed.get(str(expected["path"])) != expected:
            raise V2FinalEvaluationError("V1 paired baseline report binding differs")
    return value


def _copy_bound_file(source: Path, destination: Path, expected_sha256: str, label: str) -> dict[str, object]:
    payload = _regular_bytes(source, label)
    if _sha256(payload) != expected_sha256:
        raise V2FinalEvaluationError(f"{label} SHA-256 differs")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return {"path": destination.as_posix(), "bytes": len(payload), "sha256": expected_sha256}


def _packet_inventory(root: Path, excluded: set[str]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise V2FinalEvaluationError("V2 evaluation packet contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        records.append(_file_record(root, path))
    return records


def v2_final_evaluation_runner_payload() -> bytes:
    """Return the fixed 300-case V2/V1 compact comparison runner."""

    script = rf"""#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
export TOKENIZERS_PARALLELISM=false HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

PACKET_ROOT=/input/packet
EVALUATOR_ROOT="$PACKET_ROOT/evaluator"
REFERENCE_ROOT="$PACKET_ROOT/references"
MODEL_ROOT=/input/model
V1_EVALUATION_ROOT=/input/v1-evaluation
WORK_ROOT=/workspace/scriber-v2-compact-final-evaluation-v1
LOCAL_RESULT_ROOT="$WORK_ROOT/final"
OUTPUT_ROOT={V2_EVALUATION_OUTPUT_CONTAINER}
EXPECTED_PACKET_TREE="${{1:-}}"
EXPECTED_MANIFEST_SHA="${{2:-}}"
EXPECTED_CONTRACT_SHA="${{3:-}}"

test -d "$PACKET_ROOT" && test -d "$EVALUATOR_ROOT" && test -d "$MODEL_ROOT" && test -d "$V1_EVALUATION_ROOT"
if [ -z "$EXPECTED_PACKET_TREE" ] || [ -z "$EXPECTED_MANIFEST_SHA" ] || [ -z "$EXPECTED_CONTRACT_SHA" ]; then
  echo "three immutable packet digests are required" >&2
  exit 1
fi
if [ -e "$OUTPUT_ROOT" ] || [ -e "$WORK_ROOT" ]; then
  echo "refusing non-empty or pre-existing V2 evaluation output" >&2
  exit 1
fi

python - "$PACKET_ROOT" "$EXPECTED_PACKET_TREE" "$EXPECTED_MANIFEST_SHA" "$EXPECTED_CONTRACT_SHA" "$MODEL_ROOT" <<'PY'
import hashlib, json, pathlib, sys
packet=pathlib.Path(sys.argv[1]); expected_tree,expected_manifest,expected_contract=sys.argv[2:5]; model=pathlib.Path(sys.argv[5])
def digest(data): return "sha256:"+hashlib.sha256(data).hexdigest()
manifest_bytes=(packet/"package-manifest.json").read_bytes(); contract_bytes=(packet/"launch-contract.json").read_bytes()
if digest(manifest_bytes)!=expected_manifest or digest(contract_bytes)!=expected_contract: raise SystemExit("packet metadata hash mismatch")
manifest=json.loads(manifest_bytes); inventory=json.loads((packet/"tree-inventory.json").read_bytes())
rows=[]
for item in inventory["files"]:
    path=packet/item["path"]; data=path.read_bytes(); actual={{"path":item["path"],"bytes":len(data),"sha256":digest(data)}}
    if actual!=item: raise SystemExit("packet inventory mismatch")
    rows.append(actual)
canonical=(json.dumps(rows,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode()
if digest(canonical)!=expected_tree: raise SystemExit("packet tree mismatch")
binding=json.loads((packet/"inputs/v2-postflight-model-binding.json").read_bytes()); bound=binding["model"]
declared_bytes=(model/"checkpoint-inventory.json").read_bytes()
if digest(declared_bytes)!=bound["inventory_sha256"]: raise SystemExit("model inventory hash mismatch")
declared=json.loads(declared_bytes); files=[]
for item in declared["files"]:
    path=model/item["path"]; data=path.read_bytes(); actual={{"path":item["path"],"bytes":len(data),"sha256":digest(data)}}
    if actual!=item: raise SystemExit("model file mismatch")
    files.append(actual)
actual_paths={{p.relative_to(model).as_posix() for p in model.rglob("*") if p.is_file()}}
if actual_paths != {{item["path"] for item in files}} | {{"checkpoint-inventory.json"}}: raise SystemExit("unbound model file")
tree=hashlib.sha256()
for item in files:
    tree.update(item["path"].encode()); tree.update(b"\0"); tree.update(str(item["bytes"]).encode()); tree.update(b"\0"); tree.update(item["sha256"].encode()); tree.update(b"\n")
if "sha256:"+tree.hexdigest()!=bound["tree_sha256"] or declared["tree_sha256"]!=bound["tree_sha256"]: raise SystemExit("model tree mismatch")
if digest((model/"scriber-protection-policy.json").read_bytes())!=bound["protection_policy_sha256"]: raise SystemExit("policy mismatch")
for weight in bound["weight_files"]:
    data=(model/weight["path"]).read_bytes()
    if len(data)!=weight["bytes"] or digest(data)!=weight["sha256"]: raise SystemExit("weight mismatch")
PY

python - "$V1_EVALUATION_ROOT" <<'PY'
import hashlib, pathlib, sys
root=pathlib.Path(sys.argv[1])
expected={{
 "ai_gold":("final/ai_gold/evaluation.json",11618677,"e46bb211bf3b41bbcd6028b23fee1691bb94551eba293deab7b134a25c3460ee"),
 "critical_regression":("final/critical_regression/evaluation.json",3559245,"392f76424c5690c62f430a6d47f06baf911bef4d3cc63f44c7fcf88ae8a5b4e8"),
 "regular_test":("final/regular_test/evaluation.json",7755172,"169d85b6fdaaaf6551a45e4ac90a01fd7dc4ae5c6373c3b7ae18a9896eb702c1"),
}}
for suite,(relative,size,sha) in expected.items():
    data=(root/relative).read_bytes()
    if len(data)!=size or hashlib.sha256(data).hexdigest()!=sha: raise SystemExit("completed V1 report mismatch: "+suite)
    lane=root/"final"/suite
    if not (lane/"predictions.jsonl").is_file() or not (lane/"predictions.manifest.json").is_file(): raise SystemExit("completed V1 prediction lane missing: "+suite)
PY

mkdir -p "$WORK_ROOT" "$LOCAL_RESULT_ROOT"
python -m pip install --break-system-packages --no-cache-dir \
  accelerate==1.14.0 jsonschema==4.26.0 numpy==2.5.1 pyyaml==6.0.3 \
  safetensors==0.8.0 sentencepiece==0.2.2 transformers==4.57.6
export PYTHONPATH="$EVALUATOR_ROOT/src"
cd "$EVALUATOR_ROOT"

materialize_compact_suite() {{
  suite="$1"; references="$2"; predictions="$3"
  compact_references="$WORK_ROOT/$suite.references.jsonl"
  compact_predictions="$WORK_ROOT/$suite.v1-predictions.jsonl"
  selection="$LOCAL_RESULT_ROOT/$suite.selection.json"
  python - "$suite" "$references" "$predictions" "$compact_references" "$compact_predictions" "$selection" <<'PY'
import hashlib, json, pathlib, sys
suite,ref_name,pred_name,ref_out,pred_out,selection_out=sys.argv[1:]
seed="scriber-v2-compact-smoke-v1"; algorithm="sha256_case_id_rank_v1"
def rows(path):
    result=[]
    for raw in pathlib.Path(path).read_bytes().splitlines(keepends=True):
        value=json.loads(raw); result.append((raw if raw.endswith(b"\n") else raw+b"\n",value))
    return result
references=rows(ref_name); prediction_path=pathlib.Path(pred_name); prediction_payload=prediction_path.read_bytes(); predictions=rows(pred_name)
expected={{"ai_gold":1750,"critical_regression":500,"regular_test":1000}}[suite]
if len(references)!=expected: raise SystemExit("sealed reference count differs")
prediction_manifest=json.loads(prediction_path.with_name("predictions.manifest.json").read_bytes())
if prediction_manifest.get("candidate")!="final" or prediction_manifest.get("record_count")!=expected or prediction_manifest.get("prediction_sha256")!=hashlib.sha256(prediction_payload).hexdigest(): raise SystemExit("completed V1 prediction manifest differs")
ranked=sorted(references,key=lambda row:(hashlib.sha256((seed+"\0"+suite+"\0"+row[1]["case_id"]).encode()).digest(),row[1]["case_id"]))[:100]
case_ids=[row[1]["case_id"] for row in ranked]
if len(case_ids)!=100 or len(set(case_ids))!=100: raise SystemExit("compact selection is not exactly 100 unique cases")
prediction_map={{row[1]["case_id"]:row[0] for row in predictions}}
if len(prediction_map)!=len(predictions) or any(case_id not in prediction_map for case_id in case_ids): raise SystemExit("V1 prediction reuse coverage differs")
ref_payload=b"".join(row[0] for row in ranked); pred_payload=b"".join(prediction_map[case_id] for case_id in case_ids)
pathlib.Path(ref_out).write_bytes(ref_payload); pathlib.Path(pred_out).write_bytes(pred_payload)
case_payload=(json.dumps(case_ids,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode()
selection={{"schema_version":1,"kind":"scriber_v2_compact_evaluation_selection","algorithm":algorithm,"seed":seed,"suite":suite,"source_record_count":expected,"selected_record_count":100,"case_ids_sha256":"sha256:"+hashlib.sha256(case_payload).hexdigest(),"reference_subset_sha256":"sha256:"+hashlib.sha256(ref_payload).hexdigest(),"v1_prediction_subset_sha256":"sha256:"+hashlib.sha256(pred_payload).hexdigest(),"case_ids_exposed":False}}
pathlib.Path(selection_out).write_bytes((json.dumps(selection,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode("utf-8"))
PY
}}

run_lane() {{
  candidate="$1"; suite="$2"; references="$3"
  lane="$LOCAL_RESULT_ROOT/$candidate/$suite"
  mkdir -p "$lane"
  python -m scriber_polishing.inference \
    --model "$MODEL_ROOT" --references "$references" \
    --predictions-output "$lane/predictions.jsonl" \
    --manifest-output "$lane/predictions.manifest.json" \
    --candidate "$candidate" --batch-size 8 --max-new-tokens 512 \
    --device cuda --quantization none \
    --protection-policy-artifact "$MODEL_ROOT/scriber-protection-policy.json" \
    --local-files-only
  set +e
  python scripts/evaluate_predictions.py \
    --references "$references" --predictions "$lane/predictions.jsonl" \
    --candidate "$candidate" --gate-candidate final --config configs/evaluation.yaml \
    --output "$lane/evaluation.json"
  code=$?
  set -e
  if [ "$code" -ne 0 ] && [ "$code" -ne 2 ]; then exit "$code"; fi
  printf '%s' "$code" > "$lane/evaluation-exit.txt"
}}

evaluate_reused_v1() {{
  suite="$1"; references="$WORK_ROOT/$suite.references.jsonl"; predictions="$WORK_ROOT/$suite.v1-predictions.jsonl"
  lane="$LOCAL_RESULT_ROOT/v1_reused/$suite"
  mkdir -p "$lane"
  set +e
  python scripts/evaluate_predictions.py \
    --references "$references" --predictions "$predictions" \
    --candidate v1_reused --gate-candidate final --config configs/evaluation.yaml \
    --output "$lane/evaluation.json"
  code=$?
  set -e
  if [ "$code" -ne 0 ] && [ "$code" -ne 2 ]; then exit "$code"; fi
  printf '%s' "$code" > "$lane/evaluation-exit.txt"
}}

materialize_compact_suite regular_test "$REFERENCE_ROOT/regular_test_1000.references.jsonl" "$V1_EVALUATION_ROOT/final/regular_test/predictions.jsonl"
materialize_compact_suite critical_regression "$REFERENCE_ROOT/critical_regression_as_challenge.references.jsonl" "$V1_EVALUATION_ROOT/final/critical_regression/predictions.jsonl"
materialize_compact_suite ai_gold "$REFERENCE_ROOT/ai_gold_automatic_test_challenge.references.jsonl" "$V1_EVALUATION_ROOT/final/ai_gold/predictions.jsonl"

run_lane v2 regular_test "$WORK_ROOT/regular_test.references.jsonl"
run_lane v2 critical_regression "$WORK_ROOT/critical_regression.references.jsonl"
run_lane v2 ai_gold "$WORK_ROOT/ai_gold.references.jsonl"
evaluate_reused_v1 regular_test
evaluate_reused_v1 critical_regression
evaluate_reused_v1 ai_gold

python - "$WORK_ROOT" "$LOCAL_RESULT_ROOT" <<'PY'
import json, pathlib, re, sys
work=pathlib.Path(sys.argv[1]); result=pathlib.Path(sys.argv[2]); suites=("ai_gold","critical_regression","regular_test")
def rows(path): return [json.loads(raw) for raw in pathlib.Path(path).read_bytes().splitlines()]
def payload(records): return b"".join((json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode("utf-8") for row in records)
def unique_rows(path,label):
    records=rows(path); indexed={{}}
    for row in records:
        case_id=row.get("case_id")
        if not isinstance(case_id,str) or re.fullmatch(r"[a-z][a-z0-9_-]{{2,199}}",case_id) is None: raise SystemExit("invalid "+label+" case_id")
        if case_id in indexed: raise SystemExit("duplicate "+label+" case_id")
        indexed[case_id]=row
    if len(indexed)!=100: raise SystemExit(label+" coverage is not 100 unique cases")
    return indexed
def namespaced(suite,row):
    value=dict(row); value["case_id"]=suite+"__"+row["case_id"]
    if len(value["case_id"])>200 or re.fullmatch(r"[a-z][a-z0-9_-]{{2,199}}",value["case_id"]) is None: raise SystemExit("invalid namespaced combined case_id")
    return value
references=[]; v1=[]; v2=[]
for suite in suites:
    suite_references=unique_rows(work/(suite+".references.jsonl"),suite+" references")
    suite_v1=unique_rows(work/(suite+".v1-predictions.jsonl"),suite+" v1")
    suite_v2=unique_rows(result/"v2"/suite/"predictions.jsonl",suite+" v2")
    if set(suite_references)!=set(suite_v1) or set(suite_references)!=set(suite_v2): raise SystemExit("suite-local case coverage differs: "+suite)
    for case_id in sorted(suite_references):
        references.append(namespaced(suite,suite_references[case_id]))
        v1.append(namespaced(suite,suite_v1[case_id]))
        v2.append(namespaced(suite,suite_v2[case_id]))
for label,records in (("references",references),("v1",v1),("v2",v2)):
    ids=[row["case_id"] for row in records]
    if len(records)!=300 or len(set(ids))!=300: raise SystemExit("combined "+label+" coverage is not 300 unique cases")
    records.sort(key=lambda row:row["case_id"])
if [row["case_id"] for row in references] != [row["case_id"] for row in v1] or [row["case_id"] for row in references] != [row["case_id"] for row in v2]: raise SystemExit("combined case coverage differs")
(work/"combined.references.jsonl").write_bytes(payload(references))
(work/"combined.v1-predictions.jsonl").write_bytes(payload(v1))
(work/"combined.v2-predictions.jsonl").write_bytes(payload(v2))
PY

evaluate_combined() {{
  candidate="$1"; predictions="$2"; lane="$LOCAL_RESULT_ROOT/$candidate/combined"
  mkdir -p "$lane"
  set +e
  python scripts/evaluate_predictions.py \
    --references "$WORK_ROOT/combined.references.jsonl" --predictions "$predictions" \
    --candidate "$candidate" --gate-candidate final --config configs/evaluation.yaml \
    --output "$lane/evaluation.json"
  code=$?
  set -e
  if [ "$code" -ne 0 ] && [ "$code" -ne 2 ]; then exit "$code"; fi
  printf '%s' "$code" > "$lane/evaluation-exit.txt"
}}

evaluate_combined v1_reused "$WORK_ROOT/combined.v1-predictions.jsonl"
evaluate_combined v2 "$WORK_ROOT/combined.v2-predictions.jsonl"

python - "$PACKET_ROOT/package-manifest.json" "$LOCAL_RESULT_ROOT" "$WORK_ROOT" <<'PY'
import hashlib, json, pathlib, re, sys
manifest=json.loads(pathlib.Path(sys.argv[1]).read_bytes()); root=pathlib.Path(sys.argv[2]); work=pathlib.Path(sys.argv[3]); reports=[]; selections=[]
def load_unique(path):
    rows=[json.loads(raw) for raw in pathlib.Path(path).read_bytes().splitlines()]
    indexed={{row["case_id"]:row for row in rows}}
    if len(indexed)!=len(rows): raise SystemExit("duplicate Terra source case input")
    return indexed
def text(value,label):
    if not isinstance(value,str) or not value or len(value)>30000: raise SystemExit("invalid Terra source "+label)
    return value
def namespaced(suite,case_id):
    value=suite+"__"+case_id
    if len(value)>200 or re.fullmatch(r"[a-z][a-z0-9_-]{{2,199}}",value) is None: raise SystemExit("invalid namespaced Terra source case_id")
    return value
terra=[]
for suite in ("ai_gold","critical_regression","regular_test"):
    for candidate in ("v1_reused","v2"):
        path=root/candidate/suite/"evaluation.json"; data=path.read_bytes(); code=int((path.parent/"evaluation-exit.txt").read_text("ascii"))
        reports.append({{"candidate":candidate,"suite":suite,"path":path.relative_to(root).as_posix(),"bytes":len(data),"sha256":"sha256:"+hashlib.sha256(data).hexdigest(),"evaluation_exit":code,"record_count":100}})
    selection_path=root/(suite+".selection.json"); selection_payload=selection_path.read_bytes(); selection=json.loads(selection_payload)
    selection["path"]=selection_path.relative_to(root).as_posix(); selection["sha256"]="sha256:"+hashlib.sha256(selection_payload).hexdigest(); selections.append(selection)
    references=load_unique(work/(suite+".references.jsonl")); v1=load_unique(work/(suite+".v1-predictions.jsonl")); v2=load_unique(root/"v2"/suite/"predictions.jsonl")
    if len(references)!=100 or set(references)!=set(v1) or set(references)!=set(v2): raise SystemExit("Terra source coverage differs: "+suite)
    for case_id in sorted(references):
        if re.fullmatch(r"[a-z][a-z0-9_-]{{2,199}}",case_id) is None: raise SystemExit("invalid Terra source case_id")
        reference=references[case_id]
        terra.append({{"schema_version":1,"case_id":namespaced(suite,case_id),"suite":suite,"source_text":text(reference.get("source_text"),"source_text"),"reference_text":text(reference.get("target_plain_text"),"reference_text"),"v1_output":text(v1[case_id].get("prediction_sst"),"v1_output"),"v2_output":text(v2[case_id].get("prediction_sst"),"v2_output")}})
terra.sort(key=lambda row:(row["suite"],row["case_id"]))
if len(terra)!=300 or len({{row["case_id"] for row in terra}})!=300 or {{suite:sum(row["suite"]==suite for row in terra) for suite in ("ai_gold","critical_regression","regular_test")}}!={{"ai_gold":100,"critical_regression":100,"regular_test":100}}: raise SystemExit("Terra source count differs")
combined={{}}
for candidate in ("v1_reused","v2"):
    path=root/candidate/"combined"/"evaluation.json"; data=path.read_bytes(); code=int((path.parent/"evaluation-exit.txt").read_text("ascii")); report=json.loads(data); metrics=report.get("metrics")
    if not isinstance(metrics,dict) or metrics.get("total")!=300: raise SystemExit("combined report coverage differs")
    exact=metrics.get("normalized_exact_match"); critical=metrics.get("critical_errors")
    if isinstance(exact,bool) or not isinstance(exact,(int,float)) or not 0<=exact<=1 or not isinstance(critical,dict) or any(not isinstance(key,str) or isinstance(value,bool) or not isinstance(value,int) or value<0 for key,value in critical.items()): raise SystemExit("combined comparison metrics are invalid")
    combined[candidate]={{"normalized_exact_match":exact,"critical_error_count":sum(critical.values())}}
    reports.append({{"candidate":candidate,"suite":"combined","path":path.relative_to(root).as_posix(),"bytes":len(data),"sha256":"sha256:"+hashlib.sha256(data).hexdigest(),"evaluation_exit":code,"record_count":300}})
exact_ok=combined["v2"]["normalized_exact_match"]>=combined["v1_reused"]["normalized_exact_match"]
critical_ok=combined["v2"]["critical_error_count"]<=combined["v1_reused"]["critical_error_count"]
compact_gate={{"kind":"scriber_v2_compact_comparison_gate","source_report_suite":"combined","record_count":300,"coverage":{{"v1_reused":300,"v2":300,"exact":True}},"normalized_exact_match":{{"v1_reused":combined["v1_reused"]["normalized_exact_match"],"v2":combined["v2"]["normalized_exact_match"],"v2_gte_v1":exact_ok}},"deterministic_critical_error_count":{{"v1_reused":combined["v1_reused"]["critical_error_count"],"v2":combined["v2"]["critical_error_count"],"v2_lte_v1":critical_ok}},"decision":"passed" if exact_ok and critical_ok else "failed","publication_grade":False,"requires_terra":True}}
terra_payload=b"".join((json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n").encode("utf-8") for row in terra)
terra_path=root/"terra-source-cases.jsonl"; terra_path.write_bytes(terra_payload)
terra_source={{"path":"terra-source-cases.jsonl","bytes":len(terra_payload),"sha256":"sha256:"+hashlib.sha256(terra_payload).hexdigest(),"record_count":300,"suite_counts":{{"ai_gold":100,"critical_regression":100,"regular_test":100}},"schema_path":"contracts/v2_compact_terra_source_case_schema.json","contains_candidate_identity":True,"judge_public":False}}
result={{"schema_version":1,"kind":"scriber_hf_v2_final_evaluation_result","status":"complete","model":manifest["model"],"evaluator":manifest["evaluator"],"reference_bundle_sha256":manifest["reference_bundle_sha256"],"evaluation_scope":manifest["evaluation_scope"],"selection_evidence":selections,"reports":reports,"compact_comparison_gate":compact_gate,"terra_judge_source":terra_source,"paired_baseline":manifest["paired_baseline"],"training_steps_executed":0,"publication_grade":False,"external_mutation_performed_by_verifier":False}}
(root/"v2-final-evaluation-result.json").write_bytes((json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode("utf-8"))
PY
mkdir -p "$(dirname "$OUTPUT_ROOT")"
cp -a "$LOCAL_RESULT_ROOT" "$OUTPUT_ROOT"
printf '%s\n' 'scriber_v2_compact_final_evaluation_complete_v1' > "$OUTPUT_ROOT/COMPLETED"
rm -rf "$WORK_ROOT"
echo "v2_final_evaluation=compact_smoke_complete records=300 publication_grade=false training_steps_executed=0"
"""
    return script.encode("utf-8")


def _launch_contract(
    *, packet_tree_sha256: str, manifest_sha256: str, inventory_sha256: str
) -> dict[str, object]:
    identity_labels = {
        "attempt": V2_EVALUATION_ATTEMPT,
        "campaign": "v2-final-evaluation",
        "name": V2_EVALUATION_JOB_NAME,
        "output-prefix": f"{V2_EVALUATION_ATTEMPT}-{V2_EVALUATION_JOB_NAME}",
        "project": "scriber-polishing",
        "role": "v2-final-evaluation",
    }
    packet_volume = f"{V2_EVALUATION_PACKET_REMOTE_URI}:/input/packet:ro"
    model_volume = f"{V2_MODEL_REMOTE_URI}:/input/model:ro"
    v1_evaluation_volume = f"{V1_COMPLETED_OUTPUT_REMOTE_URI}:/input/v1-evaluation:ro"
    output_volume = f"{V2_EVALUATION_OUTPUT_MOUNT_URI}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--flavor",
        V2_EVALUATION_FLAVOR,
        "--timeout",
        f"{V2_EVALUATION_TIMEOUT_MINUTES}m",
    ]
    for key, value in identity_labels.items():
        argv.extend(("--label", f"{key}={value}"))
    argv.extend(
        (
            "-v",
            packet_volume,
            "-v",
            model_volume,
            "-v",
            v1_evaluation_volume,
            "-v",
            output_volume,
            V2_EVALUATION_IMAGE,
            "bash",
            "/input/packet/run-v2-final-evaluation.sh",
            packet_tree_sha256,
            manifest_sha256,
            "__LAUNCH_CONTRACT_SHA256__",
        )
    )
    return {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_launch_contract",
        "attempt": V2_EVALUATION_ATTEMPT,
        "job_name": V2_EVALUATION_JOB_NAME,
        "image": V2_EVALUATION_IMAGE,
        "flavor": V2_EVALUATION_FLAVOR,
        "timeout_minutes": V2_EVALUATION_TIMEOUT_MINUTES,
        "packet_remote_uri": V2_EVALUATION_PACKET_REMOTE_URI,
        "model_remote_uri": V2_MODEL_REMOTE_URI,
        "output_mount_uri": V2_EVALUATION_OUTPUT_MOUNT_URI,
        "remote_result_uri": V2_EVALUATION_OUTPUT_REMOTE_URI,
        "packet_tree_sha256": packet_tree_sha256,
        "manifest_sha256": manifest_sha256,
        "inventory_sha256": inventory_sha256,
        "packet_volume": packet_volume,
        "model_volume": model_volume,
        "v1_evaluation_volume": v1_evaluation_volume,
        "output_volume": output_volume,
        "identity_labels": identity_labels,
        "preconditions": {
            "no_prior_exact_job": {
                "required": True,
                "scope": "all_historical_and_active_namespace_jobs",
                "minimum_fresh_snapshots": 1,
                "maximum_age_seconds": 120,
            },
            "empty_remote_output": {
                "required": True,
                "target_remote_uri": V2_EVALUATION_OUTPUT_PREFIX_REMOTE_URI,
                "maximum_age_seconds": 120,
            },
            "exact_remote_packet": {
                "required": True,
                "target_remote_uri": V2_EVALUATION_PACKET_REMOTE_URI,
                "maximum_age_seconds": 120,
            },
            "permanent_claim": {
                "required_before_submission": True,
                "repository_id": "Buttermilk03/scriber-polishing-launch-claims",
                "repository_type": "dataset",
                "path": f"claims/v2-final-evaluation/{V2_EVALUATION_ATTEMPT}-{V2_EVALUATION_JOB_NAME}.json",
            },
        },
        "launch_contract_hash_argument": "replace_placeholder_with_raw_launch_contract_sha256",
        "external_job_started": False,
        "external_mutation_performed": False,
        "auto_retry_allowed": False,
        "argv_template": argv,
    }


def build_v2_final_evaluation_packet(
    *,
    postflight_binding_path: str | Path,
    v1_evaluator_packet_root: str | Path,
    v1_completed_result_path: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    """Build one new V2 evaluation packet without external side effects."""

    target = Path(output_dir).expanduser().resolve()
    if target.exists() or Path(output_dir).is_symlink():
        raise V2FinalEvaluationError("refusing to overwrite a V2 final-evaluation packet")
    postflight_path = Path(postflight_binding_path).expanduser().resolve()
    postflight_payload = _regular_bytes(postflight_path, "V2 postflight model binding")
    postflight = _validate_postflight_binding(_json_object(postflight_payload, "V2 postflight model binding"))
    v1_packet = Path(v1_evaluator_packet_root).expanduser().resolve()
    if v1_packet.is_symlink() or not v1_packet.is_dir():
        raise V2FinalEvaluationError("V1 evaluator packet must be a regular directory")
    v1_project, evaluator_records = _verify_v1_evaluator_snapshot(v1_packet)
    v1_result_path = Path(v1_completed_result_path).expanduser().resolve()
    _verify_v1_result(v1_result_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        for record in evaluator_records:
            relative = str(record["path"])
            source = v1_project / relative
            destination = staging / "evaluator" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        reference_root = v1_project / "evaluation-input"
        references: dict[str, dict[str, object]] = {}
        for suite, binding in _REFERENCES.items():
            copied = dict(binding)
            for kind in ("records", "manifest"):
                filename = str(binding[f"{kind}_filename"])
                expected = str(binding[f"{kind}_sha256"])
                destination = staging / "references" / filename
                _copy_bound_file(reference_root / filename, destination, expected, f"sealed {suite} {kind}")
                copied[f"{kind}_path"] = f"references/{filename}"
            references[suite] = copied
        input_binding_path = staging / "inputs/v2-postflight-model-binding.json"
        input_binding_path.parent.mkdir(parents=True)
        input_binding_path.write_bytes(postflight_payload)
        baseline_path = staging / "baseline/v1-final-evaluation-result.json"
        baseline_path.parent.mkdir(parents=True)
        baseline_path.write_bytes(_regular_bytes(v1_result_path, "V1 completed evaluation result"))
        runner = v2_final_evaluation_runner_payload()
        (staging / "run-v2-final-evaluation.sh").write_bytes(runner)
        inventory_files = _packet_inventory(
            staging,
            {"package-manifest.json", "tree-inventory.json", "launch-contract.json"},
        )
        packet_tree = _sha256(canonical_json_bytes(inventory_files))
        inventory = {
            "schema_version": 1,
            "kind": "scriber_hf_v2_final_evaluation_tree_inventory",
            "algorithm": "sha256-canonical-file-records-v1",
            "files": inventory_files,
            "file_count": len(inventory_files),
            "byte_count": sum(int(item["bytes"]) for item in inventory_files),
            "packet_tree_sha256": packet_tree,
        }
        inventory_payload = canonical_json_bytes(inventory)
        inventory_sha = _sha256(inventory_payload)
        (staging / "tree-inventory.json").write_bytes(inventory_payload)
        model = _mapping(postflight["model"], "postflight model")
        model_binding = {
            "remote_uri": model["remote_uri"],
            "directory": model["directory"],
            "run_fingerprint": model["run_fingerprint"],
            "tree_sha256": model["tree_sha256"],
            "weight_files": model["weight_files"],
            "protection_policy_sha256": model["protection_policy_sha256"],
            "inventory_sha256": model["inventory_sha256"],
            "reload_verification_sha256": model["reload_verification_sha256"],
            "postflight_binding_path": "inputs/v2-postflight-model-binding.json",
            "postflight_binding_sha256": _sha256(postflight_payload),
            "source_training_job_id": V2_TRAINING_JOB_ID,
            "source_training_packet_tree_sha256": V2_TRAINING_PACKET_TREE_SHA256,
        }
        evaluator = {
            "source": "completed_v1_full_model_final_evaluation_packet",
            "source_packet_manifest_sha256": V1_PACKET_MANIFEST_SHA256,
            "source_runner_sha256": V1_RUNNER_SHA256,
            "snapshot_tree_sha256": V1_EVALUATOR_SNAPSHOT_TREE_SHA256,
            "snapshot_file_count": V1_EVALUATOR_SNAPSHOT_FILE_COUNT,
            "snapshot_bytes": V1_EVALUATOR_SNAPSHOT_BYTES,
            "evaluation_config_sha256": V1_EVALUATION_CONFIG_SHA256,
            "batch_size": 8,
            "max_new_tokens": 512,
        }
        paired_baseline = {
            "candidate": "v1_full_model",
            "model_tree_sha256": V1_MODEL_TREE_SHA256,
            "completed_result_path": "baseline/v1-final-evaluation-result.json",
            "completed_result_sha256": V1_COMPLETED_RESULT_SHA256,
            "remote_result_uri": V1_COMPLETED_OUTPUT_REMOTE_URI,
            "reports": _V1_FINAL_REPORTS,
            "rerun_required": False,
            "prediction_source": "completed_v1_final_candidate_lanes",
            "predictions_remote_uri": V1_COMPLETED_OUTPUT_REMOTE_URI,
            "inference_reused": True,
            "subset_evaluation_recomputed": True,
        }
        reference_bundle_sha = _sha256(canonical_json_bytes(references))
        evaluation_scope = {
            "evidence_class": "compact_smoke_test",
            "publication_grade": False,
            "records_per_suite": V2_COMPACT_RECORDS_PER_SUITE,
            "total_records": V2_COMPACT_TOTAL_RECORDS,
            "selection_algorithm": V2_COMPACT_SELECTION_ALGORITHM,
            "selection_seed": V2_COMPACT_SELECTION_SEED,
            "v1_prediction_strategy": "reuse_completed_v1_predictions_without_inference",
        }
        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_v2_final_evaluation_packet",
            "attempt": V2_EVALUATION_ATTEMPT,
            "model": model_binding,
            "evaluator": evaluator,
            "references": references,
            "reference_bundle_sha256": reference_bundle_sha,
            "evaluation_scope": evaluation_scope,
            "paired_baseline": paired_baseline,
            "runtime": {
                "image": V2_EVALUATION_IMAGE,
                "flavor": V2_EVALUATION_FLAVOR,
                "timeout_minutes": V2_EVALUATION_TIMEOUT_MINUTES,
            },
            "training": {
                "steps_executed": 0,
                "new_data_generated": False,
                "reference_usage": "inference_and_evaluation_only",
            },
            "output": {
                "remote_uri": V2_EVALUATION_OUTPUT_REMOTE_URI,
                "container_path": V2_EVALUATION_OUTPUT_CONTAINER,
                "unique": True,
                "overwrite_allowed": False,
            },
            "runner_sha256": _sha256(runner),
            "inventory": {
                "path": "tree-inventory.json",
                "sha256": inventory_sha,
                "packet_tree_sha256": packet_tree,
                "file_count": inventory["file_count"],
                "byte_count": inventory["byte_count"],
            },
            "side_effects": {
                "external_job_started": False,
                "external_mutation_performed": False,
                "publication_allowed": False,
                "auto_retry_allowed": False,
            },
        }
        _validate_schema(_PACKET_SCHEMA, manifest, "V2 evaluation packet manifest")
        manifest_payload = canonical_json_bytes(manifest)
        manifest_sha = _sha256(manifest_payload)
        (staging / "package-manifest.json").write_bytes(manifest_payload)
        contract = _launch_contract(
            packet_tree_sha256=packet_tree,
            manifest_sha256=manifest_sha,
            inventory_sha256=inventory_sha,
        )
        _validate_schema(_LAUNCH_SCHEMA, contract, "V2 evaluation launch contract")
        contract_payload = canonical_json_bytes(contract)
        contract_sha = _sha256(contract_payload)
        (staging / "launch-contract.json").write_bytes(contract_payload)
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "packet_dir": target,
        "packet_tree_sha256": packet_tree,
        "inventory_sha256": inventory_sha,
        "manifest_sha256": manifest_sha,
        "launch_contract_sha256": contract_sha,
        "remote_result_uri": V2_EVALUATION_OUTPUT_REMOTE_URI,
        "external_job_started": False,
        "external_mutation_performed": False,
    }


def verify_v2_final_evaluation_packet(
    packet_dir: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str,
    expected_launch_contract_sha256: str,
) -> dict[str, object]:
    for value, label in (
        (expected_packet_tree_sha256, "expected V2 evaluation packet tree"),
        (expected_manifest_sha256, "expected V2 evaluation manifest"),
        (expected_launch_contract_sha256, "expected V2 evaluation launch contract"),
    ):
        _require_hash(value, label)
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise V2FinalEvaluationError("V2 evaluation packet must not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise V2FinalEvaluationError("V2 evaluation packet must be a directory")
    manifest_payload = _regular_bytes(root / "package-manifest.json", "V2 evaluation package manifest")
    inventory_payload = _regular_bytes(root / "tree-inventory.json", "V2 evaluation tree inventory")
    contract_payload = _regular_bytes(root / "launch-contract.json", "V2 evaluation launch contract")
    if _sha256(manifest_payload) != expected_manifest_sha256:
        raise V2FinalEvaluationError("V2 evaluation package manifest SHA-256 differs")
    if _sha256(contract_payload) != expected_launch_contract_sha256:
        raise V2FinalEvaluationError("V2 evaluation launch contract SHA-256 differs")
    manifest = _json_object(manifest_payload, "V2 evaluation package manifest")
    inventory = _json_object(inventory_payload, "V2 evaluation tree inventory")
    contract = _json_object(contract_payload, "V2 evaluation launch contract")
    if (
        manifest_payload != canonical_json_bytes(manifest)
        or inventory_payload != canonical_json_bytes(inventory)
        or contract_payload != canonical_json_bytes(contract)
    ):
        raise V2FinalEvaluationError("V2 evaluation packet metadata must be canonical JSON")
    _validate_schema(_PACKET_SCHEMA, manifest, "V2 evaluation packet manifest")
    _validate_schema(_LAUNCH_SCHEMA, contract, "V2 evaluation launch contract")
    files = inventory.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(item, Mapping) for item in files):
        raise V2FinalEvaluationError("V2 evaluation inventory files are invalid")
    paths: list[str] = []
    for item in files:
        path = str(item.get("path", ""))
        if not _safe_relative(path) or path in paths:
            raise V2FinalEvaluationError("V2 evaluation inventory path is unsafe or duplicated")
        payload = _regular_bytes(root / path, f"V2 evaluation inventory file {path}")
        if item.get("bytes") != len(payload) or item.get("sha256") != _sha256(payload):
            raise V2FinalEvaluationError(f"V2 evaluation inventory differs for {path}")
        paths.append(path)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected_paths = {*paths, "package-manifest.json", "tree-inventory.json", "launch-contract.json"}
    if actual_paths != expected_paths or any(path.is_symlink() for path in root.rglob("*")):
        raise V2FinalEvaluationError("V2 evaluation inventory does not bind the complete packet tree")
    packet_tree = _sha256(canonical_json_bytes(files))
    if (
        packet_tree != expected_packet_tree_sha256
        or inventory.get("packet_tree_sha256") != packet_tree
        or inventory.get("file_count") != len(files)
        or inventory.get("byte_count") != sum(int(item["bytes"]) for item in files)
        or _mapping(manifest.get("inventory"), "V2 manifest inventory").get("sha256")
        != _sha256(inventory_payload)
        or _mapping(manifest.get("inventory"), "V2 manifest inventory").get("packet_tree_sha256")
        != packet_tree
    ):
        raise V2FinalEvaluationError("V2 evaluation packet tree inventory binding differs")
    if (
        contract.get("packet_tree_sha256") != packet_tree
        or contract.get("manifest_sha256") != expected_manifest_sha256
        or contract.get("inventory_sha256") != _sha256(inventory_payload)
    ):
        raise V2FinalEvaluationError("V2 evaluation launch contract does not bind the packet")
    binding_payload = _regular_bytes(
        root / "inputs/v2-postflight-model-binding.json", "packet V2 postflight model binding"
    )
    binding = _validate_postflight_binding(_json_object(binding_payload, "packet V2 postflight model binding"))
    model = _mapping(manifest.get("model"), "V2 evaluation model")
    if (
        model.get("postflight_binding_sha256") != _sha256(binding_payload)
        or model.get("tree_sha256") != V2_MODEL_TREE_SHA256
        or model.get("weight_files") != _mapping(binding.get("model"), "packet postflight model").get("weight_files")
        or model.get("protection_policy_sha256") != V2_POLICY_ARTIFACT_SHA256
    ):
        raise V2FinalEvaluationError("V2 evaluation model binding differs from postflight")
    evaluator_root = root / "evaluator"
    evaluator_records = [_file_record(evaluator_root, path) for path in _selected_v1_evaluator_files(evaluator_root)]
    evaluator_records.sort(key=lambda item: str(item["path"]))
    if (
        len(evaluator_records) != V1_EVALUATOR_SNAPSHOT_FILE_COUNT
        or sum(int(item["bytes"]) for item in evaluator_records) != V1_EVALUATOR_SNAPSHOT_BYTES
        or _sha256(canonical_json_bytes(evaluator_records)) != V1_EVALUATOR_SNAPSHOT_TREE_SHA256
    ):
        raise V2FinalEvaluationError("packet V1 evaluator snapshot identity differs")
    references = _mapping(manifest.get("references"), "V2 evaluation references")
    for suite, expected in _REFERENCES.items():
        observed = _mapping(references.get(suite), f"V2 reference {suite}")
        for kind in ("records", "manifest"):
            path = root / str(observed[f"{kind}_path"])
            if _sha256(_regular_bytes(path, f"packet {suite} {kind}")) != expected[f"{kind}_sha256"]:
                raise V2FinalEvaluationError(f"V2 evaluation inventory reference binding differs for {suite}")
        if observed.get("source_record_count") != expected["source_record_count"] or observed.get(
            "selected_record_count"
        ) != V2_COMPACT_RECORDS_PER_SUITE:
            raise V2FinalEvaluationError("V2 compact reference counts differ")
    _verify_v1_result(root / "baseline/v1-final-evaluation-result.json")
    runner = _regular_bytes(root / "run-v2-final-evaluation.sh", "V2 compact evaluation runner")
    runner_text = runner.decode("utf-8")
    if (
        _mapping(manifest.get("evaluation_scope"), "V2 evaluation scope").get("total_records")
        != V2_COMPACT_TOTAL_RECORDS
        or _mapping(manifest.get("evaluation_scope"), "V2 evaluation scope").get("publication_grade") is not False
        or runner_text.count("run_lane v2") != 3
        or runner_text.count("\nevaluate_reused_v1 ") != 3
        or runner_text.count("\nevaluate_combined ") != 2
        or runner_text.count("--gate-candidate final") != 3
        or "terra-source-cases.jsonl" not in runner_text
        or 'printf \'%s\\n\' \'scriber_v2_compact_final_evaluation_complete_v1\'' not in runner_text
        or "run_lane base_model" in runner_text
    ):
        raise V2FinalEvaluationError("V2 compact evaluator scope differs")
    return {
        "status": "passed",
        "packet_tree_sha256": packet_tree,
        "manifest_sha256": expected_manifest_sha256,
        "launch_contract_sha256": expected_launch_contract_sha256,
        "inventory_sha256": _sha256(inventory_payload),
        "model_tree_sha256": V2_MODEL_TREE_SHA256,
        "model_weight_sha256": V2_MODEL_WEIGHT_SHA256,
        "policy_sha256": V2_POLICY_ARTIFACT_SHA256,
        "evidence_class": "compact_smoke_test",
        "selected_record_count": V2_COMPACT_TOTAL_RECORDS,
        "publication_grade": False,
        "external_job_started": False,
        "external_mutation_performed": False,
    }


def verify_v2_final_evaluation_launch_readiness(
    *,
    contract_path: str | Path,
    jobs_snapshot: Mapping[str, object],
    output_snapshot: Mapping[str, object],
    packet_snapshot: Mapping[str, object],
    now_utc: str,
) -> dict[str, object]:
    def parsed_utc(value: object, label: str) -> datetime:
        if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
            raise V2FinalEvaluationError(f"{label} must be a normalized UTC timestamp")
        try:
            parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        except ValueError as error:
            raise V2FinalEvaluationError(f"{label} is invalid") from error
        if parsed.tzinfo != UTC:
            raise V2FinalEvaluationError(f"{label} must use UTC")
        return parsed

    contract_payload = _regular_bytes(Path(contract_path).expanduser().resolve(), "V2 evaluation launch contract")
    contract = _json_object(contract_payload, "V2 evaluation launch contract")
    if contract_payload != canonical_json_bytes(contract):
        raise V2FinalEvaluationError("V2 evaluation launch contract is not canonical JSON")
    _validate_schema(_LAUNCH_SCHEMA, contract, "V2 evaluation launch contract")
    preconditions = _mapping(contract.get("preconditions"), "V2 evaluation launch preconditions")
    empty_output = _mapping(preconditions.get("empty_remote_output"), "V2 evaluation empty-output precondition")
    now = parsed_utc(now_utc, "launch-readiness time")
    snapshots = (
        (
            jobs_snapshot,
            "scriber_hf_v2_final_evaluation_jobs_snapshot",
            None,
            "jobs snapshot",
        ),
        (
            output_snapshot,
            "scriber_hf_v2_final_evaluation_output_snapshot",
            empty_output["target_remote_uri"],
            "output snapshot",
        ),
        (
            packet_snapshot,
            "scriber_hf_v2_final_evaluation_remote_packet_snapshot",
            contract["packet_remote_uri"],
            "packet snapshot",
        ),
    )
    normalized_entries: dict[str, list[object]] = {}
    for snapshot, expected_kind, expected_target, label in snapshots:
        observed = parsed_utc(snapshot.get("observed_at_utc"), label)
        age = (now - observed).total_seconds()
        entries = snapshot.get("entries")
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("kind") != expected_kind
            or snapshot.get("namespace") != "Buttermilk03"
            or snapshot.get("complete") is not True
            or not isinstance(entries, list)
            or age < 0
            or age > 120
        ):
            raise V2FinalEvaluationError(f"{label} is incomplete, stale, future-dated, or wrong-scope")
        if (
            expected_kind == "scriber_hf_v2_final_evaluation_jobs_snapshot"
            and snapshot.get("scope") != "all_historical_and_active_namespace_jobs"
        ):
            raise V2FinalEvaluationError(f"{label} is incomplete, stale, future-dated, or wrong-scope")
        if expected_target is not None and snapshot.get("target_remote_uri") != expected_target:
            raise V2FinalEvaluationError(f"{label} targets a different remote prefix")
        normalized_entries[label] = entries
    identity = _mapping(contract.get("identity_labels"), "V2 evaluation identity labels")
    for row in normalized_entries["jobs snapshot"]:
        if not isinstance(row, Mapping):
            raise V2FinalEvaluationError("jobs snapshot contains a non-object entry")
        labels = row.get("labels")
        exact_labels = isinstance(labels, Mapping) and all(labels.get(key) == value for key, value in identity.items())
        serialized = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        exact_output = str(contract["remote_result_uri"]) in serialized
        if exact_labels or exact_output:
            raise V2FinalEvaluationError("prior exact job exists; refusing a duplicate V2 evaluation")
    if normalized_entries["output snapshot"]:
        raise V2FinalEvaluationError("V2 evaluation remote output is not empty")
    packet_entries = normalized_entries["packet snapshot"]
    expected_packet = {
        "packet_tree_sha256": contract["packet_tree_sha256"],
        "manifest_sha256": contract["manifest_sha256"],
        "launch_contract_sha256": _sha256(contract_payload),
    }
    if packet_entries != [expected_packet]:
        raise V2FinalEvaluationError("remote V2 evaluation packet identity differs")
    argv = list(contract["argv_template"])
    placeholder = "__LAUNCH_CONTRACT_SHA256__"
    if argv.count(placeholder) != 1:
        raise V2FinalEvaluationError("launch contract hash placeholder is invalid")
    argv[argv.index(placeholder)] = _sha256(contract_payload)
    return {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_launch_readiness",
        "status": "ready",
        "observed_at_utc": now_utc,
        "contract_sha256": _sha256(contract_payload),
        "packet_tree_sha256": contract["packet_tree_sha256"],
        "remote_result_uri": contract["remote_result_uri"],
        "no_prior_exact_job": True,
        "remote_output_empty": True,
        "remote_packet_exact": True,
        "permanent_claim_required_before_submission": True,
        "permanent_claim": _mapping(contract.get("preconditions"), "launch preconditions")["permanent_claim"],
        "argv": argv,
        "external_job_started": False,
        "external_mutation_performed": False,
    }


def verify_v2_final_evaluation_result(
    *, packet_manifest_path: str | Path, result_root: str | Path, result_path: str | Path
) -> dict[str, object]:
    manifest_payload = _regular_bytes(
        Path(packet_manifest_path).expanduser().resolve(), "V2 evaluation packet manifest"
    )
    manifest = _json_object(manifest_payload, "V2 evaluation packet manifest")
    if manifest_payload != canonical_json_bytes(manifest):
        raise V2FinalEvaluationError("V2 evaluation packet manifest is not canonical JSON")
    _validate_schema(_PACKET_SCHEMA, manifest, "V2 evaluation packet manifest")
    root_candidate = Path(result_root).expanduser()
    if root_candidate.is_symlink():
        raise V2FinalEvaluationError("V2 evaluation result root must not be a symlink")
    root = root_candidate.resolve()
    if not root.is_dir():
        raise V2FinalEvaluationError("V2 evaluation result root must be a directory")
    completion_payload = _regular_bytes(root / "COMPLETED", "V2 evaluation completion marker")
    if completion_payload != b"scriber_v2_compact_final_evaluation_complete_v1\n":
        raise V2FinalEvaluationError("V2 evaluation completion marker differs")
    result_candidate = Path(result_path).expanduser()
    if result_candidate.is_symlink():
        raise V2FinalEvaluationError("V2 evaluation result must not be a symlink")
    result_file = result_candidate.resolve()
    try:
        result_file.relative_to(root)
    except ValueError as error:
        raise V2FinalEvaluationError("V2 evaluation result must be inside its result root") from error
    result_payload = _regular_bytes(result_file, "V2 compact evaluation result")
    result = _json_object(result_payload, "V2 compact evaluation result")
    if result_payload != canonical_json_bytes(result):
        raise V2FinalEvaluationError("V2 compact evaluation result is not canonical JSON")
    if result.get("model") != manifest.get("model"):
        raise V2FinalEvaluationError("V2 result model binding differs from the packet")
    for field in ("evaluator", "reference_bundle_sha256", "evaluation_scope", "paired_baseline"):
        if result.get(field) != manifest.get(field):
            raise V2FinalEvaluationError(f"V2 result {field} binding differs from the packet")
    _validate_schema(_RESULT_SCHEMA, result, "V2 compact evaluation result")
    expected_source_counts = {
        suite: int(binding["source_record_count"]) for suite, binding in _REFERENCES.items()
    }
    selection_rows = result.get("selection_evidence")
    if not isinstance(selection_rows, list):
        raise V2FinalEvaluationError("V2 compact selection evidence is missing")
    observed_selection_suites: set[str] = set()
    for row in selection_rows:
        if not isinstance(row, Mapping):
            raise V2FinalEvaluationError("V2 compact selection evidence contains a non-object")
        suite = str(row.get("suite", ""))
        if suite in observed_selection_suites or suite not in expected_source_counts:
            raise V2FinalEvaluationError("V2 compact selection suite is duplicated or unknown")
        observed_selection_suites.add(suite)
        if (
            row.get("source_record_count") != expected_source_counts[suite]
            or row.get("selected_record_count") != V2_COMPACT_RECORDS_PER_SUITE
            or row.get("algorithm") != V2_COMPACT_SELECTION_ALGORITHM
            or row.get("seed") != V2_COMPACT_SELECTION_SEED
            or row.get("case_ids_exposed") is not False
        ):
            raise V2FinalEvaluationError("V2 compact selection binding differs")
        relative = str(row.get("path", ""))
        if relative != f"{suite}.selection.json" or not _safe_relative(relative):
            raise V2FinalEvaluationError("V2 compact selection path differs")
        payload = _regular_bytes(root / relative, f"V2 compact {suite} selection")
        if row.get("sha256") != _sha256(payload):
            raise V2FinalEvaluationError("V2 compact selection artifact SHA-256 differs")
        selection = _json_object(payload, f"V2 compact {suite} selection")
        bound_selection = {key: value for key, value in row.items() if key not in {"path", "sha256"}}
        if payload != canonical_json_bytes(selection) or selection != bound_selection:
            raise V2FinalEvaluationError("V2 compact selection artifact binding differs")
    if observed_selection_suites != set(expected_source_counts):
        raise V2FinalEvaluationError("V2 compact selection does not cover all three suites")
    reports = result.get("reports")
    if not isinstance(reports, list):
        raise V2FinalEvaluationError("V2 compact evaluation reports are missing")
    expected_pairs = {
        (candidate, suite)
        for candidate in ("v1_reused", "v2")
        for suite in ("ai_gold", "critical_regression", "regular_test", "combined")
    }
    observed_pairs: set[tuple[str, str]] = set()
    combined_reports: dict[str, Mapping[str, object]] = {}
    for report in reports:
        if not isinstance(report, Mapping):
            raise V2FinalEvaluationError("V2 compact report binding contains a non-object")
        pair = (str(report.get("candidate", "")), str(report.get("suite", "")))
        if pair not in expected_pairs or pair in observed_pairs:
            raise V2FinalEvaluationError("V2 compact report lane is duplicated or unknown")
        observed_pairs.add(pair)
        relative = str(report.get("path", ""))
        expected_relative = f"{pair[0]}/{pair[1]}/evaluation.json"
        if relative != expected_relative or not _safe_relative(relative):
            raise V2FinalEvaluationError("V2 compact report path differs")
        payload = _regular_bytes(root / relative, f"V2 compact report {pair[0]}/{pair[1]}")
        expected_count = V2_COMPACT_TOTAL_RECORDS if pair[1] == "combined" else V2_COMPACT_RECORDS_PER_SUITE
        if (
            report.get("bytes") != len(payload)
            or report.get("sha256") != _sha256(payload)
            or report.get("record_count") != expected_count
            or report.get("evaluation_exit") not in {0, 2}
        ):
            raise V2FinalEvaluationError("V2 compact report artifact binding differs")
        if pair[1] == "combined":
            combined_reports[pair[0]] = _json_object(payload, f"V2 compact combined report {pair[0]}")
    if observed_pairs != expected_pairs:
        raise V2FinalEvaluationError("V2 compact result does not contain all eight paired reports")
    combined_values: dict[str, dict[str, int | float]] = {}
    for candidate in ("v1_reused", "v2"):
        combined_report = combined_reports.get(candidate)
        metrics = combined_report.get("metrics") if isinstance(combined_report, Mapping) else None
        if not isinstance(metrics, Mapping) or metrics.get("total") != V2_COMPACT_TOTAL_RECORDS:
            raise V2FinalEvaluationError("V2 combined report coverage differs")
        normalized_exact = metrics.get("normalized_exact_match")
        critical_errors = metrics.get("critical_errors")
        if (
            isinstance(normalized_exact, bool)
            or not isinstance(normalized_exact, int | float)
            or not math.isfinite(float(normalized_exact))
            or not 0 <= float(normalized_exact) <= 1
            or not isinstance(critical_errors, Mapping)
            or any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in critical_errors.items()
            )
        ):
            raise V2FinalEvaluationError("V2 combined comparison metrics are invalid")
        combined_values[candidate] = {
            "normalized_exact_match": float(normalized_exact),
            "critical_error_count": sum(int(value) for value in critical_errors.values()),
        }
    exact_ok = (
        combined_values["v2"]["normalized_exact_match"]
        >= combined_values["v1_reused"]["normalized_exact_match"]
    )
    critical_ok = (
        combined_values["v2"]["critical_error_count"]
        <= combined_values["v1_reused"]["critical_error_count"]
    )
    expected_compact_gate = {
        "kind": "scriber_v2_compact_comparison_gate",
        "source_report_suite": "combined",
        "record_count": V2_COMPACT_TOTAL_RECORDS,
        "coverage": {"v1_reused": 300, "v2": 300, "exact": True},
        "normalized_exact_match": {
            "v1_reused": combined_values["v1_reused"]["normalized_exact_match"],
            "v2": combined_values["v2"]["normalized_exact_match"],
            "v2_gte_v1": exact_ok,
        },
        "deterministic_critical_error_count": {
            "v1_reused": combined_values["v1_reused"]["critical_error_count"],
            "v2": combined_values["v2"]["critical_error_count"],
            "v2_lte_v1": critical_ok,
        },
        "decision": "passed" if exact_ok and critical_ok else "failed",
        "publication_grade": False,
        "requires_terra": True,
    }
    if result.get("compact_comparison_gate") != expected_compact_gate:
        raise V2FinalEvaluationError("V2 compact comparison gate differs from combined reports")
    terra_binding = _mapping(result.get("terra_judge_source"), "V2 Terra judge source binding")
    terra_relative = str(terra_binding.get("path", ""))
    if terra_relative != "terra-source-cases.jsonl" or not _safe_relative(terra_relative):
        raise V2FinalEvaluationError("V2 Terra judge source path differs")
    terra_payload = _regular_bytes(root / terra_relative, "V2 Terra judge source")
    if (
        terra_binding.get("bytes") != len(terra_payload)
        or terra_binding.get("sha256") != _sha256(terra_payload)
        or terra_binding.get("record_count") != V2_COMPACT_TOTAL_RECORDS
        or terra_binding.get("suite_counts")
        != {"ai_gold": 100, "critical_regression": 100, "regular_test": 100}
        or terra_binding.get("schema_path") != "contracts/v2_compact_terra_source_case_schema.json"
        or terra_binding.get("contains_candidate_identity") is not True
        or terra_binding.get("judge_public") is not False
    ):
        raise V2FinalEvaluationError("V2 Terra judge source artifact binding differs")
    terra_lines = terra_payload.splitlines(keepends=True)
    if len(terra_lines) != V2_COMPACT_TOTAL_RECORDS or any(
        not line.endswith(b"\n") or line.endswith(b"\r\n") for line in terra_lines
    ):
        raise V2FinalEvaluationError("V2 Terra judge source must contain 300 canonical LF JSONL rows")
    terra_keys: list[tuple[str, str]] = []
    terra_suites = {"ai_gold": 0, "critical_regression": 0, "regular_test": 0}
    seen_case_ids: set[str] = set()
    for line in terra_lines:
        row = _json_object(line, "V2 Terra judge source row")
        _validate_schema(_TERRA_SOURCE_SCHEMA, row, "V2 Terra judge source row")
        if canonical_json_bytes(row) != line:
            raise V2FinalEvaluationError("V2 Terra judge source row is not canonical JSON")
        suite = str(row["suite"])
        case_id = str(row["case_id"])
        if case_id in seen_case_ids:
            raise V2FinalEvaluationError("V2 Terra judge source contains a duplicate case_id")
        seen_case_ids.add(case_id)
        terra_suites[suite] += 1
        terra_keys.append((suite, case_id))
    if terra_suites != terra_binding["suite_counts"] or terra_keys != sorted(terra_keys):
        raise V2FinalEvaluationError("V2 Terra judge source ordering or suite coverage differs")
    return {
        "schema_version": 1,
        "kind": "scriber_hf_v2_final_evaluation_verification",
        "status": "passed",
        "result_sha256": _sha256(result_payload),
        "model_tree_sha256": V2_MODEL_TREE_SHA256,
        "model_weight_sha256": V2_MODEL_WEIGHT_SHA256,
        "policy_sha256": V2_POLICY_ARTIFACT_SHA256,
        "evaluator_snapshot_tree_sha256": V1_EVALUATOR_SNAPSHOT_TREE_SHA256,
        "reference_bundle_sha256": result["reference_bundle_sha256"],
        "report_count": len(reports),
        "evaluated_record_count": V2_COMPACT_TOTAL_RECORDS,
        "terra_source_record_count": V2_COMPACT_TOTAL_RECORDS,
        "terra_source_sha256": terra_binding["sha256"],
        "compact_comparison_gate_decision": expected_compact_gate["decision"],
        "evidence_class": "compact_smoke_test",
        "publication_grade": False,
        "v1_predictions_reused": True,
        "training_steps_executed": 0,
        "external_mutation_performed": False,
    }
