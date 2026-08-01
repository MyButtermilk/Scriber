"""Prepare a bounded, self-contained Hugging Face Job packet."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .curriculum import canonical_json_bytes, sha256_bytes
from .marker_repair import (
    MarkerRepairError,
    validate_marker_repair_manifest,
    verify_marker_repair_bundle,
)

HF_JOB_HOURLY_RATES_USD = MappingProxyType(
    {
        "l4x1": 0.80,
        "l40sx1": 1.80,
        "a10g-small": 1.00,
        "a100-large": 2.50,
    }
)
HF_MARKER_REPAIR_IMAGE = (
    "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-runtime"
)

_PROJECT_ROOT = Path(__file__).parents[2]
_REPOSITORY_ROOT = _PROJECT_ROOT.parents[1]
_REPAIR_OUTPUT_NAME = "pilot-marker-lexical-repair-v1"


@dataclass(frozen=True, slots=True)
class HfMarkerRepairPacket:
    """One immutable local directory ready for a private HF Job mount."""

    output_dir: Path
    manifest: Mapping[str, Any]


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise MarkerRepairError(f"cloud packet source is missing: {source}")
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40:
        raise MarkerRepairError(
            "cannot bind the source Git revision for the cloud packet"
        )
    return value


def _file_inventory(root: Path) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise MarkerRepairError(
                f"cloud packet may not contain symlinks: {path}"
            )
        if not path.is_file():
            continue
        payload = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(payload),
                "sha256": sha256_bytes(payload),
            }
        )
    if not records:
        raise MarkerRepairError("cloud packet inventory is empty")
    return records, "sha256:" + sha256_bytes(canonical_json_bytes(records))


def _runner_payload() -> bytes:
    script = r"""#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

INPUT_ROOT=/input/repo
WORK_ROOT=/workspace/scriber-marker-repair/repo
PROJECT_ROOT="$WORK_ROOT/ml/scriber_polishing"
OUTPUT_ROOT=/outputs/pilot-marker-lexical-repair-v1

