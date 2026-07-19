"""Frozen Hugging Face Hub surface used by Scriber's local ONNX models.

The desktop runtime only downloads and inspects public model repositories.  In
particular it never uploads, serves inference requests, runs the Hub CLI, or
serializes framework checkpoints.  Keeping the required lazy imports explicit
avoids freezing every optional Hub surface and its native ``hf_xet`` helper.
"""

from __future__ import annotations


HUGGINGFACE_HUB_REQUIRED_HIDDEN_IMPORTS: tuple[str, ...] = (
    "huggingface_hub._snapshot_download",
    "huggingface_hub.file_download",
    "huggingface_hub.hf_api",
    "huggingface_hub.utils",
    "huggingface_hub.utils._cache_manager",
    "huggingface_hub.utils.tqdm",
)


# These modules are lazy, optional product surfaces.  None is imported by the
# download/model-info/cache-scan paths above.  Explicit exclusions keep a
# future PyInstaller hook from silently re-introducing them.
HUGGINGFACE_HUB_UNUSED_MODULE_PREFIXES: tuple[str, ...] = (
    "huggingface_hub._hot_reload",
    "huggingface_hub._login",
    "huggingface_hub._oauth",
    "huggingface_hub._oidc",
    "huggingface_hub._sandbox",
    "huggingface_hub._tensorboard_logger",
    "huggingface_hub._webhooks_server",
    "huggingface_hub.fastai_utils",
    "huggingface_hub.hf_file_system",
    "huggingface_hub.hub_mixin",
    "huggingface_hub.inference",
    "huggingface_hub.inference_api",
    "huggingface_hub.keras_mixin",
    "huggingface_hub.repository",
    "huggingface_hub.serialization",
)


HUGGINGFACE_HUB_EXCLUDED_MODULES: tuple[str, ...] = (
    "hf_xet",
    *HUGGINGFACE_HUB_UNUSED_MODULE_PREFIXES,
)
