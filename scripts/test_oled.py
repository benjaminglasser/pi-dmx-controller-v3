#!/usr/bin/env python3
"""
OLED display test for V3 PCB.

SSD1322 256x64 on SPI0 CE0:
  MOSI  GPIO10  (pin 19)
  SCLK  GPIO11  (pin 23)
  CS    GPIO8   (pin 24)  — SPI CE0
  DC    GPIO23  (pin 16)
  RST   GPIO24  (pin 18)

Run:
    python3 scripts/test_oled.py
"""

import time
from luma.core.interface.serial import spi as luma_spi
from luma.core.framebuffer import full_frame
from luma.oled.device import ssd1322
from PIL import Image, ImageDraw, ImageFont

SPI_DEVICE  = 0   # CE0
SPI_PORT    = 0
DC_PIN      = 23
RST_PIN     = 24
WIDTH       = 256
HEIGHT      = 64

print("Initialising OLED (SPI0 CE0, DC=GPIO23, RST=GPIO24)...")
serial = luma_spi(
    device=SPI_DEVICE,
    port=SPI_PORT,
    bus_speed_hz=8_000_000,
    gpio_DC=DC_PIN,
    gpio_RST=RST_PIN,
)
device = ssd1322(serial, width=WIDTH, height=HEIGHT, rotate=0, framebuffer=full_frame())
device.contrast(255)
print("OK — display initialised")

try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 11)
    font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 20)
except Exception:
    font = ImageFont.load_default()
    font_lg = font

# ── Test 1: fill white ──────────────────────────────────────────────────────
print("Test 1: full white fill")
img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
device.display(img)
time.sleep(1)

# ── Test 2: fill black ──────────────────────────────────────────────────────
print("Test 2: full black fill")
img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
device.display(img)
time.sleep(1)

# ── Test 3: checkerboard ────────────────────────────────────────────────────
print("Test 3: checkerboard pattern")
img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
d = ImageDraw.Draw(img)
for x in range(0, WIDTH, 8):
    for y in range(0, HEIGHT, 8):
        if (x // 8 + y // 8) % 2 == 0:
            d.rectangle([x, y, x+7, y+7], fill=(255, 255, 255))
device.display(img)
time.sleep(1.5)

# ── Test 4: text ────────────────────────────────────────────────────────────
print("Test 4: text")
img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
d = ImageDraw.Draw(img)
d.text((4, 2),  "PI-DMX V3  OLED OK", font=font_lg, fill=(255, 255, 255))
d.text((4, 28), "256x64  SSD1322  SPI0 CE0", font=font, fill=(180, 180, 180))
d.text((4, 44), f"DC=GPIO23  RST=GPIO24  CS=GPIO8", font=font, fill=(120, 120, 120))
device.display(img)
time.sleep(2)

# ── Test 5: brightness sweep ────────────────────────────────────────────────
print("Test 5: brightness sweep (dim → bright → dim)")
img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
d = ImageDraw.Draw(img)
d.text((60, 20), "BRIGHTNESS", font=font_lg, fill=(255, 255, 255))
device.display(img)
for v in list(range(0, 256, 8)) + list(range(255, -1, -8)):
    device.contrast(v)
    time.sleep(0.01)
device.contrast(255)

# ── Done ────────────────────────────────────────────────────────────────────
img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
d = ImageDraw.Draw(img)
d.text((4, 18), "  ALL TESTS PASSED", font=font_lg, fill=(255, 255, 255))
device.display(img)
print("\nAll tests passed. Display showing 'ALL TESTS PASSED'.")
print("Ctrl-C or wait 5s to clear and exit.")
time.sleep(5)
device.hide()
