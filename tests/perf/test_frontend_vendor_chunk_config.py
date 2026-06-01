from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VITE_CONFIG = REPO_ROOT / "Frontend" / "vite.config.ts"


def test_vite_config_keeps_manual_vendor_chunks() -> None:
    config = VITE_CONFIG.read_text(encoding="utf-8")

    assert "manualChunks(id)" in config
    assert 'return "vendor-react"' in config
    assert 'return "vendor-query"' in config
    assert 'return "vendor-motion"' in config
    assert 'return "vendor-charts"' in config
    assert 'return "vendor"' in config
    assert "/node_modules/react/" in config
    assert "/node_modules/@tanstack/" in config
    assert "/node_modules/framer-motion/" in config
    assert "/node_modules/recharts/" in config
