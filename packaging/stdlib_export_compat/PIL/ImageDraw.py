"""Import-only Pillow drawing surface for PyAutoGUI screenshot helpers."""

SCRIBER_STDLIB_EXPORT_COMPAT = True


def Draw(*_args, **_kwargs):
    raise RuntimeError("screenshot drawing is not part of the Scriber runtime")
