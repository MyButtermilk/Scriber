"""Digest-verified, fail-closed OCI layer extraction for llama.cpp."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY = "ggml-org/llama.cpp"
TAG = "full-cuda12-b10156"
INDEX_DIGEST = "sha256:edbbe95d1f20b1b0451dcf61f33be9547dd43578a8ca3456b4127d03a99ce2f7"
MANIFEST_DIGEST = "sha256:b039c93fa0f667af80ca61006a6cbaba1aef1928c88644ad3613471a71952d10"
CONFIG_DIGEST = "sha256:91b633bbe9948c02ed0c838aaf8f16dd90472c1dddd2d972b4bdbfda6a993bfb"
SELECTED_LAYERS = {
    "sha256:6cbda075c071eae617924009643fa4cf584ebbfd6f9308225264303b29dd14bd": 162_560_439,
    "sha256:3989ea4278ace56c0a91cbcb77670f1ac5acf3101bff7f09d0de88666d4f26f1": 173_450_458,
}


class OciBundleError(RuntimeError):
    """OCI bytes or archive entries violated the extraction contract."""


def _verified_json(payload: bytes, digest: str, label: str) -> dict[str, Any]:
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise OciBundleError(f"{label} digest mismatch: expected {digest}, actual {actual}")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise OciBundleError(f"{label} must contain an object")
    return value


def _validate_reviewed_documents(
    index_payload: bytes,
    manifest_payload: bytes,
    config_payload: bytes,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    index = _verified_json(index_payload, INDEX_DIGEST, "OCI index")
    descriptors = index.get("manifests")
    selected = (
        [
            row
            for row in descriptors
            if isinstance(row, dict) and row.get("platform") == {"architecture": "amd64", "os": "linux"}
        ]
        if isinstance(descriptors, list)
        else []
    )
    if len(selected) != 1 or selected[0].get("digest") != MANIFEST_DIGEST:
        raise OciBundleError("OCI index does not bind the reviewed linux/amd64 manifest")
    manifest = _verified_json(manifest_payload, MANIFEST_DIGEST, "OCI manifest")
    if manifest.get("config", {}).get("digest") != CONFIG_DIGEST:
        raise OciBundleError("OCI manifest config digest differs")
    config = _verified_json(config_payload, CONFIG_DIGEST, "OCI config")
    config_section = config.get("config")
    labels = config_section.get("Labels") if isinstance(config_section, dict) else None
    environment = config_section.get("Env") if isinstance(config_section, dict) else None
    if (
        not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != "91f8c9c5fb038c086e13e9cd823c29b33b07ba54"
        or labels.get("org.opencontainers.image.source") != "https://github.com/ggml-org/llama.cpp"
        or labels.get("org.opencontainers.image.version") != "b10156"
        or not isinstance(environment, list)
        or "CUDA_VERSION=12.8.1" not in environment
    ):
        raise OciBundleError("OCI config provenance or CUDA identity differs")
    manifest_layers = {
        row.get("digest"): row.get("size") for row in manifest.get("layers", []) if isinstance(row, dict)
    }
    if any(manifest_layers.get(digest) != size for digest, size in SELECTED_LAYERS.items()):
        raise OciBundleError("OCI manifest selected-layer identity differs")
    return index, manifest, config


def _regular_file_payload(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise OciBundleError(f"{label} must be a regular file: {path}")
    return path.read_bytes()


def verify_reviewed_llama_cpp_oci_cache(cache_dir: str | Path) -> dict[str, Any]:
    """Verify the complete reviewed b10156 cache without any network access."""

    cache = Path(cache_dir).expanduser().resolve()
    if cache.is_symlink() or not cache.is_dir():
        raise OciBundleError(f"OCI cache must be a regular directory: {cache}")
    index_payload = _regular_file_payload(cache / "index.json", "OCI index")
    manifest_payload = _regular_file_payload(cache / "manifest-amd64.json", "OCI manifest")
    config_payload = _regular_file_payload(cache / "config.json", "OCI config")
    _validate_reviewed_documents(index_payload, manifest_payload, config_payload)
    layer_paths: list[str] = []
    for digest, expected_size in SELECTED_LAYERS.items():
        path = cache / f"{digest.removeprefix('sha256:')}.tar.gz"
        payload = _regular_file_payload(path, f"OCI layer {digest}")
        if len(payload) != expected_size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
            raise OciBundleError(f"cached OCI layer bytes differ: {digest}")
        layer_paths.append(str(path))
    return {
        "repository": f"ghcr.io/{REPOSITORY}",
        "tag": TAG,
        "index_digest": INDEX_DIGEST,
        "manifest_digest": MANIFEST_DIGEST,
        "config_digest": CONFIG_DIGEST,
        "layer_paths": layer_paths,
    }


def fetch_reviewed_llama_cpp_oci(cache_dir: str | Path) -> dict[str, Any]:
    """Download only the reviewed b10156 OCI metadata and selected layers."""

    cache = Path(cache_dir).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    scope = urllib.parse.quote(f"repository:{REPOSITORY}:pull")
    with urllib.request.urlopen(
        f"https://ghcr.io/token?scope={scope}",
        timeout=30,
    ) as response:
        token = json.loads(response.read())["token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ("application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json"),
        "User-Agent": "scriber-oci-materializer/1",
    }

    def request(path: str) -> bytes:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"https://ghcr.io/v2/{REPOSITORY}/{path}",
                headers=headers,
            ),
            timeout=120,
        ) as response:
            return response.read()

    index_payload = request(f"manifests/{TAG}")
    manifest_payload = request(f"manifests/{MANIFEST_DIGEST}")
    config_payload = request(f"blobs/{CONFIG_DIGEST}")
    _validate_reviewed_documents(index_payload, manifest_payload, config_payload)
    layer_paths: list[str] = []
    for digest, expected_size in SELECTED_LAYERS.items():
        destination = cache / f"{digest.removeprefix('sha256:')}.tar.gz"
        if not destination.exists():
            payload = request(f"blobs/{digest}")
            if len(payload) != expected_size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
                raise OciBundleError(f"OCI layer bytes differ: {digest}")
            destination.write_bytes(payload)
        payload = destination.read_bytes()
        if len(payload) != expected_size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
            raise OciBundleError(f"cached OCI layer bytes differ: {digest}")
        layer_paths.append(str(destination))
    (cache / "index.json").write_bytes(index_payload)
    (cache / "manifest-amd64.json").write_bytes(manifest_payload)
    (cache / "config.json").write_bytes(config_payload)
    verified = verify_reviewed_llama_cpp_oci_cache(cache)
    if verified["layer_paths"] != layer_paths:
        raise OciBundleError("OCI layer ordering changed during cache verification")
    return verified


def _safe_relative(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise OciBundleError(f"unsafe {label}: {value!r}")
    return path


def _inside(root: Path, relative: PurePosixPath) -> Path:
    target = root.joinpath(*relative.parts)
    resolved_parent = target.parent.resolve()
    if Path(os.path.commonpath([root, resolved_parent])) != root:
        raise OciBundleError(f"archive path escapes extraction root: {relative}")
    return target


def _remove_overlay_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _apply_layer(payload: bytes, root: Path) -> int:
    whiteouts = 0
    try:
        archive = tarfile.open(  # noqa: SIM115
            fileobj=io.BytesIO(payload),
            mode="r:gz",
        )
    except tarfile.TarError as error:
        raise OciBundleError(f"OCI layer is not a gzip tar archive: {error}") from error
    with archive:
        members = archive.getmembers()
        for member in members:
            relative = _safe_relative(member.name.rstrip("/"), "archive path")
            name = relative.name
            if name == ".wh..wh..opq":
                directory = _inside(root, relative.parent)
                if directory.is_dir():
                    for child in directory.iterdir():
                        _remove_overlay_target(child)
                whiteouts += 1
            elif name.startswith(".wh."):
                target = _inside(root, relative.parent / name.removeprefix(".wh."))
                _remove_overlay_target(target)
                whiteouts += 1
        for member in members:
            relative = _safe_relative(member.name.rstrip("/"), "archive path")
            if relative.name.startswith(".wh."):
                continue
            target = _inside(root, relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _remove_overlay_target(target)
            if member.isreg():
                stream = archive.extractfile(member)
                if stream is None:
                    raise OciBundleError(f"archive file has no payload: {relative}")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                os.chmod(target, member.mode & 0o777)
            elif member.issym():
                link = _safe_relative(member.linkname, "symlink target")
                resolved = (target.parent / Path(*link.parts)).resolve()
                if Path(os.path.commonpath([root, resolved])) != root:
                    raise OciBundleError(f"unsafe symlink target: {member.linkname!r}")
                os.symlink(member.linkname, target)
            elif member.islnk():
                link = _inside(root, _safe_relative(member.linkname, "hardlink target"))
                if not link.is_file() or link.is_symlink():
                    raise OciBundleError(f"unsafe hardlink target: {member.linkname!r}")
                os.link(link, target)
            else:
                raise OciBundleError(f"unsupported OCI archive entry: {relative}")
    return whiteouts


def extract_verified_oci_layers(
    layers: list[tuple[bytes, str]],
    destination: str | Path,
) -> dict[str, Any]:
    """Apply verified OCI gzip layers to one new rootfs."""

    root = Path(destination).expanduser()
    if root.exists() or root.is_symlink():
        raise OciBundleError(f"refusing to overwrite OCI rootfs: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()
    digests: list[str] = []
    whiteouts = 0
    try:
        for payload, expected in layers:
            if _DIGEST.fullmatch(expected) is None:
                raise OciBundleError("OCI layer digest is invalid")
            actual = "sha256:" + hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise OciBundleError(f"OCI layer digest mismatch: expected {expected}, actual {actual}")
            whiteouts += _apply_layer(payload, root.resolve())
            digests.append(actual)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return {
        "status": "verified",
        "layer_count": len(layers),
        "layer_digests": digests,
        "whiteout_count": whiteouts,
        "root": str(root.resolve()),
    }
