"""Stable frozen Python runtime for the packaged Scriber backend.

The modules in this package are intentionally independent from ``src``.  They
form the rarely changing PyInstaller layer, while the Scriber application code
is staged beside the executable under ``app/``.
"""
