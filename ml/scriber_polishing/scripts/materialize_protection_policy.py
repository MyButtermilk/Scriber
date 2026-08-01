"""Materialize exact Gemma unused-token protection evidence without training."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.curriculum import canonical_json_bytes  # noqa: E402
from scriber_polishing.model_factory import (  # noqa: E402
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
)
from scriber_polishing.protected_spans import (  # noqa: E402
    GEMMA_UNUSED_POLICY_ID,
    LEXICAL_KEEP_POLICY_ID,
    build_gemma_unused_policy_evidence,
    build_lexical_keep_policy_evidence,
)
from scriber_polishing.training_integrity import (  # noqa: E402
    capture_tokenizer_identity,
)


def _write_immutable(path: Path, payload: bytes) -> None:
    destination = path.resolve()
    if destination.exists():
        if (
            destination.is_symlink()
            or not destination.is_file()
            or destination.read_bytes() != payload
        ):
            raise ValueError(
                f"refusing to replace different protection policy: {destination}"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--policy-id",
        choices=(GEMMA_UNUSED_POLICY_ID, LEXICAL_KEEP_POLICY_ID),
        default=GEMMA_UNUSED_POLICY_ID,
    )
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args(argv)

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
        trust_remote_code=False,
        fix_mistral_regex=False,
        local_files_only=args.local_files_only,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    tokenizer_identity = capture_tokenizer_identity(
        tokenizer,
        model_id=BASE_MODEL_ID,
        revision=BASE_MODEL_REVISION,
    )
    builder = (
        build_lexical_keep_policy_evidence
        if args.policy_id == LEXICAL_KEEP_POLICY_ID
        else build_gemma_unused_policy_evidence
    )
    policy = builder(tokenizer, tokenizer_identity=tokenizer_identity)
    payload = canonical_json_bytes(policy)
    _write_immutable(args.output, payload)
    print(
        "protection_policy=complete "
        f"policy_id={policy['policy_id']} "
        f"markers={len(policy['markers'])} "
        f"policy_sha256={policy['policy_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
