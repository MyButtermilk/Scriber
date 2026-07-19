"""First-party trust root for the frozen YouTube QuickJS runtime.

The installed runtime manifest is descriptive evidence, not an authority for
its own executable identities.  These values are derived from and regression-
bound to ``packaging/quickjs-youtube-runtime-lock-v1.json``.  Keeping them in a
Python module makes the trust root available from the checksummed ``app`` layer
after the frozen launcher validates that layer.
"""

from __future__ import annotations

from typing import NamedTuple


class LockedRuntimeFile(NamedTuple):
    name: str
    length: int
    sha256: str


ROOT_CONTRACT = "ScriberFrozenQuickJsRuntimeRootV1"
SOURCE_LOCK_FILE = "packaging/quickjs-youtube-runtime-lock-v1.json"
SOURCE_LOCK_LENGTH = 4704
SOURCE_LOCK_SHA256 = "0e6f17a1fc855dd5001077b72a679f0d735359e597dfc54c7ba426ee0b778bc8"

WRAPPER = LockedRuntimeFile(
    name="qjs.exe",
    length=316_416,
    sha256="3594e7e4e01f755d7e9571e312f111f74036002a6b3460a4997e52b5a28533c9",
)
HARDENED_ENGINE = LockedRuntimeFile(
    name="qjs-engine.exe",
    length=1_800_265,
    sha256="f76c7df5a1153b7b8baf5befe3d2621e4a5508c739f9e9eee51a32988d62547e",
)
MANIFEST = LockedRuntimeFile(
    name="js-runtime-manifest.json",
    length=1_196,
    sha256="a019e6608585ad21e846417af9b6fa15b398e7e200d4a882022fea8b375b6a60",
)
LICENSE = LockedRuntimeFile(
    name="LICENSE.quickjs-ng.txt",
    length=1_212,
    sha256="96f73f9d2a16c21a36b418f06073be26e7d6d5e7c1bc99756b21a4f2c74ef171",
)

SELF_TEST_ARGUMENTS = ("--scriber-self-test",)
SELF_TEST_TIMEOUT_SECONDS = 15
SELF_TEST_STDOUT = (
    b'{"contract":"ScriberYtDlpQuickJsFileV1","ok":true,'
    b'"quickjsVersion":"0.15.0"}\n'
)
