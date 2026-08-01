from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from scriber_polishing.v2_q8_bf16_release import (
    V2_PUBLIC_SOURCE_ID,
    V2ReleaseError,
    build_v2_publication_receipt,
    build_v2_release_plan,
    canonical_json_bytes,
    run_v2_quantization,
    sha256_bytes,
    stage_v2_public_bundle,
    validate_v2_release_plan,
    verify_v2_public_release,
)

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:4e9379d8c26cb81d7261b57ab2e0bf5c8059850c07717debc47c0ce99072a8a5"
_SOURCE_URI = (
    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt13/"
    "scriber-v2-hardcase-replay-v1/training/final-7b23f23e70f60df9"
)
_SOURCE_TREE = "sha256:4233c0084cacdd3ba88207cece0ce20d1e1f56cfbfa64e71182a2deb2934c743"
_SOURCE_WEIGHT = "sha256:fd86d26f70e128fd1e8d4f11e69d4778ae930e628e374aa64595aca5bd0b9bb3"
_RUN_FINGERPRINT = "sha256:7b23f23e70f60df9d58961808e529ff7738032de75a7a4a62c12529bc734a45c"
_POLICY = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"


def _accepted_evaluation_binding() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "scriber_v2_release_evaluation_binding",
        "status": "accepted",
        "evaluation_result_uri": (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt14/"
            "scriber-v2-hardcase-replay-final-evaluation-v1/final"
        ),
        "evaluation_result_sha256": _HASH_A,
        "evaluation_inventory_sha256": _HASH_B,
        "training_steps_executed": 0,
        "evidence_scope": {
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
        },
        "terra_judge": {
            "evidence_class": "compact_smoke_test",
            "publication_grade": False,
            "aggregate": {
                "uri": (
                    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt14/"
                    "scriber-v2-hardcase-replay-final-evaluation-v1/terra/aggregate.json"
                ),
                "sha256": "sha256:" + "1" * 64,
            },
            "verdicts": {
                "uri": (
                    "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt14/"
                    "scriber-v2-hardcase-replay-final-evaluation-v1/terra/verdicts.jsonl"
                ),
                "sha256": "sha256:" + "2" * 64,
                "count": 100,
            },
            "judge": {
                "model_family": "gpt-5.6-terra",
                "session_id": "session_v2terra100",
                "session_sha256": "sha256:" + "4" * 64,
                "prompt_version": "v1",
                "prompt_sha256": "sha256:" + "3" * 64,
            },
            "selection": {
                "source_count": 300,
                "selected_count": 100,
                "quotas": {
                    "ai_gold": 34,
                    "critical_regression": 33,
                    "regular_test": 33,
                },
            },
            "gate": {
                "kind": "compact_promotion",
                "decision": "passed",
                "v2_wins_gte_v1_wins": True,
                "v2_critical_lte_v1_critical": True,
            },
        },
        "compact_comparison_gate": {
            "kind": "scriber_v2_compact_comparison_gate",
            "source_report_suite": "combined",
            "record_count": 300,
            "coverage": {"v1_reused": 300, "v2": 300, "exact": True},
            "normalized_exact_match": {
                "v1_reused": 0.80,
                "v2": 0.82,
                "v2_gte_v1": True,
            },
            "deterministic_critical_error_count": {
                "v1_reused": 3,
                "v2": 2,
                "v2_lte_v1": True,
            },
            "decision": "passed",
            "publication_grade": False,
            "requires_terra": True,
        },
        "model": {
            "remote_uri": _SOURCE_URI,
            "tree_sha256": _SOURCE_TREE,
            "run_fingerprint": _RUN_FINGERPRINT,
            "weight": {
                "path": "model.safetensors",
                "bytes": 536_223_056,
                "sha256": _SOURCE_WEIGHT,
            },
            "protection_policy_sha256": _POLICY,
            "postflight_binding_sha256": _HASH_C,
        },
        "reports": {
            suite: {
                "evaluation_sha256": "sha256:" + character * 64,
                "evaluation_exit": exit_code,
                "integrity_verified": True,
                "quality_gate_claimed": False,
            }
            for suite, character, exit_code in (
                ("ai_gold", "d", 0),
                ("critical_regression", "e", 2),
                ("regular_test", "f", 0),
            )
        },
    }


