"""Fail-closed loading and artifact binding for the final dataset policy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).parents[2]
DEFAULT_DATA_GENERATION_CONFIG = PROJECT_ROOT / "configs" / "data_generation.yaml"


@dataclass(frozen=True, slots=True)
class DataGenerationPolicy:
    """Repository-owned size and split constraints used by the final builder."""

    config_path: Path
    config_relative_path: str
    config_bytes: int
    config_sha256: str
    minimum_canonical_documents: int
    minimum_train_pairs: int
    target_train_pairs: int
    minimum_validation_pairs: int
    minimum_test_pairs: int
    minimum_challenge_pairs: int
    split_group: str

    @property
    def source_binding(self) -> dict[str, Any]:
        """Return the immutable identity of the exact bytes parsed for this policy."""

        return {
            "path": self.config_relative_path,
            "bytes": self.config_bytes,
            "sha256": self.config_sha256,
        }

    def resolve_target_train(self, override: int | None) -> int:
        """Allow a CLI value only when it confirms, rather than changes, policy."""

        if override is None:
            return self.target_train_pairs
        if (
            isinstance(override, bool)
            or not isinstance(override, int)
            or override <= 0
        ):
            raise ValueError("--target-train must be a positive integer")
        if override != self.target_train_pairs:
            raise ValueError(
                "--target-train must equal configured target_train_pairs "
                f"({self.target_train_pairs})"
            )
        return override


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"data generation policy {field} must be a positive integer")
    return value


def load_data_generation_policy(
    path: str | Path = DEFAULT_DATA_GENERATION_CONFIG,
) -> DataGenerationPolicy:
    """Load the one repository policy that controls final dataset sizes."""

    config_path = Path(path)
    config_snapshot = read_regular_file_snapshot(config_path, allowed_root=PROJECT_ROOT)
    try:
        raw = yaml.safe_load(config_snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"cannot load data generation policy: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("data generation policy must contain an object")
    if raw.get("schema_version") != 1:
        raise ValueError("data generation policy schema_version must be 1")
    if raw.get("split_before_variants") is not True:
        raise ValueError("data generation policy must split before variants")
    split_group = raw.get("split_group")
    if split_group != "parent_canonical_document_id":
        raise ValueError(
            "data generation policy split_group must be parent_canonical_document_id"
        )
    policy = DataGenerationPolicy(
        config_path=config_snapshot.path,
        config_relative_path=config_snapshot.relative_path,
        config_bytes=len(config_snapshot.payload),
        config_sha256=config_snapshot.sha256,
        minimum_canonical_documents=_positive_integer(
            raw.get("minimum_canonical_documents"),
            "minimum_canonical_documents",
        ),
        minimum_train_pairs=_positive_integer(
            raw.get("minimum_train_pairs"),
            "minimum_train_pairs",
        ),
        target_train_pairs=_positive_integer(
            raw.get("target_train_pairs"),
            "target_train_pairs",
        ),
        minimum_validation_pairs=_positive_integer(
            raw.get("minimum_validation_pairs"),
            "minimum_validation_pairs",
        ),
        minimum_test_pairs=_positive_integer(
            raw.get("minimum_test_pairs"),
            "minimum_test_pairs",
        ),
        minimum_challenge_pairs=_positive_integer(
            raw.get("minimum_challenge_pairs"),
            "minimum_challenge_pairs",
        ),
        split_group=split_group,
    )
    if policy.target_train_pairs < policy.minimum_train_pairs:
        raise ValueError("target_train_pairs cannot be below minimum_train_pairs")
    return policy


@dataclass(frozen=True, slots=True)
class RegularFileSnapshot:
    """Exact file bytes read once before a long-running build begins."""

    path: Path
    relative_path: str
    payload: bytes
    sha256: str

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "bytes": len(self.payload),
            "sha256": self.sha256,
        }


def _allowed_path(path: str | Path, allowed_root: str | Path) -> tuple[Path, str]:
    candidate = Path(path)
    root = Path(allowed_root).resolve()
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"artifact is outside the allowed root: {resolved}") from error
    if candidate.is_symlink():
        raise ValueError(f"artifact must not be a symlink: {resolved}")
    return resolved, relative.as_posix()


def read_regular_file_snapshot(
    path: str | Path,
    *,
    allowed_root: str | Path,
) -> RegularFileSnapshot:
    """Read and hash one allowed regular file exactly once."""

    resolved, relative_path = _allowed_path(path, allowed_root)
    if not resolved.is_file():
        raise ValueError(f"artifact must be a non-symlink regular file: {resolved}")
    payload = resolved.read_bytes()
    return RegularFileSnapshot(
        path=resolved,
        relative_path=relative_path,
        payload=payload,
        sha256=f"sha256:{hashlib.sha256(payload).hexdigest()}",
    )


def bind_payload(
    path: str | Path,
    payload: bytes,
    *,
    allowed_root: str | Path,
) -> dict[str, Any]:
    """Bind caller-owned bytes to one allowed output path without rereading it."""

    if not isinstance(payload, bytes):
        raise ValueError("bound payload must be bytes")
    _, relative_path = _allowed_path(path, allowed_root)
    return {
        "path": relative_path,
        "bytes": len(payload),
        "sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
    }


def bind_regular_file(path: str | Path, *, allowed_root: str | Path) -> dict[str, Any]:
    """Hash one regular file relative to an explicit root."""

    return read_regular_file_snapshot(path, allowed_root=allowed_root).binding
