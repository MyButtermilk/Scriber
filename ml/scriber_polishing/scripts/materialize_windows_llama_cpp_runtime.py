"""Verify and materialize the pinned native Windows CUDA llama.cpp runtime."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.windows_llama_cpp_runtime import (  # noqa: E402
    WindowsLlamaCppRuntimeError,
    materialize_windows_llama_cpp_runtime,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--distribution-lock",
        type=Path,
        default=(
            PROJECT_ROOT
            / "configs"
            / "llama_cpp_windows_cuda_runtime.lock.json"
        ),
    )
    parser.add_argument("--runtime-archive", type=Path, required=True)
    parser.add_argument("--cuda-runtime-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        runtime = materialize_windows_llama_cpp_runtime(
            distribution_lock_path=args.distribution_lock,
            runtime_archive_path=args.runtime_archive,
            cuda_archive_path=args.cuda_runtime_archive,
            output_dir=args.output,
        )
    except (OSError, WindowsLlamaCppRuntimeError) as error:
        print(f"windows_llama_cpp_runtime=failed error={error}", file=sys.stderr)
        return 2
    print(
        "windows_llama_cpp_runtime=verified "
        f"tag={runtime.release_tag} commit={runtime.commit} "
        f"tree={runtime.runtime_tree_sha256} "
        f"manifest={runtime.manifest_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
