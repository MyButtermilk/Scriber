"""Immutable, fail-closed artifact catalog for local transcript polishing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

Variant = Literal["q8_0", "bf16"]
VARIANTS: tuple[Variant, ...] = ("q8_0", "bf16")

_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class CatalogError(ValueError):
    """The local-polishing catalog is malformed or not materialized."""


class CatalogNotMaterializedError(CatalogError):
    """The shipping catalog has no immutable published artifact revision yet."""


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise CatalogError(f"artifact path must be a safe relative POSIX path: {value!r}")


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One exact file allowed to cross the Hugging Face boundary."""

    relative_path: str
    byte_size: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        if isinstance(self.byte_size, bool) or self.byte_size < 1:
            raise CatalogError("artifact byte_size must be a positive integer")
        normalized = self.sha256.removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized):
            raise CatalogError("artifact sha256 must contain exactly 64 lowercase hexadecimal characters")
        object.__setattr__(self, "sha256", normalized)


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """Exact product contract for one GGUF precision variant."""

    variant: Variant
    display_name: str
    description: str
    artifacts: tuple[ArtifactSpec, ...]
    model_relative_path: str | None
    protection_policy_relative_path: str | None
    chat_template_sha256: str | None

    def __post_init__(self) -> None:
        if self.variant not in VARIANTS:
            raise CatalogError(f"unsupported local-polishing variant: {self.variant!r}")
        paths = tuple(item.relative_path for item in self.artifacts)
        if len(set(paths)) != len(paths):
            raise CatalogError(f"variant {self.variant} contains duplicate artifact paths")
        if self.model_relative_path is not None:
            _validate_relative_path(self.model_relative_path)
            if not self.model_relative_path.lower().endswith(".gguf"):
                raise CatalogError("local-polishing model artifact must be GGUF")
        if self.protection_policy_relative_path is not None:
            _validate_relative_path(self.protection_policy_relative_path)
        if self.chat_template_sha256 is not None:
            normalized = self.chat_template_sha256.removeprefix("sha256:")
            if not _SHA256.fullmatch(normalized):
                raise CatalogError("chat_template_sha256 must contain exactly 64 lowercase hexadecimal characters")
            object.__setattr__(self, "chat_template_sha256", normalized)

    @property
    def total_bytes(self) -> int:
        return sum(item.byte_size for item in self.artifacts)

    def require_materialized(self) -> None:
        paths = {item.relative_path for item in self.artifacts}
        if not self.artifacts or self.model_relative_path not in paths:
            raise CatalogNotMaterializedError(f"variant {self.variant} has no published GGUF artifact")
        if self.protection_policy_relative_path not in paths:
            raise CatalogNotMaterializedError(f"variant {self.variant} has no published protection policy")
        if self.chat_template_sha256 is None:
            raise CatalogNotMaterializedError(f"variant {self.variant} has no pinned chat template")

    def artifact(self, relative_path: str) -> ArtifactSpec:
        for artifact in self.artifacts:
            if artifact.relative_path == relative_path:
                return artifact
        raise CatalogError(f"artifact {relative_path!r} is not allowlisted for {self.variant}")


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """One repository revision containing both required local variants."""

    schema_version: int
    repository_id: str
    revision: str | None
    requires_token: bool
    variants: Mapping[Variant, VariantSpec]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise CatalogError("unsupported local-polishing catalog schema")
        repository_parts = self.repository_id.split("/")
        if len(repository_parts) != 2 or not all(repository_parts):
            raise CatalogError("repository_id must use the owner/repository form")
        supplied = set(self.variants)
        if supplied != set(VARIANTS):
            raise CatalogError("catalog must define exactly q8_0 and bf16")
        normalized: dict[Variant, VariantSpec] = {}
        for variant in VARIANTS:
            descriptor = self.variants[variant]
            if descriptor.variant != variant:
                raise CatalogError("catalog variant key and descriptor disagree")
            normalized[variant] = descriptor
        object.__setattr__(self, "variants", MappingProxyType(normalized))
        if self.revision is not None and not _COMMIT.fullmatch(self.revision):
            raise CatalogError("catalog revision must be a full 40-character Git commit")

    @property
    def materialized(self) -> bool:
        try:
            self.require_materialized()
        except CatalogNotMaterializedError:
            return False
        return True

    def require_materialized(self) -> None:
        if self.revision is None:
            raise CatalogNotMaterializedError("local-polishing artifacts have not been published yet")
        for variant in VARIANTS:
            self.variants[variant].require_materialized()


