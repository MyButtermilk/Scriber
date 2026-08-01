#!/usr/bin/env bash
set -euo pipefail

export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export CUBLAS_WORKSPACE_CONFIG=:4096:8
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

FINAL_MODEL_DIRECTORY=final-3015c63021cf53d8
EXPECTED_MODEL_TREE_SHA256=sha256:4b4b758f2fa3e00af282845c42de2887bc714c36a5c8e58e7225a1d9a7d25e06
EXPECTED_TRAINING_REPORT_SHA256=sha256:f47d42b8511406e1df180463a1a97868c1920b74df09dd4455ddc28829d2fbc0

INPUT_ROOT=/input/repo
TRAINED_ROOT=/trained
WORK_ROOT=/workspace/scriber-marker-eval/repo
PROJECT_ROOT="$WORK_ROOT/ml/scriber_polishing"
REMOTE_MODEL_ROOT="$TRAINED_ROOT/training/$FINAL_MODEL_DIRECTORY"
LOCAL_MODEL_ROOT="/workspace/scriber-marker-eval/model/$FINAL_MODEL_DIRECTORY"
LOCAL_TRAINING_REPORT=/workspace/scriber-marker-eval/training-report.json
OUTPUT_ROOT=/outputs/pilot-marker-lexical-repair-eval-v1

python -m pip install --break-system-packages --no-cache-dir \
  accelerate==1.14.0 \
  jsonschema==4.26.0 \
  numpy==2.5.1 \
  pyyaml==6.0.3 \
  safetensors==0.8.0 \
  sentencepiece==0.2.2 \
  transformers==4.57.6

rm -rf /workspace/scriber-marker-eval
mkdir -p "$(dirname "$WORK_ROOT")"
cp -a --no-preserve=ownership "$INPUT_ROOT" "$WORK_ROOT"
mkdir -p "$(dirname "$LOCAL_MODEL_ROOT")"
cp "$TRAINED_ROOT/training-report.json" "$LOCAL_TRAINING_REPORT"

model_copy_complete=false
for copy_attempt in 1 2 3; do
  rm -rf "$LOCAL_MODEL_ROOT"
  if cp -a --no-preserve=ownership "$REMOTE_MODEL_ROOT" "$LOCAL_MODEL_ROOT"; then
    model_copy_complete=true
    break
  fi
  sleep "$copy_attempt"
done
if [ "$model_copy_complete" != true ]; then
  echo "failed to copy the completed model from private storage" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
export PYTHONPATH="$PROJECT_ROOT/src"

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

python - \
  "$LOCAL_MODEL_ROOT" \
  "$LOCAL_TRAINING_REPORT" \
  "$FINAL_MODEL_DIRECTORY" \
  "$EXPECTED_MODEL_TREE_SHA256" \
  "$EXPECTED_TRAINING_REPORT_SHA256" <<'PY'
import hashlib
import json
import pathlib
import sys

from scriber_polishing.inference import _inventory_local_model_tree

model_root = pathlib.Path(sys.argv[1]).resolve()
report_path = pathlib.Path(sys.argv[2]).resolve()
expected_directory = sys.argv[3]
expected_tree = sys.argv[4]
expected_report_sha256 = sys.argv[5]

report_payload = report_path.read_bytes()
actual_report_sha256 = "sha256:" + hashlib.sha256(report_payload).hexdigest()
if actual_report_sha256 != expected_report_sha256:
    raise SystemExit("training report differs from the completed 146-step run")
report = json.loads(report_payload)
if (
    report.get("training_completed") is not True
    or report.get("max_steps") != 146
    or report['checkpoint_state']['global_step'] != 146
    or report["checkpoint_state"].get("last_checkpoint") != "checkpoint-146"
    or report["final_model"].get("directory") != expected_directory
    or report["final_model"]["reload_verification"].get("status") != "passed"
    or report["final_model"]["inventory"].get("tree_sha256") != expected_tree
):
    raise SystemExit("training report does not prove the completed repair run")
inventory = _inventory_local_model_tree(model_root)
if (
    inventory.get("tree_sha256") != expected_tree
    or inventory.get("file_count") != 12
    or inventory.get("total_bytes") != 575466744
):
    raise SystemExit("copied final model differs from the bound trained model")
binding = {
    "schema_version": 1,
    "kind": "scriber_marker_repair_evaluation_recovery_binding",
    "training_reused": True,
    "training_steps_executed": 0,
    "completed_training_steps": 146,
    "training_report_sha256": actual_report_sha256,
    "final_model_directory": expected_directory,
    "final_model_tree_sha256": inventory["tree_sha256"],
    "final_model_file_count": inventory["file_count"],
    "final_model_total_bytes": inventory["total_bytes"],
}
pathlib.Path("/workspace/evaluation-source-binding.json").write_text(
    json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("recovery_model_binding=" + json.dumps(binding, sort_keys=True), flush=True)
PY

mkdir -p "$OUTPUT_ROOT"
if [ -f "$OUTPUT_ROOT/recovery-evaluation-result.json" ]; then
  echo "recovery_evaluation=already_complete" >&2
  exit 0
fi
if [ -e "$OUTPUT_ROOT/evaluation-source-binding.json" ]; then
  cmp /workspace/evaluation-source-binding.json \
    "$OUTPUT_ROOT/evaluation-source-binding.json"
else
  cp /workspace/evaluation-source-binding.json \
    "$OUTPUT_ROOT/evaluation-source-binding.json"
fi

python -m scriber_polishing.inference \
  --model "$LOCAL_MODEL_ROOT" \
  --references job-input/parity-dev.references.jsonl \
  --predictions-output "$OUTPUT_ROOT/predictions.jsonl" \
  --manifest-output "$OUTPUT_ROOT/predictions.manifest.json" \
  --candidate lexical_repair \
  --resume-journal "$OUTPUT_ROOT/predictions.resume.json" \
  --max-new-tokens 512 \
  --device cuda \
  --quantization none \
  --local-files-only \
  --protection-policy-artifact \
    "$LOCAL_MODEL_ROOT/scriber-protection-policy.json"

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

python - "$OUTPUT_ROOT" "$EVALUATION_EXIT" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
evaluation_exit = int(sys.argv[2])
binding = json.loads(
    (root / "evaluation-source-binding.json").read_text(encoding="utf-8")
)
files = {}
for path in sorted(root.rglob("*")):
    if path.is_file() and path.name != "recovery-evaluation-result.json":
        payload = path.read_bytes()
        files[path.relative_to(root).as_posix()] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
result = {
    "schema_version": 1,
    "kind": "scriber_hf_marker_repair_recovery_evaluation_result",
    "training_reused": True,
    "training_steps_executed": 0,
    "evaluation_completed": True,
    "evaluation_gate_exit": evaluation_exit,
    "final_model_directory": binding["final_model_directory"],
    "final_model_tree_sha256": binding["final_model_tree_sha256"],
    "training_report_sha256": binding["training_report_sha256"],
    "files": files,
}
temporary = root / "recovery-evaluation-result.json.tmp"
temporary.write_text(
    json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temporary.replace(root / "recovery-evaluation-result.json")
print("recovery_evaluation=complete", flush=True)
PY
