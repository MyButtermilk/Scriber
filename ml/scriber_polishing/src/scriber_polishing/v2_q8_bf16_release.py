"""Fail-closed V2 Q8_0/BF16 quantization and public-release contracts.

This module is deliberately inert at import and plan-build time.  It prepares
one new attempt only after an accepted, exact V2 evaluation binding; it does
not contact Hugging Face, create a claim, start a Job, or change a repository.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .hf_llama_cpp_toolchain import TOOLCHAIN_ATTEMPT, TOOLCHAIN_REMOTE_OUTPUT_URI

PROJECT_ROOT = Path(__file__).parents[2]
CONTRACTS_ROOT = PROJECT_ROOT / "contracts"

V2_SOURCE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt13/"
    "scriber-v2-hardcase-replay-v1/training/final-7b23f23e70f60df9"
)
V2_SOURCE_TREE_SHA256 = "sha256:4233c0084cacdd3ba88207cece0ce20d1e1f56cfbfa64e71182a2deb2934c743"
V2_SOURCE_WEIGHT_SHA256 = "sha256:fd86d26f70e128fd1e8d4f11e69d4778ae930e628e374aa64595aca5bd0b9bb3"
V2_SOURCE_WEIGHT_BYTES = 536_223_056
V2_RUN_FINGERPRINT = "sha256:7b23f23e70f60df9d58961808e529ff7738032de75a7a4a62c12529bc734a45c"
PROTECTION_POLICY_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
V2_POSTFLIGHT_BINDING_SHA256 = (
    "sha256:4e9379d8c26cb81d7261b57ab2e0bf5c8059850c07717debc47c0ce99072a8a5"
)
V2_PUBLIC_SOURCE_ID = f"model-tree:{V2_SOURCE_TREE_SHA256}"

V2_RELEASE_ATTEMPT = "attempt15"
V2_RELEASE_JOB_NAME = "scriber-v2-q8-bf16-release-v1"
V2_RELEASE_CLAIM_REPOSITORY = "Buttermilk03/scriber-polishing-launch-claims"
V2_RELEASE_CLAIM_PATH = (
    "claims/v2-q8-bf16-release/attempt15-scriber-v2-q8-bf16-release-v1.json"
)
V2_RELEASE_OUTPUT_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt15/"
    "scriber-v2-q8-bf16-release-v1"
)
V2_PUBLIC_REPOSITORY = "Buttermilk03/scriber-gemma3-270m-polishing-de-v2"
V1_FALLBACK_REPOSITORY = "Buttermilk03/scriber-gemma3-270m-polishing-de-v1"
V1_FALLBACK_REVISION = "65bae06a655fc209058d60064c65fe3e891e4a43"

V2_VARIANTS = ("Q8_0", "BF16")
V2_CONVERSION_ORDER = ("BF16", "Q8_0")
V2_PUBLIC_ALLOWLIST = (
    "GEMMA_LICENSE.md",
    "MODIFICATIONS.md",
    "NOTICE",
    "README.md",
    "gguf/bf16/model-BF16.gguf",
    "gguf/bf16/scriber-protection-policy.json",
    "gguf/bf16/variant-artifact-manifest.json",
    "gguf/q8_0/model-Q8_0.gguf",
    "gguf/q8_0/scriber-protection-policy.json",
    "gguf/q8_0/variant-artifact-manifest.json",
    "v2-public-release-inventory.json",
)

GEMMA_LICENSE_SHA256 = "sha256:d7bbbe9ad37291cdef58c411c31e02f21e9afb965c41f18a2d437f0893482624"
GEMMA_LICENSE_BYTES = 13_125

_PUBLIC_PAYLOAD_PATHS = tuple(
    path for path in V2_PUBLIC_ALLOWLIST if path != "v2-public-release-inventory.json"
)
_QUANTIZATION_FILES = frozenset(
    {
        "Q8_0/model-Q8_0.gguf",
        "Q8_0/scriber-protection-policy.json",
        "Q8_0/variant-artifact-manifest.json",
        "bf16/model-BF16.gguf",
        "bf16/scriber-protection-policy.json",
        "bf16/variant-artifact-manifest.json",
        "gguf-bundle-manifest.json",
        "v2-quantization-result.json",
    }
)

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class V2ReleaseError(RuntimeError):
    """The V2 quantization or publication evidence is incomplete or changed."""


class AnonymousPublicProbe(Protocol):
    """Read-only, token-free remote metadata and one-byte artifact probes."""

    def repository_info(self, repository_id: str, revision: str) -> Mapping[str, Any]: ...

    def list_files(
        self, repository_id: str, revision: str
    ) -> list[Mapping[str, Any]]: ...

    def head(
        self, repository_id: str, revision: str, path: str
    ) -> Mapping[str, Any]: ...

    def range_get(
        self,
        repository_id: str,
        revision: str,
        path: str,
        *,
        range_header: str,
    ) -> Mapping[str, Any]: ...


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@cache
def _validator(filename: str) -> Draft202012Validator:
    path = CONTRACTS_ROOT / filename
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate_schema(value: Mapping[str, Any], filename: str, label: str) -> None:
    errors = sorted(
        _validator(filename).iter_errors(dict(value)),
        key=lambda error: [str(part) for part in error.absolute_path],
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path) or "<root>"
        raise V2ReleaseError(
            f"{label} failed schema at {location}: {errors[0].message}"
        )


def _expected_source_model() -> dict[str, object]:
    return {
        "remote_uri": V2_SOURCE_URI,
        "tree_sha256": V2_SOURCE_TREE_SHA256,
        "run_fingerprint": V2_RUN_FINGERPRINT,
        "weight": {
            "path": "model.safetensors",
            "bytes": V2_SOURCE_WEIGHT_BYTES,
            "sha256": V2_SOURCE_WEIGHT_SHA256,
        },
        "protection_policy_sha256": PROTECTION_POLICY_SHA256,
        "postflight_binding_sha256": V2_POSTFLIGHT_BINDING_SHA256,
    }


def _expected_public_source_model() -> dict[str, object]:
    """Return only immutable, non-private provenance safe for public artifacts."""

    source = _expected_source_model()
    source.pop("remote_uri")
    return source


def _public_terra_judge(value: object) -> dict[str, Any]:
    """Strip private artifact locations while retaining their immutable identities."""

    if not isinstance(value, Mapping):
        raise V2ReleaseError("V2 Terra evidence is missing from the release plan")
    public = json.loads(json.dumps(value))
    for key in ("aggregate", "verdicts"):
        artifact = public.get(key)
        if not isinstance(artifact, dict) or not isinstance(artifact.pop("uri", None), str):
            raise V2ReleaseError(f"V2 Terra {key} binding is incomplete")
    return public


def _expected_evidence_scope() -> dict[str, object]:
    return {
        "kind": "compact_smoke_test",
        "publication_grade": False,
        "user_authorized_promotion": True,
        "automatic_case_count": 300,
        "automatic_cases_per_suite": {
            "ai_gold": 100,
            "critical_regression": 100,
            "regular_test": 100,
        },
        "blind_judges": {
            "terra_pair_count": 100,
            "sol_pair_count": 0,
        },
    }


def _validate_compact_comparison_gate(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise V2ReleaseError("V2 release requires the exact 300-case comparison gate")
    gate = dict(value)
    coverage = gate.get("coverage")
    exact_match = gate.get("normalized_exact_match")
    critical = gate.get("deterministic_critical_error_count")
    if (
        gate.get("kind") != "scriber_v2_compact_comparison_gate"
        or gate.get("source_report_suite") != "combined"
        or gate.get("record_count") != 300
        or gate.get("decision") != "passed"
        or gate.get("publication_grade") is not False
        or gate.get("requires_terra") is not True
        or coverage != {"v1_reused": 300, "v2": 300, "exact": True}
        or not isinstance(exact_match, Mapping)
        or not isinstance(critical, Mapping)
    ):
        raise V2ReleaseError("V2 compact comparison gate is not an exact passed 300-case gate")
    v1_match = exact_match.get("v1_reused")
    v2_match = exact_match.get("v2")
    v1_critical = critical.get("v1_reused")
    v2_critical = critical.get("v2")
    if (
        isinstance(v1_match, bool)
        or not isinstance(v1_match, int | float)
        or isinstance(v2_match, bool)
        or not isinstance(v2_match, int | float)
        or not math.isfinite(v1_match)
        or not math.isfinite(v2_match)
        or not 0 <= v1_match <= 1
        or not 0 <= v2_match <= 1
        or exact_match.get("v2_gte_v1") is not True
        or v2_match < v1_match
        or isinstance(v1_critical, bool)
        or not isinstance(v1_critical, int)
        or isinstance(v2_critical, bool)
        or not isinstance(v2_critical, int)
        or v1_critical < 0
        or v2_critical < 0
        or critical.get("v2_lte_v1") is not True
        or v2_critical > v1_critical
    ):
        raise V2ReleaseError("V2 compact comparison metrics do not prove the passed decision")
    return gate


def validate_v2_evaluation_binding(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the narrow handoff emitted only after the V2 eval passes."""

    materialized = dict(value)
    if materialized.get("status") != "accepted":
        raise V2ReleaseError("V2 evaluation binding is not accepted")
    model = materialized.get("model")
    expected = _expected_source_model()
    if not isinstance(model, Mapping) or any(
        model.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise V2ReleaseError("V2 model binding differs from the completed attempt13 model")
    if materialized.get("evidence_scope") != _expected_evidence_scope():
        raise V2ReleaseError(
            "V2 compact evaluation scope or publication-grade label differs"
        )
    _validate_compact_comparison_gate(materialized.get("compact_comparison_gate"))
    terra = materialized.get("terra_judge")
    terra_gate = terra.get("gate") if isinstance(terra, Mapping) else None
    if (
        not isinstance(terra, Mapping)
        or terra.get("evidence_class") != "compact_smoke_test"
        or terra.get("publication_grade") is not False
        or not isinstance(terra_gate, Mapping)
        or terra_gate.get("decision") != "passed"
        or terra_gate.get("v2_wins_gte_v1_wins") is not True
        or terra_gate.get("v2_critical_lte_v1_critical") is not True
    ):
        raise V2ReleaseError("V2 release requires the exact passed Terra100 compact handoff")
    _validate_schema(
        materialized,
        "v2_release_evaluation_binding_schema.json",
        "V2 evaluation binding",
    )
    if materialized.get("training_steps_executed") != 0:
        raise V2ReleaseError("V2 evaluation binding unexpectedly executed training")
    reports = materialized.get("reports")
    if not isinstance(reports, Mapping) or any(
        not isinstance(reports.get(suite), Mapping)
        or reports[suite].get("evaluation_exit") not in {0, 2}
        or reports[suite].get("integrity_verified") is not True
        or reports[suite].get("quality_gate_claimed") is not False
        for suite in ("ai_gold", "critical_regression", "regular_test")
    ):
        raise V2ReleaseError(
            "V2 automatic report integrity is incomplete or falsely claims a quality gate"
        )
    return materialized


def build_v2_release_plan(
    evaluation_binding: Mapping[str, Any],
    *,
    evaluation_binding_sha256: str,
    source_git_head: str,
) -> dict[str, object]:
    """Build an inert attempt15 plan from one accepted V2 evaluation binding."""

    binding = validate_v2_evaluation_binding(evaluation_binding)
    if _HASH.fullmatch(evaluation_binding_sha256) is None:
        raise V2ReleaseError("V2 evaluation binding SHA-256 is invalid")
    if evaluation_binding_sha256 != sha256_bytes(canonical_json_bytes(binding)):
        raise V2ReleaseError("V2 evaluation binding SHA-256 differs from its canonical bytes")
    if _COMMIT.fullmatch(source_git_head) is None:
        raise V2ReleaseError("source Git HEAD must be a full lowercase commit")
    plan: dict[str, object] = {
        "schema_version": 1,
        "kind": "scriber_v2_q8_bf16_release_plan",
        "status": "ready_after_accepted_evaluation",
        "source_git_head": source_git_head,
        "variants": list(V2_VARIANTS),
        "source_model": _expected_source_model(),
        "evaluation_gate": {
            "binding_sha256": evaluation_binding_sha256,
            "result_uri": binding["evaluation_result_uri"],
            "result_sha256": binding["evaluation_result_sha256"],
            "inventory_sha256": binding["evaluation_inventory_sha256"],
            "reports": dict(binding["reports"]),
            "compact_comparison_gate": dict(binding["compact_comparison_gate"]),
            "terra_judge": dict(binding["terra_judge"]),
            "evidence_scope": _expected_evidence_scope(),
            "training_steps_executed": 0,
        },
        "launch": {
            "attempt": V2_RELEASE_ATTEMPT,
            "job_name": V2_RELEASE_JOB_NAME,
            "claim_repository": V2_RELEASE_CLAIM_REPOSITORY,
            "claim_path": V2_RELEASE_CLAIM_PATH,
            "output_uri": V2_RELEASE_OUTPUT_URI,
            "external_job_started": False,
            "auto_retry_allowed": False,
        },
        "conversion": {
            "execution_order": list(V2_CONVERSION_ORDER),
            "gguf_quantizations": ["Q8_0"],
            "toolchain_remote_uri": TOOLCHAIN_REMOTE_OUTPUT_URI,
            "toolchain_attempt": TOOLCHAIN_ATTEMPT,
            "toolchain_result_verifier": (
                "scriber_polishing.hf_llama_cpp_toolchain."
                "verify_hf_llama_cpp_toolchain_result"
            ),
            "bundle_verifier": (
                "scriber_polishing.llama_cpp_bundle.load_verified_llama_cpp_bundle"
            ),
            "requires_gemma3_text_capability_probe": True,
            "network_access": "disabled_offline",
        },
        "publication": {
            "repository_id": V2_PUBLIC_REPOSITORY,
            "visibility": "public",
            "requires_token": False,
            "evidence_label": "user_authorized_compact_smoke_test",
            "full_release_study_claim_allowed": False,
            "allowlisted_paths": list(V2_PUBLIC_ALLOWLIST),
            "exact_inventory_required": True,
            "immutable_commit_required": True,
            "anonymous_verification": {
                "authorization_header_allowed": False,
                "head_required": True,
                "range_get_required": True,
                "range": "bytes=0-0",
                "full_model_redownload_allowed": False,
            },
        },
        "fallback": {
            "repository_id": V1_FALLBACK_REPOSITORY,
            "revision": V1_FALLBACK_REVISION,
            "remains_active_until": "v2_public_release_verified",
            "automatic_switch_allowed": False,
        },
    }
    return validate_v2_release_plan(plan)


def validate_v2_release_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete inert plan without contacting any external system."""

    materialized = dict(value)
    _validate_schema(
        materialized,
        "v2_q8_bf16_release_plan_schema.json",
        "V2 Q8/BF16 release plan",
    )
    if materialized.get("variants") != list(V2_VARIANTS):
        raise V2ReleaseError("V2 release variants differ from Q8_0 and BF16")
    source = materialized.get("source_model")
    if source != _expected_source_model():
        raise V2ReleaseError("V2 release plan source model differs")
    publication = materialized.get("publication")
    if not isinstance(publication, Mapping) or publication.get("requires_token") is not False:
        raise V2ReleaseError("V2 public release must never require a token")
    if publication.get("allowlisted_paths") != list(V2_PUBLIC_ALLOWLIST):
        raise V2ReleaseError("V2 public release allowlist differs")
    return materialized


def _regular_file_record(path: Path, *, relative_path: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise V2ReleaseError(f"V2 artifact is not a regular file: {relative_path}")
    payload_hash = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            payload_hash.update(chunk)
    size = path.stat().st_size
    if size < 1:
        raise V2ReleaseError(f"V2 artifact is empty: {relative_path}")
    return {
        "path": relative_path,
        "bytes": size,
        "sha256": "sha256:" + payload_hash.hexdigest(),
    }


def _git_blob_oid(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quantized_artifact_record(
    root: Path,
    *,
    public_variant: str,
    internal_variant: str,
    model_filename: str,
    bundle_manifest: Mapping[str, Any],
) -> dict[str, object]:
    variant_root = root / internal_variant
    records = {
        "model": _regular_file_record(
            variant_root / model_filename,
            relative_path=f"{internal_variant}/{model_filename}",
        ),
        "protection_policy": _regular_file_record(
            variant_root / "scriber-protection-policy.json",
            relative_path=f"{internal_variant}/scriber-protection-policy.json",
        ),
        "variant_manifest": _regular_file_record(
            variant_root / "variant-artifact-manifest.json",
            relative_path=f"{internal_variant}/variant-artifact-manifest.json",
        ),
    }
    if records["protection_policy"]["sha256"] != PROTECTION_POLICY_SHA256:
        raise V2ReleaseError(f"{public_variant} protection policy differs from the V2 source")
    bindings = bundle_manifest.get("variant_manifests")
    binding = bindings.get(internal_variant) if isinstance(bindings, Mapping) else None
    reload = binding.get("reload_self_test") if isinstance(binding, Mapping) else None
    if (
        not isinstance(binding, Mapping)
        or not isinstance(reload, Mapping)
        or reload.get("status") != "passed"
        or _HASH.fullmatch(str(reload.get("chat_template_sha256", ""))) is None
    ):
        raise V2ReleaseError(f"{public_variant} has no bound fresh reload evidence")
    return {
        "variant": public_variant,
        "artifact_tree_sha256": binding.get("artifact_tree_sha256"),
        "chat_template_sha256": reload["chat_template_sha256"],
        **records,
    }


def validate_v2_quantization_result(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    materialized = dict(value)
    checked_plan = validate_v2_release_plan(plan)
    _validate_schema(
        materialized,
        "v2_q8_bf16_quantization_result_schema.json",
        "V2 Q8/BF16 quantization result",
    )
    if materialized.get("plan_sha256") != sha256_bytes(canonical_json_bytes(checked_plan)):
        raise V2ReleaseError("V2 quantization result is bound to a different release plan")
    if materialized.get("evaluation_binding_sha256") != checked_plan["evaluation_gate"][
        "binding_sha256"
    ]:
        raise V2ReleaseError("V2 quantization result is bound to a different evaluation gate")
    if materialized.get("source_model") != checked_plan["source_model"]:
        raise V2ReleaseError("V2 quantization result is bound to a different source model")
    if materialized.get("variants") != list(V2_VARIANTS):
        raise V2ReleaseError("V2 quantization result contains variants outside Q8_0/BF16")
    artifacts = materialized.get("artifacts")
    if (
        not isinstance(artifacts, Mapping)
        or not isinstance(artifacts.get("Q8_0"), Mapping)
        or artifacts["Q8_0"].get("variant") != "Q8_0"
        or not isinstance(artifacts.get("BF16"), Mapping)
        or artifacts["BF16"].get("variant") != "BF16"
    ):
        raise V2ReleaseError("V2 quantization artifact variants are swapped or incomplete")
    if materialized.get("artifact_inventory_sha256") != sha256_bytes(
        canonical_json_bytes(artifacts)
    ):
        raise V2ReleaseError("V2 quantization artifact inventory SHA-256 differs")
    return materialized


def validate_v2_public_bundle_inventory(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a staged, still-unpublished V2 public payload inventory."""

    materialized = dict(value)
    _validate_schema(
        materialized,
        "v2_public_bundle_inventory_schema.json",
        "V2 public bundle inventory",
    )
    if materialized.get("allowlisted_paths") != list(V2_PUBLIC_ALLOWLIST):
        raise V2ReleaseError("V2 public bundle allowlist differs")
    records = materialized.get("payload_files")
    if not isinstance(records, list):
        raise V2ReleaseError("V2 public bundle payload inventory is missing")
    if [record.get("path") for record in records if isinstance(record, Mapping)] != list(
        _PUBLIC_PAYLOAD_PATHS
    ):
        raise V2ReleaseError("V2 public bundle payload paths differ from the closed allowlist")
    if materialized.get("payload_tree_sha256") != sha256_bytes(
        canonical_json_bytes(records)
    ):
        raise V2ReleaseError("V2 public bundle payload tree SHA-256 differs")
    return materialized


def _verified_record_path(
    root: Path,
    record: Mapping[str, Any],
    *,
    expected_relative_path: str,
) -> Path:
    if record.get("path") != expected_relative_path:
        raise V2ReleaseError(
            f"V2 artifact record path differs: expected {expected_relative_path}"
        )
    source = (root / expected_relative_path).resolve()
    try:
        source.relative_to(root)
    except ValueError as error:
        raise V2ReleaseError("V2 artifact path escaped the quantization root") from error
    actual = _regular_file_record(source, relative_path=expected_relative_path)
    if actual != dict(record):
        raise V2ReleaseError(f"V2 artifact bytes differ from inventory: {expected_relative_path}")
    return source


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def _copy_new(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def _public_notice() -> bytes:
    return (
        b"Gemma is provided under and subject to the Gemma Terms of Use found at "
        b"ai.google.dev/gemma/terms\n\n"
        b"Google LLC is the provider of Gemma. Scriber modified Gemma by full fine-tuning "
        b"it for conservative transcript polishing. Google does not endorse Scriber.\n"
    )


def _public_modifications() -> bytes:
    return (
        "# Scriber V2 modifications\n\n"
        "Base model: `google/gemma-3-270m-it`\n\n"
        "Scriber V2 is a warm-start continuation of the Scriber transcript-polishing "
        "model, trained on synthetic AI-only hard-case replay data. Its exact accepted "
        f"source has model tree `{V2_SOURCE_TREE_SHA256}` and run fingerprint "
        f"`{V2_RUN_FINGERPRINT}`.\n\n"
        "The distributed GGUF files are the BF16 reference conversion and the Q8_0 "
        "desktop default. Promotion was authorized from a compact smoke test; this "
        "evidence is not a full release study.\n"
    ).encode()


def _assert_no_private_provenance(path: Path) -> None:
    """Reject a private Hugging Face bucket marker in any public payload byte stream."""

    markers = (b"hf://buckets/", b"scriber-polishing-private-runs")
    overlap = b""
    overlap_size = max(map(len, markers)) - 1
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            window = overlap + chunk
            if any(marker in window for marker in markers):
                raise V2ReleaseError(
                    f"public V2 payload contains private provenance: {path.name}"
                )
            overlap = window[-overlap_size:]


def _public_readme() -> bytes:
    return (
        b"---\n"
        b"license: gemma\n"
        b"library_name: llama.cpp\n"
        b"language:\n"
        b"- de\n"
        b"base_model: google/gemma-3-270m-it\n"
        b"tags:\n"
        b"- text-generation\n"
        b"- gguf\n"
        b"- transcript-polishing\n"
        b"---\n\n"
        b"# Scriber German transcript polishing V2\n\n"
        b"This public repository contains the Scriber V2 local transcript-polishing "
        b"model in two GGUF variants. Q8_0 is the desktop/laptop default; BF16 is the "
        b"larger reference conversion. Downloads are public and require no Hugging Face "
        b"account or token.\n\n"
        b"## Evaluation scope\n\n"
        b"Promotion was explicitly authorized from a compact smoke test: 300 automatic "
        b"cases (100 AI-Gold, 100 critical-regression, 100 regular-test) plus 100 blind "
        b"Terra V2-vs-V1 pairs. Sol was not run. This is not a full release study, and "
        b"the repository must not be described as publication-grade evidence.\n\n"
        b"## Files\n\n"
        b"- `gguf/q8_0/model-Q8_0.gguf`: recommended local default\n"
        b"- `gguf/bf16/model-BF16.gguf`: larger reference variant\n"
        b"- `v2-public-release-inventory.json`: immutable source and artifact binding\n\n"
        b"The application remains pinned to the verified V1 release until an anonymous "
        b"V2 repository check has proven the exact commit and complete allowlist.\n"
    )


def stage_v2_public_bundle(
    plan: Mapping[str, Any],
    *,
    quantization_result: Mapping[str, Any],
    quantization_root: str | Path,
    gemma_license: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    """Stage the exact public V2 payload locally without contacting Hugging Face."""

    checked_plan = validate_v2_release_plan(plan)
    result = validate_v2_quantization_result(quantization_result, plan=checked_plan)
    source_root = Path(quantization_root).expanduser().resolve()
    if source_root.is_symlink() or not source_root.is_dir():
        raise V2ReleaseError("V2 quantization root must be a regular directory")
    observed_quantization_files = frozenset(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file()
    )
    if observed_quantization_files != _QUANTIZATION_FILES:
        raise V2ReleaseError("V2 quantization root has an unexpected file inventory")
    if any(path.is_symlink() for path in source_root.rglob("*")):
        raise V2ReleaseError("V2 quantization root contains a symbolic link")
    result_path = source_root / "v2-quantization-result.json"
    if result_path.read_bytes() != canonical_json_bytes(result):
        raise V2ReleaseError("V2 quantization result file differs from supplied result")
    manifest_record = result.get("gguf_bundle_manifest")
    if not isinstance(manifest_record, Mapping):
        raise V2ReleaseError("V2 GGUF bundle manifest record is missing")
    _verified_record_path(
        source_root,
        manifest_record,
        expected_relative_path="gguf-bundle-manifest.json",
    )

    sources: dict[str, Path] = {}
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise V2ReleaseError("V2 artifact inventory is missing")
    artifact_layout = (
        ("BF16", "bf16", "gguf/bf16"),
        ("Q8_0", "Q8_0", "gguf/q8_0"),
    )
    for public_variant, internal_variant, destination_root in artifact_layout:
        artifact = artifacts.get(public_variant)
        if not isinstance(artifact, Mapping):
            raise V2ReleaseError(f"V2 {public_variant} artifact record is missing")
        filenames = {
            "model": f"model-{public_variant}.gguf",
            "protection_policy": "scriber-protection-policy.json",
            "variant_manifest": "variant-artifact-manifest.json",
        }
        for key, filename in filenames.items():
            record = artifact.get(key)
            if not isinstance(record, Mapping):
                raise V2ReleaseError(f"V2 {public_variant} {key} record is missing")
            source = _verified_record_path(
                source_root,
                record,
                expected_relative_path=f"{internal_variant}/{filename}",
            )
            if key == "model" and source.read_bytes()[:4] != b"GGUF":
                raise V2ReleaseError(f"V2 {public_variant} model is not a GGUF artifact")
            sources[f"{destination_root}/{filename}"] = source

    license_path = Path(gemma_license).expanduser().resolve()
    license_record = _regular_file_record(license_path, relative_path="GEMMA_LICENSE.md")
    if license_record != {
        "path": "GEMMA_LICENSE.md",
        "bytes": GEMMA_LICENSE_BYTES,
        "sha256": GEMMA_LICENSE_SHA256,
    }:
        raise V2ReleaseError("Gemma license bytes differ from the reviewed contract")

    output = Path(output_root).expanduser().resolve()
    if output.exists():
        raise V2ReleaseError(f"V2 public bundle output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)).resolve()
    shutil.rmtree(staging)
    staging.mkdir()
    try:
        _copy_new(license_path, staging / "GEMMA_LICENSE.md")
        _write_new(staging / "MODIFICATIONS.md", _public_modifications())
        _write_new(staging / "NOTICE", _public_notice())
        _write_new(staging / "README.md", _public_readme())
        for relative_path, source in sources.items():
            _copy_new(source, staging / relative_path)

        payload_records = [
            _regular_file_record(staging / relative_path, relative_path=relative_path)
            for relative_path in _PUBLIC_PAYLOAD_PATHS
        ]
        inventory: dict[str, Any] = {
            "schema_version": 1,
            "kind": "scriber_v2_public_bundle_inventory",
            "status": "staged",
            "repository_id": V2_PUBLIC_REPOSITORY,
            "visibility": "public",
            "requires_token": False,
            "plan_sha256": sha256_bytes(canonical_json_bytes(checked_plan)),
            "quantization_result_sha256": sha256_bytes(canonical_json_bytes(result)),
            "source_git_head": checked_plan["source_git_head"],
            "source_model": _expected_public_source_model(),
            "variants": list(V2_VARIANTS),
            "evidence_scope": checked_plan["evaluation_gate"]["evidence_scope"],
            "compact_comparison_gate": checked_plan["evaluation_gate"][
                "compact_comparison_gate"
            ],
            "terra_judge": _public_terra_judge(
                checked_plan["evaluation_gate"]["terra_judge"]
            ),
            "payload_files": payload_records,
            "payload_tree_sha256": sha256_bytes(canonical_json_bytes(payload_records)),
            "allowlisted_paths": list(V2_PUBLIC_ALLOWLIST),
            "inventory_path": "v2-public-release-inventory.json",
            "upload_performed": False,
            "public_commit": None,
        }
        validate_v2_public_bundle_inventory(inventory)
        _write_new(
            staging / "v2-public-release-inventory.json",
            canonical_json_bytes(inventory),
        )
        for relative_path in V2_PUBLIC_ALLOWLIST:
            _assert_no_private_provenance(staging / relative_path)
        observed = {
            path.relative_to(staging).as_posix()
            for path in staging.rglob("*")
            if path.is_file()
        }
        if observed != set(V2_PUBLIC_ALLOWLIST):
            raise V2ReleaseError("staged V2 public bundle differs from the closed allowlist")
        os.replace(staging, output)
        return inventory
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_staged_v2_bundle(
    inventory: Mapping[str, Any],
    staged_root: str | Path,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    checked_inventory = validate_v2_public_bundle_inventory(inventory)
    root = Path(staged_root).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise V2ReleaseError("staged V2 public bundle must be a regular directory")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise V2ReleaseError("staged V2 public bundle contains a symbolic link")
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if observed != set(V2_PUBLIC_ALLOWLIST):
        raise V2ReleaseError("staged V2 public bundle differs from the closed allowlist")
    inventory_path = root / "v2-public-release-inventory.json"
    if inventory_path.read_bytes() != canonical_json_bytes(checked_inventory):
        raise V2ReleaseError("staged V2 inventory bytes differ from supplied inventory")
    expected_payload = checked_inventory["payload_files"]
    if not isinstance(expected_payload, list):  # pragma: no cover - schema guarded
        raise V2ReleaseError("staged V2 payload inventory is missing")
    complete_records: list[dict[str, object]] = []
    for relative_path in V2_PUBLIC_ALLOWLIST:
        actual = _regular_file_record(root / relative_path, relative_path=relative_path)
        if relative_path == "v2-public-release-inventory.json":
            complete_records.append(actual)
            continue
        expected = next(
            (
                dict(record)
                for record in expected_payload
                if isinstance(record, Mapping) and record.get("path") == relative_path
            ),
            None,
        )
        if actual != expected:
            raise V2ReleaseError(f"staged V2 payload bytes differ: {relative_path}")
        complete_records.append(actual)
    return checked_inventory, complete_records


def validate_v2_publication_receipt(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate an uploader-issued exact public commit receipt."""

    checked_plan = validate_v2_release_plan(plan)
    checked_inventory = validate_v2_public_bundle_inventory(inventory)
    expected_plan_sha256 = sha256_bytes(canonical_json_bytes(checked_plan))
    if (
        checked_inventory.get("plan_sha256") != expected_plan_sha256
        or checked_inventory.get("source_model") != _expected_public_source_model()
        or checked_inventory.get("evidence_scope")
        != checked_plan["evaluation_gate"]["evidence_scope"]
        or checked_inventory.get("compact_comparison_gate")
        != checked_plan["evaluation_gate"]["compact_comparison_gate"]
        or checked_inventory.get("terra_judge")
        != _public_terra_judge(checked_plan["evaluation_gate"]["terra_judge"])
    ):
        raise V2ReleaseError("V2 public inventory is bound to a different release plan")
    materialized = dict(value)
    _validate_schema(
        materialized,
        "v2_publication_receipt_schema.json",
        "V2 publication receipt",
    )
    if materialized.get("plan_sha256") != expected_plan_sha256:
        raise V2ReleaseError("V2 publication receipt is bound to a different plan")
    if materialized.get("inventory_sha256") != sha256_bytes(
        canonical_json_bytes(checked_inventory)
    ):
        raise V2ReleaseError("V2 publication receipt is bound to a different inventory")
    files = materialized.get("files")
    if not isinstance(files, list) or materialized.get("file_inventory_sha256") != sha256_bytes(
        canonical_json_bytes(files)
    ):
        raise V2ReleaseError("V2 publication receipt file inventory SHA-256 differs")
    if (
        [row.get("path") for row in files if isinstance(row, Mapping)]
        != list(V2_PUBLIC_ALLOWLIST)
        or materialized.get("payload_tree_sha256")
        != checked_inventory["payload_tree_sha256"]
        or materialized.get("allowlisted_paths") != list(V2_PUBLIC_ALLOWLIST)
    ):
        raise V2ReleaseError("V2 publication receipt inventory or allowlist differs")
    return materialized


def build_v2_publication_receipt(
    plan: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    staged_root: str | Path,
    public_commit: str,
    upload_operation_sha256: str,
) -> dict[str, Any]:
    """Bind a completed external upload operation to local bytes and its exact commit.

    This pure function does not upload anything. The caller must supply the operation
    hash and immutable commit returned by the separately controlled uploader.
    """

    checked_plan = validate_v2_release_plan(plan)
    checked_inventory, files = _validate_staged_v2_bundle(inventory, staged_root)
    if _COMMIT.fullmatch(public_commit) is None:
        raise V2ReleaseError("V2 public commit must be a full lowercase commit")
    if _HASH.fullmatch(upload_operation_sha256) is None:
        raise V2ReleaseError("V2 upload operation SHA-256 is invalid")
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_v2_publication_receipt",
        "status": "uploaded_unverified",
        "repository_id": V2_PUBLIC_REPOSITORY,
        "visibility": "public",
        "requires_token": False,
        "public_commit": public_commit,
        "upload_operation_sha256": upload_operation_sha256,
        "plan_sha256": sha256_bytes(canonical_json_bytes(checked_plan)),
        "inventory_sha256": sha256_bytes(canonical_json_bytes(checked_inventory)),
        "payload_tree_sha256": checked_inventory["payload_tree_sha256"],
        "files": files,
        "file_inventory_sha256": sha256_bytes(canonical_json_bytes(files)),
        "allowlisted_paths": list(V2_PUBLIC_ALLOWLIST),
        "upload_performed": True,
        "anonymous_verification_performed": False,
    }
    return validate_v2_publication_receipt(
        receipt,
        plan=checked_plan,
        inventory=checked_inventory,
    )


def _remote_file_map(rows: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        raise V2ReleaseError("anonymous V2 repository inventory is invalid")
    mapped: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise V2ReleaseError("anonymous V2 repository inventory has an invalid row")
        path = row.get("path")
        if not isinstance(path, str) or path in mapped:
            raise V2ReleaseError("anonymous V2 repository inventory has a duplicate path")
        mapped[path] = row
    if set(mapped) != set(V2_PUBLIC_ALLOWLIST):
        raise V2ReleaseError("anonymous V2 repository inventory differs from the allowlist")
    return mapped


def verify_v2_public_release(
    plan: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    staged_root: str | Path,
    publication_receipt: Mapping[str, Any],
    probe: AnonymousPublicProbe,
) -> dict[str, Any]:
    """Verify the public V2 commit anonymously without re-downloading model files."""

    checked_plan = validate_v2_release_plan(plan)
    checked_inventory, local_files = _validate_staged_v2_bundle(inventory, staged_root)
    receipt = validate_v2_publication_receipt(
        publication_receipt,
        plan=checked_plan,
        inventory=checked_inventory,
    )
    if receipt.get("files") != local_files:
        raise V2ReleaseError("V2 publication receipt differs from staged local bytes")
    repository_id = str(receipt["repository_id"])
    commit = str(receipt["public_commit"])
    repository_info = probe.repository_info(repository_id, commit)
    if (
        not isinstance(repository_info, Mapping)
        or repository_info.get("sha") != commit
        or repository_info.get("private") is not False
        or repository_info.get("gated") is not False
    ):
        raise V2ReleaseError("V2 repository is not anonymous, public, and commit-pinned")
    remote_files = _remote_file_map(probe.list_files(repository_id, commit))
    root = Path(staged_root).expanduser().resolve()
    file_probes: list[dict[str, object]] = []
    range_bytes_received = 0
    for local_record in local_files:
        relative_path = str(local_record["path"])
        remote_record = remote_files[relative_path]
        if remote_record.get("bytes") != local_record["bytes"]:
            raise V2ReleaseError(f"anonymous V2 file size differs: {relative_path}")
        remote_sha256 = remote_record.get("sha256")
        if remote_sha256 is not None and remote_sha256 != local_record["sha256"]:
            raise V2ReleaseError(f"anonymous V2 file SHA-256 differs: {relative_path}")

        head = probe.head(repository_id, commit, relative_path)
        if (
            not isinstance(head, Mapping)
            or head.get("status") != 200
            or head.get("content_length") != local_record["bytes"]
            or head.get("resolved_commit") != commit
        ):
            raise V2ReleaseError(f"anonymous HEAD failed for V2 file: {relative_path}")
        head_sha256 = head.get("sha256")
        if head_sha256 is not None and head_sha256 != local_record["sha256"]:
            raise V2ReleaseError(f"anonymous HEAD hash differs for V2 file: {relative_path}")
        if remote_sha256 is None and head_sha256 is None:
            if relative_path.endswith(".gguf"):
                raise V2ReleaseError(f"anonymous V2 GGUF hash metadata is missing: {relative_path}")
            expected_blob_oid = _git_blob_oid(root / relative_path)
            remote_blob_oids = {
                value
                for value in (
                    remote_record.get("git_blob_oid"),
                    head.get("git_blob_oid"),
                )
                if isinstance(value, str)
            }
            if expected_blob_oid not in remote_blob_oids:
                raise V2ReleaseError(
                    f"anonymous V2 Git blob identity differs: {relative_path}"
                )

        ranged = probe.range_get(
            repository_id,
            commit,
            relative_path,
            range_header="bytes=0-0",
        )
        expected_range = f"bytes 0-0/{local_record['bytes']}"
        payload = ranged.get("payload") if isinstance(ranged, Mapping) else None
        with (root / relative_path).open("rb") as stream:
            expected_first_byte = stream.read(1)
        if (
            not isinstance(ranged, Mapping)
            or ranged.get("status") != 206
            or ranged.get("content_length") != 1
            or ranged.get("content_range") != expected_range
            or ranged.get("resolved_commit") != commit
            or payload != expected_first_byte
        ):
            raise V2ReleaseError(f"anonymous one-byte Range failed for V2 file: {relative_path}")
        range_bytes_received += len(payload)
        file_probes.append(
            {
                "path": relative_path,
                "bytes": local_record["bytes"],
                "sha256": local_record["sha256"],
                "head_status": 200,
                "range_status": 206,
                "range_bytes_received": 1,
            }
        )

    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "scriber_v2_public_release_verification",
        "status": "passed",
        "repository_id": repository_id,
        "public_commit": commit,
        "visibility": "public",
        "requires_token": False,
        "plan_sha256": receipt["plan_sha256"],
        "inventory_sha256": receipt["inventory_sha256"],
        "file_inventory_sha256": receipt["file_inventory_sha256"],
        "upload_operation_sha256": receipt["upload_operation_sha256"],
        "evidence_scope": checked_inventory["evidence_scope"],
        "anonymous_head_count": len(file_probes),
        "anonymous_range_count": len(file_probes),
        "range_bytes_received": range_bytes_received,
        "range_request": "bytes=0-0",
        "authorization_header_used": False,
        "full_model_redownload_performed": False,
        "files": file_probes,
        "catalog_candidate": {
            "repository_id": repository_id,
            "revision": commit,
            "requires_token": False,
            "variants": list(V2_VARIANTS),
        },
        "catalog_transition": {
            "v2_activation_eligible": True,
            "catalog_changed": False,
            "v1_fallback_remains_active": True,
            "v1_retirement_allowed_only_after_catalog_update": True,
        },
    }
    _validate_schema(
        result,
        "v2_public_release_verification_schema.json",
        "V2 public release verification",
    )
    return result


def run_v2_quantization(
    plan: Mapping[str, Any],
    *,
    evaluation_binding: Mapping[str, Any],
    source_checkpoint: str | Path,
    llama_cpp_bundle: str | Path,
    llama_cpp_bundle_lock: str | Path,
    output_root: str | Path,
    python_executable: Path,
    materializer: Any | None = None,
) -> dict[str, Any]:
    """Materialize BF16 and Q8_0 locally; never launch or publish anything."""

    checked_plan = validate_v2_release_plan(plan)
    binding = validate_v2_evaluation_binding(evaluation_binding)
    binding_sha256 = sha256_bytes(canonical_json_bytes(binding))
    gate = checked_plan["evaluation_gate"]
    if not isinstance(gate, Mapping) or gate.get("binding_sha256") != binding_sha256:
        raise V2ReleaseError("V2 quantization evaluation binding bytes differ from the plan")
    source = Path(source_checkpoint).expanduser().resolve()
    bundle = Path(llama_cpp_bundle).expanduser().resolve()
    lock = Path(llama_cpp_bundle_lock).expanduser().resolve()
    if source.is_symlink() or not source.is_dir():
        raise V2ReleaseError("V2 source checkpoint must be a regular directory")
    if bundle.is_symlink() or not bundle.is_dir() or lock.is_symlink() or not lock.is_file():
        raise V2ReleaseError("verified llama.cpp bundle inputs are incomplete")
    output = Path(output_root).expanduser().resolve()
    if output.exists():
        raise V2ReleaseError(f"V2 quantization output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.quantizing-", dir=output.parent)
    ).resolve()
    shutil.rmtree(staging)
    if materializer is None:
        from .quantization import build_gguf_variants

        materializer = build_gguf_variants
    try:
        manifest = materializer(
            source,
            staging,
            source_id=V2_PUBLIC_SOURCE_ID,
            training_fingerprint=V2_RUN_FINGERPRINT,
            bundle_root=bundle,
            bundle_lock=lock,
            python_executable=python_executable,
            quantizations=("Q8_0",),
        )
        if not isinstance(manifest, Mapping):
            raise V2ReleaseError("GGUF materializer returned no manifest object")
        manifest = dict(manifest)
        expected_manifest_binding = {
            "source_id": V2_PUBLIC_SOURCE_ID,
            "source_tree_sha256": V2_SOURCE_TREE_SHA256,
            "training_fingerprint": V2_RUN_FINGERPRINT,
            "variants": ["bf16", "Q8_0"],
            "network_access": "disabled_offline",
        }
        if any(manifest.get(key) != expected for key, expected in expected_manifest_binding.items()):
            raise V2ReleaseError("GGUF manifest differs from the exact V2 BF16/Q8_0 plan")
        policy = manifest.get("protection_policy")
        if not isinstance(policy, Mapping) or policy.get("artifact_sha256") != PROTECTION_POLICY_SHA256:
            raise V2ReleaseError("GGUF manifest changed the V2 protection policy")
        top_level = {path.name for path in staging.iterdir()}
        if top_level != {"bf16", "Q8_0", "gguf-bundle-manifest.json"}:
            raise V2ReleaseError("GGUF materializer produced an unexpected variant or file")
        artifacts = {
            "Q8_0": _quantized_artifact_record(
                staging,
                public_variant="Q8_0",
                internal_variant="Q8_0",
                model_filename="model-Q8_0.gguf",
                bundle_manifest=manifest,
            ),
            "BF16": _quantized_artifact_record(
                staging,
                public_variant="BF16",
                internal_variant="bf16",
                model_filename="model-BF16.gguf",
                bundle_manifest=manifest,
            ),
        }
        manifest_record = _regular_file_record(
            staging / "gguf-bundle-manifest.json",
            relative_path="gguf-bundle-manifest.json",
        )
        toolchain = manifest.get("toolchain")
        if not isinstance(toolchain, Mapping):
            raise V2ReleaseError("GGUF manifest has no verified toolchain binding")
        result: dict[str, Any] = {
            "schema_version": 1,
            "kind": "scriber_v2_q8_bf16_quantization_result",
            "status": "complete",
            "plan_sha256": sha256_bytes(canonical_json_bytes(checked_plan)),
            "evaluation_binding_sha256": binding_sha256,
            "source_model": checked_plan["source_model"],
            "output_uri": V2_RELEASE_OUTPUT_URI,
            "variants": list(V2_VARIANTS),
            "gguf_bundle_manifest": manifest_record,
            "artifact_inventory_sha256": sha256_bytes(canonical_json_bytes(artifacts)),
            "artifacts": artifacts,
            "toolchain": dict(toolchain),
            "network_access": "disabled_offline",
            "publication_performed": False,
        }
        validate_v2_quantization_result(result, plan=checked_plan)
        result_path = staging / "v2-quantization-result.json"
        with result_path.open("xb") as stream:
            stream.write(canonical_json_bytes(result))
        os.replace(staging, output)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