def _binding_hash(value: dict[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _script(name: str):
    path = Path(__file__).parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file_record(path: Path, relative: str) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": relative,
        "bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }


def _quantization_fixture(
    tmp_path: Path,
    plan: dict[str, object],
    binding: dict[str, object],
) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "quantization"
    artifacts: dict[str, object] = {}
    for public_variant, internal_variant, model_name, character in (
        ("Q8_0", "Q8_0", "model-Q8_0.gguf", "6"),
        ("BF16", "bf16", "model-BF16.gguf", "7"),
    ):
        variant_root = root / internal_variant
        variant_root.mkdir(parents=True)
        model = variant_root / model_name
        model.write_bytes(b"GGUF" + public_variant.encode("ascii"))
        policy = variant_root / "scriber-protection-policy.json"
        policy.write_bytes(
            (Path(__file__).parents[1] / "configs" / "training-policy-gemma-lexical-v1.json").read_bytes()
        )
        manifest = variant_root / "variant-artifact-manifest.json"
        manifest.write_text(json.dumps({"variant": public_variant}), encoding="utf-8")
        artifacts[public_variant] = {
            "variant": public_variant,
            "artifact_tree_sha256": "sha256:" + character * 64,
            "chat_template_sha256": "sha256:" + "9" * 64,
            "model": _file_record(model, f"{internal_variant}/{model_name}"),
            "protection_policy": _file_record(
                policy,
                f"{internal_variant}/scriber-protection-policy.json",
            ),
            "variant_manifest": _file_record(
                manifest,
                f"{internal_variant}/variant-artifact-manifest.json",
            ),
        }
    gguf_manifest = root / "gguf-bundle-manifest.json"
    gguf_manifest.write_text("{}\n", encoding="utf-8")
    result = {
        "schema_version": 1,
        "kind": "scriber_v2_q8_bf16_quantization_result",
        "status": "complete",
        "plan_sha256": sha256_bytes(canonical_json_bytes(plan)),
        "evaluation_binding_sha256": _binding_hash(binding),
        "source_model": plan["source_model"],
        "output_uri": plan["launch"]["output_uri"],
        "variants": ["Q8_0", "BF16"],
        "gguf_bundle_manifest": _file_record(
            gguf_manifest,
            "gguf-bundle-manifest.json",
        ),
        "artifact_inventory_sha256": sha256_bytes(canonical_json_bytes(artifacts)),
        "artifacts": artifacts,
        "toolchain": {
            "bundle_lock_sha256": "sha256:" + "3" * 64,
            "llama_cpp_commit": "4" * 40,
            "source_tree_sha256": "sha256:" + "5" * 64,
            "build_identity_sha256": "sha256:" + "8" * 64,
        },
        "network_access": "disabled_offline",
        "publication_performed": False,
    }
    (root / "v2-quantization-result.json").write_bytes(canonical_json_bytes(result))
    return root, result


def test_release_plan_reserves_a_new_closed_v2_lane_and_keeps_v1_as_fallback() -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )

    assert plan["variants"] == ["Q8_0", "BF16"]
    assert plan["conversion"]["execution_order"] == ["BF16", "Q8_0"]
    assert plan["launch"] == {
        "attempt": "attempt15",
        "job_name": "scriber-v2-q8-bf16-release-v1",
        "claim_repository": "Buttermilk03/scriber-polishing-launch-claims",
        "claim_path": (
            "claims/v2-q8-bf16-release/"
            "attempt15-scriber-v2-q8-bf16-release-v1.json"
        ),
        "output_uri": (
            "hf://buckets/Buttermilk03/scriber-polishing-private-runs/attempt15/"
            "scriber-v2-q8-bf16-release-v1"
        ),
        "external_job_started": False,
        "auto_retry_allowed": False,
    }
    assert plan["publication"]["repository_id"] == (
        "Buttermilk03/scriber-gemma3-270m-polishing-de-v2"
    )
    assert plan["publication"]["requires_token"] is False
    assert plan["evaluation_gate"]["evidence_scope"] == {
        "kind": "compact_smoke_test",
        "publication_grade": False,
        "user_authorized_promotion": True,
        "automatic_case_count": 300,
        "automatic_cases_per_suite": {
            "ai_gold": 100,
            "critical_regression": 100,
            "regular_test": 100,
        },
        "blind_judges": {"terra_pair_count": 100, "sol_pair_count": 0},
    }
    assert plan["publication"]["evidence_label"] == "user_authorized_compact_smoke_test"
    assert plan["publication"]["full_release_study_claim_allowed"] is False
    assert plan["evaluation_gate"]["terra_judge"] == binding["terra_judge"]
    assert plan["evaluation_gate"]["compact_comparison_gate"] == binding[
        "compact_comparison_gate"
    ]
    assert plan["fallback"] == {
        "repository_id": "Buttermilk03/scriber-gemma3-270m-polishing-de-v1",
        "revision": "65bae06a655fc209058d60064c65fe3e891e4a43",
        "remains_active_until": "v2_public_release_verified",
        "automatic_switch_allowed": False,
    }


def test_release_plan_rejects_any_unaccepted_or_cross_model_evaluation_binding() -> None:
    rejected = _accepted_evaluation_binding()
    rejected["status"] = "complete"
    with pytest.raises(V2ReleaseError, match="accepted"):
        build_v2_release_plan(
            rejected,
            evaluation_binding_sha256=_binding_hash(rejected),
            source_git_head="2" * 40,
        )

    substituted = _accepted_evaluation_binding()
    substituted["model"]["tree_sha256"] = _HASH_A
    with pytest.raises(V2ReleaseError, match="V2 model"):
        build_v2_release_plan(
            substituted,
            evaluation_binding_sha256=_binding_hash(substituted),
            source_git_head="2" * 40,
        )


def test_release_plan_validation_rejects_a_third_variant_or_mutable_publication() -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )

    third_variant = deepcopy(plan)
    third_variant["variants"].append("Q4_K_M")
    with pytest.raises(V2ReleaseError, match="schema|variants"):
        validate_v2_release_plan(third_variant)

    tokenized = deepcopy(plan)
    tokenized["publication"]["requires_token"] = True
    with pytest.raises(V2ReleaseError, match="schema|token"):
        validate_v2_release_plan(tokenized)


