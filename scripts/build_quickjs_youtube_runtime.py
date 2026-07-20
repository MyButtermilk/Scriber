from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence


LOCK_CONTRACT = "ScriberQuickJsWrapperRuntimeLockV1"
UPSTREAM_LOCK_CONTRACT = "ScriberQuickJsRuntimeProvenanceLockV1"
MANIFEST_CONTRACT = "ScriberYoutubeJsRuntimeManifestV3"
BUILD_CONTRACT = "ScriberQuickJsYoutubeRuntimeBuildV1"
IMPLEMENTATION = "bounded-quickjs-wrapper"
PROTOCOL = "ScriberYtDlpQuickJsFileV1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BuildError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                total += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise BuildError(f"cannot read required file: {path}") from exc
    return total, digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BuildError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"{label} must be a JSON object")
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


def _safe_leaf(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise BuildError(f"{label} must be a plain file name")
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


def _normalized_text_bytes(path: Path) -> bytes:
    try:
        text = path.read_bytes().decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise BuildError(f"cannot read strict UTF-8 build input: {path}") from exc
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise BuildError(f"build input contains a lone carriage return: {path}")
    return text.encode("utf-8")


def _resolve_under(root: Path, relative: str, *, label: str) -> Path:
    if not relative or Path(relative).is_absolute() or "\\" in relative:
        raise BuildError(f"{label} must be a portable repository-relative path")
    root = root.resolve(strict=True)
    try:
        candidate = (root / relative).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise BuildError(f"{label} escapes the repository") from exc
    if not candidate.is_file():
        raise BuildError(f"{label} is not a file")
    return candidate


def _expected_manifest(lock: Mapping[str, Any]) -> dict[str, Any]:
    engine = lock["engine"]
    engine_source = engine["source"]
    license_value = lock["license"]
    output = lock["wrapper"]["output"]
    return {
        "contract": MANIFEST_CONTRACT,
        "schemaVersion": 3,
        "runtime": {
            "kind": "quickjs",
            "implementation": IMPLEMENTATION,
            "version": "0.15.0",
            "wrapperVersion": "3",
            "protocol": PROTOCOL,
            "executable": output["installedFileName"],
            "length": output["length"],
            "sha256": output["sha256"],
            "engine": engine["installedFileName"],
            "engineLength": engine["length"],
            "engineSha256": engine["sha256"],
            "origin": engine_source["url"],
            "license": license_value["spdx"],
            "licenseFile": license_value["installedFileName"],
            "provenanceLockEntry": lock["upstreamLock"]["entry"],
        },
        "policy": {
            "remoteComponents": False,
            "firstRunDownloads": False,
            "maximumScriptBytes": 32 * 1024 * 1024,
            "maximumStdoutBytes": 4 * 1024 * 1024,
            "maximumStderrBytes": 256 * 1024,
            "timeoutMilliseconds": 45_000,
            "memoryLimitBytes": 256 * 1024 * 1024,
            "stackLimitBytes": 4 * 1024 * 1024,
            "exactArgumentProtocol": True,
            "engineHashVerified": True,
            "killOnJobClose": True,
            "activeProcessLimit": 1,
            "processStateFailureCleanupBeforeReaderJoin": True,
            "nativeModules": False,
            "moduleLoader": False,
            "fileAccess": False,
            "processLaunch": False,
            "networkAccess": False,
            "timeoutSelfTestMilliseconds": 250,
        },
    }


def _validate_upstream_lock(
    repo_root: Path, lock: Mapping[str, Any]
) -> Mapping[str, Any]:
    upstream = lock["upstreamLock"]
    _exact_keys(
        upstream,
        {"relativePath", "length", "sha256", "entry"},
        label="upstream QuickJS lock binding",
    )
    path = _resolve_under(
        repo_root, upstream["relativePath"], label="upstream QuickJS lock"
    )
    normalized = _normalized_text_bytes(path)
    if (len(normalized), _sha256_bytes(normalized)) != (
        upstream["length"],
        upstream["sha256"],
    ):
        raise BuildError("upstream QuickJS lock differs from its wrapper binding")
    payload = _load_json_object(path, label="upstream QuickJS provenance lock")
    if (
        payload.get("contract") != UPSTREAM_LOCK_CONTRACT
        or payload.get("schemaVersion") != 1
        or payload.get("target") != {"os": "windows", "architecture": "x86_64"}
    ):
        raise BuildError("upstream QuickJS provenance lock contract is invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise BuildError("upstream QuickJS provenance entries are invalid")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("id") == upstream["entry"]
    ]
    if len(matches) != 1:
        raise BuildError("bound upstream QuickJS entry is not unique")
    entry = matches[0]
    runtime_files = entry.get("runtimeFiles")
    license_value = entry.get("license")
    asset = entry.get("asset")
    if (
        entry.get("implementation") != "quickjs-ng"
        or entry.get("version") != "0.15.0"
        or not isinstance(asset, dict)
        or not isinstance(runtime_files, list)
        or len(runtime_files) != 1
        or not isinstance(runtime_files[0], dict)
        or not isinstance(license_value, dict)
        or not isinstance(license_value.get("source"), dict)
    ):
        raise BuildError("bound upstream QuickJS entry is not the approved primary runtime")
    engine = lock["engine"]
    engine_source = engine["source"]
    installed_license = lock["license"]
    if (
        engine_source["url"] != asset.get("url")
        or engine_source["fileName"] != asset.get("fileName")
        or engine_source["length"] != asset.get("length")
        or engine_source["sha256"] != asset.get("sha256")
        or runtime_files[0].get("length") != engine_source["length"]
        or runtime_files[0].get("sha256") != engine_source["sha256"]
        or installed_license["spdx"] != license_value.get("spdx")
        or installed_license["installedFileName"]
        != license_value.get("installedFileName")
        or installed_license["url"] != license_value["source"].get("url")
        or installed_license["fileName"]
        != license_value["source"].get("fileName")
        or installed_license["length"] != license_value.get("length")
        or installed_license["sha256"] != license_value.get("sha256")
    ):
        raise BuildError("wrapper engine or license differs from the protected upstream lock")
    return entry


def _load_lock(path: Path, repo_root: Path) -> Mapping[str, Any]:
    lock = _load_json_object(path, label="QuickJS wrapper runtime lock")
    _exact_keys(
        lock,
        {
            "contract",
            "schemaVersion",
            "target",
            "toolchain",
            "upstreamLock",
            "engine",
            "license",
            "wrapper",
            "manifest",
            "manifestCanonicalSha256",
        },
        label="QuickJS wrapper runtime lock",
    )
    if (
        lock.get("contract") != LOCK_CONTRACT
        or lock.get("schemaVersion") != 1
        or lock.get("target") != {"os": "windows", "architecture": "x86_64"}
    ):
        raise BuildError("QuickJS wrapper runtime lock identity is invalid")

    toolchain = lock.get("toolchain")
    engine = lock.get("engine")
    license_value = lock.get("license")
    wrapper = lock.get("wrapper")
    if not all(isinstance(item, dict) for item in (toolchain, engine, license_value, wrapper)):
        raise BuildError("QuickJS wrapper runtime lock sections are invalid")
    _exact_keys(
        toolchain,
        {"rust", "cargoProfile", "cargoArguments", "rustFlags"},
        label="QuickJS wrapper toolchain",
    )
    if toolchain != {
        "rust": "1.97.0",
        "cargoProfile": "release",
        "cargoArguments": ["build", "--release", "--locked"],
        "rustFlags": "-C link-arg=/Brepro",
    }:
        raise BuildError("QuickJS wrapper toolchain recipe is invalid")
    _exact_keys(
        engine,
        {"source", "installedFileName", "length", "sha256", "patches"},
        label="QuickJS engine",
    )
    engine_source = engine.get("source")
    if not isinstance(engine_source, dict):
        raise BuildError("QuickJS engine source is invalid")
    _exact_keys(
        engine_source,
        {"url", "fileName", "length", "sha256"},
        label="QuickJS engine source",
    )
    _exact_keys(
        license_value,
        {
            "spdx",
            "url",
            "fileName",
            "installedFileName",
            "length",
            "sha256",
        },
        label="QuickJS license",
    )
    for value, label in (
        (engine["length"], "QuickJS engine length"),
        (engine_source["length"], "QuickJS engine source length"),
        (license_value["length"], "QuickJS license length"),
    ):
        _positive_integer(value, label=label)
    for value, label in (
        (engine["sha256"], "QuickJS engine SHA-256"),
        (engine_source["sha256"], "QuickJS engine source SHA-256"),
        (license_value["sha256"], "QuickJS license SHA-256"),
        (lock["manifestCanonicalSha256"], "QuickJS manifest SHA-256"),
    ):
        _sha256(value, label=label)
    for value, label in (
        (engine_source["fileName"], "QuickJS source asset"),
        (engine["installedFileName"], "installed QuickJS engine"),
        (license_value["fileName"], "QuickJS license source"),
        (license_value["installedFileName"], "installed QuickJS license"),
    ):
        _safe_leaf(value, label=label)
    if (
        engine["installedFileName"] != "qjs-engine.exe"
        or license_value["spdx"] != "MIT"
        or not str(engine_source["url"]).startswith("https://")
        or not str(license_value["url"]).startswith("https://")
    ):
        raise BuildError("QuickJS engine or license policy is invalid")
    expected_patches = [
        {
            "fileOffset": 3957,
            "expectedHex": "e8669c0000",
            "replacementHex": "0f1f440000",
            "purpose": "disable-qjs-std-registration",
        },
        {
            "fileOffset": 3972,
            "expectedHex": "e8079d0000",
            "replacementHex": "0f1f440000",
            "purpose": "disable-qjs-os-registration",
        },
        {
            "fileOffset": 3987,
            "expectedHex": "e858ad0000",
            "replacementHex": "0f1f440000",
            "purpose": "disable-qjs-bjson-registration",
        },
        {
            "fileOffset": 1178296,
            "expectedHex": "e8a398efff",
            "replacementHex": "0f1f440000",
            "purpose": "disable-module-loader-installation",
        },
    ]
    if engine.get("patches") != expected_patches:
        raise BuildError("QuickJS engine hardening patch contract is invalid")

    _exact_keys(
        wrapper,
        {"crateRoot", "artifact", "files", "output"},
        label="QuickJS wrapper",
    )
    files = wrapper.get("files")
    artifact = wrapper.get("artifact")
    output = wrapper.get("output")
    if (
        not isinstance(files, list)
        or len(files) != 4
        or not isinstance(artifact, dict)
        or not isinstance(output, dict)
    ):
        raise BuildError("QuickJS wrapper input or output inventory is invalid")
    _exact_keys(
        artifact,
        {"url", "fileName", "length", "sha256", "sourceTreeSha256"},
        label="QuickJS wrapper artifact",
    )
    _exact_keys(
        output,
        {"installedFileName", "length", "sha256"},
        label="QuickJS wrapper output",
    )
    if (
        output["installedFileName"] != "qjs.exe"
        or _positive_integer(output["length"], label="QuickJS wrapper length") <= 0
        or not SHA256_RE.fullmatch(str(output["sha256"]))
        or artifact["length"] != output["length"]
        or artifact["sha256"] != output["sha256"]
        or artifact["fileName"] != "scriber-quickjs-wrapper-v3-windows-x86_64.exe"
        or artifact["url"]
        != "https://github.com/MyButtermilk/Scriber/releases/download/"
        "release-cache-quickjs-wrapper-v3/"
        "scriber-quickjs-wrapper-v3-windows-x86_64.exe"
    ):
        raise BuildError("QuickJS wrapper output identity is invalid")
    expected_inputs = {
        "native/scriber-quickjs-wrapper/Cargo.toml",
        "native/scriber-quickjs-wrapper/Cargo.lock",
        "native/scriber-quickjs-wrapper/src/lib.rs",
        "native/scriber-quickjs-wrapper/src/main.rs",
    }
    actual_inputs: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BuildError("QuickJS wrapper input is invalid")
        _exact_keys(
            item,
            {"relativePath", "length", "sha256"},
            label="QuickJS wrapper input",
        )
        relative = item.get("relativePath")
        if not isinstance(relative, str) or relative in actual_inputs:
            raise BuildError("QuickJS wrapper input path is invalid or duplicated")
        actual_inputs.add(relative)
        source = _resolve_under(repo_root, relative, label="QuickJS wrapper input")
        normalized = _normalized_text_bytes(source)
        if (
            len(normalized) != _positive_integer(item["length"], label="wrapper input length")
            or _sha256_bytes(normalized)
            != _sha256(item["sha256"], label="wrapper input SHA-256")
        ):
            raise BuildError("QuickJS wrapper input differs from its lock")
    if actual_inputs != expected_inputs or wrapper["crateRoot"] != "native/scriber-quickjs-wrapper":
        raise BuildError("QuickJS wrapper source inventory is not exact")
    source_tree_bytes = (
        json.dumps(
            files,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if _sha256_bytes(source_tree_bytes) != artifact["sourceTreeSha256"]:
        raise BuildError("QuickJS wrapper artifact is not bound to its source tree")

    _validate_upstream_lock(repo_root, lock)
    expected_manifest = _expected_manifest(lock)
    manifest = lock.get("manifest")
    if (
        manifest != expected_manifest
        or _sha256_bytes(_canonical_manifest_bytes(expected_manifest))
        != lock["manifestCanonicalSha256"]
    ):
        raise BuildError("QuickJS wrapper manifest is not canonical")
    return lock


def _assert_identity(path: Path, expected: Mapping[str, Any], *, label: str) -> None:
    actual_length, actual_sha256 = _file_identity(path)
    expected_length = expected["length"]
    expected_sha256 = expected["sha256"]
    if (actual_length, actual_sha256) != (expected_length, expected_sha256):
        raise BuildError(
            f"{label} differs from its protected identity "
            f"(actual length={actual_length}, sha256={actual_sha256}; "
            f"expected length={expected_length}, sha256={expected_sha256})"
        )


def _download_locked(
    *, url: str, destination: Path, expected_length: int, expected_sha256: str
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
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open(
            "xb"
        ) as output:
            while True:
                chunk = response.read(min(1024 * 1024, expected_length - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_length:
                    raise BuildError("downloaded QuickJS input exceeds its locked length")
                digest.update(chunk)
                output.write(chunk)
        if total != expected_length or digest.hexdigest() != expected_sha256:
            raise BuildError("downloaded QuickJS input differs from its lock")
        os.replace(temporary, destination)
    except BuildError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as exc:
        temporary.unlink(missing_ok=True)
        raise BuildError("failed to download a locked QuickJS input") from exc


def _provision_input(
    *, cache_root: Path, identity: Mapping[str, Any], offline: bool
) -> Path:
    destination = cache_root / identity["fileName"]
    if destination.is_file():
        try:
            _assert_identity(destination, identity, label="cached QuickJS input")
            return destination
        except BuildError:
            destination.unlink(missing_ok=True)
    if offline:
        raise BuildError("offline QuickJS build cache is incomplete")
    _download_locked(
        url=identity["url"],
        destination=destination,
        expected_length=identity["length"],
        expected_sha256=identity["sha256"],
    )
    _assert_identity(destination, identity, label="downloaded QuickJS input")
    return destination


def _resolve_override(
    requested: Path | None, expected: Mapping[str, Any], *, label: str
) -> Path | None:
    if requested is None:
        return None
    try:
        path = requested.resolve(strict=True)
    except OSError as exc:
        raise BuildError(f"{label} override is unavailable") from exc
    if not path.is_file():
        raise BuildError(f"{label} override is not a file")
    _assert_identity(path, expected, label=f"{label} override")
    return path


def _harden_engine(
    *, source: Path, destination: Path, engine: Mapping[str, Any]
) -> Path:
    _assert_identity(source, engine["source"], label="QuickJS engine source")
    try:
        content = bytearray(source.read_bytes())
    except OSError as exc:
        raise BuildError("cannot read QuickJS engine source for hardening") from exc
    occupied: set[int] = set()
    for patch in engine["patches"]:
        offset = patch["fileOffset"]
        expected = bytes.fromhex(patch["expectedHex"])
        replacement = bytes.fromhex(patch["replacementHex"])
        if len(expected) != len(replacement) or not expected:
            raise BuildError("QuickJS hardening patch length is invalid")
        end = offset + len(expected)
        if offset < 0 or end > len(content):
            raise BuildError("QuickJS hardening patch is outside the locked engine")
        indexes = set(range(offset, end))
        if occupied.intersection(indexes):
            raise BuildError("QuickJS hardening patches overlap")
        occupied.update(indexes)
        if bytes(content[offset:end]) != expected:
            raise BuildError("QuickJS hardening patch preimage differs from its lock")
        content[offset:end] = replacement
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.stage")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_bytes(content)
        _assert_identity(temporary, engine, label="hardened QuickJS engine")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _run(
    arguments: Sequence[str],
    *,
    timeout: int,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            env=None if environment is None else dict(environment),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError(f"failed to execute: {arguments[0]}") from exc


def _cargo_target_dir(repo_root: Path, lock: Mapping[str, Any]) -> Path:
    target_key = lock["wrapper"]["output"]["sha256"][:16]
    return repo_root.resolve(strict=True) / "build" / "qjs-target" / target_key


def _build_wrapper(
    *, repo_root: Path, work_dir: Path, rustup: Path, lock: Mapping[str, Any], offline: bool
) -> Path:
    crate_root = (repo_root / lock["wrapper"]["crateRoot"]).resolve(strict=True)
    manifest = crate_root / "Cargo.toml"
    target_dir = _cargo_target_dir(repo_root, lock)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    arguments = [
        str(rustup),
        "run",
        lock["toolchain"]["rust"],
        "cargo",
        *lock["toolchain"]["cargoArguments"],
        "--manifest-path",
        str(manifest),
        "--target-dir",
        str(target_dir),
    ]
    if offline:
        arguments.append("--offline")
    environment = dict(os.environ)
    environment["RUSTFLAGS"] = lock["toolchain"]["rustFlags"]
    environment["CARGO_INCREMENTAL"] = "0"
    result = _run(arguments, timeout=600, environment=environment)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-2000:]
        raise BuildError(f"QuickJS wrapper Cargo build failed: {message}")
    output = target_dir / "release" / "scriber-quickjs-wrapper.exe"
    _assert_identity(output, lock["wrapper"]["output"], label="built QuickJS wrapper")
    return output


def _copy_exact(source: Path, destination: Path, expected: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.stage")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copyfile(source, temporary)
        _assert_identity(temporary, expected, label=f"staged {destination.name}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_runtime(
    *,
    wrapper: Path,
    engine: Path,
    license_path: Path,
    manifest_path: Path,
    lock: Mapping[str, Any],
) -> None:
    output_identity = lock["wrapper"]["output"]
    _assert_identity(wrapper, output_identity, label="QuickJS wrapper")
    _assert_identity(engine, lock["engine"], label="QuickJS engine")
    _assert_identity(license_path, lock["license"], label="QuickJS license")
    expected_manifest = _canonical_manifest_bytes(lock["manifest"])
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise BuildError("QuickJS runtime manifest is unavailable") from exc
    if (
        manifest_bytes != expected_manifest
        or _sha256_bytes(manifest_bytes) != lock["manifestCanonicalSha256"]
    ):
        raise BuildError("QuickJS runtime manifest differs from its lock")

    help_result = _run((str(wrapper), "--help"), timeout=15)
    if (
        help_result.returncode != 1
        or help_result.stdout.decode("utf-8", errors="replace").splitlines()[:1]
        != ["QuickJS-ng version 0.15.0"]
        or help_result.stderr
    ):
        raise BuildError("QuickJS wrapper help/version contract failed")
    self_test = _run((str(wrapper), "--scriber-self-test"), timeout=15)
    try:
        self_test_payload = json.loads(self_test.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError("QuickJS wrapper self-test did not return JSON") from exc
    if (
        self_test.returncode != 0
        or self_test.stderr
        or self_test_payload
        != {"contract": PROTOCOL, "ok": True, "quickjsVersion": "0.15.0"}
    ):
        raise BuildError("QuickJS wrapper self-test failed")

    timeout_started = time.monotonic()
    timeout_self_test = _run(
        (str(wrapper), "--scriber-test-timeout"), timeout=5
    )
    timeout_elapsed = time.monotonic() - timeout_started
    try:
        timeout_payload = json.loads(timeout_self_test.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError("QuickJS wrapper timeout self-test did not return JSON") from exc
    if (
        timeout_self_test.returncode != 0
        or timeout_self_test.stderr
        or timeout_payload
        != {
            "childReaped": True,
            "contract": PROTOCOL,
            "ok": True,
            "productionTimeoutMilliseconds": 45_000,
            "testTimeoutMilliseconds": 250,
        }
        or timeout_elapsed < 0.20
        or timeout_elapsed >= 5.0
    ):
        raise BuildError("QuickJS wrapper internal timeout/cleanup self-test failed")

    with tempfile.TemporaryDirectory(prefix="scriber-qjs-build-smoke-") as raw_temp:
        script = Path(raw_temp) / "smoke.js"
        script.write_text(
            'console.log(JSON.stringify({ type: "result", responses: [] }));\n',
            encoding="utf-8",
            newline="\n",
        )
        smoke = _run((str(wrapper), "--script", str(script)), timeout=15)
        if (
            smoke.returncode != 0
            or smoke.stdout != b'{"responses":[],"type":"result"}\n'
            or smoke.stderr
        ):
            raise BuildError("QuickJS wrapper EJS/JSON protocol smoke failed")
        script.write_text("throw new Error('bounded failure');\n", encoding="utf-8", newline="\n")
        failure = _run((str(wrapper), "--script", str(script)), timeout=15)
        if (
            failure.returncode == 0
            or failure.stdout
            or not failure.stderr.startswith(f"{PROTOCOL}: ".encode())
        ):
            raise BuildError("QuickJS wrapper failure protocol smoke failed")
        escape_module = Path(raw_temp) / "escape.js"
        escape_module.write_text(
            'globalThis.__scriberEscape = "module-loader-active";\n',
            encoding="utf-8",
            newline="\n",
        )
        script.write_text(
            """
const dynamicImportKeyword = "im" + "port";
const denied = async (loader) => {
  try {
    await loader();
    return false;
  } catch (_) {
    return true;
  }
};
(async () => {
  const checks = {
    globalsAbsent:
      typeof globalThis.std === "undefined" &&
      typeof globalThis.os === "undefined" &&
      typeof globalThis.bjson === "undefined" &&
      typeof globalThis.loadScript === "undefined" &&
      typeof globalThis.process === "undefined" &&
      typeof globalThis.require === "undefined",
    directStdImportDenied: await denied(() => import("qjs:std")),
    directOsImportDenied: await denied(() => import("qjs:os")),
    localFileImportDenied: await denied(() => import("./escape.js")),
    evalImportDenied: await denied(() =>
      (0, eval)(dynamicImportKeyword + "('qjs:std')")
    ),
    functionImportDenied: await denied(() =>
      new Function("return " + dynamicImportKeyword + "('qjs:os')")()
    ),
  };
  console.log(JSON.stringify({
    type: "result",
    responses: [{ capabilityBoundary: checks }],
  }));
})();
""".lstrip(),
            encoding="utf-8",
            newline="\n",
        )
        escape = _run((str(wrapper), "--script", str(script)), timeout=15)
        try:
            escape_payload = json.loads(escape.stdout)
            checks = escape_payload["responses"][0]["capabilityBoundary"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise BuildError(
                "QuickJS wrapper capability escape smoke returned invalid JSON"
            ) from exc
        if (
            escape.returncode != 0
            or escape.stderr
            or escape_payload.get("type") != "result"
            or not checks
            or not all(value is True for value in checks.values())
        ):
            raise BuildError("QuickJS wrapper capability escape smoke failed")
    invalid = _run((str(wrapper), "--eval", "1 + 1"), timeout=15)
    if (
        invalid.returncode != 64
        or invalid.stdout
        or not invalid.stderr.startswith(f"{PROTOCOL}: ".encode())
    ):
        raise BuildError("QuickJS wrapper exact argument gate failed")


def build_or_verify(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = args.repo_root.resolve(strict=True)
    lock = _load_lock(args.lock.resolve(strict=True), repo_root)
    outputs = [args.output, args.engine_output, args.license_output, args.manifest]
    resolved = [path.resolve(strict=False) for path in outputs]
    if len({path.parent for path in resolved}) != 1:
        raise BuildError("QuickJS runtime outputs must share one directory")
    wrapper_output, engine_output, license_output, manifest_output = resolved
    expected_names = (
        lock["wrapper"]["output"]["installedFileName"],
        lock["engine"]["installedFileName"],
        lock["license"]["installedFileName"],
        "js-runtime-manifest.json",
    )
    if tuple(path.name for path in resolved) != expected_names:
        raise BuildError("QuickJS runtime output names differ from the lock")

    if not args.verify_only:
        if args.work_dir is None:
            raise BuildError("QuickJS build work directory is required")
        work_dir = args.work_dir.resolve(strict=False)
        work_dir.mkdir(parents=True, exist_ok=True)
        cache_root = work_dir / "input-cache"
        engine_source = _resolve_override(
            args.quickjs_engine, lock["engine"]["source"], label="engine source"
        )
        if engine_source is None:
            engine_source = _provision_input(
                cache_root=cache_root,
                identity=lock["engine"]["source"],
                offline=args.offline,
            )
        engine = _harden_engine(
            source=engine_source,
            destination=work_dir / "hardened-engine" / lock["engine"]["installedFileName"],
            engine=lock["engine"],
        )
        license_path = _resolve_override(
            args.quickjs_license, lock["license"], label="license"
        )
        if license_path is None:
            license_path = _provision_input(
                cache_root=cache_root, identity=lock["license"], offline=args.offline
            )
        if getattr(args, "rebuild_wrapper", False):
            rustup = args.rustup
            if rustup is None:
                found = shutil.which("rustup")
                if not found:
                    raise BuildError(
                        "rustup is unavailable for the pinned QuickJS wrapper rebuild"
                    )
                rustup = Path(found)
            rustup = rustup.resolve(strict=True)
            wrapper = _build_wrapper(
                repo_root=repo_root,
                work_dir=work_dir,
                rustup=rustup,
                lock=lock,
                offline=args.offline,
            )
        else:
            wrapper = _resolve_override(
                getattr(args, "quickjs_wrapper", None),
                lock["wrapper"]["artifact"],
                label="wrapper artifact",
            )
            if wrapper is None:
                wrapper = _provision_input(
                    cache_root=cache_root,
                    identity=lock["wrapper"]["artifact"],
                    offline=args.offline,
                )
        _copy_exact(wrapper, wrapper_output, lock["wrapper"]["output"])
        _copy_exact(engine, engine_output, lock["engine"])
        _copy_exact(license_path, license_output, lock["license"])
        temporary_manifest = manifest_output.with_name(
            f"{manifest_output.name}.stage"
        )
        temporary_manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary_manifest.write_bytes(_canonical_manifest_bytes(lock["manifest"]))
        os.replace(temporary_manifest, manifest_output)

    _verify_runtime(
        wrapper=wrapper_output,
        engine=engine_output,
        license_path=license_output,
        manifest_path=manifest_output,
        lock=lock,
    )
    return {
        "contract": BUILD_CONTRACT,
        "ok": True,
        "mode": "verify" if args.verify_only else "build",
        "runtime": {
            "implementation": IMPLEMENTATION,
            "protocol": PROTOCOL,
            "wrapper": {
                "length": lock["wrapper"]["output"]["length"],
                "sha256": lock["wrapper"]["output"]["sha256"],
            },
            "engine": {
                "length": lock["engine"]["length"],
                "sha256": lock["engine"]["sha256"],
            },
        },
        "manifestSha256": lock["manifestCanonicalSha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine-output", type=Path, required=True)
    parser.add_argument("--license-output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--rustup", type=Path)
    parser.add_argument("--quickjs-wrapper", type=Path)
    parser.add_argument("--quickjs-engine", type=Path)
    parser.add_argument("--quickjs-license", type=Path)
    parser.add_argument("--rebuild-wrapper", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = build_or_verify(_parser().parse_args(argv))
    except (BuildError, OSError, ValueError) as exc:
        print(f"QuickJS runtime build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
