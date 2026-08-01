"""Immutable Hugging Face packet for the V2 BF16/Q8_0 conversion job.

Packaging and verification are deliberately local-only.  The packet mounts the
completed V2 model and the already verified llama.cpp bundle read-only; it never
copies model weights into the packet and it never submits a Hugging Face Job.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .hf_llama_cpp_toolchain import TOOLCHAIN_REMOTE_OUTPUT_URI
from .hf_v2_training_job import _materialize_git_head, _safe_relative
from .v2_q8_bf16_release import (
    V2_RELEASE_ATTEMPT,
    V2_RELEASE_JOB_NAME,
    V2_RELEASE_OUTPUT_URI,
    V2_SOURCE_URI,
    canonical_json_bytes,
    sha256_bytes,
    validate_v2_evaluation_binding,
    validate_v2_release_plan,
)

V2_RELEASE_JOB_IMAGE = (
    "pytorch/pytorch@sha256:"
    "eee11b3b3872a8c838e35ef48f08b2d5def2080902c7f666831310ca1a0ef2be"
)
V2_RELEASE_JOB_FLAVOR = "l4x1"
V2_RELEASE_JOB_TIMEOUT_MINUTES = 60
V2_RELEASE_PACKET_REMOTE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt15/packets/"
    "scriber-v2-q8-bf16-release-v1"
)
V2_RELEASE_OUTPUT_CONTAINER = "/outputs/quantization"
V2_RELEASE_OUTPUT_LABEL = "attempt15-scriber-v2-q8-bf16-release-v1"
V2_RELEASE_CLAIM_OWNER_PLACEHOLDER = "__SCRIBER_V2_RELEASE_CLAIM_OWNER__"
V2_RELEASE_CONTRACT_HASH_PLACEHOLDER = "__SCRIBER_V2_RELEASE_CONTRACT_SHA256__"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_ROOT = PROJECT_ROOT / "contracts"

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_LABEL_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")

_REQUIRED_SOURCE_FILES = frozenset(
    {
        "source/ml/scriber_polishing/src/scriber_polishing/v2_q8_bf16_release.py",
        "source/ml/scriber_polishing/src/scriber_polishing/quantization.py",
        "source/ml/scriber_polishing/scripts/run_v2_q8_bf16_quantization.py",
        "source/ml/scriber_polishing/configs/training-policy-gemma-lexical-v1.json",
    }
)


class V2ReleaseJobError(RuntimeError):
    """The attempt15 job packet could not be proven immutable and safe."""


SourceMaterializer = Callable[
    [Path, Path], tuple[str, str, list[dict[str, object]]]
]


def validate_v2_release_job_schema(
    value: Mapping[str, Any], filename: str, label: str
) -> None:
    try:
        schema = json.loads((CONTRACTS_ROOT / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(dict(value)),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
    except (OSError, json.JSONDecodeError) as error:
        raise V2ReleaseJobError(f"{label} schema is unavailable") from error
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise V2ReleaseJobError(
            f"{label} failed schema at {location}: {errors[0].message}"
        )


def _regular_bytes(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise V2ReleaseJobError(f"{label} must be a regular file")
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise V2ReleaseJobError(f"{label} changed while it was read")
    return payload


def _canonical_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2ReleaseJobError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict) or payload != canonical_json_bytes(value):
        raise V2ReleaseJobError(f"{label} must be a canonical JSON object")
    return value


def _record(path: Path, root: Path) -> dict[str, object]:
    payload = _regular_bytes(path, path.name)
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise V2ReleaseJobError(f"{label} must be a prefixed SHA-256")
    return value


def _require_source_records(
    staging: Path,
    records: list[dict[str, object]],
) -> None:
    indexed: dict[str, dict[str, object]] = {}
    for record in records:
        path = record.get("path")
        if not isinstance(path, str) or not _safe_relative(path) or path in indexed:
            raise V2ReleaseJobError("source inventory contains an unsafe or duplicate path")
        payload = _regular_bytes(staging / path, f"source file {path}")
        if record.get("bytes") != len(payload) or record.get("sha256") != sha256_bytes(payload):
            raise V2ReleaseJobError(f"source inventory differs from bytes: {path}")
        indexed[path] = record
    if not set(indexed) >= _REQUIRED_SOURCE_FILES:
        raise V2ReleaseJobError("Git source lacks the V2 conversion entrypoint")


def v2_release_runner_payload() -> bytes:
    """Return the reviewed offline conversion wrapper."""

    script = """#!/usr/bin/env bash
