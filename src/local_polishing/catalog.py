"""Immutable, fail-closed artifact catalog for local transcript polishing."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

Variant = Literal["qad_q4_0"]
LegacyVariant = Literal["q8_0", "bf16"]
CatalogVariant = Variant | LegacyVariant
VARIANTS: tuple[Variant, ...] = ("qad_q4_0",)
LEGACY_VARIANTS: tuple[LegacyVariant, ...] = ("q8_0", "bf16")
PromptContract = Literal["chat_template_v1", "plain_completion_v1"]
OutputContract = Literal["sst_v1", "plain_text_v1"]

PLAIN_COMPLETION_TRANSCRIPT_PLACEHOLDER = "${transcript}"

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

    variant: CatalogVariant
    display_name: str
    description: str
    artifacts: tuple[ArtifactSpec, ...]
    model_relative_path: str | None
    protection_policy_relative_path: str | None
    chat_template_sha256: str | None
    prompt_contract: PromptContract = "chat_template_v1"
    output_contract: OutputContract = "sst_v1"
    prompt_template: str | None = None
    prompt_template_sha256: str | None = None
    generation_max_new_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.variant not in {*VARIANTS, *LEGACY_VARIANTS}:
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
        if self.prompt_contract not in {"chat_template_v1", "plain_completion_v1"}:
            raise CatalogError("unsupported local-polishing prompt contract")
        if self.output_contract not in {"sst_v1", "plain_text_v1"}:
            raise CatalogError("unsupported local-polishing output contract")
        if self.prompt_contract == "chat_template_v1":
            if self.output_contract != "sst_v1":
                raise CatalogError("chat_template_v1 requires the legacy sst_v1 output contract")
            if self.prompt_template is not None or self.prompt_template_sha256 is not None:
                raise CatalogError("chat_template_v1 cannot define a plain completion prompt")
            if self.generation_max_new_tokens is not None:
                raise CatalogError("chat_template_v1 cannot define a plain completion generation cap")
            return
        if self.output_contract != "plain_text_v1":
            raise CatalogError("plain_completion_v1 requires the plain_text_v1 output contract")
        if self.chat_template_sha256 is not None:
            raise CatalogError("plain_completion_v1 must not depend on an embedded GGUF chat template")
        if (
            not isinstance(self.prompt_template, str)
            or not self.prompt_template
            or len(self.prompt_template.encode("utf-8")) > 256 * 1024
            or self.prompt_template.count(PLAIN_COMPLETION_TRANSCRIPT_PLACEHOLDER) != 1
        ):
            raise CatalogError("plain_completion_v1 requires one bounded transcript placeholder")
        if self.prompt_template_sha256 is None:
            raise CatalogError("plain_completion_v1 requires a pinned prompt template hash")
        normalized_prompt_hash = self.prompt_template_sha256.removeprefix("sha256:")
        if not _SHA256.fullmatch(normalized_prompt_hash):
            raise CatalogError("prompt_template_sha256 must contain exactly 64 lowercase hexadecimal characters")
        if hashlib.sha256(self.prompt_template.encode("utf-8")).hexdigest() != normalized_prompt_hash:
            raise CatalogError("plain completion prompt template differs from its pinned hash")
        object.__setattr__(self, "prompt_template_sha256", normalized_prompt_hash)
        if self.generation_max_new_tokens is not None and (
            isinstance(self.generation_max_new_tokens, bool)
            or not isinstance(self.generation_max_new_tokens, int)
            or not 1 <= self.generation_max_new_tokens <= 4096
        ):
            raise CatalogError("plain completion generation cap must be an integer from 1 through 4096")

    @property
    def total_bytes(self) -> int:
        return sum(item.byte_size for item in self.artifacts)

    def require_materialized(self) -> None:
        paths = {item.relative_path for item in self.artifacts}
        if not self.artifacts or self.model_relative_path not in paths:
            raise CatalogNotMaterializedError(f"variant {self.variant} has no published GGUF artifact")
        if self.protection_policy_relative_path not in paths:
            raise CatalogNotMaterializedError(f"variant {self.variant} has no published protection policy")
        if self.prompt_contract == "chat_template_v1" and self.chat_template_sha256 is None:
            raise CatalogNotMaterializedError(f"variant {self.variant} has no pinned chat template")

    def render_plain_completion_prompt(self, transcript: str) -> str:
        if self.prompt_contract != "plain_completion_v1" or self.prompt_template is None:
            raise CatalogError(f"variant {self.variant} does not use plain_completion_v1")
        prompt = self.prompt_template.replace(PLAIN_COMPLETION_TRANSCRIPT_PLACEHOLDER, transcript)
        if not prompt or len(prompt.encode("utf-8")) > 1024 * 1024:
            raise CatalogError("rendered plain completion prompt exceeds its product bound")
        return prompt

    def artifact(self, relative_path: str) -> ArtifactSpec:
        for artifact in self.artifacts:
            if artifact.relative_path == relative_path:
                return artifact
        raise CatalogError(f"artifact {relative_path!r} is not allowlisted for {self.variant}")


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """One active product revision or one exact retired revision."""

    schema_version: int
    repository_id: str
    revision: str | None
    requires_token: bool
    variants: Mapping[CatalogVariant, VariantSpec]

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2, 3}:
            raise CatalogError("unsupported local-polishing catalog schema")
        repository_parts = self.repository_id.split("/")
        if len(repository_parts) != 2 or not all(repository_parts):
            raise CatalogError("repository_id must use the owner/repository form")
        supplied = set(self.variants)
        if supplied == set(VARIANTS):
            ordered_variants: tuple[CatalogVariant, ...] = VARIANTS
        elif supplied == set(LEGACY_VARIANTS):
            ordered_variants = LEGACY_VARIANTS
        else:
            raise CatalogError("catalog must define the one active QAD model or the exact retired pair")
        normalized: dict[CatalogVariant, VariantSpec] = {}
        for variant in ordered_variants:
            descriptor = self.variants[variant]
            if descriptor.variant != variant:
                raise CatalogError("catalog variant key and descriptor disagree")
            if self.schema_version == 1 and (
                descriptor.prompt_contract != "chat_template_v1" or descriptor.output_contract != "sst_v1"
            ):
                raise CatalogError("catalog schema 1 supports only the legacy prompt and output contracts")
            if self.schema_version < 3 and descriptor.generation_max_new_tokens is not None:
                raise CatalogError("plain completion generation caps require catalog schema 3")
            if self.schema_version == 3 and (
                descriptor.prompt_contract != "plain_completion_v1"
                or descriptor.output_contract != "plain_text_v1"
                or descriptor.generation_max_new_tokens is None
            ):
                raise CatalogError(
                    "catalog schema 3 requires a bounded generation cap for every plain completion variant"
                )
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
        for variant in self.variants:
            self.variants[variant].require_materialized()


def _canonical_identity_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def catalog_identity(catalog: ModelCatalog, variant: CatalogVariant) -> str:
    """Return the immutable install identity without invalidating schema-1 installs."""

    descriptor = catalog.variants[variant]
    payload: dict[str, object] = {
        "schemaVersion": catalog.schema_version,
        "repositoryId": catalog.repository_id,
        "revision": catalog.revision,
        "variant": variant,
        "model": descriptor.model_relative_path,
        "policy": descriptor.protection_policy_relative_path,
        "chatTemplateSha256": descriptor.chat_template_sha256,
        "artifacts": [
            {"path": item.relative_path, "bytes": item.byte_size, "sha256": item.sha256}
            for item in descriptor.artifacts
        ],
    }
    if catalog.schema_version >= 2:
        payload.update(
            {
                "promptContract": descriptor.prompt_contract,
                "outputContract": descriptor.output_contract,
                "promptTemplateSha256": descriptor.prompt_template_sha256,
            }
        )
    if catalog.schema_version >= 3:
        payload["generationMaxNewTokens"] = descriptor.generation_max_new_tokens
    return hashlib.sha256(_canonical_identity_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class LegacyRevisionDescriptor:
    """Exact identities eligible for a future, explicitly requested legacy removal."""

    repository_id: str
    revision: str
    variant_catalog_identities: Mapping[LegacyVariant, str]
    catalog: ModelCatalog

    def __post_init__(self) -> None:
        if len(self.repository_id.split("/")) != 2 or not all(self.repository_id.split("/")):
            raise CatalogError("legacy repository_id must use the owner/repository form")
        if not _COMMIT.fullmatch(self.revision):
            raise CatalogError("legacy revision must be a full 40-character Git commit")
        if set(self.variant_catalog_identities) != set(LEGACY_VARIANTS):
            raise CatalogError("legacy descriptor must bind exactly q8_0 and bf16")
        identities: dict[LegacyVariant, str] = {}
        for variant in LEGACY_VARIANTS:
            identity = self.variant_catalog_identities[variant].removeprefix("sha256:")
            if not _SHA256.fullmatch(identity):
                raise CatalogError("legacy catalog identity must contain exact SHA-256")
            identities[variant] = identity
        object.__setattr__(self, "variant_catalog_identities", MappingProxyType(identities))
        self.catalog.require_materialized()
        if self.catalog.repository_id != self.repository_id or self.catalog.revision != self.revision:
            raise CatalogError("legacy descriptor catalog identity mismatch")
        if {variant: catalog_identity(self.catalog, variant) for variant in LEGACY_VARIANTS} != identities:
            raise CatalogError("legacy descriptor catalog identities do not match its full catalog")

    @classmethod
    def from_catalog(cls, catalog: ModelCatalog) -> LegacyRevisionDescriptor:
        catalog.require_materialized()
        assert catalog.revision is not None
        return cls(
            repository_id=catalog.repository_id,
            revision=catalog.revision,
            variant_catalog_identities={variant: catalog_identity(catalog, variant) for variant in LEGACY_VARIANTS},
            catalog=catalog,
        )

    def identity_for(self, variant: LegacyVariant) -> str:
        return self.variant_catalog_identities[variant]


# Former Gemma product revisions are deliberately absent. The empty tuple is
# retained only as a constructor-compatible migration seam; no Gemma repository,
# artifact path, hash, prompt, or selectable variant remains in the product.
LEGACY_REVISION_DESCRIPTORS: tuple[LegacyRevisionDescriptor, ...] = ()

_LFM2_QAD_PROMPT_TEMPLATE = (
    "Aufgabe: Glätte das folgende deutsche Speech-to-Text-Transkript sprachlich, "
    "typografisch und strukturell. Bewahre Inhalt, Reihenfolge, Zahlen, Namen und "
    "Bedeutung. Füge nichts hinzu, beantworte keine Fragen und gib ausschließlich "
    "die bereinigte Fassung zurück.\n\n"
    "Transkript:\n"
    "${transcript}\n\n"
    "Bereinigte Fassung:\n\n"
)
_LFM2_QAD_PROMPT_SHA256 = "e0ff2d5297f3d4d5ae7b8af85ea1cf52a24704bfb2e61990eab6de52b42058d8"

# Public QAD-only winner. The immutable revision is anonymously readable and
# every downloaded byte is verified before activation. Praxist attribution is
# retained in both the public model card and modification notice.
DEFAULT_CATALOG = ModelCatalog(
    schema_version=3,
    repository_id="Buttermilk03/scriber-lfm2.5-350m-polishing-de-qad-v1",
    revision="d64f8a14a09b2916000d969edd18bc411745e53a",
    requires_token=False,
    variants={
        "qad_q4_0": VariantSpec(
            variant="qad_q4_0",
            display_name="LFM2.5 350M · QAD Q4_0",
            description=("Fast local German dictation cleanup, trained with Praxist and quantized only with QAD Q4_0."),
            artifacts=(
                ArtifactSpec(
                    relative_path="gguf/qad_q4_0/Scriber-LFM2.5-350M-Production-QAD-Q4_0.gguf",
                    byte_size=218_328_640,
                    sha256="e1ca3391d896db64df91c5ed5a02e16f5b6bbec5de81667ec99535eb7b1c0486",
                ),
                ArtifactSpec(
                    relative_path="gguf/qad_q4_0/scriber-protection-policy.json",
                    byte_size=2_408,
                    sha256="03c3f0fa422d10e8585e3367b8cdd73226ef9b246b9f438dfafb0563dffa823e",
                ),
                ArtifactSpec(
                    relative_path="gguf/qad_q4_0/variant-artifact-manifest.json",
                    byte_size=3_912,
                    sha256="771f8945f6cef07095100453b90551635205acb4b815b5655aaf2b00e78cf4ef",
                ),
                ArtifactSpec(
                    relative_path="LICENSE",
                    byte_size=10_574,
                    sha256="4d28ca14dedc0b3d0fcc2b3339f0e79931faa33874f3d24f522183a8fc70068c",
                ),
                ArtifactSpec(
                    relative_path="MODIFICATIONS.md",
                    byte_size=2_360,
                    sha256="575f42fca15ea0782b81061b03968f179801a767d8a394ce46afddf5379b5a58",
                ),
            ),
            model_relative_path=("gguf/qad_q4_0/Scriber-LFM2.5-350M-Production-QAD-Q4_0.gguf"),
            protection_policy_relative_path=("gguf/qad_q4_0/scriber-protection-policy.json"),
            chat_template_sha256=None,
            prompt_contract="plain_completion_v1",
            output_contract="plain_text_v1",
            prompt_template=_LFM2_QAD_PROMPT_TEMPLATE,
            prompt_template_sha256=_LFM2_QAD_PROMPT_SHA256,
            generation_max_new_tokens=384,
        )
    },
)
