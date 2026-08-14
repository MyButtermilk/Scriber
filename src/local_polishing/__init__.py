"""Local, fail-closed GGUF transcript polishing."""

from .catalog import (
    DEFAULT_CATALOG,
    VARIANTS,
    ArtifactSpec,
    CatalogError,
    CatalogNotMaterializedError,
    ModelCatalog,
    Variant,
    VariantSpec,
)
from .hub import ArtifactDownloader, HuggingFaceArtifactDownloader
from .manager import (
    InstallOperationSnapshot,
    LocalPolishing,
    LocalPolishingError,
    LocalPolishingSnapshot,
    PolishOutcome,
)
from .runtime import (
    GenerationRuntime,
    GenerationRuntimeFactory,
    LlamaRuntimeError,
    LlamaServerLaunchSpec,
    LlamaServerRuntimeFactory,
    RuntimeBinary,
    packaged_runtime_factories,
)

__all__ = [
    "DEFAULT_CATALOG",
    "VARIANTS",
    "ArtifactDownloader",
    "ArtifactSpec",
    "CatalogError",
    "CatalogNotMaterializedError",
    "GenerationRuntime",
    "GenerationRuntimeFactory",
    "HuggingFaceArtifactDownloader",
    "InstallOperationSnapshot",
    "LlamaRuntimeError",
    "LlamaServerLaunchSpec",
    "LlamaServerRuntimeFactory",
    "LocalPolishing",
    "LocalPolishingError",
    "LocalPolishingSnapshot",
    "ModelCatalog",
    "PolishOutcome",
    "RuntimeBinary",
    "Variant",
    "VariantSpec",
    "packaged_runtime_factories",
]
