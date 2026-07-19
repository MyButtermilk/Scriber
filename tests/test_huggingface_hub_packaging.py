from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path

from backend_runtime.huggingface_hub_policy import (
    HUGGINGFACE_HUB_EXCLUDED_MODULES,
    HUGGINGFACE_HUB_REQUIRED_HIDDEN_IMPORTS,
    HUGGINGFACE_HUB_UNUSED_MODULE_PREFIXES,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_backend_spec_freezes_only_required_huggingface_lazy_surfaces() -> None:
    spec = (REPO_ROOT / "packaging" / "scriber-backend.spec").read_text(
        encoding="utf-8"
    )

    assert "HUGGINGFACE_HUB_REQUIRED_HIDDEN_IMPORTS" in spec
    assert "HUGGINGFACE_HUB_EXCLUDED_MODULES" in spec
    assert 'collect_submodules("huggingface_hub")' not in spec
    assert "hf_xet" in HUGGINGFACE_HUB_EXCLUDED_MODULES
    assert {
        "huggingface_hub._snapshot_download",
        "huggingface_hub.file_download",
        "huggingface_hub.hf_api",
        "huggingface_hub.utils._cache_manager",
    } <= set(HUGGINGFACE_HUB_REQUIRED_HIDDEN_IMPORTS)
    assert {
        "huggingface_hub.inference",
        "huggingface_hub.serialization",
    } <= set(HUGGINGFACE_HUB_UNUSED_MODULE_PREFIXES)


def test_frozen_huggingface_hook_forces_http_fallback(monkeypatch) -> None:
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "0")

    runpy.run_path(
        str(REPO_ROOT / "backend_runtime" / "pyinstaller_huggingface_runtime_hook.py")
    )

    assert os.environ["HF_HUB_DISABLE_XET"] == "1"


def test_required_hub_apis_work_without_excluded_lazy_surfaces(tmp_path: Path) -> None:
    """Exercise metadata, HTTP download, snapshot, and offline cache paths.

    A local endpoint keeps this deterministic while using the real Hub client.
    A meta-path guard turns any accidental import of a pruned module into a
    hard failure.  The second phase stops the server and proves both download
    APIs can resolve the populated cache offline.
    """

    script = textwrap.dedent(
        r"""
        import importlib.abc
        import json
        import os
        import sys
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        excluded = tuple(json.loads(os.environ["SCRIBER_HF_EXCLUDED_MODULES"]))

        class BlockExcluded(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + ".") for name in excluded):
                    raise ModuleNotFoundError(f"pruned Hugging Face module imported: {fullname}")
                return None

        sys.meta_path.insert(0, BlockExcluded())
        os.environ["HF_HUB_DISABLE_XET"] = "1"

        from huggingface_hub import HfApi, hf_hub_download, scan_cache_dir, snapshot_download
        from huggingface_hub.utils._runtime import is_xet_available

        repo_id = "scriber-fixtures/tiny-model"
        commit = "1" * 40
        filename = "config.json"
        body = b'{"model_type":"scriber-fixture"}\n'
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _record(self):
                requests.append((self.command, self.path))

            def do_HEAD(self):
                self._record()
                if "/resolve/" not in self.path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"' + ("2" * 64) + '"')
                self.send_header("X-Repo-Commit", commit)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                self._record()
                if self.path.startswith("/api/models/" + repo_id + "/tree/"):
                    payload = json.dumps([{
                        "type": "file",
                        "path": filename,
                        "size": len(body),
                        "oid": "2" * 40,
                    }]).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.startswith("/api/models/" + repo_id):
                    payload = json.dumps({
                        "id": repo_id,
                        "modelId": repo_id,
                        "sha": commit,
                        "siblings": [{"rfilename": filename, "size": len(body)}],
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if "/resolve/" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

        cache_dir = Path(os.environ["SCRIBER_HF_CACHE_DIR"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"

        try:
            assert not is_xet_available()
            info = HfApi(endpoint=endpoint).model_info(repo_id, files_metadata=True)
            assert info.id == repo_id
            assert info.sha == commit

            downloaded = Path(hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                endpoint=endpoint,
                cache_dir=cache_dir,
            ))
            assert downloaded.read_bytes() == body

            snapshot = Path(snapshot_download(
                repo_id=repo_id,
                endpoint=endpoint,
                cache_dir=cache_dir,
                allow_patterns=[filename],
            ))
            assert (snapshot / filename).read_bytes() == body
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        offline_file = Path(hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            revision=commit,
            cache_dir=cache_dir,
            local_files_only=True,
        ))
        offline_snapshot = Path(snapshot_download(
            repo_id=repo_id,
            revision=commit,
            cache_dir=cache_dir,
            local_files_only=True,
            allow_patterns=[filename],
        ))
        cache_info = scan_cache_dir(cache_dir)

        assert offline_file.read_bytes() == body
        assert (offline_snapshot / filename).read_bytes() == body
        assert any(repo.repo_id == repo_id for repo in cache_info.repos)
        assert any(method == "HEAD" for method, _path in requests)
        assert any(method == "GET" and "/api/models/" in path for method, path in requests)
        """
    )
    env = os.environ.copy()
    env["SCRIBER_HF_EXCLUDED_MODULES"] = json.dumps(
        list(HUGGINGFACE_HUB_EXCLUDED_MODULES)
    )
    env["SCRIBER_HF_CACHE_DIR"] = str(tmp_path / "hub")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