def test_release_plan_refuses_to_mislabel_compact_evidence_as_publication_grade() -> None:
    mislabeled = _accepted_evaluation_binding()
    mislabeled["evidence_scope"]["publication_grade"] = True

    with pytest.raises(V2ReleaseError, match="compact|publication"):
        build_v2_release_plan(
            mislabeled,
            evaluation_binding_sha256=_binding_hash(mislabeled),
            source_git_head="2" * 40,
        )


def test_release_plan_requires_a_passed_exact_terra100_handoff() -> None:
    missing = _accepted_evaluation_binding()
    del missing["terra_judge"]
    with pytest.raises(V2ReleaseError, match="Terra|schema"):
        build_v2_release_plan(
            missing,
            evaluation_binding_sha256=_binding_hash(missing),
            source_git_head="2" * 40,
        )

    failed = _accepted_evaluation_binding()
    failed["terra_judge"]["gate"]["decision"] = "failed"
    with pytest.raises(V2ReleaseError, match="Terra|schema"):
        build_v2_release_plan(
            failed,
            evaluation_binding_sha256=_binding_hash(failed),
            source_git_head="2" * 40,
        )


def test_release_plan_never_turns_automatic_integrity_into_a_quality_gate() -> None:
    invented = _accepted_evaluation_binding()
    invented["reports"]["critical_regression"]["quality_gate_claimed"] = True

    with pytest.raises(V2ReleaseError, match="report|quality|schema"):
        build_v2_release_plan(
            invented,
            evaluation_binding_sha256=_binding_hash(invented),
            source_git_head="2" * 40,
        )


def test_release_plan_requires_the_passed_exact_300_case_comparison_gate() -> None:
    failed = _accepted_evaluation_binding()
    failed["compact_comparison_gate"]["decision"] = "failed"
    failed["compact_comparison_gate"]["normalized_exact_match"]["v2_gte_v1"] = False

    with pytest.raises(V2ReleaseError, match="comparison|300|schema"):
        build_v2_release_plan(
            failed,
            evaluation_binding_sha256=_binding_hash(failed),
            source_git_head="2" * 40,
        )


def test_release_plan_rejects_an_evaluation_binding_hash_from_other_bytes() -> None:
    binding = _accepted_evaluation_binding()

    with pytest.raises(V2ReleaseError, match="bytes|SHA-256"):
        build_v2_release_plan(
            binding,
            evaluation_binding_sha256="sha256:" + "1" * 64,
            source_git_head="2" * 40,
        )


