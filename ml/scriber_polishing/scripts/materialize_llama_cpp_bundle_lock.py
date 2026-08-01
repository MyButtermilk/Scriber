"""Materialize an OCI-bound llama.cpp CUDA runtime lock."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.llama_cpp_bundle import (  # noqa: E402
    LlamaCppBundleError,
    materialize_llama_cpp_bundle_lock,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument(
        "--oci-repository",
        default="ghcr.io/ggml-org/llama.cpp",
    )
    parser.add_argument("--oci-tag", required=True)
    parser.add_argument("--oci-index-digest", required=True)
    parser.add_argument("--oci-manifest-digest", required=True)
    parser.add_argument("--oci-config-digest", required=True)
    parser.add_argument("--oci-revision", required=True)
    parser.add_argument(
        "--native-libraries-layer-digest",
        required=True,
    )
    parser.add_argument(
        "--native-libraries-layer-bytes",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--tools-conversion-layer-digest",
        required=True,
    )
    parser.add_argument(
        "--tools-conversion-layer-bytes",
        type=int,
        required=True,
    )
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument(
        "--native-runtime-root",
        default="runtime/app",
    )
    parser.add_argument("--llama-cpp-version", required=True)
    parser.add_argument("--compiler-id", required=True)
    parser.add_argument("--compiler-version", required=True)
    parser.add_argument("--generator", required=True)
    parser.add_argument("--configuration", required=True)
    parser.add_argument("--build-flag", action="append", required=True)
    parser.add_argument("--cuda-version", required=True)
    parser.add_argument(
        "--convert-hf-to-gguf-path",
        default="source/convert_hf_to_gguf.py",
    )
    parser.add_argument(
        "--llama-quantize-path",
        default="runtime/app/llama-quantize",
    )
    parser.add_argument(
        "--llama-cli-path",
        default="runtime/app/llama-cli",
    )
    parser.add_argument(
        "--llama-server-path",
        default="runtime/app/llama-server",
    )
    args = parser.parse_args(argv)
    try:
        bundle = materialize_llama_cpp_bundle_lock(
            args.bundle_root,
            args.output,
            expected_commit=args.expected_commit,
            llama_cpp_version=args.llama_cpp_version,
            compiler_id=args.compiler_id,
            compiler_version=args.compiler_version,
            cuda_enabled=True,
            cuda_version=args.cuda_version,
            generator=args.generator,
            configuration=args.configuration,
            flags=args.build_flag,
            oci_repository=args.oci_repository,
            oci_tag=args.oci_tag,
            oci_index_digest=args.oci_index_digest,
            oci_manifest_digest=args.oci_manifest_digest,
            oci_config_digest=args.oci_config_digest,
            oci_revision=args.oci_revision,
            oci_layers=[
                {
                    "digest": args.native_libraries_layer_digest,
                    "compressed_bytes": (args.native_libraries_layer_bytes),
                    "media_type": ("application/vnd.oci.image.layer.v1.tar+gzip"),
                    "purpose": "native_libraries",
                },
                {
                    "digest": args.tools_conversion_layer_digest,
                    "compressed_bytes": (args.tools_conversion_layer_bytes),
                    "media_type": ("application/vnd.oci.image.layer.v1.tar+gzip"),
                    "purpose": "tools_and_conversion",
                },
            ],
            runtime_image=args.runtime_image,
            native_runtime_root=args.native_runtime_root,
            convert_hf_to_gguf_path=args.convert_hf_to_gguf_path,
            llama_quantize_path=args.llama_quantize_path,
            llama_cli_path=args.llama_cli_path,
            llama_server_path=args.llama_server_path,
        )
    except (OSError, UnicodeError, LlamaCppBundleError) as error:
        print(f"llama_cpp_bundle_lock=failed error={error}", file=sys.stderr)
        return 2
    print(
        "llama_cpp_bundle_lock=materialized "
        f"commit={bundle.commit} source_tree={bundle.source_tree_sha256} "
        f"build_identity={bundle.build_identity_sha256} output={bundle.lock_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
