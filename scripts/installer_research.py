from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.installer_research.comparator import accept_baseline, evaluate_candidate
from scripts.installer_research.inventory import (
    InventoryError,
    build_inventory,
    write_json_atomic,
)


def compute_evaluator_hash(repo_root: Path = REPO_ROOT) -> str:
    paths = [repo_root / "scripts" / "installer_research.py"]
    paths.extend(sorted((repo_root / "scripts" / "installer_research").glob("*.py")))
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise InventoryError(f"Evaluator source is missing: {path.name}")
        relative = path.relative_to(repo_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_json_with_sha(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise InventoryError(f"{label} JSON does not exist: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"{label} is not valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise InventoryError(f"{label} must be a JSON object.")
    return value, hashlib.sha256(raw).hexdigest()


def _inventory_command(args: argparse.Namespace, *, evaluator_hash: str) -> int:
    inventory = build_inventory(
        run_id=args.run_id,
        source_commit=args.source_commit,
        replica_id=args.replica_id,
        build_root_sha256=args.build_root_sha256,
        staged_root=args.staged_root,
        backend_exe=args.backend_exe,
        component_map_path=args.component_map,
        installer=args.installer,
        artifact_dir=args.artifact_dir,
        installed_root=args.installed_root,
        product_version=args.product_version,
        compression=args.compression,
        toolchain_hash=args.toolchain_hash,
        evaluator_hash=evaluator_hash,
    )
    write_json_atomic(inventory, args.output)
    print(
        json.dumps(
            {
                "ok": inventory["ok"],
                "command": "inventory",
                "output": args.output.name,
                "installerBytes": inventory["installer"]["length"],
                "stagedBytes": inventory["payload"]["staged"]["totalBytes"],
            },
            separators=(",", ":"),
        )
    )
    return 0 if inventory["ok"] else 1


def _accept_baseline_command(args: argparse.Namespace, *, evaluator_hash: str) -> int:
    first, first_sha = _read_json_with_sha(
        args.first_inventory, label="first inventory"
    )
    second, second_sha = _read_json_with_sha(
        args.second_inventory, label="second inventory"
    )
    for label, inventory in (("first", first), ("second", second)):
        if inventory.get("evaluatorHash") != evaluator_hash:
            raise InventoryError(
                f"{label} inventory was produced by a different evaluator."
            )
    baseline = accept_baseline(
        first,
        second,
        first_inventory_sha256=first_sha,
        second_inventory_sha256=second_sha,
    )
    write_json_atomic(baseline, args.output)
    print(
        json.dumps(
            {
                "ok": baseline["accepted"],
                "command": "accept-baseline",
                "output": args.output.name,
                "reasonCodes": baseline["reasonCodes"],
            },
            separators=(",", ":"),
        )
    )
    return 0 if baseline["accepted"] else 1


def _optional_json(path: Path | None, *, label: str) -> dict[str, Any] | None:
    if path is None:
        return None
    value, _sha = _read_json_with_sha(path, label=label)
    return value


def _evaluate_command(args: argparse.Namespace, *, evaluator_hash: str) -> int:
    baseline, _baseline_sha = _read_json_with_sha(args.baseline, label="baseline")
    candidate, _candidate_sha = _read_json_with_sha(
        args.candidate_inventory, label="candidate inventory"
    )
    if baseline.get("evaluatorHash") != evaluator_hash:
        raise InventoryError("Baseline was produced by a different evaluator.")
    if candidate.get("evaluatorHash") != evaluator_hash:
        raise InventoryError("Candidate inventory was produced by a different evaluator.")
    parent = _optional_json(args.parent_inventory, label="parent inventory")
    if parent is not None and parent.get("evaluatorHash") != evaluator_hash:
        raise InventoryError("Parent inventory was produced by a different evaluator.")
    gates = _optional_json(args.gate_results, label="gate results")
    measurements: dict[str, Any] | None = None
    measurements_sha256: str | None = None
    if args.install_measurements is not None:
        measurements, measurements_sha256 = _read_json_with_sha(
            args.install_measurements,
            label="install measurements",
        )
    result = evaluate_candidate(
        baseline,
        candidate,
        run_id=args.run_id,
        packet_id=args.packet_id,
        parent_champion_id=args.parent_champion_id,
        hypothesis=args.hypothesis,
        source_commit=args.source_commit,
        parent_inventory=parent,
        comparison_kind=args.comparison_kind,
        gate_results=gates,
        install_measurements=measurements,
        install_measurements_sha256=measurements_sha256,
        min_absolute_reduction_bytes=args.min_absolute_reduction_bytes,
        min_relative_basis_points=args.min_relative_basis_points,
    )
    write_json_atomic(result, args.output)
    print(
        json.dumps(
            {
                "ok": result["decision"] == "keep",
                "command": "evaluate",
                "output": args.output.name,
                "decision": result["decision"],
                "reasonCodes": result["reasonCodes"],
            },
            separators=(",", ":"),
        )
    )
    return 0 if result["decision"] == "keep" else 1


def _path(value: str) -> Path:
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and compare byte-exact Scriber installer research evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser(
        "inventory",
        help="Inventory one exact NSIS installer and its staged payload.",
    )
    inventory.add_argument(
        "--staged-root",
        "--payload-root",
        dest="staged_root",
        type=_path,
        required=True,
    )
    inventory.add_argument("--run-id", required=True)
    inventory.add_argument("--source-commit", required=True)
    inventory.add_argument("--replica-id", required=True)
    inventory.add_argument("--build-root-sha256", required=True)
    inventory.add_argument("--backend-exe", type=_path, required=True)
    inventory.add_argument("--component-map", type=_path, required=True)
    artifact_source = inventory.add_mutually_exclusive_group(required=True)
    artifact_source.add_argument("--installer", type=_path)
    artifact_source.add_argument("--artifact-dir", type=_path)
    inventory.add_argument("--installed-root", type=_path)
    inventory.add_argument("--product-version")
    inventory.add_argument(
        "--compression",
        choices=("bzip2", "zlib", "lzma"),
        required=True,
    )
    inventory.add_argument("--toolchain-hash", required=True)
    inventory.add_argument("--output", type=_path, required=True)

    accept = subparsers.add_parser(
        "accept-baseline",
        help="Accept only two independently reproducible bzip2 inventories.",
    )
    accept.add_argument("--first-inventory", type=_path, required=True)
    accept.add_argument("--second-inventory", type=_path, required=True)
    accept.add_argument("--output", type=_path, required=True)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Compare a candidate with the frozen baseline and parent champion.",
    )
    evaluate.add_argument("--baseline", type=_path, required=True)
    evaluate.add_argument("--candidate-inventory", type=_path, required=True)
    evaluate.add_argument("--parent-inventory", type=_path)
    evaluate.add_argument("--run-id", required=True)
    evaluate.add_argument("--packet-id", required=True)
    evaluate.add_argument("--parent-champion-id", required=True)
    evaluate.add_argument("--hypothesis", required=True)
    evaluate.add_argument("--source-commit", required=True)
    evaluate.add_argument(
        "--comparison-kind",
        choices=("payload", "compression"),
        default="payload",
    )
    evaluate.add_argument("--gate-results", type=_path)
    evaluate.add_argument("--install-measurements", type=_path)
    evaluate.add_argument(
        "--min-absolute-reduction-bytes",
        type=int,
        default=256 * 1024,
    )
    evaluate.add_argument(
        "--min-relative-basis-points",
        type=int,
        default=25,
    )
    evaluate.add_argument("--output", type=_path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        evaluator_hash = compute_evaluator_hash()
        if args.command == "inventory":
            return _inventory_command(args, evaluator_hash=evaluator_hash)
        if args.command == "accept-baseline":
            return _accept_baseline_command(args, evaluator_hash=evaluator_hash)
        if args.command == "evaluate":
            return _evaluate_command(args, evaluator_hash=evaluator_hash)
        raise AssertionError(f"Unhandled command: {args.command}")
    except (InventoryError, OSError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "command": getattr(args, "command", None),
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