def test_prepare_cli_writes_one_immutable_inert_plan(tmp_path: Path) -> None:
    binding = _accepted_evaluation_binding()
    binding_path = tmp_path / "accepted-v2-evaluation-binding.json"
    binding_path.write_bytes(canonical_json_bytes(binding))
    output = tmp_path / "v2-q8-bf16-release-plan.json"
    script = _script("prepare_v2_q8_bf16_release_plan.py")

    assert script.main(
        [
            "--evaluation-binding",
            str(binding_path),
            "--source-git-head",
            "2" * 40,
            "--output",
            str(output),
        ]
    ) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["launch"]["external_job_started"] is False
    assert plan["publication"]["repository_id"].endswith("-v2")

    assert script.main(
        [
            "--evaluation-binding",
            str(binding_path),
            "--source-git-head",
            "2" * 40,
            "--output",
            str(output),
        ]
    ) == 0


def test_quantization_runner_builds_only_two_bound_artifacts_in_attempt15(
    tmp_path: Path,
) -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )
    observed: dict[str, object] = {}

    def materialize(checkpoint: Path, destination: Path, **kwargs):
        observed.update(kwargs)
        assert checkpoint == (tmp_path / "source-model").resolve()
        for variant, filename in (
            ("bf16", "model-BF16.gguf"),
            ("Q8_0", "model-Q8_0.gguf"),
        ):
            root = destination / variant
            root.mkdir(parents=True)
            (root / filename).write_bytes(b"GGUF" + variant.encode("ascii"))
            (root / "scriber-protection-policy.json").write_bytes(
                (Path(__file__).parents[1] / "configs" / "training-policy-gemma-lexical-v1.json").read_bytes()
            )
            (root / "variant-artifact-manifest.json").write_text(
                json.dumps({"variant": variant, "source_tree_sha256": _SOURCE_TREE}),
                encoding="utf-8",
            )
        manifest = {
            "schema_version": 4,
            "source_id": V2_PUBLIC_SOURCE_ID,
            "source_tree_sha256": _SOURCE_TREE,
            "training_fingerprint": _RUN_FINGERPRINT,
            "variants": ["bf16", "Q8_0"],
            "variant_manifests": {
                variant: {
                    "path": f"{variant}/variant-artifact-manifest.json",
                    "artifact_tree_sha256": "sha256:" + character * 64,
                    "reload_self_test": {
                        "status": "passed",
                        "chat_template_sha256": "sha256:" + "9" * 64,
                    },
                }
                for variant, character in (("bf16", "6"), ("Q8_0", "7"))
            },
            "toolchain": {
                "bundle_lock_sha256": "sha256:" + "3" * 64,
                "llama_cpp_commit": "4" * 40,
                "source_tree_sha256": "sha256:" + "5" * 64,
                "build_identity_sha256": "sha256:" + "8" * 64,
            },
            "protection_policy": {"artifact_sha256": _POLICY},
            "network_access": "disabled_offline",
        }
        (destination / "gguf-bundle-manifest.json").write_bytes(
            canonical_json_bytes(manifest)
        )
        return manifest

    source = tmp_path / "source-model"
    source.mkdir()
    bundle = tmp_path / "verified-llama-cpp"
    bundle.mkdir()
    lock = bundle / "llama-cpp-bundle-lock.json"
    lock.write_text("{}", encoding="utf-8")

    result = run_v2_quantization(
        plan,
        evaluation_binding=binding,
        source_checkpoint=source,
        llama_cpp_bundle=bundle,
        llama_cpp_bundle_lock=lock,
        output_root=tmp_path / "attempt15-output",
        python_executable=Path("python.exe"),
        materializer=materialize,
    )

    assert observed["quantizations"] == ("Q8_0",)
    assert observed["source_id"] == V2_PUBLIC_SOURCE_ID
    assert result["variants"] == ["Q8_0", "BF16"]
    assert set(result["artifacts"]) == {"Q8_0", "BF16"}
    assert result["publication_performed"] is False
    assert not (tmp_path / "attempt15-output" / "Q4_K_M").exists()
    assert (tmp_path / "attempt15-output" / "v2-quantization-result.json").is_file()


