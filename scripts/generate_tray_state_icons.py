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


REPO_ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = REPO_ROOT / "Frontend" / "src-tauri" / "icons"
ICON_SIZE = 32
BADGE_SIZE = 16
SUPERSAMPLE = 8

INK = (24, 31, 38, 255)
WHITE = (255, 255, 255, 255)
UPDATE_BLUE = (47, 111, 237, 255)
RECORDING_RED = (229, 72, 77, 255)


def _ellipse(draw: ImageDraw.ImageDraw, radius: float, fill: tuple[int, int, int, int]) -> None:
    center = BADGE_SIZE / 2
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


def _badge(kind: str) -> Image.Image:
    canvas_size = BADGE_SIZE * SUPERSAMPLE
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
        (BADGE_SIZE, BADGE_SIZE),
        resample=Image.Resampling.LANCZOS,
    )


def _write_pair(name: str, image: Image.Image) -> None:
    png_path = ICON_DIR / f"{name}.png"
    rgba_path = ICON_DIR / f"{name}.rgba"
    image.save(png_path, format="PNG", optimize=True, compress_level=9)
    rgba_path.write_bytes(image.tobytes())


def main() -> None:
    normal_path = ICON_DIR / "tray-normal.png"
    with Image.open(normal_path) as source:
        normal = source.convert("RGBA")
    if normal.size != (ICON_SIZE, ICON_SIZE):
        raise ValueError(f"Expected {ICON_SIZE}x{ICON_SIZE} normal tray icon, got {normal.size}")

    # Keep the raw normal pair synchronized as part of the same deterministic
    # generation command, but do not rewrite its PNG identity source.
    (ICON_DIR / "tray-normal.rgba").write_bytes(normal.tobytes())

    for kind in ("update", "recording"):
        state_icon = normal.copy()
        state_icon.alpha_composite(
            _badge(kind),
            dest=(ICON_SIZE - BADGE_SIZE, ICON_SIZE - BADGE_SIZE),
        )
        _write_pair(f"tray-{kind}", state_icon)


if __name__ == "__main__":
    main()
