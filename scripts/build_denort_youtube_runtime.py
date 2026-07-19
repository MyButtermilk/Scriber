from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence


LOCK_CONTRACT = "ScriberDenortRuntimeProvenanceLockV1"
MANIFEST_CONTRACT = "ScriberYoutubeJsRuntimeManifestV2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_PREFIX = "deno "
SMOKE_MARKER = "scriber-denort-smoke"
# Deno compile embeds the source-file mtime in its module archive.  Keep the
# exact millisecond used by the protected output identity; the final SHA-256
# remains the authoritative check that this compatibility value is correct.
LOCKED_SOURCE_MTIME_NS = 1_784_464_482_599_000_000
SMOKE_ARGUMENTS = (
    "run",
    "--ext=js",
    "--no-code-cache",
    "--no-prompt",
    "--no-remote",
    "--no-lock",
    "--node-modules-dir=none",
    "--no-config",
    "--cached-only",
    "--no-npm",
    "-",
)


class BuildError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(path: Path) -> tuple[int, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"cannot read required file: {path}") from exc
    return len(data), _sha256_bytes(data)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BuildError(f"duplicate JSON key in denort provenance lock: {key}")
        result[key] = value
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid denort provenance lock: {path}") from exc
    if not isinstance(value, dict):
        raise BuildError("denort provenance lock must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise BuildError(f"{label} fields are not exact")


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BuildError(f"{label} must be a positive integer")
    return value


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise BuildError(f"{label} must be a lowercase SHA-256")
    return value


def _canonical_manifest_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _load_lock(path: Path) -> Mapping[str, Any]:
    payload = _load_json_object(path)
    _exact_keys(
        payload,
        {"contract", "schemaVersion", "campaign", "target", "entry"},
        label="denort provenance lock",
    )
    if (
        payload.get("contract") != LOCK_CONTRACT
        or payload.get("schemaVersion") != 1
        or payload.get("campaign") != "installer-size-v2"
        or payload.get("target") != {"os": "windows", "architecture": "x86_64"}
    ):
        raise BuildError("denort provenance lock identity is invalid")
    entry = payload.get("entry")
    if not isinstance(entry, dict):
        raise BuildError("denort provenance entry must be an object")
    _exact_keys(
        entry,
        {
            "id",
            "implementation",
            "version",
            "wrapperVersion",
            "protocol",
            "compiler",
            "denortAsset",
            "wrapper",
            "compileArguments",
            "output",
            "license",
            "manifest",
            "manifestCanonicalSha256",
        },
        label="denort provenance entry",
    )
    for field in ("compiler", "denortAsset", "wrapper", "output", "manifest"):
        if not isinstance(entry.get(field), dict):
            raise BuildError(f"denort provenance {field} must be an object")
    compiler = entry["compiler"]
    denort = entry["denortAsset"]
    wrapper = entry["wrapper"]
    output = entry["output"]
    _exact_keys(compiler, {"version", "length", "sha256"}, label="compiler")
    _exact_keys(
        denort,
        {
            "url",
            "fileName",
            "format",
            "length",
            "sha256",
            "executableLength",
            "executableSha256",
        },
        label="denort asset",
    )
    _exact_keys(wrapper, {"relativePath", "length", "sha256"}, label="wrapper")
    _exact_keys(output, {"installedFileName", "length", "sha256"}, label="output")
    if compiler.get("version") != entry.get("version"):
        raise BuildError("compiler version differs from the locked runtime version")
    for value, label in (
        (compiler.get("length"), "compiler length"),
        (denort.get("length"), "denort archive length"),
        (denort.get("executableLength"), "denort executable length"),
        (wrapper.get("length"), "wrapper length"),
        (output.get("length"), "output length"),
    ):
        _positive_integer(value, label=label)
    for value, label in (
        (compiler.get("sha256"), "compiler SHA-256"),
        (denort.get("sha256"), "denort archive SHA-256"),
        (denort.get("executableSha256"), "denort executable SHA-256"),
        (wrapper.get("sha256"), "wrapper SHA-256"),
        (output.get("sha256"), "output SHA-256"),
        (entry.get("manifestCanonicalSha256"), "manifest SHA-256"),
    ):
        _sha256(value, label=label)
    arguments = entry.get("compileArguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or any(not isinstance(value, str) or not value for value in arguments)
        or arguments.count("<OUTPUT>") != 1
        or arguments.count("<SOURCE>") != 1
        or arguments[-2:] != ["<OUTPUT>", "<SOURCE>"]
    ):
        raise BuildError("denort compile arguments are invalid")
    manifest = entry["manifest"]
    if manifest.get("contract") != MANIFEST_CONTRACT:
        raise BuildError("denort runtime manifest contract is invalid")
    manifest_bytes = _canonical_manifest_bytes(manifest)
    if _sha256_bytes(manifest_bytes) != entry["manifestCanonicalSha256"]:
        raise BuildError("denort runtime manifest differs from its canonical hash")
    return entry


def _resolve_under(root: Path, relative: str, *, label: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise BuildError(f"{label} must be repository-relative")
    root = root.resolve(strict=True)
    candidate = (root / relative).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BuildError(f"{label} escapes the repository") from exc
    if not candidate.is_file():
        raise BuildError(f"{label} is not a file: {candidate}")
    return candidate


def _normalized_wrapper_bytes(source: Path) -> bytes:
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BuildError("denort wrapper source is not strict UTF-8") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise BuildError("denort wrapper source contains a lone carriage return")
    return text.encode("utf-8")


def _assert_identity(path: Path, expected: Mapping[str, Any], *, label: str) -> None:
    length, sha256 = _file_identity(path)
    if length != expected["length"] or sha256 != expected["sha256"]:
        raise BuildError(f"{label} differs from the protected provenance lock")


def _run(
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(arguments),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=None if environment is None else dict(environment),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"failed to execute: {arguments[0]}") from exc


def _verify_compiler(path: Path, entry: Mapping[str, Any]) -> None:
    compiler = entry["compiler"]
    _assert_identity(path, compiler, label="Deno compiler")
    result = _run((str(path), "--version"), timeout=30)
    first_line = result.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    if result.returncode != 0 or first_line != [f"{VERSION_PREFIX}{compiler['version']} (stable, release, x86_64-pc-windows-msvc)"]:
        raise BuildError("Deno compiler version output differs from the lock")


def _verify_denort(path: Path, entry: Mapping[str, Any]) -> None:
    _assert_identity(
        path,
        {
            "length": entry["denortAsset"]["executableLength"],
            "sha256": entry["denortAsset"]["executableSha256"],
        },
        label="DENORT_BIN",
    )


def _download_locked_denort_archive(
    *,
    url: str,
    destination: Path,
    expected_length: int,
    expected_sha256: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.download")
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Scriber-installer-build/1"}
        )
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as output:
            while True:
                chunk = response.read(min(1024 * 1024, expected_length - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_length:
                    raise BuildError("downloaded denort archive exceeds its locked length")
                digest.update(chunk)
                output.write(chunk)
        if total != expected_length or digest.hexdigest() != expected_sha256:
            raise BuildError("downloaded denort archive differs from the protected lock")
        os.replace(temporary, destination)
    except BuildError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise BuildError("failed to download the locked denort archive") from exc


def _extract_locked_denort(
    *, archive: Path, destination: Path, entry: Mapping[str, Any]
) -> None:
    temporary = destination.with_name(f"{destination.name}.extract")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            candidates = [
                member
                for member in bundle.infolist()
                if not member.is_dir()
                and Path(member.filename.replace("\\", "/")).name.lower()
                == "denort.exe"
            ]
            if len(candidates) != 1:
                raise BuildError("locked denort archive must contain exactly one denort.exe")
            member = candidates[0]
            if member.file_size != entry["denortAsset"]["executableLength"]:
                raise BuildError("denort archive executable length differs from the lock")
            digest = hashlib.sha256()
            total = 0
            with bundle.open(member) as source, temporary.open("xb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > entry["denortAsset"]["executableLength"]:
                        raise BuildError("denort archive executable exceeds its locked length")
                    digest.update(chunk)
                    output.write(chunk)
            if (
                total != entry["denortAsset"]["executableLength"]
                or digest.hexdigest()
                != entry["denortAsset"]["executableSha256"]
            ):
                raise BuildError("denort archive executable differs from the protected lock")
        os.replace(temporary, destination)
    except BuildError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        temporary.unlink(missing_ok=True)
        raise BuildError("failed to extract the locked denort archive") from exc


def _provision_denort(work_dir: Path, entry: Mapping[str, Any]) -> Path:
    asset = entry["denortAsset"]
    cache_dir = work_dir / "denort-input-cache"
    archive = cache_dir / asset["fileName"]
    executable = cache_dir / "denort.exe"

    if executable.is_file():
        try:
            _verify_denort(executable, entry)
            return executable
        except BuildError:
            executable.unlink(missing_ok=True)

    archive_identity = None
    if archive.is_file():
        archive_identity = _file_identity(archive)
    if archive_identity != (asset["length"], asset["sha256"]):
        archive.unlink(missing_ok=True)
        _download_locked_denort_archive(
            url=asset["url"],
            destination=archive,
            expected_length=asset["length"],
            expected_sha256=asset["sha256"],
        )
    _extract_locked_denort(archive=archive, destination=executable, entry=entry)
    _verify_denort(executable, entry)
    return executable


def _resolve_denort_for_build(
    args: argparse.Namespace, work_dir: Path, entry: Mapping[str, Any]
) -> Path:
    requested = args.denort or os.environ.get("DENORT_BIN")
    if requested:
        try:
            override = Path(requested).resolve(strict=True)
        except OSError as exc:
            raise BuildError("DENORT_BIN does not point to a file") from exc
        if not override.is_file():
            raise BuildError("DENORT_BIN does not point to a file")
        _verify_denort(override, entry)
        return override
    return _provision_denort(work_dir, entry)


def _verify_wrapper(repo_root: Path, entry: Mapping[str, Any]) -> tuple[Path, bytes]:
    wrapper = entry["wrapper"]
    source = _resolve_under(repo_root, wrapper["relativePath"], label="wrapper source")
    normalized = _normalized_wrapper_bytes(source)
    if (
        len(normalized) != wrapper["length"]
        or _sha256_bytes(normalized) != wrapper["sha256"]
    ):
        raise BuildError("normalized denort wrapper differs from the protected lock")
    return source, normalized


def _verify_output(path: Path, manifest_path: Path, entry: Mapping[str, Any]) -> None:
    _assert_identity(path, entry["output"], label="compiled denort runtime")
    expected_manifest = _canonical_manifest_bytes(entry["manifest"])
    try:
        actual_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise BuildError(f"cannot read denort runtime manifest: {manifest_path}") from exc
    if (
        actual_manifest != expected_manifest
        or _sha256_bytes(actual_manifest) != entry["manifestCanonicalSha256"]
    ):
        raise BuildError("denort runtime manifest is not byte-exact with the lock")
    version = _run((str(path), "--version"), timeout=30)
    first_line = version.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    if version.returncode != 0 or first_line != [f"deno {entry['version']} (stable, release, x86_64-pc-windows-msvc)"]:
        raise BuildError("compiled denort runtime version output differs from the lock")
    smoke = _run(
        (str(path), *SMOKE_ARGUMENTS),
        input_bytes=f'console.log("{SMOKE_MARKER}");\n'.encode(),
        timeout=30,
    )
    if smoke.returncode != 0 or smoke.stdout.strip() != SMOKE_MARKER.encode():
        raise BuildError("compiled denort runtime stdin protocol smoke failed")


def build_or_verify(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = args.repo_root.resolve(strict=True)
    entry = _load_lock(args.lock.resolve(strict=True))
    _source, normalized_wrapper = _verify_wrapper(repo_root, entry)
    output = args.output.resolve(strict=False)
    manifest = args.manifest.resolve(strict=False)
    if output.name != entry["output"]["installedFileName"]:
        raise BuildError("output file name differs from the lock")
    if output.parent != manifest.parent:
        raise BuildError("denort runtime and manifest must share one directory")
    output.parent.mkdir(parents=True, exist_ok=True)

    if not args.verify_only:
        compiler = args.compiler.resolve(strict=True)
        _verify_compiler(compiler, entry)
        work_dir = args.work_dir.resolve(strict=False)
        work_dir.mkdir(parents=True, exist_ok=True)
        denort = _resolve_denort_for_build(args, work_dir, entry)
        normalized_source = work_dir / "yt_dlp_runtime_wrapper.ts"
        temporary_output = work_dir / "deno.exe"
        normalized_source.write_bytes(normalized_wrapper)
        os.utime(
            normalized_source,
            ns=(LOCKED_SOURCE_MTIME_NS, LOCKED_SOURCE_MTIME_NS),
        )
        if temporary_output.exists():
            temporary_output.unlink()
        compile_arguments = [
            str(compiler),
            *(
                str(temporary_output) if value == "<OUTPUT>" else
                str(normalized_source) if value == "<SOURCE>" else value
                for value in entry["compileArguments"]
            ),
        ]
        environment = dict(os.environ)
        environment["DENORT_BIN"] = str(denort)
        result = _run(compile_arguments, environment=environment, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise BuildError(f"Deno compile failed with exit code {result.returncode}: {stderr}")
        _assert_identity(temporary_output, entry["output"], label="compiled denort runtime")
        manifest_bytes = _canonical_manifest_bytes(entry["manifest"])
        temporary_manifest = work_dir / "js-runtime-manifest.json"
        temporary_manifest.write_bytes(manifest_bytes)
        os.replace(temporary_output, output)
        os.replace(temporary_manifest, manifest)

    _verify_output(output, manifest, entry)
    length, sha256 = _file_identity(output)
    return {
        "contract": "ScriberDenortYoutubeRuntimeBuildV1",
        "ok": True,
        "mode": "verify" if args.verify_only else "build",
        "runtime": {"length": length, "sha256": sha256},
        "manifestSha256": _sha256_bytes(manifest.read_bytes()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--compiler", type=Path)
    parser.add_argument("--denort", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.verify_only and (args.work_dir is None or args.compiler is None):
        parser.error("--work-dir and --compiler are required for a build")
    try:
        report = build_or_verify(args)
    except BuildError as exc:
        print(f"denort runtime build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