def test_public_bundle_stages_only_the_closed_anonymous_v2_allowlist(
    tmp_path: Path,
) -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )
    quantization_root, result = _quantization_fixture(tmp_path, plan, binding)
    destination = tmp_path / "public-v2"

    inventory = stage_v2_public_bundle(
        plan,
        quantization_result=result,
        quantization_root=quantization_root,
        gemma_license=Path(__file__).parents[1] / "contracts" / "GEMMA_LICENSE.md",
        output_root=destination,
    )

    observed = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert observed == set(plan["publication"]["allowlisted_paths"])
    assert inventory["repository_id"] == "Buttermilk03/scriber-gemma3-270m-polishing-de-v2"
    assert inventory["requires_token"] is False
    assert inventory["upload_performed"] is False
    assert inventory["evidence_scope"]["publication_grade"] is False
    assert inventory["terra_judge"]["aggregate"] == {
        "sha256": binding["terra_judge"]["aggregate"]["sha256"]
    }
    assert inventory["terra_judge"]["verdicts"] == {
        "sha256": binding["terra_judge"]["verdicts"]["sha256"],
        "count": 100,
    }
    assert "remote_uri" not in inventory["source_model"]
    readme = (destination / "README.md").read_text(encoding="utf-8")
    assert "compact smoke test" in readme.lower()
    assert "not a full release study" in readme.lower()
    for path in destination.rglob("*"):
        if path.is_file():
            payload = path.read_bytes()
            assert b"hf://buckets/" not in payload
            assert b"scriber-polishing-private-runs" not in payload


def test_public_bundle_refuses_private_provenance_in_a_shipped_artifact(
    tmp_path: Path,
) -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )
    quantization_root, result = _quantization_fixture(tmp_path, plan, binding)
    manifest = quantization_root / "Q8_0" / "variant-artifact-manifest.json"
    manifest.write_bytes(b'{"source":"hf://buckets/private"}\n')
    result["artifacts"]["Q8_0"]["variant_manifest"] = _file_record(
        manifest,
        "Q8_0/variant-artifact-manifest.json",
    )
    result["artifact_inventory_sha256"] = sha256_bytes(
        canonical_json_bytes(result["artifacts"])
    )
    (quantization_root / "v2-quantization-result.json").write_bytes(
        canonical_json_bytes(result)
    )

    with pytest.raises(V2ReleaseError, match="private provenance"):
        stage_v2_public_bundle(
            plan,
            quantization_result=result,
            quantization_root=quantization_root,
            gemma_license=Path(__file__).parents[1] / "contracts" / "GEMMA_LICENSE.md",
            output_root=tmp_path / "public-v2",
        )


class _AnonymousProbe:
    def __init__(
        self,
        root: Path,
        commit: str,
        *,
        extra_path: str | None = None,
        git_oid_metadata: bool = False,
        tampered_oid_path: str | None = None,
    ) -> None:
        self.root = root
        self.commit = commit
        self.extra_path = extra_path
        self.git_oid_metadata = git_oid_metadata
        self.tampered_oid_path = tampered_oid_path
        self.calls: list[tuple[str, str, str, str | None]] = []

    @staticmethod
    def _git_blob_oid(payload: bytes) -> str:
        header = f"blob {len(payload)}\0".encode("ascii")
        return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()

    def _metadata(self, path: str) -> dict[str, object]:
        payload = (self.root / path).read_bytes()
        record = _file_record(self.root / path, path)
        if not self.git_oid_metadata or path.endswith(".gguf"):
            return record
        oid = self._git_blob_oid(payload)
        if path == self.tampered_oid_path:
            oid = "0" * 40
        return {**record, "sha256": None, "git_blob_oid": oid}

    def repository_info(self, repository_id: str, revision: str) -> dict[str, object]:
        self.calls.append(("repository_info", repository_id, revision, None))
        return {
            "sha": self.commit,
            "private": False,
            "gated": False,
        }

    def list_files(self, repository_id: str, revision: str) -> list[dict[str, object]]:
        self.calls.append(("list_files", repository_id, revision, None))
        records = [
            self._metadata(path.relative_to(self.root).as_posix())
            for path in self.root.rglob("*")
            if path.is_file()
        ]
        if self.extra_path is not None:
            records.append(
                {
                    "path": self.extra_path,
                    "bytes": 1,
                    "sha256": "sha256:" + "0" * 64,
                }
            )
        return records

    def head(self, repository_id: str, revision: str, path: str) -> dict[str, object]:
        self.calls.append(("head", repository_id, revision, path))
        record = self._metadata(path)
        return {
            "status": 200,
            "content_length": record["bytes"],
            "resolved_commit": self.commit,
            "sha256": record["sha256"],
            "git_blob_oid": record.get("git_blob_oid"),
        }

    def range_get(
        self,
        repository_id: str,
        revision: str,
        path: str,
        *,
        range_header: str,
    ) -> dict[str, object]:
        self.calls.append(("range_get", repository_id, revision, path))
        payload = (self.root / path).read_bytes()
        assert range_header == "bytes=0-0"
        return {
            "status": 206,
            "content_length": 1,
            "content_range": f"bytes 0-0/{len(payload)}",
            "resolved_commit": self.commit,
            "payload": payload[:1],
        }


