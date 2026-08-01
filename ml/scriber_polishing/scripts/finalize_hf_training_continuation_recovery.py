"""Finalize the completed Attempt-9 recovery without training or evaluation."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scriber_polishing.hf_training_continuation_finalization import (  # noqa: E402
    ContinuationFinalizationError,
    _sha256,
    _write_new_json,
    attempt9_finalization_source_contract,
    build_finalization_result,
    canonical_json_bytes,
    claim_finalization_output,
    verify_attempt9_finalization_source,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--expected-packet-tree-sha256",
        required=True,
    )
    parser.add_argument(
        "--expected-source-contract-sha256",
        required=True,
    )
    parser.add_argument("--owner-id")
    args = parser.parse_args(argv)

    contract = attempt9_finalization_source_contract()
    contract_sha256 = _sha256(canonical_json_bytes(contract))
    if contract_sha256 != args.expected_source_contract_sha256:
        raise ContinuationFinalizationError("source contract differs from external SHA-256")
    verification = verify_attempt9_finalization_source(
        args.source_root,
        source_contract=contract,
    )
    corrected_report = verification.pop("corrected_training_report")
    output = args.output_root.expanduser().resolve()
    claim_finalization_output(
        output,
        source_root=args.source_root,
        packet_tree_sha256=args.expected_packet_tree_sha256,
        source_contract_sha256=contract_sha256,
        owner_id=args.owner_id or str(uuid.uuid4()),
    )
    _write_new_json(
        output / "attempt9-finalization-source-contract.json",
        contract,
    )
    corrected_sha256 = _write_new_json(
        output / "training-report.json",
        corrected_report,
    )
    verification_sha256 = _write_new_json(
        output / "attempt9-finalization-verification.json",
        verification,
    )
    result = build_finalization_result(
        corrected_training_report_sha256=corrected_sha256,
        source_verification_sha256=verification_sha256,
    )
    _write_new_json(output / "continuation-result.json", result)
    print(
        "continuation_finalization=complete "
        "cumulative_training_steps=1100 "
        "training_steps_executed=0 "
        "validation_reexecuted=false",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
