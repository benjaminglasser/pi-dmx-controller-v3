#!/usr/bin/env python3
"""
Interactive encoder + button test for V3 PCB.

Tests all 5 rotary encoders (via MCP23017) and 3 direct GPIO buttons.
Shows each CW/CCW click and button press in real time.

Run:
    python3 scripts/test_encoders.py
"""

import time
import sys
import RPi.GPIO as GPIO
import board
import busio
from digitalio import Direction, Pull
from adafruit_mcp230xx.mcp23017 import MCP23017

# ── Pin map (matches dmx_audio_react.py V3 constants) ──────────────────────
MCP_ADDR = 0x20

ENC1_CLK, ENC1_DT, ENC1_SW = 8, 9, 10
ENC2_CLK, ENC2_DT, ENC2_SW = 11, 12, 13
ENC3_CLK, ENC3_DT, ENC3_SW = 14, 0, 1
ENC4_CLK, ENC4_DT, ENC4_SW = 2, 3, 4
ENC5_CLK, ENC5_DT           = 5, 6
ENC5_SW_GPIO    = 17
RESET_GPIO      = 25
EXTRA_GPIO      = 7

ENCODERS = [
    ("E1", ENC1_CLK, ENC1_DT, ENC1_SW,  None),
    ("E2", ENC2_CLK, ENC2_DT, ENC2_SW,  None),
    ("E3", ENC3_CLK, ENC3_DT, ENC3_SW,  None),
    ("E4", ENC4_CLK, ENC4_DT, ENC4_SW,  None),
    ("E5", ENC5_CLK, ENC5_DT, None,     None),
]

GPIO_BUTTONS = [
    ("E5_SW",  ENC5_SW_GPIO),
    ("RESET",  RESET_GPIO),
    ("EXTRA",  EXTRA_GPIO),
]

# ── Setup ───────────────────────────────────────────────────────────────────
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
for _, pin in GPIO_BUTTONS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

i2c = busio.I2C(board.SCL, board.SDA)
mcp = MCP23017(i2c, address=MCP_ADDR)

all_mcp_pins = [
    ENC1_CLK, ENC1_DT, ENC1_SW,
    ENC2_CLK, ENC2_DT, ENC2_SW,
    ENC3_CLK, ENC3_DT, ENC3_SW,
    ENC4_CLK, ENC4_DT, ENC4_SW,
    ENC5_CLK, ENC5_DT,
]
for idx in all_mcp_pins:
    p = mcp.get_pin(idx)
    p.direction = Direction.INPUT
    p.pull = Pull.UP

# ── State ───────────────────────────────────────────────────────────────────
snapshot = mcp.gpio  # bulk read once

def read(idx):
    return (snapshot >> idx) & 1

enc_state  = {name: (read(clk) << 1) | read(dt) for name, clk, dt, *_ in ENCODERS}
last_sw    = {name: read(sw) for name, _, _, sw, _ in ENCODERS if sw is not None}
last_gpio  = {name: GPIO.input(pin) for name, pin in GPIO_BUTTONS}

click_count = {name: 0 for name, *_ in ENCODERS}

# ── Quadrature lookup (full 4-state table, one click per detent) ────────────
# Returns +1 (CW), -1 (CCW), or 0
_QUAD = {}
# CW sequence: 3→1→0→2→3  (register click at 0→2)
# CCW sequence: 3→2→0→1→3 (register click at 0→1)
_QUAD[(3,1)] = 0; _QUAD[(1,0)] = 0; _QUAD[(0,2)] = 1;  _QUAD[(2,3)] = 0
_QUAD[(3,2)] = 0; _QUAD[(2,0)] = 0; _QUAD[(0,1)] = -1; _QUAD[(1,3)] = 0

def quad(old, new):
    return _QUAD.get((old, new), 0)

# ── Display ─────────────────────────────────────────────────────────────────
print("=" * 52)
print("  Encoder + button interactive test  (Ctrl-C to stop)")
print("=" * 52)
print(f"  {'Control':<12}  {'Event':<14}  Clicks")
print("-" * 52)

def log(name, event, count=""):
    print(f"  {name:<12}  {event:<14}  {count}")
    sys.stdout.flush()

try:
    while True:
        snapshot = mcp.gpio  # one I2C read for all 16 pins

        for name, clk, dt, sw, _ in ENCODERS:
            new = (read(clk) << 1) | read(dt)
            old = enc_state[name]
            if new != old:
                direction = quad(old, new)
                enc_state[name] = new
                if direction == 1:
                    click_count[name] += 1
                    log(name, "CW  ▶", click_count[name])
                elif direction == -1:
                    click_count[name] -= 1
                    log(name, "◀  CCW", click_count[name])

            if sw is not None:
                v = read(sw)
                if v != last_sw[name]:
                    last_sw[name] = v
                    log(name, "PRESSED" if v == 0 else "released")

        for name, pin in GPIO_BUTTONS:
            v = GPIO.input(pin)
            if v != last_gpio[name]:
                last_gpio[name] = v
                log(name, "PRESSED" if v == 0 else "released")

        time.sleep(0.002)  # 2ms poll — fast enough to catch all detents

except KeyboardInterrupt:
    print("\n" + "-" * 52)
    print("Final click counts:")
    for name, *_ in ENCODERS:
        print(f"  {name}: {click_count[name]:+d}")
    GPIO.cleanup()
    print("Done.")
