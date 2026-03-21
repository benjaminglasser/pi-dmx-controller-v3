#!/usr/bin/env python3
"""OLED boot splash: shows CSW logo with CRT-style reveal, then exits.

oled_splash.service runs this before pi-dmx. On exit we set persist=True so
luma cleanup does not DISPLAYOFF/clear the panel — avoids a long black gap
until dmx_audio_react reopens SPI and draws the UI.

Timing: LOGO_SECONDS (total); CRT reveal uses reveal_seconds inside the loop.
"""
import io
import os
import sys
import time

LOGO_SECONDS = 3.5  # Total splash (CRT reveal ~2s, then hold); tweak if needed
FPS = 15

try:
    from luma.core.interface.serial import spi
    from luma.oled.device import ssd1322
    from PIL import Image
    import numpy as np
except ImportError as e:
    print(f"[oled_boot] missing deps: {e}", file=sys.stderr)
    sys.exit(0)

# Match dmx_audio_react.py hardware
OLED_SPI_DEV = 1
OLED_RST_PIN = 12
OLED_DC_PIN = 24
OLED_WIDTH = 256
OLED_HEIGHT = 64


# 4x4 Bayer matrix for ordered dithering - classic retro bitmap look
BAYER_4 = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)


def bayer_dither(img):
    """Apply Bayer ordered dither for retro 1-bit look."""
    gray = img.convert("L")
    out = Image.new("RGB", gray.size, (0, 0, 0))
    px = gray.load()
    opx = out.load()
    h, w = gray.size[1], gray.size[0]
    for y in range(h):
        for x in range(w):
            v = px[x, y]
            b = BAYER_4[y % 4][x % 4]
            v_adj = v * 0.5
            thresh = (b + 0.5) / 16.0 * 255
            on = 255 if v_adj >= thresh else 0
            opx[x, y] = (on, on, on)
    return out


def crt_reveal(base_img, t, reveal_seconds=2.0):
    """Reveal logo line by line like a CRT scanning in, over reveal_seconds."""
    h = base_img.size[1]
    progress = min(1.0, t / reveal_seconds)
    reveal_lines = int(progress * h)
    arr = np.array(base_img)
    # Black out lines not yet "scanned"
    if reveal_lines < h:
        arr[reveal_lines:, :, :] = 0
    return Image.fromarray(arr)


def load_logo():
    """Render CSW logo to PIL Image (256x64) with retro dither effect.
    Tries pre-rendered PNG first (no deps), then SVG via cairosvg."""
    assets_dir = os.path.join(os.path.dirname(__file__), "assets")
    png_path = os.path.join(assets_dir, "csw_logo.png")
    svg_path = os.path.join(assets_dir, "csw_logo.svg")

    # 1) Pre-rendered PNG (reliable, no cairosvg needed)
    if os.path.isfile(png_path):
        try:
            img = Image.open(png_path).convert("RGB")
            if img.size == (OLED_WIDTH, OLED_HEIGHT):
                return img
        except Exception:
            pass

    # 2) Render from SVG (requires cairosvg)
    if os.path.isfile(svg_path):
        try:
            import cairosvg
            png_bytes = cairosvg.svg2png(
                url=svg_path,
                output_width=OLED_WIDTH,
                output_height=OLED_HEIGHT,
            )
            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            return bayer_dither(img)
        except Exception:
            pass

    return None


def main():
    try:
        ser = spi(device=OLED_SPI_DEV, port=0, bus_speed_hz=4000000,
                 gpio_DC=OLED_DC_PIN, gpio_RST=OLED_RST_PIN)
        device = ssd1322(ser, width=OLED_WIDTH, height=OLED_HEIGHT, rotate=0)
    except Exception as e:
        print(f"[oled_boot] init failed: {e}", file=sys.stderr)
        sys.exit(0)

    base_img = load_logo()
    if base_img is None:
        base_img = Image.new("RGB", (OLED_WIDTH, OLED_HEIGHT), (0, 0, 0))
        for y in range(OLED_HEIGHT // 2 - 8, OLED_HEIGHT // 2 + 8):
            for x in range(OLED_WIDTH):
                base_img.putpixel((x, y), (128, 128, 128))

    try:
        frame_time = 1.0 / FPS
        t0 = time.monotonic()
        while time.monotonic() - t0 < LOGO_SECONDS:
            t = time.monotonic() - t0
            frame = crt_reveal(base_img, t, reveal_seconds=2.0)
            device.display(frame)
            elapsed = time.monotonic() - t0
            sleep_time = frame_time - (elapsed % frame_time)
            if sleep_time > 0:
                time.sleep(sleep_time)
    except Exception:
        pass

    # Leave the last frame visible while SPI is released. Default cleanup() calls
    # hide()+clear() which blanks the panel until dmx_audio_react re-inits — long gap.
    try:
        device.persist = True
        device.cleanup()
    except Exception:
        pass


if __name__ == "__main__":
    main()