def _staged_release_fixture(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, dict[str, object]]:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )
    quantization_root, result = _quantization_fixture(tmp_path, plan, binding)
    destination = tmp_path / "public-v2"
    inventory = stage_v2_public_bundle(
        plan,
        quantization_result=result,
        quantization_root=quantization_root,
        gemma_license=Path(__file__).parents[1] / "contracts" / "GEMMA_LICENSE.md",
        output_root=destination,
    )
    return plan, destination, inventory


def test_anonymous_verifier_uses_only_head_and_one_byte_ranges_at_exact_commit(
    tmp_path: Path,
) -> None:
    plan, staged_root, inventory = _staged_release_fixture(tmp_path)
    commit = "3" * 40
    receipt = build_v2_publication_receipt(
        plan,
        inventory=inventory,
        staged_root=staged_root,
        public_commit=commit,
        upload_operation_sha256="sha256:" + "4" * 64,
    )
    probe = _AnonymousProbe(staged_root, commit)

    verification = verify_v2_public_release(
        plan,
        inventory=inventory,
        staged_root=staged_root,
        publication_receipt=receipt,
        probe=probe,
    )

    assert verification["status"] == "passed"
    assert verification["public_commit"] == commit
    assert verification["requires_token"] is False
    assert verification["full_model_redownload_performed"] is False
    assert verification["anonymous_head_count"] == 11
    assert verification["anonymous_range_count"] == 11
    assert verification["range_bytes_received"] == 11
    assert verification["catalog_transition"] == {
        "v2_activation_eligible": True,
        "catalog_changed": False,
        "v1_fallback_remains_active": True,
        "v1_retirement_allowed_only_after_catalog_update": True,
    }
    assert {call[0] for call in probe.calls} == {
        "repository_info",
        "list_files",
        "head",
        "range_get",
    }
    assert all(call[1] == plan["publication"]["repository_id"] for call in probe.calls)
    assert all(call[2] == commit for call in probe.calls)


def test_anonymous_verifier_rejects_extra_remote_files_before_any_byte_probe(
    tmp_path: Path,
) -> None:
    plan, staged_root, inventory = _staged_release_fixture(tmp_path)
    commit = "3" * 40
    receipt = build_v2_publication_receipt(
        plan,
        inventory=inventory,
        staged_root=staged_root,
        public_commit=commit,
        upload_operation_sha256="sha256:" + "4" * 64,
    )
    probe = _AnonymousProbe(staged_root, commit, extra_path="unexpected.bin")

    with pytest.raises(V2ReleaseError, match="allowlist|inventory"):
        verify_v2_public_release(
            plan,
            inventory=inventory,
            staged_root=staged_root,
            publication_receipt=receipt,
            probe=probe,
        )

    assert [call[0] for call in probe.calls] == ["repository_info", "list_files"]


def test_anonymous_verifier_requires_exact_git_blob_identity_for_small_files(
    tmp_path: Path,
) -> None:
    plan, staged_root, inventory = _staged_release_fixture(tmp_path)
    commit = "3" * 40
    receipt = build_v2_publication_receipt(
        plan,
        inventory=inventory,
        staged_root=staged_root,
        public_commit=commit,
        upload_operation_sha256="sha256:" + "4" * 64,
    )
    probe = _AnonymousProbe(
        staged_root,
        commit,
        git_oid_metadata=True,
        tampered_oid_path="v2-public-release-inventory.json",
    )

    with pytest.raises(V2ReleaseError, match="hash|identity|blob"):
        verify_v2_public_release(
            plan,
            inventory=inventory,
            staged_root=staged_root,
            publication_receipt=receipt,
            probe=probe,
        )


