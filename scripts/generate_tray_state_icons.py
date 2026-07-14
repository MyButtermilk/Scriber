r"""Generate Scriber's contrast-safe Windows tray state artwork.

The normal white-disc feather is the identity source. Update and recording
states preserve that source pixel-for-pixel outside the bounded lower-right
badge, so every state remains legible on a dark Windows taskbar.

Run from the repository root:

    venv\Scripts\python.exe scripts\generate_tray_state_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from generate_windows_app_icon import TRAY_SIZES


REPO_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = REPO_ROOT / "Frontend" / "src-tauri" / "icons"
LEGACY_ICON_SIZE = 32
BADGE_CANVAS_SIZE = 16
SUPERSAMPLE = 8

INK = (24, 31, 38, 255)
WHITE = (255, 255, 255, 255)
UPDATE_BLUE = (47, 111, 237, 255)
RECORDING_RED = (229, 72, 77, 255)


def _ellipse(draw: ImageDraw.ImageDraw, radius: float, fill: tuple[int, int, int, int]) -> None:
    center = BADGE_CANVAS_SIZE / 2
    bounds = tuple(
        round(value * SUPERSAMPLE)
        for value in (
            center - radius,
            center - radius,
            center + radius,
            center + radius,
        )
    )
    draw.ellipse(bounds, fill=fill)


def _badge(kind: str, output_size: int) -> Image.Image:
    canvas_size = BADGE_CANVAS_SIZE * SUPERSAMPLE
    badge = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)

    # Dark keyline, white separation ring, then the semantic state color.
    # This remains distinct both where the badge overlaps the white identity
    # disc and where its edge meets a dark Windows taskbar.
    _ellipse(draw, 7.35, INK)
    _ellipse(draw, 6.50, WHITE)
    _ellipse(draw, 5.55, UPDATE_BLUE if kind == "update" else RECORDING_RED)

    if kind == "update":
        scale = SUPERSAMPLE
        width = round(1.45 * scale)
        draw.line(
            [(8.0 * scale, 4.2 * scale), (8.0 * scale, 9.8 * scale)],
            fill=WHITE,
            width=width,
        )
        draw.line(
            [
                (5.55 * scale, 7.65 * scale),
                (8.0 * scale, 10.1 * scale),
                (10.45 * scale, 7.65 * scale),
            ],
            fill=WHITE,
            width=width,
            joint="curve",
        )
        draw.line(
            [(5.3 * scale, 11.55 * scale), (10.7 * scale, 11.55 * scale)],
            fill=WHITE,
            width=round(1.25 * scale),
        )

    return badge.resize(
        (output_size, output_size),
        resample=Image.Resampling.LANCZOS,
    )


def _write_pair(name: str, image: Image.Image) -> None:
    png_path = ICON_DIR / f"{name}.png"
    rgba_path = ICON_DIR / f"{name}.rgba"
    image.save(png_path, format="PNG", optimize=True, compress_level=9)
    rgba_path.write_bytes(image.tobytes())


def _write_rgba(name: str, image: Image.Image) -> None:
    (ICON_DIR / f"{name}.rgba").write_bytes(image.tobytes())


def main() -> None:
    for icon_size in TRAY_SIZES:
        normal_bytes = (ICON_DIR / f"tray-normal-{icon_size}.rgba").read_bytes()
        expected_bytes = icon_size * icon_size * 4
        if len(normal_bytes) != expected_bytes:
            raise ValueError(f"Expected {expected_bytes} bytes for the {icon_size}px tray icon")
        normal = Image.frombytes("RGBA", (icon_size, icon_size), normal_bytes)

        # The 16 px Windows tray exception uses a 10 px annotation. Larger
        # variants keep the established half-size badge proportion.
        badge_size = 10 if icon_size == 16 else round(icon_size / 2)
        for kind in ("update", "recording"):
            state_icon = normal.copy()
            state_icon.alpha_composite(
                _badge(kind, badge_size),
                dest=(icon_size - badge_size, icon_size - badge_size),
            )
            _write_rgba(f"tray-{kind}-{icon_size}", state_icon)
            if icon_size == LEGACY_ICON_SIZE:
                _write_pair(f"tray-{kind}", state_icon)


if __name__ == "__main__":
    main()
