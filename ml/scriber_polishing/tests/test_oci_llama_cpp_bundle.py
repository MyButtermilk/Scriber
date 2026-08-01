from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

import scriber_polishing.oci_llama_cpp_bundle as oci_bundle
from scriber_polishing.oci_llama_cpp_bundle import (
    OciBundleError,
    extract_verified_oci_layers,
    verify_reviewed_llama_cpp_oci_cache,
)


def _layer(rows: list[tuple[str, bytes | None, str | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload, linkname in rows:
            info = tarfile.TarInfo(name)
            if linkname is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
            else:
                assert payload is not None
                info.size = len(payload)
            archive.addfile(
                info,
                io.BytesIO(payload) if payload is not None else None,
            )
    return gzip.compress(buffer.getvalue(), mtime=0)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def test_verified_oci_layers_apply_whiteouts_and_preserve_safe_symlinks(
    tmp_path: Path,
) -> None:
    base = _layer(
        [
            ("app/old.so", b"old", None),
            ("app/keep.so.1", b"elf", None),
        ]
    )
    overlay = _layer(
        [
            ("app/.wh.old.so", b"", None),
            ("app/new.so", b"new", None),
        ]
    )

    report = extract_verified_oci_layers(
        [
            (base, _digest(base)),
            (overlay, _digest(overlay)),
        ],
        tmp_path / "rootfs",
    )

    assert not (tmp_path / "rootfs/app/old.so").exists()
    assert (tmp_path / "rootfs/app/new.so").read_bytes() == b"new"
    assert report["layer_count"] == 2
    assert report["whiteout_count"] == 1


def test_verified_oci_layers_reject_digest_escape_and_unsafe_symlink(
    tmp_path: Path,
) -> None:
    escape = _layer([("../escape", b"x", None)])
    with pytest.raises(OciBundleError, match="unsafe archive path"):
        extract_verified_oci_layers(
            [(escape, _digest(escape))],
            tmp_path / "escape-root",
        )

    link = _layer([("app/lib.so", None, "../../outside")])
    with pytest.raises(OciBundleError, match="unsafe symlink"):
        extract_verified_oci_layers(
            [(link, _digest(link))],
            tmp_path / "link-root",
        )

    with pytest.raises(OciBundleError, match="digest"):
        extract_verified_oci_layers(
            [(link, "sha256:" + "0" * 64)],
            tmp_path / "digest-root",
        )


def test_verified_oci_layers_preserve_safe_relative_symlink(
    tmp_path: Path,
) -> None:
    layer = _layer(
        [
            ("app/lib.so.1", b"elf", None),
            ("app/lib.so", None, "lib.so.1"),
        ]
    )
    try:
        extract_verified_oci_layers(
            [(layer, _digest(layer))],
            tmp_path / "root",
        )
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    assert (tmp_path / "root/app/lib.so").is_symlink()


def test_reviewed_oci_cache_verifier_is_offline_and_hash_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = b"reviewed-layer"
    layer_digest = _digest(layer)
    config = json.dumps(
        {
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": "91f8c9c5fb038c086e13e9cd823c29b33b07ba54",
                    "org.opencontainers.image.source": "https://github.com/ggml-org/llama.cpp",
                    "org.opencontainers.image.version": "b10156",
                },
                "Env": ["CUDA_VERSION=12.8.1"],
            }
        },
        separators=(",", ":"),
    ).encode()
    config_digest = _digest(config)
    manifest = json.dumps(
        {
            "config": {"digest": config_digest},
            "layers": [{"digest": layer_digest, "size": len(layer)}],
        },
        separators=(",", ":"),
    ).encode()
    manifest_digest = _digest(manifest)
    index = json.dumps(
        {
            "manifests": [
                {
                    "digest": manifest_digest,
                    "platform": {"architecture": "amd64", "os": "linux"},
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    monkeypatch.setattr(oci_bundle, "INDEX_DIGEST", _digest(index))
    monkeypatch.setattr(oci_bundle, "MANIFEST_DIGEST", manifest_digest)
    monkeypatch.setattr(oci_bundle, "CONFIG_DIGEST", config_digest)
    monkeypatch.setattr(oci_bundle, "SELECTED_LAYERS", {layer_digest: len(layer)})
    (tmp_path / "index.json").write_bytes(index)
    (tmp_path / "manifest-amd64.json").write_bytes(manifest)
    (tmp_path / "config.json").write_bytes(config)
    layer_path = tmp_path / f"{layer_digest.removeprefix('sha256:')}.tar.gz"
    layer_path.write_bytes(layer)

    verified = verify_reviewed_llama_cpp_oci_cache(tmp_path)

    assert verified["layer_paths"] == [str(layer_path)]
    layer_path.write_bytes(b"tampered")
    with pytest.raises(OciBundleError, match="cached OCI layer bytes differ"):
        verify_reviewed_llama_cpp_oci_cache(tmp_path)
