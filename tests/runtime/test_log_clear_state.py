import hashlib
import os
from contextlib import contextmanager

import pytest

from src.runtime import log_clear_state


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing contract")
def test_clear_fingerprint_allows_rotation_and_hashes_the_open_generation(monkeypatch, tmp_path):
    active = tmp_path / "latest.log"
    archive = tmp_path / "latest.1.log"
    original = b"original diagnostic generation\n" * 20
    active.write_bytes(original)
    open_reader = log_clear_state.open_log_reader

    @contextmanager
    def rotate_while_reading(path):
        with open_reader(path) as handle:
            os.replace(active, archive)
            active.write_bytes(b"new generation\n")
            yield handle

    monkeypatch.setattr(log_clear_state, "open_log_reader", rotate_while_reading)
    fingerprint = log_clear_state._tail_fingerprint(active, end_offset=len(original))
    assert fingerprint == hashlib.sha256(original[-log_clear_state._CLEAR_FINGERPRINT_BYTES :]).hexdigest()
    assert archive.read_bytes() == original
    assert active.read_bytes() == b"new generation\n"
