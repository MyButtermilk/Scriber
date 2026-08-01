"""Exact, fail-closed binding for the frozen Scriber lexical protection policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from .protected_spans import (
    LEXICAL_KEEP_POLICY_ID,
    PROTECTION_POLICY_FILENAME,
    ProtectionPolicyError,
    validate_protection_policy_evidence,
)

FROZEN_LEXICAL_POLICY_ID: Final = LEXICAL_KEEP_POLICY_ID
FROZEN_LEXICAL_POLICY_SHA256: Final = "sha256:9f10947282aad273fbc0af8f08f0c2bd9444eb6a8d10a54d9274fa0eebd133ce"
FROZEN_LEXICAL_POLICY_ARTIFACT_SHA256: Final = "sha256:8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d"
FROZEN_LEXICAL_TOKENIZER_IDENTITY_SHA256: Final = (
    "sha256:f2fd01c3ec43ff4adb6f920a5466cbdba96801901b4fa778a029af561f499f20"
)


class PolicyArtifactError(ValueError):
    """The saved model does not contain the exact frozen lexical policy."""


def frozen_lexical_policy_requirement() -> dict[str, str]:
    """Return the immutable identity expected throughout release processing."""

    return {
        "filename": PROTECTION_POLICY_FILENAME,
        "artifact_sha256": FROZEN_LEXICAL_POLICY_ARTIFACT_SHA256,
        "policy_id": FROZEN_LEXICAL_POLICY_ID,
        "policy_sha256": FROZEN_LEXICAL_POLICY_SHA256,
    }


def frozen_lexical_policy_binding() -> dict[str, str]:
    """Return the complete release identity, including the tokenizer binding."""

    return {
        **frozen_lexical_policy_requirement(),
        "tokenizer_identity_sha256": FROZEN_LEXICAL_TOKENIZER_IDENTITY_SHA256,
    }


def validate_frozen_lexical_policy_requirement(
    value: Mapping[str, object],
) -> dict[str, str]:
    """Reject a configuration that names any policy other than the frozen one."""

    expected = frozen_lexical_policy_requirement()
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise PolicyArtifactError("configured protection policy differs from the frozen lexical policy")
    return expected


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _read_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise PolicyArtifactError(f"{label} must be a regular file: {path}")
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PolicyArtifactError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise PolicyArtifactError(f"{label} must contain a JSON object")
    return value, payload


def verify_frozen_lexical_policy_artifact(
    model_root: str | Path,
    *,
    tokenizer: Any | None = None,
    expected: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Verify exact policy bytes, self-hash, tokenizer identity, and saved config."""

    root = Path(model_root).resolve()
    requirement = validate_frozen_lexical_policy_requirement(
        expected if expected is not None else frozen_lexical_policy_requirement()
    )
    raw_policy, payload = _read_json_object(
        root / PROTECTION_POLICY_FILENAME,
        "protection policy artifact",
    )
    try:
        policy = validate_protection_policy_evidence(raw_policy, tokenizer=tokenizer)
    except ProtectionPolicyError as error:
        raise PolicyArtifactError(f"protection policy artifact validation failed: {error}") from error
    if payload != _canonical_json(policy):
        raise PolicyArtifactError("protection policy artifact is not canonical immutable JSON")
    artifact_sha256 = "sha256:" + hashlib.sha256(payload).hexdigest()
    if (
        policy.get("policy_id") != requirement["policy_id"]
        or policy.get("policy_sha256") != requirement["policy_sha256"]
        or artifact_sha256 != requirement["artifact_sha256"]
    ):
        raise PolicyArtifactError("protection policy artifact differs from the frozen lexical policy")
    tokenizer_binding = policy.get("tokenizer")
    tokenizer_identity_sha256 = (
        tokenizer_binding.get("identity_sha256") if isinstance(tokenizer_binding, Mapping) else None
    )
    if not isinstance(tokenizer_identity_sha256, str):
        raise PolicyArtifactError("protection policy artifact has no tokenizer identity binding")
    if tokenizer_identity_sha256 != FROZEN_LEXICAL_TOKENIZER_IDENTITY_SHA256:
        raise PolicyArtifactError("protection policy tokenizer differs from the frozen lexical policy")
    model_config, _ = _read_json_object(root / "config.json", "model config")
    if (
        model_config.get("scriber_protection_policy_id") != requirement["policy_id"]
        or model_config.get("scriber_protection_policy_sha256") != requirement["policy_sha256"]
    ):
        raise PolicyArtifactError("model config does not bind the frozen lexical protection policy")
    return {
        **requirement,
        "tokenizer_identity_sha256": tokenizer_identity_sha256,
    }
