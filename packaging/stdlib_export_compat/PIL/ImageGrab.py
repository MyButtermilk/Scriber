"""Import-only Pillow capture surface for PyAutoGUI screenshot helpers."""

SCRIBER_STDLIB_EXPORT_COMPAT = True


def grab(*_args, **_kwargs):
    raise RuntimeError("screenshots are not part of the Scriber runtime")
