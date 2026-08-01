"""Build pinned llama.cpp Q8_0 and Q4_K_M artifacts from a local BF16 checkpoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.quantization import (  # noqa: E402
    QuantizationError,
    build_gguf_variants,
    load_quantization_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "quantization.yaml")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--training-fingerprint", required=True)
    parser.add_argument("--llama-cpp-bundle", type=Path)
    parser.add_argument("--llama-cpp-bundle-lock", type=Path)
    parser.add_argument("--windows-llama-cpp-runtime", type=Path)
    parser.add_argument("--windows-llama-cpp-runtime-manifest", type=Path)
    parser.add_argument("--llama-cpp-source", type=Path)
    parser.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_quantization_config(args.config)
        if config["gguf_variants"] != ["Q8_0", "Q4_K_M"]:
            raise QuantizationError("GGUF policy does not require exactly Q8_0 and Q4_K_M")
        if config["gguf_requires_verified_gemma3_text_support"] is not True:
            raise QuantizationError("GGUF policy disabled the mandatory Gemma 3 text support gate")
        manifest = build_gguf_variants(
            args.checkpoint,
            args.output,
            source_id=args.source_id,
            training_fingerprint=args.training_fingerprint,
            bundle_root=args.llama_cpp_bundle,
            bundle_lock=args.llama_cpp_bundle_lock,
            windows_runtime_root=args.windows_llama_cpp_runtime,
            windows_runtime_manifest=args.windows_llama_cpp_runtime_manifest,
            llama_cpp_source=args.llama_cpp_source,
            python_executable=args.python_executable,
        )
    except (OSError, QuantizationError) as error:
        print(f"gguf=failed error={error}", file=sys.stderr)
        return 2
    print(
        f"gguf=complete variants={','.join(manifest['variants'])} "
        f"bytes={manifest['total_bytes']} tree={manifest['artifact_tree_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
