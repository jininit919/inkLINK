#!/usr/bin/env python3
"""
Generate source assets for Capacitor (@capacitor/assets) from the master logo.

Outputs into mobile/assets/:
  - icon.png            (1024x1024, white bg, App Store)
  - icon-foreground.png (1024x1024, transparent, black logo only)
  - icon-background.png (1024x1024, paper #faf8f3 solid)
  - splash.png          (2732x2732, paper bg + logo centered ~40%)
  - splash-dark.png     (2732x2732, same as splash for now)

After regenerating, run from mobile/:
  npm run assets:generate && npx cap sync
"""
import os
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow", file=sys.stderr)
    sys.exit(1)

# Source — adjust path if logo lives elsewhere
SRC_CANDIDATES = [
    Path.home() / "Downloads" / "inkLink mini.jpg",
    Path(__file__).resolve().parent.parent / "public" / "img" / "inkLink-mini.jpg",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "mobile" / "assets"
PAPER = (250, 248, 243)  # #faf8f3
WHITE_THRESHOLD = 240


def find_source() -> Path:
    for p in SRC_CANDIDATES:
        if p.exists():
            return p
    print(f"ERROR: source image not found in any of: {SRC_CANDIDATES}", file=sys.stderr)
    sys.exit(1)


def make_transparent(src: Image.Image, size: int) -> Image.Image:
    """Convert white-bg source to transparent-bg with black foreground."""
    img = src.convert("RGBA").resize((size, size), Image.LANCZOS)
    px = img.load()
    for y in range(size):
        for x in range(size):
            r, g, b, _ = px[x, y]
            if r > WHITE_THRESHOLD and g > WHITE_THRESHOLD and b > WHITE_THRESHOLD:
                px[x, y] = (0, 0, 0, 0)
            else:
                px[x, y] = (0, 0, 0, 255)
    return img


def main() -> int:
    src_path = find_source()
    print(f"Source: {src_path}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    src = Image.open(src_path).convert("RGB")
    print(f"  dims: {src.size}, mode: {src.mode}")

    # 1) icon.png — App Store icon, white bg as-is
    icon = src.resize((1024, 1024), Image.LANCZOS)
    icon.save(OUT_DIR / "icon.png", "PNG", optimize=True)

    # 2) icon-foreground.png — Android adaptive foreground (transparent)
    fg = make_transparent(src, 1024)
    fg.save(OUT_DIR / "icon-foreground.png", "PNG", optimize=True)

    # 3) icon-background.png — Android adaptive background (paper solid)
    Image.new("RGB", (1024, 1024), PAPER).save(
        OUT_DIR / "icon-background.png", "PNG", optimize=True
    )

    # 4) splash.png — paper bg, logo ~40% width, centered
    splash = Image.new("RGB", (2732, 2732), PAPER)
    logo_size = int(2732 * 0.75)
    logo = make_transparent(src, logo_size)
    pos = ((2732 - logo_size) // 2, (2732 - logo_size) // 2)
    splash.paste(logo, pos, logo)
    splash.save(OUT_DIR / "splash.png", "PNG", optimize=True)

    # 5) splash-dark.png — same as light for now
    splash.save(OUT_DIR / "splash-dark.png", "PNG", optimize=True)

    for f in sorted(OUT_DIR.glob("*.png")):
        kb = f.stat().st_size // 1024
        print(f"  {f.name}: {kb} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
