from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scriber_polishing.variant_artifacts import (
    VARIANT_MANIFEST_FILENAME,
    VariantArtifactError,
    build_variant_artifact_manifest,
    verify_saved_bitsandbytes_config,
    verify_variant_artifact_manifest,
)

_FINGERPRINT = "sha256:" + "a" * 64
_SOURCE_TREE = "sha256:" + "b" * 64
_POLICY_ID = "gemma_lexical_v1"
_POLICY_SHA256 = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
_POLICY_ARTIFACT_SHA256 = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"


def _policy_source() -> Path:
    return Path(__file__).parents[1] / "artifacts" / "protection" / "gemma_lexical_v1.json"


def _transformers_artifact(root: Path) -> Path:
    root.mkdir()
    (root / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["Gemma3ForCausalLM"],
                "model_type": "gemma3_text",
                "scriber_protection_policy_id": _POLICY_ID,
                "scriber_protection_policy_sha256": _POLICY_SHA256,
                "quantization_config": {
                    "quant_method": "bitsandbytes",
                    "load_in_4bit": True,
                    "bnb_4bit_quant_type": "nf4",
                    "bnb_4bit_compute_dtype": "bfloat16",
                    "bnb_4bit_use_double_quant": True,
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "tokenizer.json").write_text('{"version":"1.0"}\n', encoding="utf-8")
    (root / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": "{{ messages | tojson }}"}),
        encoding="utf-8",
    )
    (root / "model.safetensors").write_bytes(b"quantized-weights")
    shutil.copyfile(_policy_source(), root / "scriber-protection-policy.json")
    return root


def _reload() -> dict[str, object]:
    return {
        "status": "passed",
        "process_boundary": "fresh_subprocess",
        "configuration_source": "saved_artifact",
        "loaded_quantization": "int4_nf4",
        "deterministic": True,
        "valid_sst": True,
        "generated_tokens": 17,
        "protection_policy": {
            "filename": "scriber-protection-policy.json",
            "artifact_sha256": _POLICY_ARTIFACT_SHA256,
            "policy_id": _POLICY_ID,
            "policy_sha256": _POLICY_SHA256,
            "tokenizer_identity_sha256": ("sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"),
        },
    }


def test_variant_manifest_binds_the_complete_artifact_and_fixed_gemma_identity(
    tmp_path: Path,
) -> None:
    artifact = _transformers_artifact(tmp_path / "int4_nf4")

    manifest = build_variant_artifact_manifest(
        artifact,
        variant="int4_nf4",
        backend="transformers_bitsandbytes",
        source_id="final-bf16",
        source_tree_sha256=_SOURCE_TREE,
        training_fingerprint=_FINGERPRINT,
        quantization={
            "quant_method": "bitsandbytes",
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_compute_dtype": "bfloat16",
            "bnb_4bit_use_double_quant": True,
        },
        toolchain={
            "kind": "transformers_bitsandbytes",
            "packages": {
                "bitsandbytes": "0.49.2",
                "torch": "2.11.0",
                "transformers": "4.57.6",
            },
        },
        reload_evidence=_reload(),
    )

    assert manifest["variant"] == "int4_nf4"
    assert manifest["architecture"] == {
        "transformers_class": "Gemma3ForCausalLM",
        "model_type": "gemma3_text",
    }
    assert manifest["base_model"] == {
        "id": "google/gemma-3-270m-it",
        "revision": "ac82b4e820549b854eebf28ce6dedaf9fdfa17b3",
    }
    assert manifest["training_fingerprint"] == _FINGERPRINT
    assert manifest["artifact"]["tree_sha256"].startswith("sha256:")
    assert manifest["tokenizer"]["chat_template_sha256"].startswith("sha256:")
    assert manifest["quantization"]["config_sha256"].startswith("sha256:")
    assert manifest["protection_policy"] == _reload()["protection_policy"]
    assert (artifact / VARIANT_MANIFEST_FILENAME).is_file()
    assert verify_variant_artifact_manifest(artifact) == manifest

    (artifact / "model.safetensors").write_bytes(b"tampered")
    with pytest.raises(VariantArtifactError, match="artifact inventory"):
        verify_variant_artifact_manifest(artifact)


def test_variant_manifest_accepts_transformers_chat_template_jinja_layout(
    tmp_path: Path,
) -> None:
    artifact = _transformers_artifact(tmp_path / "int4_nf4")
    (artifact / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (artifact / "chat_template.jinja").write_text(
        "{{ messages | tojson }}",
        encoding="utf-8",
    )

    manifest = build_variant_artifact_manifest(
        artifact,
        variant="int4_nf4",
        backend="transformers_bitsandbytes",
        source_id="final-bf16",
        source_tree_sha256=_SOURCE_TREE,
        training_fingerprint=_FINGERPRINT,
        quantization={
            "quant_method": "bitsandbytes",
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_compute_dtype": "bfloat16",
            "bnb_4bit_use_double_quant": True,
        },
        toolchain={
            "kind": "transformers_bitsandbytes",
            "packages": {
                "bitsandbytes": "0.49.2",
                "torch": "2.11.0",
                "transformers": "4.57.6",
            },
        },
        reload_evidence=_reload(),
    )

    assert manifest["tokenizer"]["chat_template_sha256"].startswith("sha256:")


def test_variant_manifest_rejects_tampered_frozen_lexical_policy(
    tmp_path: Path,
) -> None:
    artifact = _transformers_artifact(tmp_path / "int4_nf4")
    policy_path = artifact / "scriber-protection-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["max_spans"] = 31
    policy_path.write_text(
        json.dumps(policy, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(VariantArtifactError, match="protection policy"):
        build_variant_artifact_manifest(
            artifact,
            variant="int4_nf4",
            backend="transformers_bitsandbytes",
            source_id="final-bf16",
            source_tree_sha256=_SOURCE_TREE,
            training_fingerprint=_FINGERPRINT,
            quantization={
                "quant_method": "bitsandbytes",
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": "bfloat16",
                "bnb_4bit_use_double_quant": True,
            },
            toolchain={
                "kind": "transformers_bitsandbytes",
                "packages": {
                    "bitsandbytes": "0.49.2",
                    "torch": "2.11.0",
                    "transformers": "4.57.6",
                },
            },
            reload_evidence=_reload(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda root: (root / "config.json").write_text(
                '{"architectures":["OtherModel"],"model_type":"gemma3_text"}',
                encoding="utf-8",
            ),
            "Gemma3ForCausalLM",
        ),
        (
            lambda root: (root / "tokenizer_config.json").write_text(
                '{"chat_template":""}',
                encoding="utf-8",
            ),
            "chat template",
        ),
    ],
)
def test_variant_manifest_rejects_unbound_architecture_or_chat_template(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    artifact = _transformers_artifact(tmp_path / "artifact")
    mutation(artifact)

    with pytest.raises(VariantArtifactError, match=message):
        build_variant_artifact_manifest(
            artifact,
            variant="int4_nf4",
            backend="transformers_bitsandbytes",
            source_id="final-bf16",
            source_tree_sha256=_SOURCE_TREE,
            training_fingerprint=_FINGERPRINT,
            quantization={
                "quant_method": "bitsandbytes",
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": "bfloat16",
                "bnb_4bit_use_double_quant": True,
            },
            toolchain={
                "kind": "transformers_bitsandbytes",
                "packages": {
                    "bitsandbytes": "0.49.2",
                    "torch": "2.11.0",
                    "transformers": "4.57.6",
                },
            },
            reload_evidence=_reload(),
        )


def test_variant_manifest_requires_fresh_subprocess_reload_and_saved_config(
    tmp_path: Path,
) -> None:
    artifact = _transformers_artifact(tmp_path / "artifact")
    reload_evidence = _reload()
    reload_evidence["process_boundary"] = "same_process"

    with pytest.raises(VariantArtifactError, match="fresh_subprocess"):
        build_variant_artifact_manifest(
            artifact,
            variant="int4_nf4",
            backend="transformers_bitsandbytes",
            source_id="final-bf16",
            source_tree_sha256=_SOURCE_TREE,
            training_fingerprint=_FINGERPRINT,
            quantization={
                "quant_method": "bitsandbytes",
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": "bfloat16",
                "bnb_4bit_use_double_quant": True,
            },
            toolchain={
                "kind": "transformers_bitsandbytes",
                "packages": {
                    "bitsandbytes": "0.49.2",
                    "torch": "2.11.0",
                    "transformers": "4.57.6",
                },
            },
            reload_evidence=reload_evidence,
        )


def test_saved_bitsandbytes_config_is_the_only_reload_configuration_source(
    tmp_path: Path,
) -> None:
    artifact = _transformers_artifact(tmp_path / "artifact")

    assert verify_saved_bitsandbytes_config(artifact, "int4_nf4") == {
        "quant_method": "bitsandbytes",
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_compute_dtype": "bfloat16",
        "bnb_4bit_use_double_quant": True,
    }

    config = json.loads((artifact / "config.json").read_text(encoding="utf-8"))
    config["quantization_config"]["bnb_4bit_use_double_quant"] = False
    (artifact / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(VariantArtifactError, match="NF4 double-quant"):
        verify_saved_bitsandbytes_config(artifact, "int4_nf4")
