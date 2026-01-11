#!/usr/bin/env python3
"""
MCP3008 Knob Test Script
Tests all 6 channels and displays live readings.

Run: python tests/mcp3008_test.py
Press Ctrl+C to exit.
"""

import time
import sys

try:
    import spidev
except ImportError:
    print("ERROR: spidev not installed. Run: pip install spidev")
    sys.exit(1)

# MCP3008 on SPI0 CE0
SPI_BUS = 0
SPI_DEV = 0

# Channel labels (matching dmx_audio_react.py)
CHANNEL_LABELS = [
    "CH0: Center Freq",
    "CH1: Q Factor   ",
    "CH2: Threshold  ",
    "CH3: Cycle Steps",
    "CH4: Decay Time ",
    "CH5: Brightness ",
    "CH6: (unused)   ",
    "CH7: (unused)   ",
]

def init_spi():
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEV)
    spi.max_speed_hz = 1350000
    spi.mode = 0
    return spi

def read_channel(spi, channel):
    """Read a single MCP3008 channel (0-7). Returns 0-1023."""
    if channel < 0 or channel > 7:
        return 0
    cmd = [1, (8 + channel) << 4, 0]
    resp = spi.xfer2(cmd)
    value = ((resp[1] & 3) << 8) | resp[2]
    return value

def value_to_bar(value, width=30):
    """Convert 0-1023 to a visual bar."""
    filled = int((value / 1023) * width)
    return "█" * filled + "░" * (width - filled)

def main():
    print("=" * 60)
    print("MCP3008 Knob Test")
    print("=" * 60)
    print("Wiring check:")
    print("  VDD/VREF → 3.3V (Pin 1)")
    print("  AGND/DGND → GND (Pin 6)")
    print("  CLK → BCM11 (Pin 23)")
    print("  MOSI → BCM10 (Pin 19)")
    print("  MISO → BCM9 (Pin 21)")
    print("  CS/CE0 → BCM8 (Pin 24)")
    print("=" * 60)
    print()

    try:
        spi = init_spi()
        print("✓ SPI initialized successfully!")
        print()
    except Exception as e:
        print(f"✗ SPI init failed: {e}")
        print()
        print("Troubleshooting:")
        print("  1. Is SPI enabled? Run: sudo raspi-config → Interface Options → SPI")
        print("  2. Check: ls /dev/spidev0.*  (should show spidev0.0)")
        print("  3. Verify wiring to MCP3008")
        sys.exit(1)

    print("Reading all 8 channels. Turn knobs to see values change.")
    print("Press Ctrl+C to exit.")
    print()

    try:
        while True:
            # Clear screen and move cursor to top
            print("\033[H\033[J", end="")  # ANSI clear screen
            print("MCP3008 Live Readings (Ctrl+C to exit)")
            print("-" * 60)
            
            for ch in range(8):
                value = read_channel(spi, ch)
                percent = (value / 1023) * 100
                bar = value_to_bar(value, width=25)
                label = CHANNEL_LABELS[ch]
                print(f"{label}  {value:4d}  ({percent:5.1f}%)  {bar}")
            
            print("-" * 60)
            print()
            print("Expected behavior:")
            print("  - Knob fully CCW: ~0")
            print("  - Knob fully CW:  ~1023")
            print("  - Unconnected channels: noisy/floating (normal)")
            
            time.sleep(0.1)  # 10 Hz update

    except KeyboardInterrupt:
        print("\n\nExiting...")
    finally:
        spi.close()
        print("SPI closed. Done.")

if __name__ == "__main__":
    main()