set -euo pipefail
unset PYTHONPATH PYTHONHOME HF_TOKEN HUGGING_FACE_HUB_TOKEN
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export PYTHONNOUSERSITE=1
export TOKENIZERS_PARALLELISM=false
python -m pip install --break-system-packages --no-cache-dir \
  jsonschema==4.26.0 numpy==2.5.1 pyyaml==6.0.3 \
  safetensors==0.8.0 sentencepiece==0.2.2 transformers==4.57.6
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export NO_PROXY='*'
LLAMA_CPP_LIBRARY_PATH="$(python -I - <<'PY'
import pathlib
import sys

sys.path.insert(0, "/input/packet/source/ml/scriber_polishing/src")
from scriber_polishing.llama_cpp_bundle import load_verified_llama_cpp_bundle

bundle = load_verified_llama_cpp_bundle(
    pathlib.Path("/input/llama-cpp"),
    pathlib.Path("/input/llama-cpp/llama-cpp-bundle-lock.json"),
)
print(":".join(str(path) for path in bundle.runtime_library_paths))
PY
)"
if [ -z "$LLAMA_CPP_LIBRARY_PATH" ]; then
  echo "verified llama.cpp library path is empty" >&2
  exit 1
fi
export LD_LIBRARY_PATH="$LLAMA_CPP_LIBRARY_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec python -I /input/packet/source/ml/scriber_polishing/scripts/run_v2_q8_bf16_quantization.py \
  --plan /input/packet/inputs/v2-release-plan.json \
  --evaluation-binding /input/packet/inputs/v2-evaluation-binding.json \
  --source-checkpoint /input/model \
  --llama-cpp-bundle /input/llama-cpp \
  --llama-cpp-bundle-lock /input/llama-cpp/llama-cpp-bundle-lock.json \
  --output-root /outputs/quantization \
  --python-executable "$(command -v python)"
