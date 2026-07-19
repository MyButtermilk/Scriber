"""Write a path-redacted manifest for an installer research Python environment."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Iterable


REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _record_identity(distribution: importlib.metadata.Distribution) -> str | None:
    record = distribution.read_text("RECORD")
    if record is None:
        return None

    rows: list[tuple[str, str, str]] = []
    for row in csv.reader(record.splitlines()):
        if len(row) != 3:
            raise ValueError(
                f"Invalid RECORD row for {distribution.metadata.get('Name', '<unknown>')}"
            )
        path, digest, length = row
        normalized = path.replace("\\", "/")
        # Wheel RECORD entries for console scripts legitimately use paths such
        # as ``../../Scripts/tool.exe`` relative to the dist-info directory.
        # They are identity strings here and are never opened.  Reject only
        # absolute/drive/NUL values rather than breaking valid Windows wheels.
        if (
            "\0" in normalized
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
        ):
            raise ValueError(f"Unsafe RECORD path: {path}")
        rows.append((normalized, digest, length))
    rows.sort()
    return _canonical_sha256(rows)


def _plain_file(path: Path, *, label: str) -> Path:
    candidate = Path(path.absolute())
    info = candidate.lstat()
    if candidate.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise ValueError(f"{label} must be a plain file")
    resolved = candidate.resolve(strict=True)
    resolved_info = resolved.lstat()
    if not resolved.is_file() or resolved.is_symlink() or bool(
        getattr(resolved_info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise ValueError(f"{label} must be a plain file")
    return resolved


def _plain_directory(path: Path, *, label: str, environment_root: Path) -> Path:
    candidate = Path(path.absolute())
    info = candidate.lstat()
    if candidate.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise ValueError(f"{label} must be a plain directory")
    resolved = candidate.resolve(strict=True)
    resolved_info = resolved.lstat()
    if not resolved.is_dir() or resolved.is_symlink() or bool(
        getattr(resolved_info, "st_file_attributes", 0) & REPARSE_POINT
    ):
        raise ValueError(f"{label} must be a plain directory")
    try:
        resolved.relative_to(environment_root)
    except ValueError as exc:
        raise ValueError(f"{label} escaped the research environment") from exc
    return resolved


def _distribution_content_identity(
    distribution: importlib.metadata.Distribution,
    *,
    environment_root: Path,
) -> tuple[int, int, str]:
    rows: list[dict[str, object]] = []
    for item in distribution.files or ():
        normalized = str(item).replace("\\", "/")
        if "\0" in normalized or normalized.startswith("/") or re.match(
            r"^[A-Za-z]:", normalized
        ):
            raise ValueError(f"Unsafe installed distribution path: {normalized}")
        located = _plain_file(
            Path(distribution.locate_file(item)),
            label=f"installed distribution file {normalized}",
        )
        try:
            located.relative_to(environment_root)
        except ValueError as exc:
            raise ValueError(
                f"Installed distribution file escaped the research environment: {normalized}"
            ) from exc
        rows.append(
            {
                "path": normalized,
                "length": located.stat().st_size,
                "sha256": _sha256_file(located),
            }
        )
    rows.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    if not rows:
        name = distribution.metadata.get("Name", "<unknown>")
        raise ValueError(f"Installed distribution has no attestable files: {name}")
    return len(rows), sum(int(item["length"]) for item in rows), _canonical_sha256(rows)


def _distribution_entries(*, environment_root: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise ValueError("Installed distribution is missing its Name metadata")
        normalized_name = raw_name.lower().replace("_", "-")
        if normalized_name in seen:
            raise ValueError(f"Duplicate installed distribution: {normalized_name}")
        seen.add(normalized_name)
        file_count, installed_bytes, content_sha256 = _distribution_content_identity(
            distribution,
            environment_root=environment_root,
        )
        entries.append(
            {
                "name": normalized_name,
                "version": distribution.version,
                "recordSha256": _record_identity(distribution),
                "fileCount": file_count,
                "installedBytes": installed_bytes,
                "contentSha256": content_sha256,
            }
        )
    entries.sort(key=lambda item: str(item["name"]))
    return entries


def _generated_tree_identity(
    root: Path,
    *,
    tree_id: str,
    environment_root: Path,
) -> dict[str, object]:
    root = _plain_directory(
        root,
        label=f"generated tree {tree_id}",
        environment_root=environment_root,
    )
    rows: list[dict[str, object]] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix().encode("utf-8")):
        relative = candidate.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        if candidate.is_dir():
            _plain_directory(
                candidate,
                label=f"generated tree directory {tree_id}/{relative.as_posix()}",
                environment_root=environment_root,
            )
            continue
        plain = _plain_file(
            candidate,
            label=f"generated tree file {tree_id}/{relative.as_posix()}",
        )
        try:
            plain.relative_to(environment_root)
        except ValueError as exc:
            raise ValueError(
                f"Generated tree file escaped the research environment: {tree_id}"
            ) from exc
        rows.append(
            {
                "path": relative.as_posix(),
                "length": plain.stat().st_size,
                "sha256": _sha256_file(plain),
            }
        )
    if not rows:
        raise ValueError(f"Generated tree has no attestable files: {tree_id}")
    return {
        "id": tree_id,
        "fileCount": len(rows),
        "installedBytes": sum(int(item["length"]) for item in rows),
        "contentSha256": _canonical_sha256(rows),
    }


def _generated_tree_entries(*, environment_root: Path) -> list[dict[str, object]]:
    comtypes = importlib.metadata.distribution("comtypes")
    comtypes_root = Path(comtypes.locate_file("comtypes"))
    return [
        _generated_tree_identity(
            comtypes_root / "gen",
            tree_id="comtypes.gen",
            environment_root=environment_root,
        )
    ]


def _file_entries(paths: Iterable[Path], *, label: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda item: item.name.lower()):
        path = _plain_file(path, label=f"{label} input")
        entries.append(
            {
                "name": path.name,
                "length": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    if not entries:
        raise ValueError(f"No {label} files were found")
    return entries


def build_manifest(
    *,
    run_id: str,
    environment_name: str,
    wheelhouse: Path,
    requirements: list[Path],
    python_executable: Path,
) -> dict[str, object]:
    wheel_entries = _file_entries(wheelhouse.glob("*.whl"), label="wheelhouse")
    requirement_entries = _file_entries(requirements, label="requirements")
    environment_root = Path(sys.prefix).resolve(strict=True)
    distributions = _distribution_entries(environment_root=environment_root)
    generated_trees = _generated_tree_entries(environment_root=environment_root)
    python_entry = {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "architecture": platform.machine(),
        "executableName": python_executable.name,
        "length": python_executable.stat().st_size,
        "sha256": _sha256_file(python_executable),
    }
    return {
        "schemaVersion": 1,
        "kind": "scriber-installer-research-python-environment",
        "runId": run_id,
        "environmentName": environment_name,
        "python": python_entry,
        "requirements": requirement_entries,
        "requirementsSha256": _canonical_sha256(requirement_entries),
        "wheelhouse": wheel_entries,
        "wheelhouseSha256": _canonical_sha256(wheel_entries),
        "distributions": distributions,
        "generatedTrees": generated_trees,
        "productDependenciesSha256": _canonical_sha256(
            {"distributions": distributions, "generatedTrees": generated_trees}
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--environment-name", required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument(
        "--requirements",
        type=Path,
        action="append",
        required=True,
        help="Repeat once for every requirements input.",
    )
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument(
        "--verify",
        type=Path,
        help="Recompute the environment identity and compare it with this manifest.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        parsed_run_id = uuid.UUID(args.run_id)
    except ValueError as exc:
        raise ValueError("--run-id must be a canonical RFC 4122 UUID") from exc
    canonical_run_id = str(parsed_run_id)
    if (
        args.run_id != canonical_run_id
        or parsed_run_id.int == 0
        or parsed_run_id.variant != uuid.RFC_4122
    ):
        raise ValueError("--run-id must be a canonical non-nil RFC 4122 UUID")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", args.environment_name):
        raise ValueError("--environment-name is not safely scoped")
    manifest = build_manifest(
        run_id=canonical_run_id,
        environment_name=args.environment_name,
        wheelhouse=args.wheelhouse.resolve(strict=True),
        requirements=[path.resolve(strict=True) for path in args.requirements],
        python_executable=Path(sys.executable).resolve(strict=True),
    )
    if args.verify is not None:
        try:
            expected = json.loads(args.verify.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("--verify is not a valid environment manifest") from exc
        if not isinstance(expected, dict) or expected != manifest:
            print(json.dumps({"ok": False, "reason": "environment-manifest-drift"}))
            return 2
        print(
            json.dumps(
                {
                    "ok": True,
                    "runId": canonical_run_id,
                    "environmentName": args.environment_name,
                    "productDependenciesSha256": manifest["productDependenciesSha256"],
                },
                separators=(",", ":"),
            )
        )
        return 0
    assert args.output is not None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
