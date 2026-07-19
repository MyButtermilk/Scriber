"""Import-only Pillow image surface for Pipecat's unused video path."""

SCRIBER_STDLIB_EXPORT_COMPAT = True


class Image:
    """Marker type retained for import-time annotations and type checks."""


def _unsupported(*_args, **_kwargs):
    raise RuntimeError("image rendering is not part of the Scriber runtime")


open = _unsupported
frombytes = _unsupported