def test_publication_receipt_rejects_an_inventory_from_another_plan(
    tmp_path: Path,
) -> None:
    plan, staged_root, inventory = _staged_release_fixture(tmp_path)
    inventory["plan_sha256"] = "sha256:" + "0" * 64
    (staged_root / "v2-public-release-inventory.json").write_bytes(
        canonical_json_bytes(inventory)
    )

    with pytest.raises(V2ReleaseError, match="plan"):
        build_v2_publication_receipt(
            plan,
            inventory=inventory,
            staged_root=staged_root,
            public_commit="3" * 40,
            upload_operation_sha256="sha256:" + "4" * 64,
        )


def test_stage_receipt_and_anonymous_verify_clis_are_local_and_immutable(
    tmp_path: Path,
) -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    quantization_root, result = _quantization_fixture(tmp_path, plan, binding)
    result_path = quantization_root / "v2-quantization-result.json"
    public_root = tmp_path / "public-v2"

    stage = _script("stage_v2_public_release.py")
    assert stage.main(
        [
            "--plan",
            str(plan_path),
            "--quantization-result",
            str(result_path),
            "--quantization-root",
            str(quantization_root),
            "--gemma-license",
            str(Path(__file__).parents[1] / "contracts" / "GEMMA_LICENSE.md"),
            "--output-root",
            str(public_root),
        ]
    ) == 0
    inventory_path = public_root / "v2-public-release-inventory.json"
    inventory = json.loads(inventory_path.read_bytes())
    assert inventory["upload_performed"] is False

    commit = "3" * 40
    receipt_path = tmp_path / "publication-receipt.json"
    receipt_cli = _script("prepare_v2_publication_receipt.py")
    receipt_argv = [
        "--plan",
        str(plan_path),
        "--inventory",
        str(inventory_path),
        "--staged-root",
        str(public_root),
        "--public-commit",
        commit,
        "--upload-operation-sha256",
        "sha256:" + "4" * 64,
        "--output",
        str(receipt_path),
    ]
    assert receipt_cli.main(receipt_argv) == 0
    assert receipt_cli.main(receipt_argv) == 0

    verification_path = tmp_path / "public-verification.json"
    verifier = _script("verify_v2_public_release.py")
    verify_argv = [
        "--plan",
        str(plan_path),
        "--inventory",
        str(inventory_path),
        "--staged-root",
        str(public_root),
        "--publication-receipt",
        str(receipt_path),
        "--output",
        str(verification_path),
    ]
    assert verifier.main(verify_argv, probe=_AnonymousProbe(public_root, commit)) == 0
    original = verification_path.read_bytes()
    assert verifier.main(verify_argv, probe=_AnonymousProbe(public_root, commit)) == 0
    assert verification_path.read_bytes() == original
    assert json.loads(original)["full_model_redownload_performed"] is False


def test_quantization_cli_passes_only_local_inputs_to_attempt15_runner(
    tmp_path: Path,
) -> None:
    binding = _accepted_evaluation_binding()
    plan = build_v2_release_plan(
        binding,
        evaluation_binding_sha256=_binding_hash(binding),
        source_git_head="2" * 40,
    )
    plan_path = tmp_path / "plan.json"
    binding_path = tmp_path / "binding.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    binding_path.write_bytes(canonical_json_bytes(binding))
    source = tmp_path / "source"
    bundle = tmp_path / "bundle"
    source.mkdir()
    bundle.mkdir()
    lock = bundle / "lock.json"
    lock.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "attempt15"
    observed: dict[str, object] = {}
    script = _script("run_v2_q8_bf16_quantization.py")

    def fake_runner(passed_plan, **kwargs):
        observed["plan"] = passed_plan
        observed.update(kwargs)
        return {
            "status": "complete",
            "variants": ["Q8_0", "BF16"],
            "publication_performed": False,
        }

    script.run_v2_quantization = fake_runner
    assert script.main(
        [
            "--plan",
            str(plan_path),
            "--evaluation-binding",
            str(binding_path),
            "--source-checkpoint",
            str(source),
            "--llama-cpp-bundle",
            str(bundle),
            "--llama-cpp-bundle-lock",
            str(lock),
            "--output-root",
            str(output),
            "--python-executable",
            "python.exe",
        ]
    ) == 0
    assert observed["plan"] == plan
    assert observed["evaluation_binding"] == binding
    assert observed["output_root"] == output
    assert "job" not in observed
    assert "token" not in observed