"""
    return script.replace("\r\n", "\n").encode("utf-8")


def v2_release_bootstrap_payload() -> str:
    """Verify every packet byte in-container before executing the wrapper."""

    return (
        "import hashlib,json,os,pathlib,sys;"
        "r=pathlib.Path('/input/packet');e=sys.argv[1:5];"
        "m=(r/'package-manifest.json').read_bytes();"
        "c=(r/'launch-contract.json').read_bytes();"
        "i=(r/'tree-inventory.json').read_bytes();"
        "h=lambda b:'sha256:'+hashlib.sha256(b).hexdigest();"
        "(h(m),h(c),h(i))==tuple(e[1:4]) or sys.exit('packet metadata hash mismatch');"
        "v=json.loads(i);f=v['files'];"
        "all((lambda p,x:p.is_file() and not p.is_symlink() and p.stat().st_size==x['bytes'] and h(p.read_bytes())==x['sha256'])(r/x['path'],x) for x in f) or sys.exit('packet file mismatch');"
        "h((json.dumps(f,ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False)+'\\n').encode())==e[0] or sys.exit('packet tree mismatch');"
        "os.execv('/bin/bash',['bash',str(r/'run-v2-release.sh')])"
    )


def _require_label_syntax(argv: list[str]) -> None:
    for index, item in enumerate(argv):
        if item != "--label":
            continue
        if index + 1 >= len(argv) or "=" not in argv[index + 1]:
            raise V2ReleaseJobError("HF job label is malformed")
        key, value = argv[index + 1].split("=", 1)
        if _LABEL_COMPONENT.fullmatch(key) is None or _LABEL_COMPONENT.fullmatch(value) is None:
            raise V2ReleaseJobError("HF job label is not accepted by the Jobs API")


def launch_contract(
    *,
    source_git_head: str,
    packet_tree_sha256: str,
    manifest_sha256: str,
    inventory_sha256: str,
) -> dict[str, object]:
    """Build the one reviewed detached Jobs CLI invocation."""

    if _COMMIT.fullmatch(source_git_head) is None:
        raise V2ReleaseJobError("source Git HEAD is invalid")
    for value, label in (
        (packet_tree_sha256, "packet tree"),
        (manifest_sha256, "manifest"),
        (inventory_sha256, "inventory"),
    ):
        _require_hash(value, label)
    packet_volume = f"{V2_RELEASE_PACKET_REMOTE_URI}:/input/packet:ro"
    model_volume = f"{V2_SOURCE_URI}:/input/model:ro"
    llama_volume = f"{TOOLCHAIN_REMOTE_OUTPUT_URI}:/input/llama-cpp:ro"
    output_volume = f"{V2_RELEASE_OUTPUT_URI}:/outputs:rw"
    argv = [
        "hf",
        "jobs",
        "run",
        "--detach",
        "--flavor",
        V2_RELEASE_JOB_FLAVOR,
        "--timeout",
        f"{V2_RELEASE_JOB_TIMEOUT_MINUTES}m",
        "--label",
        f"name={V2_RELEASE_JOB_NAME}",
        "--label",
        "project=scriber-polishing",
        "--label",
        "campaign=v2-q8-bf16-release",
        "--label",
        f"attempt={V2_RELEASE_ATTEMPT}",
        "--label",
        "role=v2-quantization",
        "--label",
        f"output-prefix={V2_RELEASE_OUTPUT_LABEL}",
        "--label",
        f"launch-claim={V2_RELEASE_CLAIM_OWNER_PLACEHOLDER}",
        "-v",
        packet_volume,
        "-v",
        model_volume,
        "-v",
        llama_volume,
        "-v",
        output_volume,
        V2_RELEASE_JOB_IMAGE,
        "python",
        "-I",
        "-c",
        v2_release_bootstrap_payload(),
        packet_tree_sha256,
        manifest_sha256,
        V2_RELEASE_CONTRACT_HASH_PLACEHOLDER,
        inventory_sha256,
    ]
    _require_label_syntax(argv)
    contract = {
        "schema_version": 1,
        "kind": "scriber_hf_v2_q8_bf16_release_launch_contract",
        "attempt": V2_RELEASE_ATTEMPT,
        "source_git_head": source_git_head,
        "image": V2_RELEASE_JOB_IMAGE,
        "flavor": V2_RELEASE_JOB_FLAVOR,
        "timeout": f"{V2_RELEASE_JOB_TIMEOUT_MINUTES}m",
        "job_name": V2_RELEASE_JOB_NAME,
        "packet_remote_uri": V2_RELEASE_PACKET_REMOTE_URI,
        "model_remote_uri": V2_SOURCE_URI,
        "llama_cpp_remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "output_mount_uri": V2_RELEASE_OUTPUT_URI,
        "remote_result_uri": f"{V2_RELEASE_OUTPUT_URI}/quantization",
        "packet_volume": packet_volume,
        "model_volume": model_volume,
        "llama_cpp_volume": llama_volume,
        "output_volume": output_volume,
        "packet_tree_sha256": packet_tree_sha256,
        "manifest_sha256": manifest_sha256,
        "inventory_sha256": inventory_sha256,
        "launch_claim_placeholder": V2_RELEASE_CLAIM_OWNER_PLACEHOLDER,
        "contract_hash_placeholder": V2_RELEASE_CONTRACT_HASH_PLACEHOLDER,
        "remote_packet_exact_inventory_required": True,
        "remote_output_absence_required": True,
        "active_job_snapshots_required": 3,
        "external_job_started": False,
        "auto_retry_allowed": False,
        "secret_names": [],
        "argv": argv,
    }
    validate_v2_release_job_schema(
        contract,
        "hf_v2_release_launch_contract_schema.json",
        "attempt15 launch contract",
    )
    return contract


def package_v2_release_job(
    *,
    repository_root: str | Path,
    plan_path: str | Path,
    evaluation_binding_path: str | Path,
    output_dir: str | Path,
    source_materializer: SourceMaterializer = _materialize_git_head,
) -> dict[str, object]:
    """Package attempt15 from exact accepted evidence without model weights."""

    plan_payload = _regular_bytes(Path(plan_path).expanduser().resolve(), "V2 release plan")
    binding_payload = _regular_bytes(
        Path(evaluation_binding_path).expanduser().resolve(), "V2 evaluation binding"
    )
    try:
        plan = validate_v2_release_plan(_canonical_object(plan_payload, "V2 release plan"))
        validate_v2_evaluation_binding(
            _canonical_object(binding_payload, "V2 evaluation binding")
        )
    except Exception as error:
        raise V2ReleaseJobError(f"V2 plan or evaluation binding is not accepted: {error}") from error
    binding_sha = sha256_bytes(binding_payload)
    if plan["evaluation_gate"]["binding_sha256"] != binding_sha:
        raise V2ReleaseJobError("V2 plan and accepted evaluation binding differ")

    target = Path(output_dir).expanduser().resolve()
    if target.exists():
        raise V2ReleaseJobError("refusing to overwrite an attempt15 packet")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        source_head, source_tree, records = source_materializer(
            Path(repository_root).expanduser().resolve(), staging / "source"
        )
        if source_head != plan["source_git_head"]:
            raise V2ReleaseJobError("V2 plan source Git HEAD differs from packet source")
        _require_source_records(staging, records)

        inputs = staging / "inputs"
        inputs.mkdir()
        (inputs / "v2-release-plan.json").write_bytes(plan_payload)
        (inputs / "v2-evaluation-binding.json").write_bytes(binding_payload)
        records.extend(
            [
                _record(inputs / "v2-release-plan.json", staging),
                _record(inputs / "v2-evaluation-binding.json", staging),
            ]
        )
        runner = v2_release_runner_payload()
        (staging / "run-v2-release.sh").write_bytes(runner)
        records.append(_record(staging / "run-v2-release.sh", staging))
        records.sort(key=lambda item: str(item["path"]))

        packet_tree = sha256_bytes(canonical_json_bytes(records))
        inventory = {
            "schema_version": 1,
            "kind": "scriber_hf_v2_q8_bf16_release_tree_inventory",
            "algorithm": "sha256-canonical-file-records-v1",
            "files": records,
            "file_count": len(records),
            "byte_count": sum(int(record["bytes"]) for record in records),
            "packet_tree_sha256": packet_tree,
        }
        validate_v2_release_job_schema(
            inventory,
            "hf_v2_release_tree_inventory_schema.json",
            "attempt15 tree inventory",
        )
        inventory_payload = canonical_json_bytes(inventory)
        inventory_sha = sha256_bytes(inventory_payload)
        (staging / "tree-inventory.json").write_bytes(inventory_payload)

        manifest = {
            "schema_version": 1,
            "kind": "scriber_hf_v2_q8_bf16_release_job_packet",
            "attempt": V2_RELEASE_ATTEMPT,
            "source": {
                "git_head": source_head,
                "git_tree": source_tree,
                "scope": "complete_tracked_HEAD_tree",
                "dirty_worktree_included": False,
            },
            "plan": {
                "path": "inputs/v2-release-plan.json",
                "sha256": sha256_bytes(plan_payload),
            },
            "evaluation_binding": {
                "path": "inputs/v2-evaluation-binding.json",
                "sha256": binding_sha,
                "status": "accepted",
                "automatic_case_count": 300,
                "terra_pair_count": 100,
                "publication_grade": False,
            },
            "model": {
                **dict(plan["source_model"]),
                "mount_path": "/input/model",
                "copy_into_packet": False,
            },
            "toolchain": {
                "remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
                "mount_path": "/input/llama-cpp",
                "lock_path": "/input/llama-cpp/llama-cpp-bundle-lock.json",
                "read_only": True,
            },
            "conversion": {
                "execution_order": ["BF16", "Q8_0"],
                "gguf_quantizations": ["Q8_0"],
                "other_variants_allowed": False,
                "network_access": "disabled_offline",
            },
            "runtime": {
                "image": V2_RELEASE_JOB_IMAGE,
                "flavor": V2_RELEASE_JOB_FLAVOR,
                "timeout_minutes": V2_RELEASE_JOB_TIMEOUT_MINUTES,
            },
            "output": {
                "remote_uri": f"{V2_RELEASE_OUTPUT_URI}/quantization",
                "container_path": V2_RELEASE_OUTPUT_CONTAINER,
                "unique": True,
                "overwrite_allowed": False,
            },
            "inventory": {
                "path": "tree-inventory.json",
                "sha256": inventory_sha,
                "packet_tree_sha256": packet_tree,
                "file_count": len(records),
                "byte_count": inventory["byte_count"],
            },
            "side_effects": {
                "publication_allowed": False,
                "auto_retry_allowed": False,
                "secrets_required": [],
            },
        }
        validate_v2_release_job_schema(
            manifest,
            "hf_v2_release_packet_manifest_schema.json",
            "attempt15 packet manifest",
        )
        manifest_payload = canonical_json_bytes(manifest)
        manifest_sha = sha256_bytes(manifest_payload)
        (staging / "package-manifest.json").write_bytes(manifest_payload)
        contract = launch_contract(
            source_git_head=source_head,
            packet_tree_sha256=packet_tree,
            manifest_sha256=manifest_sha,
            inventory_sha256=inventory_sha,
        )
        contract_payload = canonical_json_bytes(contract)
        contract_sha = sha256_bytes(contract_payload)
        (staging / "launch-contract.json").write_bytes(contract_payload)
        staging.replace(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "packet_dir": target,
        "source_git_head": source_head,
        "source_git_tree": source_tree,
        "packet_tree_sha256": packet_tree,
        "inventory_sha256": inventory_sha,
        "manifest_sha256": manifest_sha,
        "launch_contract_sha256": contract_sha,
        "external_job_started": False,
    }


def verify_v2_release_job_packet(
    packet_dir: str | Path,
    *,
    expected_packet_tree_sha256: str,
    expected_manifest_sha256: str,
    expected_launch_contract_sha256: str,
) -> dict[str, object]:
    """Verify every packet byte and reconstruct the exact launch contract."""

    for value, label in (
        (expected_packet_tree_sha256, "expected packet tree"),
        (expected_manifest_sha256, "expected manifest"),
        (expected_launch_contract_sha256, "expected launch contract"),
    ):
        _require_hash(value, label)
    candidate = Path(packet_dir).expanduser()
    if candidate.is_symlink():
        raise V2ReleaseJobError("attempt15 packet must not be a symlink")
    root = candidate.resolve()
    if not root.is_dir():
        raise V2ReleaseJobError("attempt15 packet must be a directory")
    manifest_payload = _regular_bytes(root / "package-manifest.json", "packet manifest")
    inventory_payload = _regular_bytes(root / "tree-inventory.json", "packet inventory")
    contract_payload = _regular_bytes(root / "launch-contract.json", "launch contract")
    if sha256_bytes(manifest_payload) != expected_manifest_sha256:
        raise V2ReleaseJobError("packet manifest SHA-256 differs")
    if sha256_bytes(contract_payload) != expected_launch_contract_sha256:
        raise V2ReleaseJobError("launch contract SHA-256 differs")
    manifest = _canonical_object(manifest_payload, "packet manifest")
    inventory = _canonical_object(inventory_payload, "packet inventory")
    contract = _canonical_object(contract_payload, "launch contract")
    validate_v2_release_job_schema(
        manifest,
        "hf_v2_release_packet_manifest_schema.json",
        "attempt15 packet manifest",
    )
    validate_v2_release_job_schema(
        inventory,
        "hf_v2_release_tree_inventory_schema.json",
        "attempt15 tree inventory",
    )
    validate_v2_release_job_schema(
        contract,
        "hf_v2_release_launch_contract_schema.json",
        "attempt15 launch contract",
    )
    files = inventory.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(row, Mapping) for row in files):
        raise V2ReleaseJobError("packet inventory is empty or malformed")
    paths: set[str] = set()
    for row in files:
        path = row.get("path")
        if not isinstance(path, str) or not _safe_relative(path) or path in paths:
            raise V2ReleaseJobError("packet inventory path is unsafe or duplicated")
        payload = _regular_bytes(root / path, f"packet file {path}")
        if row.get("bytes") != len(payload) or row.get("sha256") != sha256_bytes(payload):
            raise V2ReleaseJobError(f"packet file differs from inventory: {path}")
        paths.add(path)
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected = paths | {"package-manifest.json", "tree-inventory.json", "launch-contract.json"}
    if observed != expected or any(path.is_symlink() for path in root.rglob("*")):
        raise V2ReleaseJobError("packet contains an unbound file or symlink")
    packet_tree = sha256_bytes(canonical_json_bytes(files))
    if (
        packet_tree != expected_packet_tree_sha256
        or inventory.get("packet_tree_sha256") != packet_tree
        or inventory.get("file_count") != len(files)
        or inventory.get("byte_count") != sum(int(row["bytes"]) for row in files)
        or manifest.get("inventory", {}).get("sha256") != sha256_bytes(inventory_payload)
    ):
        raise V2ReleaseJobError("packet tree binding differs")
    _require_source_records(root, [dict(row) for row in files if str(row.get("path", "")).startswith("source/")])
    plan_payload = _regular_bytes(root / "inputs/v2-release-plan.json", "packet V2 plan")
    binding_payload = _regular_bytes(
        root / "inputs/v2-evaluation-binding.json", "packet V2 binding"
    )
    try:
        plan = validate_v2_release_plan(_canonical_object(plan_payload, "packet V2 plan"))
        validate_v2_evaluation_binding(_canonical_object(binding_payload, "packet V2 binding"))
    except Exception as error:
        raise V2ReleaseJobError(f"packet plan or Terra binding is invalid: {error}") from error
    if (
        plan["evaluation_gate"]["binding_sha256"] != sha256_bytes(binding_payload)
        or manifest.get("plan", {}).get("sha256") != sha256_bytes(plan_payload)
        or manifest.get("evaluation_binding", {}).get("sha256") != sha256_bytes(binding_payload)
        or manifest.get("model", {}).get("remote_uri") != V2_SOURCE_URI
        or manifest.get("toolchain", {}).get("remote_uri") != TOOLCHAIN_REMOTE_OUTPUT_URI
        or manifest.get("conversion", {}).get("gguf_quantizations") != ["Q8_0"]
        or manifest.get("side_effects")
        != {"publication_allowed": False, "auto_retry_allowed": False, "secrets_required": []}
    ):
        raise V2ReleaseJobError("packet immutable V2/model/toolchain bindings differ")
    source = manifest.get("source")
    if not isinstance(source, Mapping) or source.get("git_head") != plan["source_git_head"]:
        raise V2ReleaseJobError("packet source Git HEAD differs from the plan")
    expected_contract = launch_contract(
        source_git_head=str(source["git_head"]),
        packet_tree_sha256=packet_tree,
        manifest_sha256=expected_manifest_sha256,
        inventory_sha256=sha256_bytes(inventory_payload),
    )
    if contract != expected_contract:
        raise V2ReleaseJobError("launch contract differs from the exact attempt15 argv")
    return {
        "status": "ready",
        "source_git_head": source["git_head"],
        "source_git_tree": source["git_tree"],
        "packet_tree_sha256": packet_tree,
        "manifest_sha256": expected_manifest_sha256,
        "launch_contract_sha256": expected_launch_contract_sha256,
        "model_remote_uri": V2_SOURCE_URI,
        "llama_cpp_remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
        "remote_result_uri": f"{V2_RELEASE_OUTPUT_URI}/quantization",
        "variants": ["Q8_0", "BF16"],
        "external_job_started": False,
        "file_count": len(files),
    }
