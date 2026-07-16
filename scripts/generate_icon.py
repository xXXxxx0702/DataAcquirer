"""Regenerate DataAcquirer's application icon assets.

The design is intentionally geometric and flat so it stays recognizable in
the Windows taskbar at 16 px.  Pillow is only needed when regenerating the
committed assets; it is not a runtime dependency of DataAcquirer.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "src" / "data_acquirer" / "assets"
SOURCE_SIZE = 1024
SUPERSAMPLE = 4

NAVY = "#0B2036"
CYAN = "#25D5D1"
WHITE = "#F7FBFF"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _scaled(value: float) -> int:
    return round(value * SUPERSAMPLE)


def _points(values: list[tuple[float, float]]) -> list[tuple[int, int]]:
    return [(_scaled(x), _scaled(y)) for x, y in values]


def _draw_icon() -> Image.Image:
    size = SOURCE_SIZE * SUPERSAMPLE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # App tile: a compact navy field with transparent rounded corners.
    draw.rounded_rectangle(
        (_scaled(64), _scaled(64), _scaled(960), _scaled(960)),
        radius=_scaled(210),
        fill=NAVY,
    )

    # Database silhouette.  Wide strokes survive Windows' smallest icon size.
    database_stroke = _scaled(62)
    draw.ellipse(
        (_scaled(222), _scaled(250), _scaled(802), _scaled(450)),
        outline=CYAN,
        width=database_stroke,
    )
    draw.line(
        _points([(222, 350), (222, 690)]),
        fill=CYAN,
        width=database_stroke,
    )
    draw.line(
        _points([(802, 350), (802, 690)]),
        fill=CYAN,
        width=database_stroke,
    )
    draw.arc(
        (_scaled(222), _scaled(590), _scaled(802), _scaled(790)),
        start=0,
        end=180,
        fill=CYAN,
        width=database_stroke,
    )

    # A sampled sensor waveform across the stored data.
    waveform = [
        (302, 555),
        (392, 555),
        (468, 480),
        (548, 650),
        (632, 535),
        (722, 535),
    ]
    draw.line(
        _points(waveform),
        fill=WHITE,
        width=_scaled(48),
        joint="curve",
    )
    for x, y in (waveform[1], waveform[2], waveform[3], waveform[4]):
        radius = 25
        draw.ellipse(
            (
                _scaled(x - radius),
                _scaled(y - radius),
                _scaled(x + radius),
                _scaled(y + radius),
            ),
            fill=WHITE,
        )

    return image.resize((SOURCE_SIZE, SOURCE_SIZE), Image.Resampling.LANCZOS)


def _draw_small_icon(size: int) -> Image.Image:
    """Draw a pixel-fitted frame instead of shrinking the 1024 px artwork.

    Windows commonly requests 16–48 px icons depending on display scaling.
    Native integer coordinates keep the one- and two-pixel strokes crisp.
    """
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = max(1, round(size * 0.0625))
    draw.rounded_rectangle(
        (margin, margin, size - 1 - margin, size - 1 - margin),
        radius=max(2, round(size * 0.205)),
        fill=NAVY,
    )

    left = round(size * 0.22)
    right = round(size * 0.78)
    top = round(size * 0.25)
    ellipse_bottom = round(size * 0.45)
    side_start = round(size * 0.35)
    side_end = round(size * 0.69)
    bottom_top = round(size * 0.59)
    bottom_end = round(size * 0.79)
    database_stroke = max(1, round(size * 0.07))

    draw.ellipse(
        (left, top, right, ellipse_bottom),
        outline=CYAN,
        width=database_stroke,
    )
    draw.line(
        ((left, side_start), (left, side_end)),
        fill=CYAN,
        width=database_stroke,
    )
    draw.line(
        ((right, side_start), (right, side_end)),
        fill=CYAN,
        width=database_stroke,
    )
    draw.arc(
        (left, bottom_top, right, bottom_end),
        start=0,
        end=180,
        fill=CYAN,
        width=database_stroke,
    )

    waveform = [
        (round(size * x), round(size * y))
        for x, y in (
            (0.30, 0.555),
            (0.39, 0.555),
            (0.47, 0.48),
            (0.55, 0.65),
            (0.63, 0.535),
            (0.72, 0.535),
        )
    ]
    draw.line(
        waveform,
        fill=WHITE,
        width=max(1, round(size * 0.05)),
        joint="curve",
    )
    return image


def _ico_frames(icon: Image.Image) -> list[Image.Image]:
    return [
        _draw_small_icon(size)
        if size <= 48
        else icon.resize((size, size), Image.Resampling.LANCZOS)
        for size in ICO_SIZES
    ]


def _svg_source() -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect x="64" y="64" width="896" height="896" rx="210" fill="{NAVY}"/>
  <g fill="none" stroke="{CYAN}" stroke-width="62" stroke-linecap="round" stroke-linejoin="round">
    <ellipse cx="512" cy="350" rx="290" ry="100"/>
    <path d="M222 350v340c0 55 130 100 290 100s290-45 290-100V350"/>
  </g>
  <polyline points="302,555 392,555 468,480 548,650 632,535 722,535"
            fill="none" stroke="{WHITE}" stroke-width="48"
            stroke-linecap="round" stroke-linejoin="round"/>
  <g fill="{WHITE}">
    <circle cx="392" cy="555" r="25"/>
    <circle cx="468" cy="480" r="25"/>
    <circle cx="548" cy="650" r="25"/>
    <circle cx="632" cy="535" r="25"/>
  </g>
</svg>
'''


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    icon = _draw_icon()
    icon.save(ASSET_DIR / "app_icon.png", optimize=True)
    ico_frames = _ico_frames(icon)
    ico_frames[-1].save(
        ASSET_DIR / "app_icon.ico",
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=ico_frames[:-1],
    )
    (ASSET_DIR / "app_icon.svg").write_text(_svg_source(), encoding="utf-8")
    print(f"Generated icon assets in {ASSET_DIR}")


if __name__ == "__main__":
    main()
