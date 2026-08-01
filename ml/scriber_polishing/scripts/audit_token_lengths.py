"""Audit exact Gemma chat-template token lengths for frozen dataset splits."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.token_length_audit import (  # noqa: E402
    audit_token_lengths,
    load_exact_tokenizer_and_evidence,
)

TokenizerLoader = Callable[..., tuple[Any, dict[str, Any]]]


def main(
    argv: Sequence[str] | None = None,
    *,
    tokenizer_loader: TokenizerLoader = load_exact_tokenizer_and_evidence,
) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--test", type=Path)
    parser.add_argument("--challenge", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args(raw_argv)
    split_paths = {
        split: path
        for split in ("train", "validation", "test", "challenge")
        if (path := getattr(args, split)) is not None
    }
    try:
        tokenizer, tokenizer_evidence = tokenizer_loader(
            cache_dir=args.cache_dir,
            local_files_only=args.local_files_only,
        )
        program = Path(__file__).resolve().relative_to(PROJECT_ROOT).as_posix()
        report = audit_token_lengths(
            split_paths,
            tokenizer=tokenizer,
            tokenizer_evidence=tokenizer_evidence,
            output_path=args.output,
            command=(Path(sys.executable).name, program, *raw_argv),
            implementation_paths={"script": Path(__file__)},
        )
    except (ImportError, OSError, TypeError, ValueError) as error:
        print(f"token_length_audit=failed reason={error}", file=sys.stderr)
        return 2
    print(
        "token_length_audit=pass "
        f"examples={report['statistics']['all']['token_length']['count']} "
        f"p95={report['statistics']['all']['token_length']['p95']} "
        f"max={report['statistics']['all']['token_length']['max']} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
