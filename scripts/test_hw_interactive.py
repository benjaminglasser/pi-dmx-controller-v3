#!/usr/bin/env python3
"""
Interactive hardware test: all 5 encoders, encoder switches, 2 reset/extra buttons,
and OLED display. Values update in real time on both the OLED and terminal.

Run:
    /home/pi/pi-dmx-controller-v2/.venv/bin/python3 scripts/test_hw_interactive.py
"""

import sys
import time
import threading

import RPi.GPIO as GPIO
import board
import busio
from digitalio import Direction, Pull
from adafruit_mcp230xx.mcp23017 import MCP23017
from PIL import Image, ImageDraw, ImageFont
from luma.core.interface.serial import spi as luma_spi
from luma.oled.device import ssd1322

# ── Pin map (V3 PCB) ────────────────────────────────────────────────────────
MCP_ADDR = 0x20

ENC1_CLK, ENC1_DT, ENC1_SW = 8,  9,  10
ENC2_CLK, ENC2_DT, ENC2_SW = 11, 12, 13
ENC3_CLK, ENC3_DT, ENC3_SW = 14, 0,  1
ENC4_CLK, ENC4_DT, ENC4_SW = 2,  3,  4
ENC5_CLK, ENC5_DT           = 5,  6

ENC5_SW_GPIO   = 17
RESET_GPIO     = 25
EXTRA_GPIO     = 7

OLED_SPI_DEV = 0
OLED_RST_PIN = 24
OLED_DC_PIN  = 23
OLED_WIDTH   = 256
OLED_HEIGHT  = 64

# encoder name, mcp_clk, mcp_dt, mcp_sw (or None), gpio_sw (or None)
ENCODERS = [
    ("E1", ENC1_CLK, ENC1_DT, ENC1_SW, None),
    ("E2", ENC2_CLK, ENC2_DT, ENC2_SW, None),
    ("E3", ENC3_CLK, ENC3_DT, ENC3_SW, None),
    ("E4", ENC4_CLK, ENC4_DT, ENC4_SW, None),
    ("E5", ENC5_CLK, ENC5_DT, None,    ENC5_SW_GPIO),
]

GPIO_BUTTONS = [
    ("RESET", RESET_GPIO),
    ("EXTRA", EXTRA_GPIO),
]

# ── Quadrature table ─────────────────────────────────────────────────────────
_QUAD = {
    (3, 1):  0, (1, 0):  0, (0, 2):  1, (2, 3): 0,
    (3, 2):  0, (2, 0):  0, (0, 1): -1, (1, 3): 0,
}

def quad(old, new):
    return _QUAD.get((old, new), 0)

# ── State ────────────────────────────────────────────────────────────────────
click_count   = {name: 0    for name, *_ in ENCODERS}
sw_pressed    = {name: False for name, *_ in ENCODERS}
btn_pressed   = {name: False for name, _ in GPIO_BUTTONS}

# last 6 events for OLED log
event_log = []
event_lock = threading.Lock()

def push_event(msg):
    with event_lock:
        event_log.append(msg)
        if len(event_log) > 6:
            event_log.pop(0)

# ── Hardware setup ───────────────────────────────────────────────────────────
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

for _, pin in GPIO_BUTTONS:
    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(ENC5_SW_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)

i2c = busio.I2C(board.SCL, board.SDA)
mcp = MCP23017(i2c, address=MCP_ADDR)

_mcp_pins = [
    ENC1_CLK, ENC1_DT, ENC1_SW,
    ENC2_CLK, ENC2_DT, ENC2_SW,
    ENC3_CLK, ENC3_DT, ENC3_SW,
    ENC4_CLK, ENC4_DT, ENC4_SW,
    ENC5_CLK, ENC5_DT,
]
for idx in _mcp_pins:
    p = mcp.get_pin(idx)
    p.direction = Direction.INPUT
    p.pull = Pull.UP

# ── OLED setup ───────────────────────────────────────────────────────────────
serial = luma_spi(
    device=OLED_SPI_DEV, port=0, bus_speed_hz=4_000_000,
    gpio_DC=OLED_DC_PIN, gpio_RST=OLED_RST_PIN,
)
oled = ssd1322(serial, width=OLED_WIDTH, height=OLED_HEIGHT, rotate=2)

try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 9)
    font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 8)
except Exception:
    font = ImageFont.load_default()
    font_sm = font

