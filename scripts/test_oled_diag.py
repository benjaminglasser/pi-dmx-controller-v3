#!/usr/bin/env python3
"""
OLED diagnostic script — tests each part of the SPI chain independently.
Run with the project venv:
  /home/pi/pi-dmx-controller-v2/.venv/bin/python3 scripts/test_oled_diag.py
"""
import time, sys
import RPi.GPIO as GPIO
import spidev

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

DC  = 23
RST = 24
CS  = 8   # CE0

GPIO.setup(DC,  GPIO.OUT)
GPIO.setup(RST, GPIO.OUT)
# Note: CS (GPIO8) is NOT set up here — it's a hardware SPI pin, tested in Test 2

# ── Test 1: DC + RST toggle only ───────────────────────────────────────────
print("\n=== Test 1: DC + RST pin toggle (probe with multimeter) ===")
print("Toggling DC(GPIO23) and RST(GPIO24) — each should read 0V / 3.3V")
print("Press Ctrl-C to move on to the next test.\n")
try:
    while True:
        GPIO.output(DC, 1); GPIO.output(RST, 1)
        print("  HIGH — probe pin 14 and pin 15, should read 3.3V"); time.sleep(1)
        GPIO.output(DC, 0); GPIO.output(RST, 0)
        print("  LOW  — probe pin 14 and pin 15, should read 0V");   time.sleep(1)
except KeyboardInterrupt:
    print("\n  Moving on...")
GPIO.output(DC, 0)
GPIO.output(RST, 1)

# ── Test 2: Hardware SPI CE0 verification ──────────────────────────────────
# Release DC from GPIO control first so spidev can use it cleanly
print("\n=== Test 2: Hardware SPI CE0 (GPIO8 / OLED pin 16) ===")
print("Opening spidev0.0...")
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 500_000
spi.mode = 0
print(f"  SPI mode: {spi.mode}, speed: {spi.max_speed_hz} Hz")
print("  Red probe on OLED pin 16 (CS/GPIO8)")
print("  Between transfers: should read 3.3V")
print("  During transfers:  briefly dips, meter may show ~2.5-3.0V")
print("  Steady 0V the whole time = GPIO conflict")
print("  Press Ctrl-C to move on.\n")
try:
    i = 0
    while True:
        GPIO.output(DC, 0)
        spi.xfer2([0xFD, 0x12])
        if i % 20 == 0:
            print(f"  Sent {i} transfers — what does the meter read on pin 16?")
        time.sleep(0.05)
        i += 1
except KeyboardInterrupt:
    print("\n  Moving on...")

# ── Test 3: Bit-bang with manual CS — bypasses hardware SPI entirely ───────
print("\n=== Test 3: Full bit-bang SPI (manual CS on GPIO8) ===")

MOSI = 10
SCLK = 11

GPIO.setup(MOSI, GPIO.OUT)
GPIO.setup(SCLK, GPIO.OUT)
GPIO.output(SCLK, 0)
GPIO.output(CS, 1)

def bb_transfer(dc_val, data):
    GPIO.output(DC, dc_val)
    GPIO.output(CS, 0)
    for byte in data:
        for i in range(7, -1, -1):
            GPIO.output(MOSI, (byte >> i) & 1)
            GPIO.output(SCLK, 1)
            GPIO.output(SCLK, 0)
    GPIO.output(CS, 1)

def cmd(*args): bb_transfer(0, list(args))
def dat(*args): bb_transfer(1, list(args))

# Hardware reset with long pulse
print("  Hard reset (500ms)...")
GPIO.output(RST, 0); time.sleep(0.5)
GPIO.output(RST, 1); time.sleep(0.5)

# Full SSD1322 init
print("  Sending full init sequence via bit-bang...")
cmd(0xFD, 0x12)
cmd(0xAE)
cmd(0xB3, 0x91)
cmd(0xCA, 0x3F)
cmd(0xA2, 0x00)
cmd(0xA1, 0x00)
cmd(0xA0, 0x14, 0x11)
cmd(0xAB, 0x01)
cmd(0xB4, 0xA0, 0xFD)
cmd(0xC1, 0x9F)
cmd(0xC7, 0x0F)
cmd(0xB9)
cmd(0xB1, 0xE2)
cmd(0xD1, 0x82, 0x20)
cmd(0xBB, 0x1F)
cmd(0xB6, 0x08)
cmd(0xBE, 0x07)
cmd(0xA6)
cmd(0xAF)
time.sleep(0.2)

# Fill all white
print("  Filling screen white...")
cmd(0x15, 0x00, 0x77)   # columns 0-119
cmd(0x75, 0x00, 0x3F)   # rows 0-63
cmd(0x5C)               # write RAM
dat(*([0xFF] * (120 * 64 // 2)))

print("  Done — do you see anything on the display?")
time.sleep(3)

# Try alternating pattern
print("  Sending checkerboard pattern...")
cmd(0x15, 0x00, 0x77)
cmd(0x75, 0x00, 0x3F)
cmd(0x5C)
dat(*([0xF0, 0x0F] * (120 * 64 // 4)))

print("  Done — do you see stripes?")
time.sleep(3)

# ── Test 4: Try SPI mode 3 ─────────────────────────────────────────────────
print("\n=== Test 4: Hardware SPI mode 3 (some displays need this) ===")
spi.close()
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 500_000
spi.mode = 3

def cmd_hw(*args):
    GPIO.output(DC, 0)
    spi.xfer2(list(args))

def dat_hw(data):
    GPIO.output(DC, 1)
    spi.xfer2(data)

GPIO.output(RST, 0); time.sleep(0.5)
GPIO.output(RST, 1); time.sleep(0.5)

cmd_hw(0xFD, 0x12); cmd_hw(0xAE)
cmd_hw(0xB3, 0x91); cmd_hw(0xCA, 0x3F)
cmd_hw(0xA2, 0x00); cmd_hw(0xA1, 0x00)
cmd_hw(0xA0, 0x14, 0x11); cmd_hw(0xAB, 0x01)
cmd_hw(0xB4, 0xA0, 0xFD); cmd_hw(0xC1, 0x9F)
cmd_hw(0xC7, 0x0F); cmd_hw(0xB9)
cmd_hw(0xB1, 0xE2); cmd_hw(0xD1, 0x82, 0x20)
cmd_hw(0xBB, 0x1F); cmd_hw(0xB6, 0x08)
cmd_hw(0xBE, 0x07); cmd_hw(0xA6); cmd_hw(0xAF)
time.sleep(0.2)

cmd_hw(0x15, 0x00, 0x77)
cmd_hw(0x75, 0x00, 0x3F)
cmd_hw(0x5C)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
dat_hw([0xFF] * 256)
print("  Mode 3 test done — anything visible?")
time.sleep(3)

spi.close()
GPIO.cleanup()
print("\nAll tests complete.")