_PUBLIC_REVISION = "65bae06a655fc209058d60064c65fe3e891e4a43"
# llama-server exposes the embedded GGUF template without the source file's
# trailing newline. Pin the exact canonical `/props` value used at runtime.
_CHAT_TEMPLATE_SHA256 = "66317715a6a57b5e16bc1e5a4d17e296fa18cadfea46bfbdf07538dc48f72e65"
_LEGAL_ARTIFACTS = (
    ArtifactSpec(
        relative_path="GEMMA_LICENSE.md",
        byte_size=13_125,
        sha256="d7bbbe9ad37291cdef58c411c31e02f21e9afb965c41f18a2d437f0893482624",
    ),
    ArtifactSpec(
        relative_path="MODIFICATIONS.md",
        byte_size=472,
        sha256="7f4a4cb5f51c06dac60d96158e1a79c732fa80bf1bab5099d3812e38bb327847",
    ),
    ArtifactSpec(
        relative_path="NOTICE",
        byte_size=253,
        sha256="7958917fcf44b9c5e9a4610c1e9bb08cb27393161d227c18b29b93edc1efc4c2",
    ),
)


# Public, non-gated Hugging Face revision. Every downloaded byte is pinned to
# this immutable commit and verified locally before activation. No account,
# token, repository code, or mutable branch name participates in this path.
DEFAULT_CATALOG = ModelCatalog(
    schema_version=1,
    repository_id="Buttermilk03/scriber-gemma3-270m-polishing-de-v1",
    revision=_PUBLIC_REVISION,
    requires_token=False,
    variants={
        "q8_0": VariantSpec(
            variant="q8_0",
            display_name="Q8_0",
            description="Fast desktop model with low memory use.",
            artifacts=(
                ArtifactSpec(
                    relative_path="gguf/q8_0/model-Q8_0.gguf",
                    byte_size=291_545_440,
                    sha256="9f2d74c4fda2b615dd5ae4bdce9c71a910cbcafbcaa5e095c6d0b7f09e46baf1",
                ),
                ArtifactSpec(
                    relative_path="gguf/q8_0/scriber-protection-policy.json",
                    byte_size=4_361,
                    sha256="8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
                ),
                ArtifactSpec(
                    relative_path="gguf/q8_0/variant-artifact-manifest.json",
                    byte_size=5_960,
                    sha256="34777c9b9fd99a280aff3844e4b4bc7f0f6d3457a1664fd281ddec3fb1e79228",
                ),
                *_LEGAL_ARTIFACTS,
            ),
            model_relative_path="gguf/q8_0/model-Q8_0.gguf",
            protection_policy_relative_path="gguf/q8_0/scriber-protection-policy.json",
            chat_template_sha256=_CHAT_TEMPLATE_SHA256,
        ),
        "bf16": VariantSpec(
            variant="bf16",
            display_name="BF16",
            description="Higher precision reference with greater memory use.",
            artifacts=(
                ArtifactSpec(
                    relative_path="gguf/bf16/model-BF16.gguf",
                    byte_size=542_835_328,
                    sha256="337e8fac818d0c697ceed20f88dfad5cba95e63883d2cd9dc354b4bd14246c8b",
                ),
                ArtifactSpec(
                    relative_path="gguf/bf16/scriber-protection-policy.json",
                    byte_size=4_361,
                    sha256="8872354f8aba8258c688186b03a17df2637ee2f126b388239addc4a540b2c10d",
                ),
                ArtifactSpec(
                    relative_path="gguf/bf16/variant-artifact-manifest.json",
                    byte_size=6_001,
                    sha256="de2627b73eeb9b1d40a61943667b53810c0346763b42e17937882f90c5935b71",
                ),
                *_LEGAL_ARTIFACTS,
            ),
            model_relative_path="gguf/bf16/model-BF16.gguf",
            protection_policy_relative_path="gguf/bf16/scriber-protection-policy.json",
            chat_template_sha256=_CHAT_TEMPLATE_SHA256,
        ),
    },
)
