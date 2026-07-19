"""Import-only Pillow transform surface for PyAutoGUI screenshot helpers."""

SCRIBER_STDLIB_EXPORT_COMPAT = True


def grayscale(*_args, **_kwargs):
    raise RuntimeError("screenshot transforms are not part of the Scriber runtime")
