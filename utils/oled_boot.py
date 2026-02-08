#!/usr/bin/env python3
import time, subprocess, os
from io import BytesIO
try:
    import board, busio
    from PIL import Image, ImageDraw, ImageFont
    import adafruit_ssd1305
except Exception:
    raise SystemExit(0)

# Try to import cairosvg for SVG support (optional)
try:
    import cairosvg
    HAS_CAIROSVG = True
except ImportError:
    HAS_CAIROSVG = False

ASSETS_DIR = "/home/benglasser/pi-dmx-controller-v2/assets"
LOGO_SVG = os.path.join(ASSETS_DIR, "csw.svg")
LOGO_JPG = os.path.join(ASSETS_DIR, "logo.jpg")
W, H = 128, 32
HOLD_SECONDS = 5.0   # show logo for 5 seconds before switching

def safe_font(size=8):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except Exception:
        return ImageFont.load_default()

def load_image(path):
    """Load an image file (SVG, JPG, PNG, BMP) and return a PIL Image."""
    if path.endswith('.svg') and HAS_CAIROSVG:
        # Convert SVG to PNG in memory, then load as PIL Image
        png_data = cairosvg.svg2png(url=path, output_width=W, output_height=H)
        return Image.open(BytesIO(png_data)).convert("1")
    else:
        return Image.open(path).convert("1")

def draw_logo(oled):
    """Draw the logo on the OLED. Tries csw.svg first, falls back to logo.jpg."""
    try:
        # Try SVG first if it exists and cairosvg is available
        if os.path.exists(LOGO_SVG) and HAS_CAIROSVG:
            img = load_image(LOGO_SVG)
        elif os.path.exists(LOGO_JPG):
            img = load_image(LOGO_JPG)
        else:
            return False
        
        if (img.width, img.height) != (W, H):
            img = img.resize((W, H))
        oled.image(img)
        oled.show()
        return True
    except Exception:
        return False

def draw_neutral(oled, tick=0):
    image = Image.new("1", (W, H))
    d = ImageDraw.Draw(image)
    font = safe_font(8)
    # Minimal, abstract “Starting” with subtle animated dot
    d.rectangle((0,0,W-1,H-1), outline=1, fill=0)
    d.text((6, 6), "Starting…", font=font, fill=1)
    dot_x = 90 + (tick % 3) * 8
    d.ellipse((dot_x, 7, dot_x+3, 10), outline=1, fill=1)
    oled.image(image); oled.show()

def is_main_active():
    """Return True once the main service is running."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "dmx_audio_react.service"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=0.6
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False

def main():
    t0 = time.time()
    oled = None
    while time.time() - t0 < 3.0:
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            oled = adafruit_ssd1305.SSD1305_I2C(W, H, i2c, addr=0x3D)
            break
        except Exception:
            time.sleep(0.15)
    if oled is None:
        raise SystemExit(0)

    draw_logo(oled)

    # --- Phase 1: show logo briefly, exit early if main app starts
    end = time.time() + HOLD_SECONDS
    while time.time() < end:
        if is_main_active():
            return
        time.sleep(0.1)

    # --- Phase 2: subtle “Starting…” pulse until main app starts (max ~6s)
    start = time.time()
    while (time.time() - start) < 6.0:
        if is_main_active():
            return
        draw_neutral(oled, int((time.time() - start) * 10))
        time.sleep(0.1)

if __name__ == "__main__":
    main()
