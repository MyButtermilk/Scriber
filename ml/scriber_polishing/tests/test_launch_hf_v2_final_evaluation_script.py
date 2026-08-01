from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytest.importorskip("hf_xet")
pytest.importorskip("huggingface_hub")


def _load_launcher_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/launch_hf_v2_final_evaluation.py"
    spec = importlib.util.spec_from_file_location("testable_hf_v2_final_evaluation_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MetadataApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool | None]] = []

    def list_bucket_tree(
        self,
        bucket_id: str,
        prefix: str | None = None,
        *,
        recursive: bool | None = None,
    ) -> list[object]:
        self.calls.append((bucket_id, prefix, recursive))
        return [
            SimpleNamespace(
                type="file",
                path=f"{prefix}/launch-contract.json",
                size=123,
                xet_hash="a" * 64,
            )
        ]

    def download_bucket_files(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("bucket contents must never be downloaded")


def test_control_plane_bucket_checks_do_not_invoke_cli_or_content_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher_script()
    api = MetadataApi()
    control = launcher.HuggingFaceV2EvaluationControlPlane(api=api)

    def forbidden_cli(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("bucket metadata must not use a CLI subprocess")

    monkeypatch.setattr(control, "_run_cli", forbidden_cli)
    entries = control.packet_entries()

    assert entries[0]["path"].endswith("/launch-contract.json")
    assert api.calls == [
        (
            "Buttermilk03/scriber-polishing-private-runs",
            "attempt14/packets/scriber-v2-hardcase-replay-final-evaluation-v1",
            True,
        )
    ]


class ClaimMetadataApi:
    def __init__(self) -> None:
        self.parent = "1" * 40
        self.commit = "2" * 40
        self.payload: bytes | None = None
        self.metadata_call: tuple[str, list[str], str, str] | None = None

    def create_repo(self, *_args: object, **_kwargs: object) -> None:
        return None

    def repo_info(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(private=True, sha=self.parent)

    def repo_exists(self, *_args: object, **_kwargs: object) -> bool:
        return True

    def list_repo_commits(self, *_args: object, **_kwargs: object) -> list[object]:
        return [SimpleNamespace(commit_id=self.parent)]

    def list_repo_files(self, *_args: object, **_kwargs: object) -> list[str]:
        return []

    def create_commit(self, *_args: object, **kwargs: object) -> object:
        operations = kwargs["operations"]
        assert isinstance(operations, list) and len(operations) == 1
        stream = operations[0].path_or_fileobj
        assert isinstance(stream, io.BytesIO)
        self.payload = stream.getvalue()
        return SimpleNamespace(oid=self.commit)

    def get_paths_info(
        self,
        repo_id: str,
        paths: list[str],
        *,
        repo_type: str,
        revision: str,
    ) -> list[object]:
        self.metadata_call = (repo_id, paths, repo_type, revision)
        assert self.payload is not None
        blob_id = hashlib.sha1(
            b"blob " + str(len(self.payload)).encode("ascii") + b"\0" + self.payload,
            usedforsecurity=False,
        ).hexdigest()
        return [
            SimpleNamespace(
                path=paths[0],
                size=len(self.payload),
                blob_id=blob_id,
                lfs=None,
                xet_hash=None,
            )
        ]

    def hf_hub_download(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("claim contents must never be downloaded")


def test_claim_acquisition_uses_exact_commit_metadata_instead_of_download() -> None:
    launcher = _load_launcher_script()
    api = ClaimMetadataApi()
    control = launcher.HuggingFaceV2EvaluationControlPlane(api=api)
    payload = b'{"kind":"attempt14-test-claim"}\n'

    assert control.acquire_permanent_claim(payload) == api.commit
    assert api.metadata_call == (
        "Buttermilk03/scriber-polishing-launch-claims",
        [
            "claims/v2-final-evaluation/attempt14-"
            "scriber-v2-hardcase-replay-final-evaluation-v1.json"
        ],
        "dataset",
        api.commit,
    )
