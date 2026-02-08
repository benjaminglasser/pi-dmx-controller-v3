#!/usr/bin/env python3
"""Boot splash - shows logo on SPI OLED (SSD1309) for 4 seconds."""
import time
import os
import sys

try:
    from PIL import Image
    from luma.core.interface.serial import spi as luma_spi
    from luma.oled.device import ssd1309
except Exception as e:
    print(f"Import error: {e}", file=sys.stderr)
    raise SystemExit(0)

# SPI OLED pins (same as dmx_audio_react.py)
OLED_SPI_DEV = 1   # CE1 (GPIO 7)
OLED_RST_PIN = 12
OLED_DC_PIN  = 24
OLED_WIDTH   = 128
OLED_HEIGHT  = 64

ASSETS_DIR = "/home/benglasser/pi-dmx-controller-v2/assets"
LOGO_JPG = os.path.join(ASSETS_DIR, "logo.jpg")
LOGO_BMP = os.path.join(ASSETS_DIR, "logo.BMP")
HOLD_SECONDS = 4.0

def main():
    try:
        serial = luma_spi(
            device=OLED_SPI_DEV,
            port=0,
            bus_speed_hz=2000000,
            gpio_DC=OLED_DC_PIN,
            gpio_RST=OLED_RST_PIN,
        )
        oled = ssd1309(serial, width=OLED_WIDTH, height=OLED_HEIGHT, rotate=0)
    except Exception as e:
        print(f"OLED init failed: {e}", file=sys.stderr)
        raise SystemExit(0)

    # Load and display logo
    logo_path = LOGO_JPG if os.path.exists(LOGO_JPG) else LOGO_BMP
    try:
        img = Image.open(logo_path).convert("1")  # 1-bit black/white for OLED
        # Center the 128x32 logo on the 128x64 display
        if img.size != (OLED_WIDTH, OLED_HEIGHT):
            bg = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
            x = (OLED_WIDTH - img.width) // 2
            y = (OLED_HEIGHT - img.height) // 2
            bg.paste(img, (x, y))
            img = bg
        oled.display(img)
        print("Logo displayed successfully")
    except Exception as e:
        import traceback
        print(f"Logo load failed: {e}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(0)

    # Hold for specified time
    time.sleep(HOLD_SECONDS)

if __name__ == "__main__":
    main()