# ── OLED renderer ─────────────────────────────────────────────────────────────
def render_oled():
    """Draw current state to OLED: encoder values on left, event log on right."""
    img  = Image.new("RGB", (OLED_WIDTH, OLED_HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Left panel: encoder counts (5 rows) + button states (2 rows)
    WHITE = (255, 255, 255)
    GRAY  = (128, 128, 128)
    GREEN = (80, 255, 80)
    DIM   = (60, 60, 60)

    row_h = 8
    x_left = 1

    # Header
    draw.text((x_left, 0), "ENC  VAL  SW", font=font_sm, fill=GRAY)

    for i, (name, _, _, mcp_sw, gpio_sw) in enumerate(ENCODERS):
        y = 9 + i * row_h
        val  = click_count[name]
        sw   = sw_pressed[name]
        sw_str = "[SW]" if sw else "    "
        fill = WHITE if abs(val) > 0 or sw else DIM
        draw.text((x_left, y), f"{name}  {val:+4d}  {sw_str}", font=font_sm, fill=fill)

    # Buttons row below encoders
    y = 9 + 5 * row_h
    for j, (bname, _) in enumerate(GPIO_BUTTONS):
        pressed = btn_pressed[bname]
        fill = GREEN if pressed else DIM
        draw.text((x_left + j * 52, y), f"{bname}", font=font_sm, fill=fill)

    # Divider
    draw.line([(115, 0), (115, OLED_HEIGHT - 1)], fill=GRAY)

    # Right panel: event log (last 6 events)
    x_right = 118
    with event_lock:
        log_snap = list(event_log)

    for k, entry in enumerate(log_snap):
        draw.text((x_right, k * 9), entry, font=font_sm, fill=WHITE)

    oled.display(img)

# ── Terminal header ──────────────────────────────────────────────────────────
def print_header():
    print("=" * 58)
    print("  Hardware interactive test  (Ctrl-C to quit)")
    print("=" * 58)
    print(f"  {'Control':<10}  {'Event':<16}  {'Count/State'}")
    print("-" * 58)

def log_terminal(name, event, extra=""):
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {name:<10}  {event:<16}  {extra}")
    sys.stdout.flush()

# ── Poll loop ────────────────────────────────────────────────────────────────
snapshot   = mcp.gpio
enc_state  = {name: ((snapshot >> clk) & 1) << 1 | ((snapshot >> dt) & 1)
              for name, clk, dt, *_ in ENCODERS}
last_sw    = {name: (snapshot >> sw) & 1
              for name, _, _, sw, _ in ENCODERS if sw is not None}
last_gpio  = {name: GPIO.input(pin) for name, pin in GPIO_BUTTONS}
last_e5sw  = GPIO.input(ENC5_SW_GPIO)

_oled_dirty = threading.Event()
_oled_dirty.set()

def oled_thread():
    while True:
        _oled_dirty.wait()
        _oled_dirty.clear()
        try:
            render_oled()
        except Exception as e:
            print(f"[oled] render error: {e}", file=sys.stderr)
        time.sleep(0.05)  # cap at ~20fps

threading.Thread(target=oled_thread, daemon=True).start()

print_header()

try:
    while True:
        snapshot = mcp.gpio

        for name, clk, dt, mcp_sw, gpio_sw in ENCODERS:
            new = ((snapshot >> clk) & 1) << 1 | ((snapshot >> dt) & 1)
            old = enc_state[name]
            if new != old:
                d = quad(old, new)
                enc_state[name] = new
                if d == 1:
                    click_count[name] += 1
                    ev = f"CW  ▶  {click_count[name]:+d}"
                    log_terminal(name, "CW ▶", f"count={click_count[name]:+d}")
                    push_event(f"{name} CW {click_count[name]:+d}")
                    _oled_dirty.set()
                elif d == -1:
                    click_count[name] -= 1
                    log_terminal(name, "◀ CCW", f"count={click_count[name]:+d}")
                    push_event(f"{name} CCW {click_count[name]:+d}")
                    _oled_dirty.set()

            if mcp_sw is not None:
                v = (snapshot >> mcp_sw) & 1
                if v != last_sw[name]:
                    last_sw[name] = v
                    pressed = (v == 0)
                    sw_pressed[name] = pressed
                    label = "PRESSED" if pressed else "released"
                    log_terminal(name, f"SW {label}")
                    push_event(f"{name} SW {label}")
                    _oled_dirty.set()

        # E5 switch (direct GPIO)
        v5 = GPIO.input(ENC5_SW_GPIO)
        if v5 != last_e5sw:
            last_e5sw = v5
            pressed = (v5 == 0)
            sw_pressed["E5"] = pressed
            label = "PRESSED" if pressed else "released"
            log_terminal("E5", f"SW {label}")
            push_event(f"E5 SW {label}")
            _oled_dirty.set()

        for name, pin in GPIO_BUTTONS:
            v = GPIO.input(pin)
            if v != last_gpio[name]:
                last_gpio[name] = v
                pressed = (v == 0)
                btn_pressed[name] = pressed
                label = "PRESSED" if pressed else "released"
                log_terminal(name, label)
                push_event(f"{name} {label}")
                _oled_dirty.set()

        time.sleep(0.002)

except KeyboardInterrupt:
    print("\n" + "-" * 58)
    print("Final encoder counts:")
    for name, *_ in ENCODERS:
        print(f"  {name}: {click_count[name]:+d}")
    print("-" * 58)
    oled.clear()
    GPIO.cleanup()
    print("Done.")