if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends git
  rm -rf /var/lib/apt/lists/*
fi

python -m pip install --break-system-packages --no-cache-dir \
  accelerate==1.14.0 \
  jsonschema==4.26.0 \
  numpy==2.5.1 \
  pyyaml==6.0.3 \
  safetensors==0.8.0 \
  sentencepiece==0.2.2 \
  tensorboard==2.21.0 \
  transformers==4.57.6

rm -rf /workspace/scriber-marker-repair
mkdir -p "$(dirname "$WORK_ROOT")"
cp -a --no-preserve=ownership "$INPUT_ROOT" "$WORK_ROOT"

unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_CEILING_DIRECTORIES
git -C "$WORK_ROOT" init --quiet
git -C "$WORK_ROOT" config --local user.name "Scriber HF Job"
git -C "$WORK_ROOT" config --local user.email "scriber-hf-job@invalid.local"
git -C "$WORK_ROOT" add \
  "ml/scriber_polishing/src" \
  "ml/scriber_polishing/scripts" \
  "ml/scriber_polishing/contracts" \
  "ml/scriber_polishing/configs" \
  "ml/scriber_polishing/README.md" \
  "ml/scriber_polishing/pyproject.toml"
export GIT_AUTHOR_DATE=2026-07-30T00:00:00Z
export GIT_COMMITTER_DATE=2026-07-30T00:00:00Z
git -C "$WORK_ROOT" commit --quiet \
  -m "chore(ml): materialize immutable HF marker repair job"

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"

python scripts/verify_marker_repair.py \
  --bundle-dir job-input \
  --tokenizer-path job-input/parent-checkpoint \
  --report /workspace/marker-repair-dry-bind.json

python - <<'PY'
import json
import platform
import torch
import transformers

runtime = {
    "python": platform.python_version(),
    "torch": torch.__version__,
    "cuda_build": torch.version.cuda,
    "transformers": transformers.__version__,
    "cuda_available": torch.cuda.is_available(),
    "bf16_supported": (
        torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    ),
    "gpu": (
        torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    ),
}
print("cloud_runtime=" + json.dumps(runtime, sort_keys=True), flush=True)
if not runtime["python"].startswith("3.12."):
    raise SystemExit("unexpected Python runtime")
if runtime["torch"] != "2.11.0+cu128":
    raise SystemExit("unexpected PyTorch runtime")
if runtime["transformers"] != "4.57.6":
    raise SystemExit("unexpected Transformers runtime")
if not runtime["cuda_available"] or not runtime["bf16_supported"]:
    raise SystemExit("CUDA BF16 runtime is unavailable")
PY

if [ -e "$OUTPUT_ROOT" ] && [ -n "$(find "$OUTPUT_ROOT" -mindepth 1 -print -quit)" ]; then
  echo "refusing to overwrite non-empty output path: $OUTPUT_ROOT" >&2
  exit 1
fi
mkdir -p "$OUTPUT_ROOT"
cp /workspace/marker-repair-dry-bind.json "$OUTPUT_ROOT/dry-bind.json"
cp job-input/repair-manifest.json "$OUTPUT_ROOT/repair-manifest.json"
cp job-input/preflight.json "$OUTPUT_ROOT/preflight.json"
cp job-input/acceptance-policy.yaml "$OUTPUT_ROOT/acceptance-policy.yaml"

python scripts/train_sft.py \
  --config job-input/train.yaml \
  --dataset-manifest job-input/dataset-manifest.json \
  --train job-input/selected-train.jsonl \
  --validation job-input/safe-validation.jsonl \
  --output-dir "$OUTPUT_ROOT/training" \
  --report "$OUTPUT_ROOT/training-report.json" \
  --resume none \
  --save-final

MODEL_DIR="$(find "$OUTPUT_ROOT/training" -maxdepth 1 -type d -name 'final-*' -print -quit)"
if [ -z "$MODEL_DIR" ]; then
  echo "final model directory is missing" >&2
  exit 1
fi

python -m scriber_polishing.inference \
  --model "$MODEL_DIR" \
  --references job-input/parity-dev.references.jsonl \
  --predictions-output "$OUTPUT_ROOT/predictions.jsonl" \
  --manifest-output "$OUTPUT_ROOT/predictions.manifest.json" \
  --candidate lexical_repair \
  --resume-journal "$OUTPUT_ROOT/predictions.resume.json" \
  --max-new-tokens 512 \
  --device cuda \
  --quantization none \
  --local-files-only \
  --protection-policy-artifact "$MODEL_DIR/scriber-protection-policy.json"

set +e
python scripts/evaluate_predictions.py \
  --references job-input/parity-dev.references.jsonl \
  --predictions "$OUTPUT_ROOT/predictions.jsonl" \
  --candidate lexical_repair \
  --gate-candidate shuffled \
  --config configs/evaluation.yaml \
  --output "$OUTPUT_ROOT/evaluation.json"
EVALUATION_EXIT=$?
set -e
if [ "$EVALUATION_EXIT" -ne 0 ] && [ "$EVALUATION_EXIT" -ne 2 ]; then
  exit "$EVALUATION_EXIT"
fi

python - "$OUTPUT_ROOT" "$MODEL_DIR" "$EVALUATION_EXIT" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
model = pathlib.Path(sys.argv[2])
evaluation_exit = int(sys.argv[3])
files = {}
for path in sorted(root.rglob("*")):
    if path.is_file():
        payload = path.read_bytes()
        files[path.relative_to(root).as_posix()] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
result = {
    "schema_version": 1,
    "kind": "scriber_hf_marker_repair_result",
    "training_completed": True,
    "evaluation_completed": True,
    "evaluation_gate_exit": evaluation_exit,
    "final_model_directory": model.name,
    "files": files,
}
(root / "cloud-result.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("cloud_marker_repair=complete", flush=True)
PY
"""
    return script.replace("\r\n", "\n").encode("utf-8")


def build_hf_marker_repair_packet(
    *,
    bundle_dir: str | Path,
    output_dir: str | Path,
    tokenizer: Any,
    budget_usd: float,
    flavor: str,
    timeout_minutes: int,
) -> HfMarkerRepairPacket:
    """Build a fail-closed HF packet whose maximum GPU charge is bounded."""

    if (
        isinstance(budget_usd, bool)
        or not isinstance(budget_usd, int | float)
        or budget_usd <= 0
    ):
        raise MarkerRepairError("HF budget must be a positive number")
    if flavor not in HF_JOB_HOURLY_RATES_USD:
        raise MarkerRepairError(f"unsupported bounded HF flavor: {flavor}")
    if (
        isinstance(timeout_minutes, bool)
        or not isinstance(timeout_minutes, int)
        or timeout_minutes <= 0
    ):
        raise MarkerRepairError(
            "HF timeout must be a positive integer number of minutes"
        )
    maximum_cost = round(
        HF_JOB_HOURLY_RATES_USD[flavor] * timeout_minutes / 60,
        4,
    )
    if maximum_cost > float(budget_usd):
        raise MarkerRepairError(
            f"HF job maximum ${maximum_cost:.2f} exceeds "
            f"the ${float(budget_usd):.2f} budget"
        )

    source_bundle = Path(bundle_dir).expanduser().resolve()
    dry_bind = verify_marker_repair_bundle(
        source_bundle,
        tokenizer=tokenizer,
    )
    repair_manifest_payload = (
        source_bundle / "repair-manifest.json"
    ).read_bytes()
    repair_manifest = validate_marker_repair_manifest(
        json.loads(repair_manifest_payload),
        root=source_bundle,
    )
    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise MarkerRepairError(
            f"refusing to overwrite HF marker-repair packet: {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    )
    try:
        project = temp_root / "repo" / "ml" / "scriber_polishing"
        project.mkdir(parents=True)
        _copy_tree(_PROJECT_ROOT / "src", project / "src")
        _copy_tree(_PROJECT_ROOT / "contracts", project / "contracts")
        _copy_tree(_PROJECT_ROOT / "scripts", project / "scripts")
        (project / "configs").mkdir()
        shutil.copy2(
            _PROJECT_ROOT / "configs" / "evaluation.yaml",
            project / "configs" / "evaluation.yaml",
        )
        for filename in ("pyproject.toml", "README.md"):
            shutil.copy2(_PROJECT_ROOT / filename, project / filename)
        _copy_tree(source_bundle, project / "job-input")

        runner_payload = _runner_payload()
        (temp_root / "run-marker-repair.sh").write_bytes(runner_payload)
        inventory, tree_sha256 = _file_inventory(temp_root)
        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_marker_repair_job_packet",
            "source_git_head": _git_head(),
            "repair_input_sha256": repair_manifest[
                "repair_input_sha256"
            ],
            "repair_manifest_sha256": "sha256:"
            + hashlib.sha256(repair_manifest_payload).hexdigest(),
            "image": HF_MARKER_REPAIR_IMAGE,
            "flavor": flavor,
            "hourly_rate_usd": HF_JOB_HOURLY_RATES_USD[flavor],
            "timeout_minutes": timeout_minutes,
            "maximum_job_cost_usd": maximum_cost,
            "budget_usd": float(budget_usd),
            "private_output_required": True,
            "generative_api": False,
            "training": {
                "train_examples": dry_bind["train_examples"],
                "validation_examples": dry_bind["validation_examples"],
                "update_steps": dry_bind["update_steps"],
                "effective_order_sha256": dry_bind[
                    "effective_order_sha256"
                ],
                "warm_start_not_resume": True,
            },
            "packet_tree_sha256": tree_sha256,
            "file_count": len(inventory),
            "byte_count": sum(int(item["bytes"]) for item in inventory),
        }
        (temp_root / "job-manifest.json").write_bytes(
            canonical_json_bytes(manifest)
        )
        temp_root.replace(target)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return HfMarkerRepairPacket(
        output_dir=target,
        manifest=MappingProxyType(manifest),
    )
