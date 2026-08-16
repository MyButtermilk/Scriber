from __future__ import annotations

import asyncio
import sqlite3
import ssl
import sys

import pytest

from scripts.check_backend_runtime_imports import check_python314_native_runtime

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or sys.version_info[:3] != (3, 14, 6),
    reason="the shipping native-runtime qualification targets Windows CPython 3.14.7",
)


def test_python314_native_dependency_surface_is_functional() -> None:
    from compression import zstd as stdlib_zstd

    import comtypes
    import cryptography
    import grpc
    import lxml.etree
    import numpy
    import onnxruntime
    import sounddevice
    import win32ctypes
    from cryptography.fernet import Fernet

    # The shared source/test venv deliberately retains the public wheel. The
    # frozen import check separately requires the locked +scriber.noblas wheel.
    assert numpy.__version__ == "2.4.6"
    assert int(numpy.asarray([1, 2, 3], dtype=numpy.int64).sum()) == 6
    assert onnxruntime.get_available_providers()
    assert grpc.insecure_channel("127.0.0.1:1") is not None
    assert lxml.etree.fromstring(b"<root/>").tag == "root"
    assert sounddevice.get_portaudio_version()[0] > 0
    assert comtypes.__version__ == "1.4.16"
    assert win32ctypes.__file__
    assert cryptography.__version__
    key = Fernet.generate_key()
    assert Fernet(key).decrypt(Fernet(key).encrypt(b"scriber")) == b"scriber"
    payload = b"scriber-python314" * 128
    assert stdlib_zstd.decompress(stdlib_zstd.compress(payload)) == payload
    assert ssl.OPENSSL_VERSION

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE smoke(value INTEGER)")
        connection.execute("INSERT INTO smoke VALUES (314)")
        assert connection.execute("SELECT value FROM smoke").fetchone() == (314,)
    finally:
        connection.close()

    assert check_python314_native_runtime() == []


def test_python314_aiohttp_loop_transport_smoke() -> None:
    import aiohttp

    async def exercise() -> None:
        timeout = aiohttp.ClientTimeout(total=0.01)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            assert session.closed is False
        assert session.closed is True

    asyncio.run(exercise())
