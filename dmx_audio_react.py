#!/usr/bin/env python3
# dmx_audio_react.py (v2: NO OLA) + DEV_NO_HW + Plug&Play Audio + FFT OLED Display
#
# Audio-reactive DMX with optional hardware:
#   - 5 rotary encoders with push buttons via MCP23017 I2C expander (0x20)
#   - SPI OLED display with FFT spectrum (CE0) - EastRising 3.2" SSD1322 256x64
#
# V3 PCB Hardware Wiring:
#   SPI OLED (CE0):
#     RST: GPIO 24  (physical pin 18)
#     DC:  GPIO 23  (physical pin 16)
#     CS:  CE0 / GPIO 8  (physical pin 24)
#
#   MCP23017 I2C expander (addr 0x20):
#     Encoder 1 (Submenu col): CLK=GPB0, DT=GPB1, SW=GPB2
#     Encoder 2 (Param A):     CLK=GPB3, DT=GPB4, SW=GPB5
#     Encoder 3 (Param B):     CLK=GPB6, DT=GPA0, SW=GPA1
#     Encoder 4 (Param C):     CLK=GPA2, DT=GPA3, SW=GPA4
#     Encoder 5 (Brightness):  CLK=GPA5, DT=GPA6
#
#   Direct Pi GPIO:
#     Encoder 5 switch: GPIO 17  (physical pin 11)
#     Reset button:     GPIO 25  (physical pin 22)
#     Extra button:     GPIO 7   (physical pin 26)
#
# DMX backends:
#   DMX_BACKEND=null  -> run without DMX output
#   DMX_BACKEND=uart  -> DMX over UART (/dev/serial0) via RS485 transceiver (pyserial)
#
# DEV mode (skip all hardware except audio + DMX):
#   DEV_NO_HW=1 -> skips SPI/MCP3008, GPIO, OLED
#
# Plug & Play audio device selection:
#   AUDIO_DEVICE=1
#   AUDIO_DEVICE_NAME="USB Audio"

import os, sys, time, math, threading, curses, re, random, subprocess

# Set gpiozero to use RPi.GPIO backend (avoids conflict with RPi.GPIO pin setup)
os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'rpigpio')

from dataclasses import dataclass
from collections import deque

import numpy as np
import sounddevice as sd

# Optional hardware libs (guarded)
try:
    import spidev
except Exception:
    spidev = None

try:
    import RPi.GPIO as GPIO
except Exception:
    GPIO = None

# --- OLED (optional, SPI) ---
_OLED_AVAILABLE = False
try:
    from PIL import Image, ImageDraw, ImageFont
    from luma.core.interface.serial import spi as luma_spi
    from luma.core.framebuffer import full_frame as luma_full_frame
    from luma.oled.device import ssd1322  # EastRising 3.2" uses SSD1322
    _OLED_AVAILABLE = True
except Exception:
    _OLED_AVAILABLE = False

# --- MCP23017 I/O expander (optional, I2C) ---
_MCP_AVAILABLE = False
try:
    import board
    import busio
    from digitalio import Direction, Pull
    from adafruit_mcp230xx.mcp23017 import MCP23017
    _MCP_AVAILABLE = True
except Exception:
    _MCP_AVAILABLE = False


# ===================== Config =====================

DEV_NO_HW = os.environ.get("DEV_NO_HW", "0").strip() == "1"

DMX_BACKEND = os.environ.get("DMX_BACKEND", "uart").strip().lower()  # Default to uart for RS485 transceiver
DMX_UART_DEVICE = os.environ.get("DMX_UART_DEVICE", "/dev/serial0")
DMX_UART_BAUD = 250000  # DMX: 250k 8N2
# Pad past your channel count with zeros. Many dimmer packs (e.g. Chauvet) need ≥128–256+ slots to decode.
DMX_UART_MIN_SLOTS = max(1, int(os.environ.get("DMX_UART_MIN_SLOTS", "256").strip() or "256"))
# DMX break: "ioctl" (UART BREAK) or "baud" (9600-byte trick). Match scripts/dmx_probe.py if one works and the other does not.
DMX_BREAK_STYLE = os.environ.get("DMX_BREAK_STYLE", "ioctl").strip().lower()

UNIVERSE   = 0
DMX_CHANS  = 24  # Max supported channels (actual count controlled by DMX_CHANNEL_COUNT)

# DMX Output Mode: 0=Dimmer, 1=DMX (DMX mode disabled for now)
DMX_OUTPUT_MODE = 0
DMX_OUTPUT_MODES = ["Dimmer", "(DMX)"]  # DMX in parentheses = disabled/not yet implemented

# DMX Channel Count (4-24, affects all preset patterns)
DMX_CHANNEL_COUNT = 4

# Startup defaults (used by "LOW" mode) — tuned for kick body around 80 Hz, classic mode
DEFAULT_CENTER_HZ = 80.0    # 80 Hz: kick body
DEFAULT_Q         = 1.5     # focused but not too narrow
DEFAULT_THRESH    = 0.50    # 50 on 0-99 scale (sits above classic's ~33% steady state, trips on peaks)
DEFAULT_ATTACK_MS = 10.0
DEFAULT_DECAY_MS  = 542.0   # 10 on 0-99 scale ((542-40)/4960*99 = 10.02 → 10)
DEFAULT_BRIGHT    = 0.5

# Defaults modes: LOW, MID, HIGH are built-in presets, USR 1-3 are user-saveable slots
# Each mode has (center_hz, thresh, decay_ms, q, thresh_mode, release_mode)
# Q display mapping: 0 = narrow (Q=8), 99 = wide (Q=0.5), so display 96 ≈ Q=0.74
# thresh_mode: legacy sixth field; always 0 (fixed threshold). release_mode: 0=fixed, 1=react, 2=bright, 3=both, 4=rand
DEFAULTS_MODES = ["LOW", "MID", "HIGH", "USR 1", "USR 2", "USR 3"]
DEFAULTS_PRESETS = {
    #           (center_hz, thresh, decay_ms, q_factor, thresh_mode, release_mode)
    "LOW":   (80.0,   0.50, 542.0, 1.5, 0, 0),    # Kick body ~80Hz, thresh=50 (classic mode default)
    "MID":   (1000.0, 0.41, 542.0, 1.5, 0, 0),    # Mid frequencies ~1kHz, thresh=40
    "HIGH":  (5000.0, 0.25, 542.0, 0.82, 0, 0),   # High frequencies ~5kHz, thresh=25, Q display=90
    "USR 1": (1200.0, 0.40, 542.0, 0.65, 0, 0),   # User preset 1: 1.2kHz, Q display=99
    "USR 2": (1200.0, 0.40, 542.0, 0.65, 0, 0),   # User preset 2: 1.2kHz, Q display=99
    "USR 3": (1200.0, 0.40, 542.0, 0.65, 0, 0),   # User preset 3: 1.2kHz, Q display=99
}

# Config file for persisting settings
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dmx_config")

# Input gain applied to audio (absolute dB). UI shows relative dB with 0 = INPUT_GAIN_REF_DB.
# Reference calibrated for Behringer UFO202 and similar consumer USB interfaces.
# IMPORTANT: INPUT_GAIN_REF_DB anchors the visual "0 dB" in Settings. Bumping the
# underlying default for a calmer chain (e.g. to make FFT visible) does NOT shift
# the user's 0 point — the UI keeps showing 0 dB at whatever default we choose.
# Calibrated for the "old" detector: previous +12 dB reference saturated the
# trigger at 1 on most music, so the reference was lowered by 24 dB. The user's
# preferred "-24 dB display" operating point now corresponds to 0 dB display.
INPUT_GAIN_REF_DB = -12  # 0dB display = -12 dB absolute (calmer, more dynamic for "old" detector)
INPUT_GAIN_MIN_DB = INPUT_GAIN_REF_DB - 24  # absolute floor (display: -24 dB)
INPUT_GAIN_MAX_DB = INPUT_GAIN_REF_DB + 24  # absolute ceiling (display: +24 dB)
INPUT_GAIN_DB = INPUT_GAIN_REF_DB

def load_defaults_mode():
    """Load defaults mode, DMX output mode, channel count, input gain, and any custom preset values from config."""
    global DEFAULTS_PRESETS, DMX_OUTPUT_MODE, DMX_CHANNEL_COUNT, INPUT_GAIN_DB
    mode_idx = 0
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("defaults_mode="):
                        mode_name = line.split("=")[1]
                        if mode_name in DEFAULTS_MODES:
                            mode_idx = DEFAULTS_MODES.index(mode_name)
                    elif line.startswith("dmx_output_mode="):
                        output_mode = line.split("=")[1]
                        if output_mode in DMX_OUTPUT_MODES:
                            DMX_OUTPUT_MODE = DMX_OUTPUT_MODES.index(output_mode)
                    elif line.startswith("dmx_channel_count="):
                        try:
                            count = int(line.split("=")[1])
                            if 4 <= count <= 24:
                                DMX_CHANNEL_COUNT = count
                        except ValueError:
                            pass
                    elif line.startswith("input_gain_db="):
                        try:
                            gain = int(line.split("=")[1])
                            if INPUT_GAIN_MIN_DB <= gain <= INPUT_GAIN_MAX_DB:
                                INPUT_GAIN_DB = gain
                        except ValueError:
                            pass
                    elif "=" in line:
                        # Parse preset override: LOW=120.0,0.40,542.0,2.0,0,0
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            parts = val.split(",")
                            if len(parts) >= 4:
                                base = tuple(float(p) for p in parts[:4])
                                if len(parts) >= 6:
                                    # New format with thresh_mode and release_mode (thresh always fixed: 0)
                                    DEFAULTS_PRESETS[key] = base + (0, int(parts[5]))
                                else:
                                    # Old format - default modes to 0 (fixed)
                                    DEFAULTS_PRESETS[key] = base + (0, 0)
    except Exception:
        pass
    return mode_idx  # Default to LOW (0)

def save_defaults_mode(idx):
    """Save the defaults mode to config file, preserving preset overrides, DMX output mode, channel count, and gain."""
    try:
        mode_name = DEFAULTS_MODES[idx]
        # Read existing preset overrides and DMX settings
        preset_overrides = {}
        dmx_output = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
        channel_count = DMX_CHANNEL_COUNT
        input_gain = INPUT_GAIN_DB
        detect_line = None
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("dmx_output_mode="):
                        dmx_output = line.split("=")[1]
                    elif line.startswith("dmx_channel_count="):
                        try:
                            channel_count = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("input_gain_db="):
                        try:
                            input_gain = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("detect_mode_index="):
                        detect_line = line
                    elif "=" in line and not line.startswith("defaults_mode="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated mode
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={mode_name}\n")
            f.write(f"dmx_output_mode={dmx_output}\n")
            f.write(f"dmx_channel_count={channel_count}\n")
            f.write(f"input_gain_db={input_gain}\n")
            if detect_line:
                f.write(f"{detect_line}\n")
            for key, val in preset_overrides.items():
                f.write(f"{key}={val}\n")
    except Exception:
        pass

def save_dmx_output_mode(mode_idx):
    """Save the DMX output mode to config file, preserving other settings."""
    try:
        output_mode = DMX_OUTPUT_MODES[mode_idx]
        # Read existing config
        defaults_mode = "LOW"
        channel_count = DMX_CHANNEL_COUNT
        input_gain = INPUT_GAIN_DB
        detect_line = None
        preset_overrides = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("defaults_mode="):
                        defaults_mode = line.split("=")[1]
                    elif line.startswith("dmx_channel_count="):
                        try:
                            channel_count = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("input_gain_db="):
                        try:
                            input_gain = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("detect_mode_index="):
                        detect_line = line
                    elif "=" in line and not line.startswith("dmx_output_mode="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated DMX output mode
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={defaults_mode}\n")
            f.write(f"dmx_output_mode={output_mode}\n")
            f.write(f"dmx_channel_count={channel_count}\n")
            f.write(f"input_gain_db={input_gain}\n")
            if detect_line:
                f.write(f"{detect_line}\n")
            for key, val in preset_overrides.items():
                f.write(f"{key}={val}\n")
    except Exception:
        pass

def save_dmx_channel_count(count):
    """Save the DMX channel count to config file, preserving other settings."""
    try:
        # Read existing config
        defaults_mode = "LOW"
        output_mode = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
        input_gain = INPUT_GAIN_DB
        detect_line = None
        preset_overrides = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("defaults_mode="):
                        defaults_mode = line.split("=")[1]
                    elif line.startswith("dmx_output_mode="):
                        output_mode = line.split("=")[1]
                    elif line.startswith("input_gain_db="):
                        try:
                            input_gain = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("detect_mode_index="):
                        detect_line = line
                    elif "=" in line and not line.startswith("dmx_channel_count="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated channel count
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={defaults_mode}\n")
            f.write(f"dmx_output_mode={output_mode}\n")
            f.write(f"dmx_channel_count={count}\n")
            f.write(f"input_gain_db={input_gain}\n")
            if detect_line:
                f.write(f"{detect_line}\n")
            for key, val in preset_overrides.items():
                f.write(f"{key}={val}\n")
    except Exception:
        pass

def save_input_gain(gain_db):
    """Save the input gain (dB) to config file, preserving other settings."""
    try:
        # Read existing config
        defaults_mode = "LOW"
        output_mode = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
        channel_count = DMX_CHANNEL_COUNT
        detect_line = None
        preset_overrides = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("defaults_mode="):
                        defaults_mode = line.split("=")[1]
                    elif line.startswith("dmx_output_mode="):
                        output_mode = line.split("=")[1]
                    elif line.startswith("dmx_channel_count="):
                        try:
                            channel_count = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("detect_mode_index="):
                        detect_line = line
                    elif "=" in line and not line.startswith("input_gain_db="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated input gain
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={defaults_mode}\n")
            f.write(f"dmx_output_mode={output_mode}\n")
            f.write(f"dmx_channel_count={channel_count}\n")
            f.write(f"input_gain_db={gain_db}\n")
            if detect_line:
                f.write(f"{detect_line}\n")
            for key, val in preset_overrides.items():
                f.write(f"{key}={val}\n")
    except Exception:
        pass

def save_preset_values(mode_name, center_hz, thresh, decay_ms, q, thresh_mode, release_mode):
    """Save custom preset values to config file (includes thresh_mode and release_mode)."""
    try:
        # Read existing config
        defaults_mode = "LOW"
        dmx_output = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
        channel_count = DMX_CHANNEL_COUNT
        input_gain = INPUT_GAIN_DB
        detect_line = None
        preset_overrides = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("defaults_mode="):
                        defaults_mode = line.split("=")[1]
                    elif line.startswith("dmx_output_mode="):
                        dmx_output = line.split("=")[1]
                    elif line.startswith("dmx_channel_count="):
                        try:
                            channel_count = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("input_gain_db="):
                        try:
                            input_gain = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("detect_mode_index="):
                        detect_line = line
                    elif "=" in line:
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Update the preset override with all 6 values
        preset_overrides[mode_name] = f"{center_hz},{thresh},{decay_ms},{q},{thresh_mode},{release_mode}"
        # Write back
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={defaults_mode}\n")
            f.write(f"dmx_output_mode={dmx_output}\n")
            f.write(f"dmx_channel_count={channel_count}\n")
            f.write(f"input_gain_db={input_gain}\n")
            if detect_line:
                f.write(f"{detect_line}\n")
            for key, val in preset_overrides.items():
                f.write(f"{key}={val}\n")
    except Exception:
        pass

DEFAULTS_MODE_INDEX = load_defaults_mode()  # Load from config or default to LOW

# Q factor range: frequency-dependent minimum to 8.0 (very narrow)
# Display: 0 = narrow (Q=8), 99 = wide (Q varies for consistent visual width)
# Q_MIN is calculated to give ~55 pixel visual width on the 128px log-scale FFT display
Q_MIN_LOW = 0.1    # Q_MIN at 120Hz (gives ~54px visual width)
Q_MIN_HIGH = 0.65  # Q_MIN for frequencies >= 500Hz (gives ~55px visual width)
Q_MAX = 8.0        # Narrowest (display 0)

def get_q_min(center_hz):
    """Return the minimum Q value (widest bandwidth) for consistent visual width.
    
    Calculated to give ~55 pixel visual width on the 128px log-scale FFT display
    regardless of center frequency. This ensures display 99 looks the same at all frequencies.
    
    Uses logarithmic interpolation between Q_MIN_LOW (at 120Hz) and Q_MIN_HIGH (at 500Hz+).
    """
    import math
    if center_hz >= 500:
        return Q_MIN_HIGH
    elif center_hz <= 120:
        return Q_MIN_LOW
    else:
        # Logarithmic interpolation between 120Hz and 500Hz
        # This gives a smooth transition that maintains ~55px visual width
        t = math.log(center_hz / 120.0) / math.log(500.0 / 120.0)
        return Q_MIN_LOW + t * (Q_MIN_HIGH - Q_MIN_LOW)

THRESH_MIN = 0.0  # Was 0.001, set to 0 for debugging
THRESH_MAX = 1.0
MIN_CENTER_HZ = 80.0
MAX_CENTER_HZ = 12000.0

APP_STATE = "boot"   # "boot" | "loading" | "ready" | "error"
APP_ERROR = ""

# Audio
# Sample rate must match the audio device's native rate when asound.conf is
# `type hw` (no software resampling). HiFiBerry DAC+ ADC defaults to 48000.
# Override via AUDIO_SR env var (e.g. AUDIO_SR=44100 for USB audio interfaces).
SR  = int(os.environ.get("AUDIO_SR", "48000").strip() or "48000")
HOP = int(os.environ.get("AUDIO_HOP", "1024").strip() or "1024")  # Larger default for stability with OLED rendering
_HANNING_WINDOW = None  # Pre-computed, initialized on first use

# Detection / logic
ENV_EMA       = 0.55   # Restored old-project value; smooth envelope without chatter
AGC_ON        = True
AGC_TARGET    = 0.020
# CLASSIC_UI_SCALE divides the AGC-stabilized envelope so the meter has dynamic
# range. AGC pulls the post-EMA value toward AGC_TARGET (=0.02) at steady state;
# with scale=3.0, denom=0.06 → average music sits at ~0.33 on the 0..1 meter and
# transients (kick hits) pop into the 0.6–1.0 range. Lower it (e.g. 1.0) to make
# the meter more "always-on", raise it (e.g. 5.0) for an even more peaky meter.
CLASSIC_UI_SCALE = float(os.environ.get("CLASSIC_UI_SCALE", "3.0"))
# Noise gate (classic mode): if the raw band envelope mean falls below this absolute
# floor, freeze AGC (don't let it pump noise up toward target) and decay the displayed
# envelope toward zero. ~0.0008 ≈ -62 dBFS; tune for your input chain.
NOISE_GATE_RMS = float(os.environ.get("NOISE_GATE_RMS", "0.0008"))
# Cap the AGC's maximum gain (was 20×). 8× ≈ +18 dB still amplifies quiet music,
# but won't pump silence to full scale.
AGC_MAX_GAIN = float(os.environ.get("AGC_MAX_GAIN", "8.0"))
REFRACTORY_MS = 110.0  # Match old project; refractory + edge detect handles double-fires
# Hysteresis margins are no longer used by the new trigger modes (classic/compander/kick).
# The compander/onset path naturally suppresses chatter; refractory + edge detect is enough.
HYST_REARM_MARGIN = 0.0     # Disabled; new modes don't use it
HYST_FIRE_MARGIN = 0.0      # Disabled; new modes don't use it
Q_BAND_SMOOTH_COEF = 0.65   # Restored; FFT path only (display, not trigger)
# Adaptive threshold: hysteresis and smoother floor
ADAPT_REARM_MARGIN = 0.03   # Must fall this far below floor before next trigger can fire
ADAPT_FLOOR_EMA = 0.93      # EMA for floor when env below floor; averaged baseline, not instant
ADAPT_FIRE_MARGIN = 0.015   # Extra rise above adapt_thresh required to fire
WEIGHTING_ON  = False
# INPUT_GAIN_DB / limits defined with CONFIG_FILE (loaded by load_defaults_mode)
BRIGHTNESS    = DEFAULT_BRIGHT

# Which stereo channel feeds the FFT/trigger. HiFiBerry DAC+ADC aux input is on left (ch0).
# Set env AUDIO_INPUT_CHANNEL=left | right | mix  (default left = index 0 for HiFiBerry aux input)
_ai_ch = os.environ.get("AUDIO_INPUT_CHANNEL", "left").strip().lower()
if _ai_ch in ("0", "l", "left", "1l"):
    AUDIO_INPUT_CHANNEL_MODE = "left"
elif _ai_ch in ("mix", "mono", "both", "avg", "stereo"):
    AUDIO_INPUT_CHANNEL_MODE = "mix"
else:
    AUDIO_INPUT_CHANNEL_MODE = "right"

# V3: all encoders are on MCP23017 — no I2S pin conflicts.

def db_to_linear(db):
    """Convert dB to linear gain multiplier."""
    return 10 ** (db / 20.0)

def input_gain_relative_db():
    """UI gain relative to INPUT_GAIN_REF_DB (0 = default calmer level)."""
    return INPUT_GAIN_DB - INPUT_GAIN_REF_DB

def format_input_gain_display():
    """e.g. '0dB' at ref, '+6dB' hotter, '-6dB' quieter."""
    r = input_gain_relative_db()
    if r == 0:
        return "0dB"
    return f"{r:+d}dB"

# Threshold detection: fixed only (adaptive mode and enc3 "Th-Mode" toggle removed).
# THRESH_MODE_INDEX is kept 0 for preset file compatibility (6-tuple format).
THRESH_MODES = ("fixed",)
THRESH_MODE_INDEX = 0
_recent_min = 1.0           # Tracks recent minimum for adaptive mode
_effective_thresh = 0.3     # Effective threshold for display (varies by mode)

# Transient detection state
_q_band_running_avg = 0.1   # Running average for onset ratio calculation
_flux_recent_max = 0.001    # Tracks recent max flux for auto-normalization

# Per-Q-band normalization: auto-scales the targeted frequency range independently
# so that frequency-specific triggers are consistent regardless of overall mix level.
_q_band_recent_max = 0.15   # Running max for the Q-band (decays slowly)
_q_band_recent_min = 0.01   # Running floor for the Q-band
Q_BAND_NORM_DECAY = 0.994   # How fast the Q-band normalizer forgets peaks (faster decay = more dynamic)
Q_BAND_NORM_ATTACK = 0.5    # How fast to jump to new peaks (faster = more responsive)
Q_BAND_NORM_FLOOR_DECAY = 0.998  # Floor decay (faster = tighter floor tracking)
Q_BAND_NORM_ENABLED = os.environ.get("Q_BAND_NORM", "0").strip() != "0"  # Disabled by default; FFT path no longer drives trigger

# Release modes
RELEASE_MODES = ["fixed", "react", "bright", "both", "rand"]
RELEASE_MODE_INDEX = 0  # Default to fixed (current behavior)
_reactive_brightness_scale = 1.0  # For bright mode: scales brightness by level above threshold
_effective_release_display = 40  # For displaying reactive release values (in ms)
_effective_brightness_display = 50  # For displaying reactive brightness (0-99, 50 = default)
_brightness_knob_last_turn = 0.0  # Timestamp of last brightness knob turn
_release_knob_last_turn = 0.0  # Timestamp of last release knob turn
REACTIVE_BUFFER_SECONDS = 2.0  # Seconds to wait after knob turn before reactivity kicks in
_trigger_speed_multiplier = 1.0  # 0.3 to 2.0, fast = lower, slow = higher
TRIGGER_SPEED_FAST_MS = 200.0  # Triggers faster than this = min multiplier (0.3x)
TRIGGER_SPEED_SLOW_MS = 1000.0  # Triggers slower than this = max multiplier (2.0x)
TRIGGER_SPEED_MIN_MULT = 0.3  # Minimum multiplier for fast triggers (dampens effect)
TRIGGER_SPEED_MAX_MULT = 2.0  # Maximum multiplier for slow triggers

# "react" release: time between triggers maps directly to release length (uses same ms
# breakpoints as TRIGGER_SPEED_*). Tight spacing → shorter than knob; sparse → longer.
REACT_SPACING_RELEASE_MIN_MULT = 0.22  # at/under TRIGGER_SPEED_FAST_MS interval
REACT_SPACING_RELEASE_MAX_MULT = 5.2  # mult reached at TRIGGER_SPEED_SLOW_MS (then idle ramp)
# Between fast/slow: t=(ts-fast)/(slow-fast) is curved so mid/long gaps grow release faster.
REACT_SPACING_CURVE_POWER = 1.65
# Beyond slow ms, release multiplier keeps climbing (dramatic sparse hits / long silence).
REACT_SPACING_GAP_EXTRA_PER_MS = 0.00105  # +~1.0 mult per ~950 ms past slow breakpoint
REACT_SPACING_GAP_EXTRA_MAX = 3.8  # cap on top of REACT_SPACING_RELEASE_MAX_MULT
REACT_SPACING_AMP_SCALE_MAX = 1.4  # small extra from hit strength (on top of spacing)

# Reactive "bright" / "both" brightness: extra drive when signal is far above threshold (0..1 excess)
BRIGHT_THRESHOLD_EXCESS_MAX_DELTA = 0.46  # added to BRIGHTNESS when drive==1 (before cap at 1.0)
# Lower threshold (smaller ref) → higher gain; deeper overshoot → stronger curve toward 1.0
BRIGHT_EXCESS_REF_PIVOT = 0.44
BRIGHT_EXCESS_SENS_POWER = 1.05
BRIGHT_EXCESS_SENS_CAP = 4.25
BRIGHT_EXCESS_REF_MIN = 0.03  # floor for sensitivity divisor only (stability at very low thresh)
# shaped_rel = 1 - (1-rel)^curve  rewards being well above the line vs barely over
BRIGHT_EXCESS_CURVE = 2.15

# Trigger detection modes (each is a separate algorithm; FFT spectrum display is unaffected).
# - "classic":   biquad bandpass + envelope + AGC + outer EMA (faithful old-project chain)
# - "compander": biquad + envelope + adaptive compander (RMS floor / peak ceiling) + onset boost
# - "kick":      cascaded biquads + faster envelope + decay-only floor + slope-weighted + peak-hold meter
DETECT_MODES = ["classic", "compander", "kick", "old"]

# Detection mode is locked to "old". The other entries above are kept so
# the trigger functions and any references to DETECT_MODES still resolve, but
# the index is fixed and is not loaded from or saved to .dmx_config. The
# Settings UI no longer exposes a way to switch modes.
DETECT_MODE_INDEX = DETECT_MODES.index("old")

# Beat detection method: 0 = FFT_STANDARD (Q-band analysis)
# 3-band mode has been removed - now using FFT-only mode
BEAT_DETECT_METHOD = 0

# 3-band detector update rate (kept for legacy code compatibility)
_last_3band_update = 0.0
THREEBAND_UPDATE_HZ = 50

# Program state
PROGRAM      = 1
BASE_PROGRAM = 1

RUNNING      = True
STOP_THREADS = False

# When > time.time(), knob readings are ignored
IGNORE_KNOBS_UNTIL = 0.0

# Preferred device name patterns — HiFiBerry first since that's the installed HAT
PREFERRED_INPUTS = [
    r"hifiberry",
    r"snd_rpi_hifiberry",
    r"scarlett",
    r"focusrite",
    r"usb audio",
    r"wm8960",
    r"codec",
    r"line",
    r"pulse",
]


def _looks_like_placeholder_capture(name: str) -> bool:
    """True for snd_dummy / silent test devices — never preferred over USB."""
    nl = (name or "").lower()
    if not nl.strip():
        return True
    for s in ("dummy", "discard", ": null"):
        if s in nl:
            return True
    return False


def _capture_devices_with_preferences(devs, *, prefer_real: bool):
    """Enumerate PortAudio indices with usable input channels, optionally skipping placeholders."""
    out = []
    for i, d in enumerate(devs):
        inch = int(d.get("max_input_channels", 0) or 0)
        if inch < 1:
            continue
        nm = d.get("name") or ""
        if prefer_real and _looks_like_placeholder_capture(nm):
            continue
        out.append((i, d))
    return out


# MCP23017 I2C address (A0/A1/A2 all tied to GND)
MCP23017_ADDRESS = 0x20

# MCP23017 pin indices: GPA0-GPA7 = 0-7, GPB0-GPB7 = 8-15
# GPA7 (7) and GPB7 (15) are intentionally unused per hardware spec.

# Rotary Encoder 1 (Page selection) — MCP23017
ENC1_CLK = 8   # GPB0
ENC1_DT  = 9   # GPB1
ENC1_SW  = 10  # GPB2

# Rotary Encoder 2 - Param A (Freq/Speed/Preset) — MCP23017
ENC2_CLK = 11  # GPB3
ENC2_DT  = 12  # GPB4
ENC2_SW  = 13  # GPB5

# Rotary Encoder 3 - Param B (Thresh/Beats) — MCP23017
ENC3_CLK = 14  # GPB6
ENC3_DT  = 0   # GPA0
ENC3_SW  = 1   # GPA1

# Rotary Encoder 4 - Param C (Release/Mode) — MCP23017
ENC4_CLK = 2   # GPA2
ENC4_DT  = 3   # GPA3
ENC4_SW  = 4   # GPA4

# Rotary Encoder 5 - Brightness (global) — MCP23017 A/B, direct GPIO switch
ENC5_CLK     = 5   # GPA5
ENC5_DT      = 6   # GPA6
ENC5_SW_GPIO = 17  # Direct Pi BCM GPIO17 (physical pin 11)

# Direct Pi GPIO buttons
RESET_BUTTON_GPIO = 25  # BCM GPIO25 (physical pin 22)
EXTRA_BUTTON_GPIO = 7   # BCM GPIO7  (physical pin 26)

# SPI OLED pins (EastRising 3.2" SSD1322 on CE0)
OLED_SPI_DEV = 0   # CE0 (GPIO8, physical pin 24)
OLED_RST_PIN = 24  # BCM GPIO24 (physical pin 18)
OLED_DC_PIN  = 23  # BCM GPIO23 (physical pin 16)
OLED_WIDTH   = 256
OLED_HEIGHT  = 64

# OLED colors (SSD1322 uses RGB mode for grayscale)
OLED_WHITE = (255, 255, 255)
OLED_BLACK = (0, 0, 0)
OLED_GRAY = (128, 128, 128)

# ===================== Submenu System =====================
# New UI: Top-left = FFT, Top-right = submenu tabs, Bottom = HOME controls

# Submenu tabs (cycled by reset button)
SUBMENU_TABS = ["Presets", "Settings", "Setup"]
submenu_tab = 0        # 0=PRE, 1=SET, 2=SETUP
submenu_column = 0     # Column index; max depends on tab (Presets: 0–2; Settings: 0–2; Setup: 0–1)
submenu_editing = False  # Label of selected column is always highlighted

# Submenu column labels for each tab
SUBMENU_LABELS = {
    "Presets": ["Preset", "Mode", "Beats"],
    "Settings": ["Gain", "Defaults", ""],          # 3rd column blank (Detect mode locked to "old")
    "Setup": ["Output", "Chans", ""],              # Output=Dimmer/DMX, Chans=4-24; col 3 blank (layout)
}

# HOME controls (always visible on bottom half)
# Encoder toggle states for HOME controls
_home_enc2_alt = False  # False = Freq, True = Q
# Integer 0–99 for Range (Q) mode only; None = sync from band.q on next turn. Avoids float round-trip
# where two encoder steps re-map to the same rounded display value.
_home_enc2_range_pct = None
_home_enc4_alt = False  # False = Release, True = ReleaseMode

# Encoder toggle states for Settings submenu tab
_setup_enc3_detect = False    # Deprecated: Detect-mode toggle removed; mode is locked to "old". Kept for any stale references.
_setup_enc4_channels = False  # False = DMX Output Mode, True = Channel Count

# Legacy compatibility - keep current_page and get_pages() for remaining references
current_page = 0
_LEGACY_PAGES = ["HOME"]  # Only HOME page now - other pages are in submenu

def get_pages():
    """Legacy function for compatibility - always returns HOME page."""
    return _LEGACY_PAGES

# Legacy page labels (kept for _draw_pot_values compatibility)
PAGE_POT_LABELS = {
    "HOME": ["Freq", "Thresh", "Rels"],
}

# Legacy page icons (kept for compatibility)
PAGE_ICONS = {
    "HOME": [
        (4, 0), (3, 1), (5, 1), (2, 2), (6, 2), (1, 3), (7, 3),
        (0, 4), (8, 4), (1, 4), (2, 4), (3, 4), (4, 4), (5, 4), (6, 4), (7, 4),
        (2, 5), (2, 6), (2, 7), (2, 8), (6, 5), (6, 6), (6, 7), (6, 8),
        (2, 8), (3, 8), (4, 8), (5, 8), (6, 8), (4, 6), (4, 7), (4, 8),
    ],
}

_MAX_PAGES = 1  # Legacy constant

# Program names for display
# Program 1: ALL - all channels trigger together
# Program 2: CHASE - sequential single channel cycling through all channels
# Program 3: GROUPS - first half of channels alternate with second half
# Program 4: SWAP - odd channels (1,3,5...) alternate with even (2,4,6...)
# Program 5: RANDOM - random channel each trigger
# Program 6: AMBIENT - non-audio-reactive random fading
PROGRAM_NAMES = ["ALL", "CHASE", "GROUPS", "SWAP", "RANDOM", "AMBIENT"]

# ===================== FFT Display =====================

# FFT settings - 64 bands, 20Hz–16kHz (EQ-style spectrum; set FFT_ANALYZER_RAW=1 for unsmoothed per-hop bars)
FFT_MIN_FREQ = 20
FFT_MAX_FREQ = 16000
FFT_NUM_BANDS = 64
FFT_SIZE = 1024  # Zero-pad to 1024 for ~43Hz resolution (tradeoff: faster FFT vs freq precision)

# Optional DSP on the FFT path (defaults off for “normal” full-range analyzer like EQ Eight spectrum)
FFT_HPF_HZ = float(os.environ.get("FFT_HPF_HZ", "0"))  # 0 = off; set e.g. 40 to cut sub rumble
FFT_SPECTRAL_FLOOR = float(os.environ.get("FFT_SPECTRAL_FLOOR", "0"))  # After auto-scale; 0 = no squashing
FFT_FLUX_DEADZONE = float(os.environ.get("FFT_FLUX_DEADZONE", "0"))  # 0 = raw spectral flux
FFT_HYBRID_MIN_LEVEL = float(os.environ.get("FFT_HYBRID_MIN_LEVEL", "0.07"))  # Hybrid detect mode only
# Dashed horizontal threshold ruler inside the FFT Q band (visual only). Default off — env meter shows level vs thresh.
FFT_SPECTRUM_THRESH_LINE = os.environ.get("FFT_SPECTRUM_THRESH_LINE", "0").strip() == "1"

def apply_fft_highpass(x, sr, hz, y_prev, x_prev):
    """Stateful first-order high-pass (RC) across callbacks. hz<=0 returns x unchanged."""
    if hz <= 0.0 or x is None or len(x) == 0:
        return x, y_prev, x_prev
    rc = 1.0 / (2.0 * math.pi * max(hz, 1.0))
    dt = 1.0 / sr
    alpha = rc / (rc + dt)
    y = float(y_prev)
    xp = float(x_prev)
    out = np.empty(x.shape[0], dtype=np.float32)
    xv = np.asarray(x, dtype=np.float32)
    n = int(xv.shape[0])
    for i in range(n):
        xi = float(xv[i])
        y = alpha * (y + xi - xp)
        out[i] = y
        xp = xi
    return out, y, xp

def generate_log_bands(num_bands, min_freq, max_freq):
    """Generate logarithmically spaced frequency bands with low-end compression.
    
    Uses a warped scale that compresses the sub-100Hz range to give more
    visual weight to the musically important 100Hz-10kHz range.
    """
    bands = []
    
    # Warp factor: lower = closer to pure log (cleaner, more “EQ analyzer” spread); higher compresses lows
    # 0.0 = pure log; ~0.12 = mild low-end emphasis without heavy bunching
    warp = 0.12
    
    for i in range(num_bands):
        # Linear position 0-1
        t_lo = i / num_bands
        t_hi = (i + 1) / num_bands
        
        # Apply warping: shift weight toward higher frequencies
        # This uses a power curve that compresses the low end
        t_lo_warped = t_lo ** (1.0 - warp)
        t_hi_warped = t_hi ** (1.0 - warp)
        
        # Map warped position to log frequency
        log_min = math.log10(min_freq)
        log_max = math.log10(max_freq)
        low = 10 ** (log_min + t_lo_warped * (log_max - log_min))
        high = 10 ** (log_min + t_hi_warped * (log_max - log_min))
        bands.append((low, high))
    return bands

FFT_BANDS = generate_log_bands(FFT_NUM_BANDS, FFT_MIN_FREQ, FFT_MAX_FREQ)

# Frequency compensation curve
def calculate_freq_compensation():
    compensation = []
    for low, high in FFT_BANDS:
        center = math.sqrt(low * high)
        ref_freq = 800.0
        octaves_from_ref = math.log2(center / ref_freq)
        db_adjustment = octaves_from_ref * 5.0
        gain = 10 ** (db_adjustment / 20.0)
        gain = max(0.15, min(6.0, gain))
        compensation.append(gain)
    return compensation

FFT_COMPENSATION = calculate_freq_compensation()

# Pre-compute FFT bin indices for each band (PERFORMANCE OPTIMIZATION)
# This avoids creating numpy masks on every audio callback
_FFT_BIN_INDICES = None  # Will be computed on first use with actual freqs array
_FFT_BIN_STARTS = None   # Numpy array of start indices for vectorized computation
_FFT_BIN_ENDS = None     # Numpy array of end indices
_FFT_BIN_COUNTS = None   # Numpy array of bin counts per band

def _precompute_bin_indices(freqs):
    """Pre-compute start/end bin indices for each FFT band."""
    global _FFT_BIN_INDICES, _FFT_BIN_STARTS, _FFT_BIN_ENDS, _FFT_BIN_COUNTS
    indices = []
    starts = []
    ends = []
    for low_hz, high_hz in FFT_BANDS:
        start_idx = np.searchsorted(freqs, low_hz)
        end_idx = np.searchsorted(freqs, high_hz)
        if end_idx <= start_idx:
            end_idx = start_idx + 1
        end_idx = min(end_idx, len(freqs))
        indices.append((start_idx, end_idx))
        starts.append(start_idx)
        ends.append(end_idx)
    _FFT_BIN_INDICES = indices
    _FFT_BIN_STARTS = np.array(starts, dtype=np.int32)
    _FFT_BIN_ENDS = np.array(ends, dtype=np.int32)
    _FFT_BIN_COUNTS = np.maximum(1, _FFT_BIN_ENDS - _FFT_BIN_STARTS).astype(np.float32)
    return indices

def get_band_energy_fast(fft_magnitudes, band_idx):
    """Fast band energy using pre-computed bin indices."""
    start, end = _FFT_BIN_INDICES[band_idx]
    if end > start:
        return float(np.mean(fft_magnitudes[start:end]))
    return float(fft_magnitudes[start]) if start < len(fft_magnitudes) else 0.0

def get_all_band_energies_vectorized(fft_mag):
    """Compute all band energies in one vectorized pass using cumsum."""
    cumsum = np.zeros(len(fft_mag) + 1, dtype=np.float64)
    cumsum[1:] = np.cumsum(fft_mag)
    band_sums = cumsum[_FFT_BIN_ENDS] - cumsum[_FFT_BIN_STARTS]
    return (band_sums / _FFT_BIN_COUNTS).astype(np.float32)

# Visual gain for FFT display (makes bars appear taller within fixed view)
FFT_VISUAL_GAIN = 2.5  # Boost bar height for HiFiBerry line-level input (-18dBFS typical)

# FFT state (numpy arrays for faster vectorized operations)
fft_bands = np.zeros(len(FFT_BANDS), dtype=np.float32)
fft_peaks = np.zeros(len(FFT_BANDS), dtype=np.float32)
fft_peak_times = np.zeros(len(FFT_BANDS), dtype=np.float64)
PEAK_HOLD_TIME = 0.4
fft_recent_max = 0.3
fft_max_decay = 0.995  # Classic analyzer: smooth level changes, stable overall scale
# Cap per-hop rise of the auto-scale max — stops one loud band from instantly rescaling everything (low-end “glitch”)
FFT_SCALE_MAX_JUMP_RATIO = 1.32  # looser = scale catches loud hits faster (better short kicks)
FFT_SCALE_MAX_JUMP_ABS = 0.018
# When the whole spectrum drops a lot (kick gone), pull the scale down faster so lows don’t “stutter” against a stale peak
FFT_SCALE_COLLAPSE_RATIO = 0.48   # if current_max < this * fft_recent_max, accelerate scale down
FFT_SCALE_COLLAPSE_PULL = 0.984   # multiply stale peak each hop while collapsed (higher = gentler)
FFT_SCALE_COLLAPSE_FLOOR_MUL = 1.07  # don’t sit below current_max * this (headroom)
# Faster bar decay on the lowest columns only (same smoothing as mids on attack)
FFT_LOW_DECAY_BANDS = 4
FFT_LOW_DECAY_MULT = 0.86   # per hop when falling; mids use 0.92 below
# Optional extra EMA on the lowest N bands. Default 0: same attack/decay as rest of spectrum
# (old default 4 @ 0.62 made <~100Hz columns fall much slower). Set FFT_LOW_BAND_COUNT=4 to restore.
FFT_LOW_BAND_COUNT = int(os.environ.get("FFT_LOW_BAND_COUNT", "0"))
FFT_LOW_SMOOTH_COEF = float(os.environ.get("FFT_LOW_SMOOTH_COEF", "0.62"))

# Display smoothing: EMA applied to fft_bands → fft_display_bands for drawing only (no effect on triggers).
# 0.70 = smoother, less choppy; 0.85 = snappier; 1.0 = no smoothing (draw raw fft_bands).
FFT_DISPLAY_SMOOTH = float(os.environ.get("FFT_DISPLAY_SMOOTH", "0.72"))
# Spatial smoothing: 3-point [0.25,0.5,0.25] kernel across adjacent bands for a softer curve. 0=off, 1=on.
FFT_SPATIAL_SMOOTH = int(os.environ.get("FFT_SPATIAL_SMOOTH", "1"))
# 1 = per-hop FFT levels only (no bar inertia / display EMA / spatial blur — looks jittery; for diagnostics).
# Default 0 = EQ-style analyzer (usable spectrum). Override in env for experiments.
FFT_ANALYZER_RAW = int(os.environ.get("FFT_ANALYZER_RAW", "0"))
if FFT_ANALYZER_RAW:
    FFT_DISPLAY_SMOOTH = 1.0
    FFT_SPATIAL_SMOOTH = 0

# High-pass state (rumble / DC)
_fft_hp_y = 0.0
_fft_hp_x_prev = 0.0

# Spectral flux state for onset/transient detection
prev_band_energies = np.zeros(len(FFT_BANDS), dtype=np.float32)
fft_flux = np.zeros(len(FFT_BANDS), dtype=np.float32)
_fft_low_smooth = np.zeros(len(FFT_BANDS), dtype=np.float32)

# Display-only smoothed bands (EMA + optional spatial); triggers use raw fft_bands
fft_display_bands = np.zeros(len(FFT_BANDS), dtype=np.float32)

# Per-band normalization state (spectral whitening)
band_running_mean = [0.01] * len(FFT_BANDS)
band_running_max = [0.1] * len(FFT_BANDS)
BAND_NORM_DECAY = 0.995

def get_spectral_flux(current_energies, prev_energies):
    """Calculate half-wave rectified spectral flux per band (vectorized)."""
    return np.maximum(0.0, current_energies - prev_energies)

def _smooth_fft_display_bands(bands):
    """Apply 3-point [0.25, 0.5, 0.25] kernel for softer band-to-band curve.
    Returns a new array; leaves bands unchanged."""
    if not FFT_SPATIAL_SMOOTH or len(bands) < 2:
        return bands
    out = np.empty_like(bands)
    out[0] = 0.75 * bands[0] + 0.25 * bands[1]
    for i in range(1, len(bands) - 1):
        out[i] = 0.25 * bands[i - 1] + 0.5 * bands[i] + 0.25 * bands[i + 1]
    out[-1] = 0.25 * bands[-2] + 0.75 * bands[-1]
    return out

def normalize_band(energy, band_idx):
    """Normalize band energy relative to its own history (spectral whitening).
    This compensates for the natural spectral slope of music."""
    global band_running_mean, band_running_max
    
    # Update running statistics
    band_running_mean[band_idx] = (BAND_NORM_DECAY * band_running_mean[band_idx] + 
                                    (1 - BAND_NORM_DECAY) * energy)
    if energy > band_running_max[band_idx]:
        band_running_max[band_idx] = energy
    else:
        band_running_max[band_idx] *= BAND_NORM_DECAY
    
    # Normalize: subtract mean, divide by range
    range_val = band_running_max[band_idx] - band_running_mean[band_idx] + 1e-10
    normalized = (energy - band_running_mean[band_idx]) / range_val
    return max(0, min(1, normalized))

def get_band_energy_with_q(fft_mag, freqs, center_hz, q):
    """Get energy using a Q-shaped Gaussian frequency response curve.
    This creates a smooth bell-curve response centered on the target frequency,
    with Q controlling the width for better frequency isolation."""
    bandwidth = center_hz / max(0.1, q)
    sigma = bandwidth / 2.355  # Convert FWHM to Gaussian sigma
    
    # Create Gaussian weighting centered on target frequency
    weights = np.exp(-0.5 * ((freqs - center_hz) / sigma) ** 2)
    
    # Weighted sum of FFT magnitudes
    weight_sum = np.sum(weights) + 1e-10
    energy = np.sum(fft_mag * weights) / weight_sum
    return float(energy)

def get_envelope_times(center_hz):
    """Return (attack_ms, release_ms) based on frequency.
    Low frequencies use fast attack to capture kick transients (2-5ms),
    while highs need even faster response."""
    if center_hz < 200:      # Lows
        return (3.0, 60.0)   # Fast attack for kick transients, moderate release
    elif center_hz < 2000:   # Mids
        return (8.0, 80.0)    # Default
    else:                    # Highs
        return (4.0, 50.0)    # Fast attack, quick release

def format_release_display(ms_value):
    """Format release time for display. Shows seconds with 1 decimal for values >= 1000ms."""
    if ms_value >= 1000:
        return f"{ms_value / 1000.0:.1f}s"
    return f"{int(ms_value)}ms"


def react_release_scale_from_interval_ms(time_since_last_ms, boost_amount):
    """Scale factor for 'react' / 'both' release from trigger spacing and hit level.

    Rapid retriggers (<= TRIGGER_SPEED_FAST_MS) use REACT_SPACING_RELEASE_MIN_MULT.
    Between fast and slow breakpoints, spacing maps with a convex curve (longer gaps
    push release up faster). At/above slow, base is REACT_SPACING_RELEASE_MAX_MULT and
    keeps rising with extra silence up to REACT_SPACING_GAP_EXTRA_MAX.
    ``boost_amount`` (0..1) adds a modest extra so loud hits are slightly longer."""
    ts = time_since_last_ms
    slow = TRIGGER_SPEED_SLOW_MS
    fast = TRIGGER_SPEED_FAST_MS
    lo = REACT_SPACING_RELEASE_MIN_MULT
    hi = REACT_SPACING_RELEASE_MAX_MULT
    if ts <= fast:
        spacing_mult = lo
    elif ts >= slow:
        spacing_mult = hi + min(
            REACT_SPACING_GAP_EXTRA_MAX,
            max(0.0, ts - slow) * REACT_SPACING_GAP_EXTRA_PER_MS,
        )
    else:
        speed_range = slow - fast
        t = (ts - fast) / speed_range
        t_ease = max(0.0, min(1.0, t)) ** REACT_SPACING_CURVE_POWER
        spacing_mult = lo + t_ease * (hi - lo)
    amp_scale = 1.0 + min(
        REACT_SPACING_AMP_SCALE_MAX - 1.0,
        max(0.0, boost_amount) * (REACT_SPACING_AMP_SCALE_MAX - 1.0),
    )
    return spacing_mult * amp_scale


def _bright_reactive_drive(ref, sig):
    """Map (threshold ref, signal) → 0..1 drive for reactive brightness.

    Uses overshoot ``sig - ref`` relative to headroom ``1 - ref``, then:
    - convex curve so moderate/large overshoots push harder than tiny ones;
    - sensitivity ``~(pivot/ref)^power`` (capped) so a *lower* threshold yields a *bigger*
      brightness jump for the same peak level."""
    ref_fire = min(0.999, max(0.0, float(ref)))
    sig_f = float(sig)
    excess = max(0.0, sig_f - ref_fire)
    headroom = max(1e-6, 1.0 - ref_fire)
    rel = min(1.0, excess / headroom)
    r = min(1.0, max(0.0, rel))
    shaped = 1.0 - (1.0 - r) ** BRIGHT_EXCESS_CURVE
    ref_sens = max(BRIGHT_EXCESS_REF_MIN, min(0.99, ref_fire))
    sens = min(
        BRIGHT_EXCESS_SENS_CAP,
        (BRIGHT_EXCESS_REF_PIVOT / ref_sens) ** BRIGHT_EXCESS_SENS_POWER,
    )
    return min(1.0, max(0.0, shaped * sens))


def bright_excess_normalized(live_band_env, thresh_mode, band, effective_thresh_display):
    """0..1 drive for reactive brightness from how far signal sits above the active threshold.

    Lower threshold (smaller ref) increases gain; stronger peaks past the line push harder."""
    global three_band_detector
    if BEAT_DETECT_METHOD == 1 and three_band_detector is not None:
        bi = THREEBAND_SELECTED
        bc = three_band_detector.bands[bi]
        ref = float(bc.trigger_thresh)
        sig = float(three_band_detector.normalized_slope[bi])
        return _bright_reactive_drive(ref, sig)
    if thresh_mode == "fixed":
        ref = float(band.thresh + HYST_FIRE_MARGIN)
        return _bright_reactive_drive(ref, live_band_env)
    ref = float(effective_thresh_display)
    return _bright_reactive_drive(ref, live_band_env)


# ===================== Encoder / Pot State =====================

encoder1_value = 0
encoder1_button = False
_reset_last_state = 1  # Reset button state (1 = not pressed)
_reset_press_time = 0  # Timestamp when reset button was pressed (for long-press detection)
_fft_reset_msg_until = 0.0  # Show centered "reset!" modal until this timestamp
_extra_last_state = 1  # Extra button (GPIO7) state (1 = not pressed)

# Encoder state for all 5 encoders
# Encoders 1-4: Page, Param A, Param B, Param C (indices 0-3)
# Encoder 5: Brightness (index 4)
_enc_last_clk = [None, None, None, None, None]
_enc_last_dt = [None, None, None, None, None]  # Track DT state too for quadrature
_enc_last_sw = [1, 1, 1, 1, 1]  # Switch states (1 = not pressed)
_enc_awaiting_release = [False, False, False, False, False]  # Non-blocking button release tracking
# Encoder deltas - accumulated since last update call (legacy, _apply_encoder_delta now used)
_enc_delta = [0, 0, 0, 0, 0]

# Quadrature state machine for reliable direction detection
# State is encoded as (CLK << 1) | DT, giving values 0-3
# Valid transitions: 0->1->3->2->0 (CW) or 0->2->3->1->0 (CCW)
_enc_state = [3, 3, 3, 3, 3]  # Current state per encoder, 3 = rest (both high)
_enc_rotation_dir = [0, 0, 0, 0, 0]  # Accumulated rotation direction since leaving rest
_enc_count = [0, 0, 0, 0, 0]  # Raw quadrature counts (legacy, kept for compatibility)
_enc_direction = [0, 0, 0, 0, 0]  # Locked direction during a rotation (-1, 0, or 1)

# Velocity-sensitive encoding: track timestamps and smoothed velocity
_enc_last_click_time = [0.0, 0.0, 0.0, 0.0, 0.0]  # Time of last click per encoder
_enc_prev_click_time = [0.0, 0.0, 0.0, 0.0, 0.0]  # Time of previous click (for velocity calc)
_enc_velocity = [0.0, 0.0, 0.0, 0.0, 0.0]  # Smoothed velocity (clicks/sec) per encoder

# Debounce timers for discrete controls (presets, modes, etc.)
# These controls need one change per physical detent, not velocity-based
_discrete_last_change = [0.0, 0.0, 0.0, 0.0, 0.0]  # Last change time per encoder
DISCRETE_DEBOUNCE_MS = 120  # Minimum ms between discrete value changes

# Long-press state for encoder 2 (save defaults on SET page)
_enc2_press_time = 0.0       # When button was pressed
_enc2_saving = False         # True while in 3-second hold on SET page
_enc2_save_complete = 0.0    # Timestamp when save completed (for "Saved" display)

# Long-press state for encoder 1 (save preset on Settings page, Reset column)
_enc1_press_time = 0.0       # When encoder 1 button was pressed (0 = not pressed)
_enc1_save_progress = 0.0    # 0.0-1.0 progress for countdown display (3 seconds total)
_enc1_save_complete = 0.0    # Timestamp when save completed (for "Saved" display)
ENC1_SAVE_HOLD_DURATION = 3.0  # Seconds to hold for preset save

# Preset toggle state for encoder 2 on PRE page (toggle to/from ambient)
_last_preset_before_ambient = 1  # Stores the preset to return to when toggling from ambient

# Simple velocity parameters - just max multiplier per parameter type
# Velocity is calculated as clicks-per-second, then mapped logarithmically
VELOCITY_MAX_FREQ = 50        # Frequency: large range, high acceleration for fast sweeps
VELOCITY_MAX_Q = 20           # Q factor: 0-99 range
VELOCITY_MAX_PRESET = 1       # Presets: no acceleration (always 1x)
VELOCITY_MAX_PAGE = 1         # Pages: no acceleration (always 1x)

# Encoders 3–5: one quadrature step = one displayed unit (no velocity on these paths)
ENC_STEP_TRIG_BRIGHT = 1.0 / 99.0  # HOME OLED uses int(val * 99) for Trigger and Brightness
ENC_STEP_RELEASE_MS = 1.0  # Release readout is int(band.decay_ms) in ms
ENC_STEP_AMBIENT_FADE_S = 0.1  # Ambient "Fade" column shows tenths of a second

# Brightness fade toggle state
_brightness_saved = DEFAULT_BRIGHT  # Saved brightness before fade-out
_brightness_fading = False  # True while fading
_brightness_target = DEFAULT_BRIGHT  # Target for fade animation
_brightness_off = False  # True when faded to zero
BRIGHTNESS_FADE_DURATION = 0.5  # Fade duration in seconds
_brightness_fade_start_time = 0.0  # When fade started
_brightness_fade_start_value = DEFAULT_BRIGHT  # Brightness when fade started
_brightness_click_flash = 0.0  # Decays over time, >0 means click detected recently
_brightness_enc5sw_state = 1  # Current ENC5_SW_GPIO state for debug display

# Display-specific smoothed values (separate from control values)
_display_freq = DEFAULT_CENTER_HZ
_display_thresh = DEFAULT_THRESH
_display_q = DEFAULT_Q
_display_bright = DEFAULT_BRIGHT
_display_release = int(DEFAULT_DECAY_MS)  # Release display value in ms
_display_q_pct = 50  # Q display value (0-99), will be recalculated dynamically

# DMX throttling (44Hz is near max for DMX512 protocol)
DMX_RATE_HZ       = 44.0
_DMX_MIN_INTERVAL = 1.0 / DMX_RATE_HZ

# --- Plug & Play Audio Selection ---
AUDIO_DEVICE      = os.environ.get("AUDIO_DEVICE", "").strip()
AUDIO_DEVICE_NAME = os.environ.get("AUDIO_DEVICE_NAME", "").strip()

AUDIO_DEBUG = os.environ.get("AUDIO_DEBUG", "0").strip() == "1"
TRIG_DEBUG  = os.environ.get("TRIG_DEBUG",  "0").strip() == "1"
_audio_dbg_count = 0  # Throttle counter for AUDIO_DEBUG logging

# --- TUI flash message ---
_ui_flash_msg   = ""
_ui_flash_until = 0.0

# --- Persistent error state for TUI ---
_error_msg = ""
_error_time = 0.0
_error_type = "error"  # "error", "warning", "info"

def set_error(msg: str, error_type: str = "error"):
    """Set a persistent error message (displayed until cleared).
    
    Args:
        msg: The error message to display
        error_type: One of "error", "warning", or "info"
    """
    global _error_msg, _error_time, _error_type
    _error_msg = msg
    _error_time = time.time()
    _error_type = error_type

def clear_error():
    """Clear the persistent error message."""
    global _error_msg, _error_time, _error_type
    _error_msg = ""
    _error_time = 0.0
    _error_type = "error"

def get_error():
    """Get current error message, time, and type."""
    return _error_msg, _error_time, _error_type

def set_warning(msg: str):
    """Set a warning message (displayed until cleared)."""
    set_error(msg, "warning")

def set_info(msg: str):
    """Set an info message (displayed until cleared)."""
    set_error(msg, "info")

def ui_flash(msg: str, seconds: float = 1.5):
    global _ui_flash_msg, _ui_flash_until
    _ui_flash_msg   = msg
    _ui_flash_until = time.time() + seconds

def _set_stop(val: bool):
    global STOP_THREADS
    STOP_THREADS = val

def _set_run(val: bool):
    global RUNNING
    RUNNING = val

# ===================== Cycle logic =====================

# Beat cycle options: 4, 8, 16, 32, 64, 128, 256, 512, 1024
CYCLE_STEPS_OPTIONS = [4, 8, 16, 32, 64, 128, 256, 512, 1024]
CYCLE_STEPS_INDEX   = 0  # Index into CYCLE_STEPS_OPTIONS (default 4)
CYCLE_STEPS         = CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX]  # Initialize from index
CYCLE_TRIGGER_COUNT = 0
CYCLE_PHASE         = 0
CYCLE_AMBIENT_START = 0  # Timestamp when ambient phase started (for rnd/amb mode)

# Cycles between modes: off disables cycling, x+1/rnd/rnd/amb enable it
CYCLES_BETWEEN_MODES = ["off", "x+1", "rnd", "rnd/amb"]
CYCLES_BETWEEN_INDEX = 0  # Start with mode off

# Number of presets (can be expanded later)
NUM_PRESETS = 6

def program_pair_for_base(base: int):
    """Get the pair of programs for cycling: current and next (wrapping, excluding AMBIENT)."""
    # base is 1-indexed (1 to NUM_PRESETS)
    # Returns (current, next) where next wraps around
    # Note: AMBIENT (6) should never reach here since beats are disabled for it
    if base == 6:  # AMBIENT - no cycling (fallback)
        return (base, base)
    elif base == 5:  # RANDOM wraps to ALL
        return (base, 1)
    else:  # 1-4 cycle to next
        return (base, base + 1)

def set_cycle_steps(steps: int):
    global CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE
    CYCLE_STEPS         = int(steps)
    CYCLE_TRIGGER_COUNT = 0
    CYCLE_PHASE         = 0

def set_cycle_steps_by_index(idx: int):
    """Set cycle steps by index into CYCLE_STEPS_OPTIONS."""
    global CYCLE_STEPS_INDEX
    CYCLE_STEPS_INDEX = max(0, min(len(CYCLE_STEPS_OPTIONS) - 1, idx))
    set_cycle_steps(CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX])

# ===================== DMX backends =====================

class DmxBackendBase:
    def send(self, vals):
        raise NotImplementedError
    def close(self):
        pass

class NullDmx(DmxBackendBase):
    def __init__(self):
        print("[DMX] Backend: null (no DMX output).")
    def send(self, vals):
        pass

class UartDmx(DmxBackendBase):
    # Linux: start/stop UART BREAK for DMX (pyserial's send_break() uses coarse tcsendbreak steps).
    _TIOCSBRK = 0x5427
    _TIOCCBRK = 0x5428

    def __init__(self, device="/dev/serial0"):
        try:
            import serial
        except Exception as e:
            raise RuntimeError(f"pyserial missing: {e}")

        self.serial = serial.Serial(
            port=device,
            baudrate=DMX_UART_BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
            write_timeout=0,
            exclusive=True,
        )
        print(
            f"[DMX] Backend: uart ({device}) @ {DMX_UART_BAUD} 8N2 "
            f"(min_slots={DMX_UART_MIN_SLOTS} break={DMX_BREAK_STYLE})"
        )

    def _send_break_baud(self):
        try:
            self.serial.baudrate = 9600
            self.serial.write(b"\x00")
            self.serial.flush()
            time.sleep(0.001)
        finally:
            self.serial.baudrate = DMX_UART_BAUD

    def _send_break(self):
        import fcntl

        if DMX_BREAK_STYLE == "baud":
            self._send_break_baud()
            return
        fd = self.serial.fileno()
        try:
            fcntl.ioctl(fd, self._TIOCSBRK, 0)
            time.sleep(0.000092)  # ≥88µs break (DMX512)
            fcntl.ioctl(fd, self._TIOCCBRK, 0)
            time.sleep(0.000012)  # short MAB before start code
        except OSError:
            self._send_break_baud()

    def send(self, vals):
        n = len(vals)
        payload_slots = max(n, DMX_UART_MIN_SLOTS)
        buf = bytearray(1 + payload_slots)
        buf[0] = 0x00  # DMX start code
        for i in range(n):
            buf[1 + i] = max(0, min(255, int(vals[i])))
        self._send_break()
        self.serial.write(buf)
        self.serial.flush()

    def close(self):
        try:
            self.serial.close()
        except Exception:
            pass

def make_dmx_backend():
    if DMX_BACKEND == "uart":
        return UartDmx(device=DMX_UART_DEVICE)
    return NullDmx()

_pending_vals  = [0] * DMX_CHANS
_dmx_dirty     = False
_last_dmx_send = 0.0
_dmx_lock      = threading.Lock()

def send_dmx(vals):
    global _dmx_dirty
    with _dmx_lock:
        # Only copy the values we have (DMX_CHANNEL_COUNT)
        for i in range(min(len(vals), DMX_CHANS)):
            _pending_vals[i] = max(0, min(255, int(vals[i])))
        _dmx_dirty = True

def dmx_sender_loop(dmx_backend: DmxBackendBase):
    global _dmx_dirty, _last_dmx_send, APP_STATE, APP_ERROR
    try:
        while not STOP_THREADS:
            now = time.time()
            do_send = False
            vals = None
            with _dmx_lock:
                if _dmx_dirty and (now - _last_dmx_send) >= _DMX_MIN_INTERVAL:
                    vals = list(_pending_vals)
                    _dmx_dirty = False
                    do_send = True
                    _last_dmx_send = now
            if do_send and vals is not None:
                try:
                    # Send only the configured number of channels
                    dmx_backend.send(vals[:DMX_CHANNEL_COUNT])
                except Exception as e:
                    APP_STATE = "error"
                    APP_ERROR = f"DMX send failed: {e}"
            time.sleep(0.002)
    finally:
        try:
            # Send zeros on shutdown for configured channels
            shutdown_vals = [0] * DMX_CHANNEL_COUNT
            dmx_backend.send(shutdown_vals)
            time.sleep(0.05)
        except Exception:
            pass
        try:
            dmx_backend.close()
        except Exception:
            pass

# ===================== Color Conversion =====================

import colorsys

def hsv_to_rgb(h, s, v):
    """Convert HSV (0-1 range) to RGB (0-255 range)."""
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return int(r * 255), int(g * 255), int(b * 255)

def temp_to_rgb(temp, brightness=1.0):
    """Convert color temperature (0-99) to RGB.
    0=cool white (blueish), 99=warm white (orangeish)."""
    # Map 0-99 to approximate color temperature range
    # Cool white: more blue, less red
    # Warm white: more red/yellow, less blue
    t = temp / 99.0
    r = int(255 * brightness * (0.8 + 0.2 * t))
    g = int(255 * brightness * (0.85 + 0.1 * t - 0.05 * t * t))
    b = int(255 * brightness * (1.0 - 0.4 * t))
    return min(255, r), min(255, g), min(255, b)

# ===================== Light envelopes =====================

@dataclass
class LightState:
    env: float = 0.0
    post: float = 0.0
    t_ms: float = 0.0
    active: bool = False

states  = [LightState() for _ in range(DMX_CHANS)]
POST_EMA = 0.6

class BandParams:
    def __init__(self):
        global THRESH_MODE_INDEX, RELEASE_MODE_INDEX
        # Initialize from the loaded defaults mode (persisted from last session)
        mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
        preset = DEFAULTS_PRESETS[mode_name]
        # Handle both old 4-value and new 6-value preset formats
        if len(preset) >= 6:
            center_hz, thresh, decay_ms, q_factor, thresh_mode, release_mode = preset
            THRESH_MODE_INDEX = 0  # fixed threshold only; ignore stored thresh_mode
            RELEASE_MODE_INDEX = release_mode
        else:
            center_hz, thresh, decay_ms, q_factor = preset[:4]
        self.center    = center_hz
        self.q         = q_factor
        self.thresh    = thresh
        self.attack_ms = DEFAULT_ATTACK_MS
        self.decay_ms  = decay_ms

band = BandParams()
_runtime = {'attack_ms': band.attack_ms, 'decay_ms': band.decay_ms}

def trigger_idxs(idxs, attack_ms, decay_ms):
    for i in idxs:
        s = states[i]
        s.env = s.post = 0.0
        s.t_ms = 0.0
        s.active = True
    _runtime['attack_ms'] = attack_ms
    _runtime['decay_ms']  = decay_ms

def _get_color_page_dmx_values():
    """Generate DMX values for COLOR page direct control.
    Each DMX channel is treated as an individual light/dimmer.
    The brightness value is derived from HSV (hue + saturation -> grayscale intensity)
    or from temperature mode."""
    vals = [0] * DMX_CHANNEL_COUNT
    
    # Calculate brightness value for the channel(s)
    # In color mode, we convert HSV to a single brightness value
    # (since each channel is a single dimmer, not RGB)
    if _color_enc3_temp_mode:
        # Temperature mode: map 0-99 to brightness with slight warm/cool tint effect
        # For single-channel dimmers, just use brightness directly
        brightness_val = int(255 * BRIGHTNESS)
    else:
        # Hue mode: convert HSV to a brightness value
        # Use full value (V=1) and let BRIGHTNESS control the output
        h = _color_hue / 99.0
        s = _color_saturation / 99.0
        r, g, b = hsv_to_rgb(h, s, 1.0)
        # Convert RGB to perceived brightness (luminance)
        # Using standard luminance formula
        luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        brightness_val = int(255 * luminance * BRIGHTNESS)
    
    # Apply to selected channels
    for ch_idx in range(DMX_CHANNEL_COUNT):
        apply = False
        if _color_light_selection == 0:  # all
            apply = True
        elif _color_light_selection == 1:  # odd (1, 3, 5...)
            apply = (ch_idx % 2 == 0)  # 0-indexed, so 0=ch1, 2=ch3
        elif _color_light_selection == 2:  # even (2, 4, 6...)
            apply = (ch_idx % 2 == 1)
        else:  # specific channel (1-indexed in UI)
            apply = (ch_idx == _color_light_selection - 3)
        
        if apply:
            vals[ch_idx] = brightness_val
    
    return vals

def update_lights(dt_ms):
    global _reactive_brightness_scale, _effective_brightness_display, _effective_release_display
    
    # Check if COLOR page is active - use direct color control
    pages = get_pages()
    if len(pages) > current_page and pages[current_page] == "COLOR":
        return _get_color_page_dmx_values()
    
    a = max(1e-3, _runtime['attack_ms'])
    d = max(1e-3, _runtime['decay_ms'])
    vals = []
    # Only process configured channels (DMX_CHANNEL_COUNT)
    release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
    
    # Calculate base release display value (in ms)
    base_release_display = int(band.decay_ms)
    base_release_display = max(0, min(99, base_release_display))
    
    # Calculate base brightness display value
    base_brightness_display = int(BRIGHTNESS * 99)
    
    if release_mode in ("bright", "both"):
        # Bright/both: DMX follows reactive scale; keep OLED readout in sync every frame (like release ms).
        effective_brightness = _reactive_brightness_scale
        _effective_brightness_display = max(0, min(99, int(effective_brightness * 99)))
    else:
        effective_brightness = BRIGHTNESS
        # Reset reactive brightness to base when not in bright/both mode
        _reactive_brightness_scale = BRIGHTNESS
        _effective_brightness_display = base_brightness_display
    
    # React, rand, and both modes: keep their release values until next trigger
    # Only reset display when switching away from these modes
    if release_mode not in ("react", "rand", "both"):
        _effective_release_display = base_release_display
    
    for i in range(DMX_CHANNEL_COUNT):
        s = states[i]
        if s.active:
            if s.t_ms < a:
                s.env = min(1.0, s.env + dt_ms/a)
            else:
                s.env = max(0.0, s.env - dt_ms/d)
            s.t_ms += dt_ms
            if s.env <= 0.0 and s.t_ms > a:
                s.active = False
        else:
            s.env = max(0.0, s.env - dt_ms/d)
        s.post = POST_EMA*s.env + (1.0-POST_EMA)*s.post
        vals.append(int(255 * s.post * effective_brightness))
    return vals

def update_ambient_mode(dt_ms):
    """Update ambient mode - non-audio-reactive sequential fading.
    
    Strictly 1-2 channels on at a time. One channel fades in while another
    fades out, creating a smooth rotation through all configured outputs.
    """
    global ambient_targets, ambient_current, ambient_last_change
    global ambient_speed, ambient_fade_time, _ambient_next_time
    
    now = time.time()
    dt_sec = dt_ms / 1000.0
    
    # Fade rate for both in and out
    fade_rate = dt_sec / max(0.1, ambient_fade_time)
    
    # Count channels that are on or turning on (current > 0.1 OR target == 1)
    # Only consider configured channels (0 to DMX_CHANNEL_COUNT-1)
    on_channels = [i for i in range(DMX_CHANNEL_COUNT) if ambient_current[i] > 0.1 or ambient_targets[i] == 1.0]
    off_channels = [i for i in range(DMX_CHANNEL_COUNT) if ambient_current[i] < 0.05 and ambient_targets[i] == 0.0]
    
    # Time to switch?
    if now >= _ambient_next_time:
        # If we have 2+ channels on, fade one out
        if len(on_channels) >= 2:
            # Pick the one that's been on longest (earliest last_change time)
            # This ensures fair rotation - the channel that started fading in first gets faded out first
            on_channels_sorted = sorted(on_channels, key=lambda i: ambient_last_change[i])
            ambient_targets[on_channels_sorted[0]] = 0.0
        
        # If we have less than 2 on and there are off channels, turn one on
        if len(on_channels) < 2 and off_channels:
            new_ch = random.choice(off_channels)
            ambient_targets[new_ch] = 1.0
            ambient_last_change[new_ch] = now  # Track when this channel started
        
        # If nothing is on at all, start one
        if not on_channels:
            new_ch = random.randint(0, DMX_CHANNEL_COUNT - 1)
            ambient_targets[new_ch] = 1.0
            ambient_last_change[new_ch] = now  # Track when this channel started
        
        # Schedule next switch - speed controls interval
        base_interval = 0.5 + random.random() * 1.0  # 0.5-1.5 sec base
        _ambient_next_time = now + (base_interval / ambient_speed)
    
    # Update all configured channels - fade toward targets
    for i in range(DMX_CHANNEL_COUNT):
        diff = ambient_targets[i] - ambient_current[i]
        
        if abs(diff) < fade_rate:
            ambient_current[i] = ambient_targets[i]
        else:
            ambient_current[i] += fade_rate if diff > 0 else -fade_rate
        
        ambient_current[i] = max(0.0, min(1.0, ambient_current[i]))
        
        # Apply to state
        states[i].env = ambient_current[i]
        states[i].post = ambient_current[i]

# ===================== DSP =====================

class BiquadBandpass:
    def __init__(self, sr, center_hz, q):
        self.sr = sr
        self.center = center_hz
        self.q = q
        self.reset()
        self._design()
    def reset(self):
        self.x1=self.x2=self.y1=self.y2=0.0
    def set_params(self, center_hz, q):
        self.center = max(MIN_CENTER_HZ, min(MAX_CENTER_HZ, float(center_hz)))
        q_min = get_q_min(self.center)
        self.q      = max(q_min,         min(Q_MAX,         float(q)))
        self._design()
    def _design(self):
        w0 = 2.0*math.pi*self.center/self.sr
        alpha = math.sin(w0)/(2.0*self.q)
        b0,b1,b2 =  math.sin(w0)/2.0, 0.0, -math.sin(w0)/2.0
        a0,a1,a2 =  1.0 + alpha, -2.0*math.cos(w0), 1.0 - alpha
        self.b0,self.b1,self.b2 = b0/a0, b1/a0, b2/a0
        self.a1,self.a2 = a1/a0, a2/a0
    def process(self, x):
        y = np.empty_like(x, dtype=np.float32)
        b0,b1,b2,a1,a2 = self.b0,self.b1,self.b2,self.a1,self.a2
        x1,x2,y1,y2 = self.x1,self.x2,self.y1,self.y2
        for i in range(len(x)):
            xi = float(x[i])
            yo = b0*xi + b1*x1 + b2*x2 - a1*y1 - a2*y2
            y[i] = yo
            x2, x1 = x1, xi
            y2, y1 = y1, yo
        self.x1,self.x2,self.y1,self.y2 = x1,x2,y1,y2
        return y

class EnvDetector:
    def __init__(self, sr, attack_ms=8.0, release_ms=80.0):
        self.sr = sr
        self.set_times(attack_ms, release_ms)
        self.y = 0.0
    def set_times(self, attack_ms, release_ms):
        self.alpha_a = math.exp(-1.0/(max(1e-3, attack_ms)*1e-3*self.sr))
        self.alpha_r = math.exp(-1.0/(max(1e-3, release_ms)*1e-3*self.sr))
    def process(self, x):
        out = np.empty_like(x, dtype=np.float32)
        y = self.y
        aa, ar = self.alpha_a, self.alpha_r
        for i in range(len(x)):
            s = abs(float(x[i]))
            if s > y: y = aa*y + (1.0-aa)*s
            else:     y = ar*y + (1.0-ar)*s
            out[i] = y
        self.y = y
        return out

class Agc:
    def __init__(self, target=0.02, tau=0.95, max_gain=8.0):
        self.target=target
        self.gain  = 1.0
        self.tau   = tau
        self.max_gain = max_gain
    def update(self, env_mean):
        eps=1e-6
        desired=self.target/max(eps, env_mean)
        desired=max(0.1, min(self.max_gain, desired))
        self.gain=self.tau*self.gain+(1.0-self.tau)*desired
        return self.gain

# ===================== Trigger Mode State =====================
# Per-mode running state for the three trigger algorithms.
# Each helper updates only its own state; switching modes leaves the others untouched.

# classic mode: outer EMA (legacy) — uses module-level live_band_env and Agc instance
# Kept here for clarity; actual EMA accumulator is `classic_abs_ema` (already declared).

# compander mode state
_comp_running_rms  = 0.001  # ~5s running mean of band envelope (long-term floor)
_comp_running_peak = 0.05   # ~250ms decaying peak of band envelope (short-term ceiling)
_comp_running_smax = 0.01   # decaying max of slope (for onset normalization)
_comp_prev_e       = 0.0    # last per-block envelope scalar
_comp_meter_ema    = 0.0    # smoothed live_band_env for display

# kick mode state (separate from compander so they can coexist)
_kick_bp2 = None            # second cascaded biquad (initialized in audio_loop)
_kick_envd = None           # dedicated faster envelope detector
_kick_running_rms  = 0.001
_kick_running_peak = 0.05
_kick_running_smax = 0.01
_kick_prev_e       = 0.0
_kick_peak_hold    = 0.0    # peak-hold meter value
_kick_peak_t       = 0.0    # timestamp of last peak-set
KICK_PEAK_HOLD_S   = 0.15   # how long meter holds peaks before decaying

# "old" mode: FFT-window level detector.
# Source signal is `display_mean_in_q` — the mean of normalized FFT bands whose
# centers fall inside band.center ± bandwidth/2. Same bars the user sees
# highlighted on the spectrum, so the meter and trigger now correspond exactly
# to visible energy in the configured frequency range. No biquad bandpass,
# no AGC pumping of room noise. A small noise gate zeroes out sub-floor
# residuals and a light EMA smooths the meter for visual stability.
OLD_FFT_NOISE_GATE = float(os.environ.get("OLD_FFT_NOISE_GATE", "0.06"))  # below this, meter snaps to 0
OLD_FFT_SMOOTH_EMA = float(os.environ.get("OLD_FFT_SMOOTH_EMA", "0.55"))  # display smoothing (matches ENV_EMA feel)
_old_fft_ema = 0.0
# Legacy state names kept for compatibility with _reset_trigger_state references; unused now.
OLD_CLASSIC_UI_SCALE = 5.0
OLD_AGC_MAX_GAIN     = 20.0
_old_classic_abs_ema = 0.0
_old_agc             = None  # lazily constructed on first use to honor module-level Agc


def _trigger_classic(x_block, bp_obj, envd_obj, agc_obj):
    """Faithful restore of the old project's signal chain, with noise gate.

    Pipeline: bandpass -> envelope -> noise gate -> AGC -> outer EMA -> live_band_env
    Returns: (live_band_env, trigger_score) -- both equal in this mode.
    """
    global classic_abs_ema
    bp_obj.set_params(band.center, band.q)
    y_bp  = bp_obj.process(x_block)
    e_env = envd_obj.process(y_bp)
    e_mean_raw = float(np.mean(e_env))

    denom = max(AGC_TARGET * CLASSIC_UI_SCALE, 1e-10)

    # Noise gate: if the raw band envelope is below the absolute floor, freeze the
    # AGC (don't ramp up to chase noise) and decay the displayed envelope toward 0.
    # Without this, AGC pumps room noise up to ~AGC_TARGET and live_band_env sits
    # near 1.0 even with no signal.
    if e_mean_raw < NOISE_GATE_RMS:
        a_em = ENV_EMA
        # Decay toward zero (no signal contribution) at the same EMA rate.
        # Approximate per-block decay: a_em^N where N=len(x_block); cheap and stable.
        try:
            n = len(x_block)
        except TypeError:
            n = 1
        decay = a_em ** n
        classic_abs_ema = float(classic_abs_ema) * decay
        lbe = min(1.0, float(classic_abs_ema) / denom)
        return lbe, lbe

    g_agc = agc_obj.update(e_mean_raw) if AGC_ON else 1.0
    wgt   = math.sqrt(max(1.0, band.center / 100.0)) if WEIGHTING_ON else 1.0
    e_scaled = e_env * (g_agc * wgt)

    # Per-sample EMA on the scaled envelope, like the old project
    va  = classic_abs_ema
    a_em = ENV_EMA
    for s in e_scaled:
        va = a_em * va + (1.0 - a_em) * float(s)
    classic_abs_ema = va

    # Map AGC-scaled envelope to 0..1 (CLASSIC_UI_SCALE=1.0 lets it reach 1.0)
    lbe = min(1.0, float(va) / denom)
    return lbe, lbe


def _trigger_compander(x_block, bp_obj, envd_obj):
    """Adaptive compander + onset boost.

    Pipeline:
      bandpass -> envelope -> per-block scalar
      -> running RMS (floor) + running peak (ceiling) -> normalize 0..1
      -> half-wave-rectified slope -> normalize against running max -> onset 0..1
      -> score = max(norm, 0.5*onset)
    Returns: (live_band_env_smoothed_for_display, raw_score_for_trigger)
    """
    global _comp_running_rms, _comp_running_peak, _comp_running_smax
    global _comp_prev_e, _comp_meter_ema

    bp_obj.set_params(band.center, band.q)
    y_bp  = bp_obj.process(x_block)
    e_env = envd_obj.process(y_bp)
    e_scalar = float(np.mean(e_env))

    # Running RMS (~5s window at 48kSR/1024hop ~= 47Hz block rate; coef tuned for ~5s)
    _comp_running_rms = 0.998 * _comp_running_rms + 0.002 * e_scalar
    # Running peak with ~250ms decay
    _comp_running_peak = max(_comp_running_peak * 0.995, e_scalar)

    floor   = max(0.001, _comp_running_rms)
    ceiling = max(_comp_running_peak, floor * 4.0)
    norm    = (e_scalar - floor) / max(ceiling - floor, 1e-6)
    norm    = max(0.0, min(1.0, norm))

    # Slope (rising-edge only)
    slope = max(0.0, e_scalar - _comp_prev_e)
    _comp_prev_e = e_scalar
    _comp_running_smax = max(_comp_running_smax * 0.995, slope)
    onset = slope / max(_comp_running_smax, 1e-6)
    onset = max(0.0, min(1.0, onset))

    score = max(norm, 0.5 * onset)
    # Meter: smoothed for visual stability, but trigger uses raw score
    _comp_meter_ema = ENV_EMA * _comp_meter_ema + (1.0 - ENV_EMA) * score
    return _comp_meter_ema, score


def _trigger_kick(x_block, bp_obj):
    """Dedicated kick detector.

    Pipeline:
      cascaded biquad (bp_obj -> _kick_bp2) -> faster envelope (5ms/60ms)
      -> compander (running_rms only updated on decays) -> norm 0..1
      -> slope-weighted score = 0.4*norm + 0.6*onset
      -> peak-hold meter (~150ms)
    Returns: (peak_hold_meter, raw_score_for_trigger)
    """
    global _kick_bp2, _kick_envd
    global _kick_running_rms, _kick_running_peak, _kick_running_smax
    global _kick_prev_e, _kick_peak_hold, _kick_peak_t

    bp_obj.set_params(band.center, band.q)
    if _kick_bp2 is None:
        _kick_bp2 = BiquadBandpass(SR, band.center, band.q)
    if _kick_envd is None:
        _kick_envd = EnvDetector(SR, attack_ms=5.0, release_ms=60.0)
    _kick_bp2.set_params(band.center, band.q)

    # Cascaded bandpass for sharper sub-bass roll-off
    y1 = bp_obj.process(x_block)
    y2 = _kick_bp2.process(y1)
    e_env = _kick_envd.process(y2)
    e_scalar = float(np.mean(e_env))

    # Slope first so we can decide whether to update the floor
    slope = e_scalar - _kick_prev_e
    _kick_prev_e = e_scalar

    # Running RMS only updated on decays (slope <= 0): "learn the noise floor when nothing is hitting"
    if slope <= 0.0:
        _kick_running_rms = 0.995 * _kick_running_rms + 0.005 * e_scalar
    # Peak with ~200ms decay
    _kick_running_peak = max(_kick_running_peak * 0.993, e_scalar)

    floor   = max(0.001, _kick_running_rms)
    ceiling = max(_kick_running_peak, floor * 5.0)
    norm    = (e_scalar - floor) / max(ceiling - floor, 1e-6)
    norm    = max(0.0, min(1.0, norm))

    # Onset = positive slope normalized against its own decaying max
    pos_slope = max(0.0, slope)
    _kick_running_smax = max(_kick_running_smax * 0.993, pos_slope)
    onset = pos_slope / max(_kick_running_smax, 1e-6)
    onset = max(0.0, min(1.0, onset))

    # Slope-weighted score: kicks are dominated by onsets, but level still matters
    score = 0.4 * norm + 0.6 * onset
    score = max(0.0, min(1.0, score))

    # Peak-hold meter for max visual contrast
    now = time.time()
    if score >= _kick_peak_hold:
        _kick_peak_hold = score
        _kick_peak_t = now
    else:
        if (now - _kick_peak_t) > KICK_PEAK_HOLD_S:
            # decay toward current score after hold expires
            _kick_peak_hold = max(score, _kick_peak_hold * 0.92)

    return _kick_peak_hold, score


def _trigger_kick_effective_thresh():
    """Adaptive trigger margin for kick mode: never let the trigger fire below the noise floor."""
    return max(band.thresh, _kick_running_rms * 2.5 + 0.05)


def _trigger_old(display_mean_in_q):
    """FFT-window level detector for "old" mode.

    Source: `display_mean_in_q` — mean of normalized FFT bands inside
    band.center ± bandwidth/2 (the same bars highlighted on the spectrum).
    Replaces the legacy biquad → envelope → AGC chain so the meter and trigger
    reflect ONLY the energy in the user-visible frequency window. Out-of-band
    content cannot drive the trigger, and silence keeps the meter at 0.

    Pipeline: noise-gate → EMA smooth → clip
    Returns: (live_band_env, trigger_score) — both equal in this mode.
    """
    global _old_fft_ema
    src = float(display_mean_in_q)
    gated = src if src >= OLD_FFT_NOISE_GATE else 0.0
    _old_fft_ema = OLD_FFT_SMOOTH_EMA * _old_fft_ema + (1.0 - OLD_FFT_SMOOTH_EMA) * gated
    val = min(1.0, max(0.0, _old_fft_ema))
    return val, val


def _reset_trigger_state():
    """Clear per-mode running state (used on mode switch or reset)."""
    global _comp_running_rms, _comp_running_peak, _comp_running_smax
    global _comp_prev_e, _comp_meter_ema
    global _kick_running_rms, _kick_running_peak, _kick_running_smax
    global _kick_prev_e, _kick_peak_hold, _kick_peak_t
    global classic_abs_ema
    global _old_classic_abs_ema, _old_agc, _old_fft_ema
    _comp_running_rms = 0.001
    _comp_running_peak = 0.05
    _comp_running_smax = 0.01
    _comp_prev_e = 0.0
    _comp_meter_ema = 0.0
    _kick_running_rms = 0.001
    _kick_running_peak = 0.05
    _kick_running_smax = 0.01
    _kick_prev_e = 0.0
    _kick_peak_hold = 0.0
    _kick_peak_t = 0.0
    _old_classic_abs_ema = 0.0
    _old_agc = None  # legacy: rebuilt on next call if any old caller still uses it
    _old_fft_ema = 0.0
    try:
        classic_abs_ema = 0.0
    except NameError:
        pass


# ===================== 3-Band Onset Detector =====================

@dataclass
class BandConfig:
    """Configuration for a single frequency band in the 3-band detector.
    
    TouchDesigner-style signal chain: Analyze -> Lag -> Slope -> Gain+Limit -> Trigger
    """
    name: str              # Band name: "LOW", "MID", "HIGH"
    f_lo: float            # Low frequency bound (Hz)
    f_hi: float            # High frequency bound (Hz)
    trigger_thresh: float  # Trigger threshold (0-1) - level slope must cross to fire
    cooldown_ms: float     # Cooldown between triggers (ms)
    lag_attack: float      # Lag attack rate (0-1, higher = faster attack)
    lag_decay: float       # Lag decay rate (0-1, higher = faster decay)
    gain: float = 1.0      # Per-band gain multiplier (scales slope before triggering)


class ThreeBandOnsetDetector:
    """3-band onset detector using TouchDesigner-style signal chain.
    
    Signal chain (like TouchDesigner):
    1. Analyze: Extract band energy from FFT
    2. Lag: Asymmetric smoothing (fast attack, slow decay) - creates sawtooth envelope
    3. Slope: Derivative of lagged signal - only spikes on rising edges
    4. Gain+Limit: Normalize slope to 0-1 range
    5. Trigger: Fire when normalized slope crosses trigger threshold
    """
    
    # Guardrail constants - enforce safe band ranges
    MIN_WIDTH_HZ = {"LOW": 40, "MID": 200, "HIGH": 500}
    MAX_WIDTH_HZ = {"LOW": 600, "MID": 3000, "HIGH": 8000}
    # Trigger threshold range for UI (0-1)
    TRIGGER_RANGE = (0.05, 0.95)
    
    def __init__(self, sample_rate, n_fft=2048):
        self.sr = sample_rate
        self.sample_rate = sample_rate  # Alias for compatibility
        self.n_fft = n_fft
        self.nyquist = sample_rate / 2
        
        # Ring buffer for audio samples (kept for legacy update() method)
        self.ring_buffer = np.zeros(n_fft, dtype=np.float32)
        self.ring_idx = 0
        
        # Precompute window
        self.window = np.hanning(n_fft).astype(np.float32)
        
        # ========== TOUCHDESIGNER-STYLE BAND CONFIG ==========
        # BandConfig(name, f_lo, f_hi, trigger_thresh, cooldown_ms, lag_attack, lag_decay, gain)
        #
        # trigger_thresh: Level (0-1) that normalized slope must cross to trigger
        # cooldown_ms: Minimum time between triggers (like Trigger CHOP re-trigger delay)
        # lag_attack: How fast to follow rising signal (higher = faster, ~0.3 for kicks)
        # lag_decay: How fast to decay (lower = slower decay, ~0.02 for sawtooth shape)
        # gain: Boost slope signal before limiting (higher = more sensitive)
        #
        self.bands = [
            # LOW: Kick - fast attack, slow decay for clean sawtooth
            # trigger_thresh 0.24 = moderate sensitivity (UI shows 30 at this value)
            # lag_attack 0.4 = follow rising signal at 40% per frame
            # lag_decay 0.015 = slow decay (1.5% per frame) for sawtooth shape
            # gain 6.3 = +16dB boost (UI shows 0dB at this value as new center)
            BandConfig("LOW", 40, 150, 0.24, 200, 0.4, 0.015, 6.3),
            # MID: Snare - similar tuning
            BandConfig("MID", 1000, 4000, 0.24, 120, 0.35, 0.02, 6.3),
            # HIGH: Hats - similar tuning
            BandConfig("HIGH", 6000, 16000, 0.24, 50, 0.5, 0.03, 6.3),
        ]
        
        # Cross-band suppression: when one band triggers, suppress others briefly
        self.suppression_time = [0.0, 0.0, 0.0]  # Time when suppression started
        self.SUPPRESSION_MS = 30  # How long to suppress other bands after a trigger
        
        # ===== TouchDesigner-style signal chain state =====
        # Stage 1: Raw band energy
        self.energy = [0.0, 0.0, 0.0]           # Current band energy from FFT
        
        # Stage 2: Lagged energy (asymmetric smoothing like Lag CHOP)
        self.lagged_energy = [0.0, 0.0, 0.0]    # Smoothed energy (fast attack, slow decay)
        self.prev_lagged = [0.0, 0.0, 0.0]      # Previous lagged value for slope calc
        
        # Stage 3: Slope (derivative like Slope CHOP)
        self.slope = [0.0, 0.0, 0.0]            # Rate of change of lagged energy
        
        # Stage 4: Normalized slope (after gain + limit)
        self.normalized_slope = [0.0, 0.0, 0.0] # Slope after gain and 0-1 clamping
        
        # Stage 5: Trigger state
        self.trigger = [False, False, False]
        self.trigger_flash_time = [0.0, 0.0, 0.0]  # Timestamps when each band last triggered
        self.last_trigger_time = [0.0, 0.0, 0.0]
        
        # Display values for UI
        self.display_flux = [0.0, 0.0, 0.0]     # Normalized slope for display (0-1)
        self.display_thresh = [0.24, 0.24, 0.24]  # Trigger threshold for display (default: UI shows 30)
        self.scaled_onset = [0.0, 0.0, 0.0]     # Alias for display_flux (UI compat)
        
        # Legacy aliases for UI compatibility
        self.flux = [0.0, 0.0, 0.0]             # Alias for slope
        self.prev_flux = [0.0, 0.0, 0.0]        # Previous slope
        self.flux_mean = [0.01, 0.01, 0.01]     # Not used in new approach but kept
        self.onset = [0.0, 0.0, 0.0]            # Alias for normalized_slope
        self.lagged = [0.0, 0.0, 0.0]           # Alias for lagged_energy
        self.lagged_prev = [0.0, 0.0, 0.0]      # Alias for prev_lagged
        self.prev_energy = [0.0, 0.0, 0.0]      # Not used but kept for compat
        self.armed = [True, True, True]         # Not used but kept for compat
        
        # History buffers for UI (deque is faster than list slicing)
        self.onset_history = [deque([0.0] * 64, maxlen=64) for _ in range(3)]
        self.trigger_history = [deque([False] * 64, maxlen=64) for _ in range(3)]
        
        # AGC disabled
        self.agc_enabled = False
        self.agc_gain = 1.0
    
    def push_samples(self, samples):
        """Add samples to ring buffer - NO processing, just store."""
        if len(samples) == 0:
            return
        
        # Store in ring buffer
        n = len(samples)
        if n >= self.n_fft:
            # If we have more samples than buffer, just take the last n_fft
            self.ring_buffer[:] = samples[-self.n_fft:]
            self.ring_idx = 0
        else:
            # Wrap around if needed
            end_idx = self.ring_idx + n
            if end_idx <= self.n_fft:
                self.ring_buffer[self.ring_idx:end_idx] = samples
                self.ring_idx = end_idx % self.n_fft
            else:
                # Split across buffer end
                first_part = self.n_fft - self.ring_idx
                self.ring_buffer[self.ring_idx:] = samples[:first_part]
                self.ring_buffer[:n - first_part] = samples[first_part:]
                self.ring_idx = n - first_part
    
    def update_from_fft_bands(self, fft_bands, fft_band_freqs, dt):
        """
        TouchDesigner-style onset detection using signal chain:
        Analyze -> Lag -> Slope -> Gain+Limit -> Trigger
        
        This produces clean, consistent trigger pulses like TouchDesigner's chan4 output.
        
        Pipeline:
        1. Analyze: Get band energy from normalized fft_bands (0-1)
        2. Lag: Asymmetric smoothing (fast attack, slow decay) - creates sawtooth envelope
        3. Slope: Derivative of lagged signal (half-wave rectified) - only spikes on rising edges
        4. Gain+Limit: Normalize slope to 0-1 range
        5. Trigger: Fire when normalized slope crosses trigger threshold
        """
        now = time.time()
        
        # First pass: compute normalized slope for all bands
        for i, band in enumerate(self.bands):
            # ===== Stage 1: ANALYZE - Get band energy =====
            band_energy = 0.0
            band_count = 0
            for j, (band_lo, band_hi) in enumerate(fft_band_freqs):
                band_center = (band_lo + band_hi) / 2
                if band.f_lo <= band_center <= band.f_hi:
                    band_energy += fft_bands[j]
                    band_count += 1
            if band_count > 0:
                band_energy /= band_count
            self.energy[i] = float(band_energy)
            
            # ===== Stage 2: LAG - Asymmetric smoothing (like Lag CHOP) =====
            # Fast attack: follow rising signal quickly
            # Slow decay: hold and decay slowly (creates sawtooth shape)
            if self.energy[i] > self.lagged_energy[i]:
                # Rising - fast attack
                self.lagged_energy[i] += (self.energy[i] - self.lagged_energy[i]) * band.lag_attack
            else:
                # Falling - slow decay
                self.lagged_energy[i] += (self.energy[i] - self.lagged_energy[i]) * band.lag_decay
            
            # ===== Stage 3: SLOPE - Derivative (like Slope CHOP) =====
            # Half-wave rectified: only positive slopes (rising edges)
            # This is what makes kicks stand out - they have the fastest rise
            raw_slope = max(0.0, self.lagged_energy[i] - self.prev_lagged[i])
            self.slope[i] = raw_slope
            self.prev_lagged[i] = self.lagged_energy[i]
            
            # ===== Stage 4: GAIN + LIMIT (like Math CHOP + Limit CHOP) =====
            # Apply gain to boost slope to usable range, then clamp to 0-1
            self.normalized_slope[i] = min(1.0, raw_slope * band.gain)
            
            # Store for legacy compatibility
            self.flux[i] = self.slope[i]
            self.prev_flux[i] = self.slope[i]
        
        # Second pass: trigger decision with cross-band suppression
        # Find which band has the strongest normalized slope
        slope_values = [self.normalized_slope[i] for i in range(3)]
        dominant_band = slope_values.index(max(slope_values)) if max(slope_values) > 0 else -1
        
        for i, band in enumerate(self.bands):
            # Check if this band is suppressed by another band's recent trigger
            suppressed = False
            for j in range(3):
                if j != i and (now - self.suppression_time[j]) * 1000 < self.SUPPRESSION_MS:
                    suppressed = True
                    break
            
            # ===== Stage 5: TRIGGER (like Trigger CHOP) =====
            # Simple threshold crossing on normalized slope
            above_threshold = self.normalized_slope[i] > band.trigger_thresh
            cooldown_ok = (now - self.last_trigger_time[i]) * 1000 >= band.cooldown_ms
            
            # Cross-band isolation: prefer dominant band
            is_dominant = (i == dominant_band)
            significantly_above = (self.normalized_slope[i] > band.trigger_thresh * 1.5)
            
            if above_threshold and cooldown_ok and not suppressed and (is_dominant or significantly_above):
                self.trigger[i] = True
                self.trigger_flash_time[i] = now
                self.last_trigger_time[i] = now
                self.suppression_time[i] = now  # Start suppressing other bands
            else:
                self.trigger[i] = False
            
            # Update display values for UI
            # normalized_slope is already 0-1, perfect for display
            self.display_flux[i] = self.normalized_slope[i]
            self.scaled_onset[i] = self.normalized_slope[i]
            
            # Threshold line position is directly the trigger_thresh (0-1)
            self.display_thresh[i] = band.trigger_thresh
            
            # Update aliases for UI compatibility
            self.onset[i] = self.normalized_slope[i]
            self.lagged[i] = self.lagged_energy[i]
            self.lagged_prev[i] = self.prev_lagged[i]
            
            # Update history for UI graphs
            self.onset_history[i].append(self.scaled_onset[i])
            self.trigger_history[i].append(self.trigger[i])
        
        return [(self.energy[i], self.flux[i], self.scaled_onset[i], self.trigger[i]) 
                for i in range(3)]
    
    def update(self, dt):
        """Legacy method - computes own FFT. Use update_from_fft_bands for normalized input.
        
        Now uses spectral flux approach for consistency with update_from_fft_bands.
        """
        now = time.time()
        
        buf = np.concatenate([
            self.ring_buffer[self.ring_idx:],
            self.ring_buffer[:self.ring_idx]
        ])
        windowed = buf * self.window
        fft_mag = np.abs(np.fft.rfft(windowed)) / self.n_fft
        freqs = np.fft.rfftfreq(self.n_fft, 1.0 / self.sr)
        
        for i, band in enumerate(self.bands):
            # Stage 1: Get band energy
            mask = (freqs >= band.f_lo) & (freqs < band.f_hi)
            if np.any(mask):
                self.energy[i] = float(np.mean(fft_mag[mask]))
            else:
                self.energy[i] = 0.0
            
            # Stage 2: Asymmetric Lag (fast attack, slow decay)
            if self.energy[i] > self.lagged_energy[i]:
                self.lagged_energy[i] += (self.energy[i] - self.lagged_energy[i]) * band.lag_attack
            else:
                self.lagged_energy[i] += (self.energy[i] - self.lagged_energy[i]) * band.lag_decay
            
            # Stage 3: Slope (derivative, half-wave rectified)
            self.slope[i] = max(0.0, self.lagged_energy[i] - self.prev_lagged[i])
            self.prev_lagged[i] = self.lagged_energy[i]
            
            # Stage 4: Gain + Limit
            self.normalized_slope[i] = min(1.0, self.slope[i] * band.gain)
            
            # Stage 5: Trigger
            above_threshold = self.normalized_slope[i] > band.trigger_thresh
            cooldown_ok = (now - self.last_trigger_time[i]) * 1000 >= band.cooldown_ms
            
            if above_threshold and cooldown_ok:
                self.trigger[i] = True
                self.trigger_flash_time[i] = now
                self.last_trigger_time[i] = now
            else:
                self.trigger[i] = False
            
            # Update display values
            self.display_flux[i] = self.normalized_slope[i]
            self.scaled_onset[i] = self.normalized_slope[i]
            self.display_thresh[i] = band.trigger_thresh
            
            # Update aliases for UI
            self.flux[i] = self.slope[i]
            self.onset[i] = self.normalized_slope[i]
            self.lagged[i] = self.lagged_energy[i]
            self.lagged_prev[i] = self.prev_lagged[i]
            
            self.onset_history[i].append(self.scaled_onset[i])
            self.trigger_history[i].append(self.trigger[i])
        
        return [(self.energy[i], self.flux[i], self.scaled_onset[i], self.trigger[i]) 
                for i in range(3)]
    
    def get_adaptive_threshold(self, band_idx):
        """Get the current trigger threshold for a band (for UI display)."""
        band = self.bands[band_idx]
        return band.trigger_thresh
    
    def is_flash_active(self, band_idx):
        """Check if trigger flash is active for a band (time-based)."""
        return (time.time() - self.trigger_flash_time[band_idx]) < TRIGGER_FLASH_DURATION
    
    def adjust_width(self, band_idx, delta_pct):
        """Adjust band width with band-specific cropping behavior.
        
        - LOW: Crops from right only (anchored at low end) - adjust how high kicks reach
        - MID: Crops from both sides uniformly (centered) - adjust snare width symmetrically
        - HIGH: Crops from left only (anchored at high end) - adjust how low hats reach
        """
        band = self.bands[band_idx]
        width = band.f_hi - band.f_lo
        
        # Apply percentage change to width
        new_width = width * (1 + delta_pct / 100.0)
        
        # Enforce min/max width
        min_w = self.MIN_WIDTH_HZ[band.name]
        max_w = self.MAX_WIDTH_HZ[band.name]
        new_width = max(min_w, min(max_w, new_width))
        
        # Compute new bounds based on band type
        if band.name == "LOW":
            # Anchor at low end, adjust high end only
            new_lo = band.f_lo
            new_hi = band.f_lo + new_width
        elif band.name == "HIGH":
            # Anchor at high end, adjust low end only
            new_hi = band.f_hi
            new_lo = band.f_hi - new_width
        else:  # MID
            # Symmetric around center (original behavior)
            center = (band.f_lo + band.f_hi) / 2
            new_lo = center - new_width / 2
            new_hi = center + new_width / 2
        
        # Clamp to valid frequency range
        if new_lo < 20:
            new_lo = 20
            if band.name == "LOW":
                new_hi = new_lo + new_width
        if new_hi > self.nyquist - 100:
            new_hi = self.nyquist - 100
            if band.name == "HIGH":
                new_lo = new_hi - new_width
        
        # Ensure f_lo < f_hi
        if new_lo >= new_hi:
            new_lo = new_hi - min_w
        
        band.f_lo = max(20, new_lo)
        band.f_hi = min(self.nyquist - 100, new_hi)
    
    def get_trigger_for_selected(self, selected_idx):
        """Return trigger state for DMX integration."""
        return self.trigger[selected_idx]


# Global 3-band detector instance (initialized in audio_loop)
three_band_detector = None

# ===================== Input device pick =====================

def _portaudio_troubleshooting_hints():
    """Extra hints when PortAudio has no devices (often kernel/ALSAR/USB did not expose a card)."""
    parts = []
    try:
        out = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        blob = (out.stdout or "") + (out.stderr or "")
        if not re.search(r"card\s+\d+:", blob, re.I):
            parts.append(
                "ALSA reports no capture devices (same as `arecord -l`): the kernel did not register "
                "any mic/in interface, so PortAudio has nothing to open."
            )
    except Exception:
        pass

    kernel_usb = ""
    try:
        p = subprocess.run(
            ["journalctl", "-k", "-n", "250", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        ks = (p.stdout or "")
        low = ks.lower()
        if "not enough bandwidth" in low:
            kernel_usb += (
                'Recent kernel logs mention USB "Not enough bandwidth" / failed config '
                "(common with full‑speed adapters behind a crowded hub).\nTry: plug "
                "the audio interface straight into one of the Pi’s USB ports (not "
                "through an unpowered hub), disconnect other bandwidth‑heavy USB devices, reboot, "
                "then unplug/replug.\nConfirm with `arecord -l` listing a capture `card`, then rerun."
            )
        elif re.search(r"can't set config.*error\s*-28", ks, re.I):
            kernel_usb += (
                "Kernel failed to activate the USB audio device (often USB bandwidth/power). "
                "Same remediation as above."
            )
    except Exception:
        pass

    if kernel_usb:
        parts.append(kernel_usb)

    try:
        ar = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        ap = subprocess.run(
            ["aplay", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        a_rec = (ar.stdout or "") + (ar.stderr or "")
        a_play = (ap.stdout or "") + (ap.stderr or "")
        if re.search(r"card\s+\d+:", a_play, re.I) and not re.search(
            r"card\s+\d+:", a_rec, re.I
        ):
            parts.append(
                "ALSA can see playback (often HDMI-only) but no capture devices at all — PortAudio "
                "may enumerate zero interfaces (ALSA pcm asym / asym_default has no capture half). "
                "For USB-input-only builds, ship /etc/modules-load.d with `snd-dummy` so there is "
                "always a harmless capture PCM, then fix USB enumeration until `arecord -l` also "
                "lists your dongle."
            )
    except Exception:
        pass

    return ("\n" + "\n".join(parts)) if parts else ""


def _portaudio_devices_debug():
    """Human-readable list of all PortAudio devices (for error messages)."""
    try:
        lines = []
        for i, d in enumerate(sd.query_devices()):
            inch = d.get("max_input_channels", 0)
            outch = d.get("max_output_channels", 0)
            lines.append(f"  [{i}] in={inch} out={outch} {d.get('name', '')!r}")
        empty = "\n".join(lines) if lines else "  (empty device list)"
        if not lines:
            empty += _portaudio_troubleshooting_hints()
        return empty
    except Exception as e:
        return f"  (could not list devices: {e})"


def pick_input_device():
    devs = sd.query_devices()
    use = _capture_devices_with_preferences(devs, prefer_real=True)
    if not use:
        use = _capture_devices_with_preferences(devs, prefer_real=False)
    if not use:
        raise RuntimeError(
            "No suitable input device (>=1ch) found.\n"
            "PortAudio sees:\n"
            + _portaudio_devices_debug()
            + "\nPlug in USB audio or enable your HAT, then retry. "
            "Or pick a line above with in>0 and run: export AUDIO_DEVICE=<index>"
        )

    for pat in PREFERRED_INPUTS:
        rx = re.compile(pat, re.I)
        for i, d in use:
            if rx.search(d.get("name", "")):
                return i, d["name"], min(d.get("max_input_channels", 1), 2)
    i, d = use[0]
    return i, d["name"], min(d.get("max_input_channels", 1), 2)

def choose_input_device():
    devs = sd.query_devices()

    if AUDIO_DEVICE:
        idx = int(AUDIO_DEVICE)
        n = len(devs)
        if idx < 0 or idx >= n:
            raise RuntimeError(
                f"AUDIO_DEVICE={idx} is out of range (0..{n - 1}).\n"
                "Unset AUDIO_DEVICE (export -n AUDIO_DEVICE) and use AUDIO_DEVICE_NAME=USB, "
                "or pick a valid index from:\n"
                + _portaudio_devices_debug()
            )
        try:
            d = sd.query_devices(idx)
        except sd.PortAudioError as e:
            raise RuntimeError(
                f"AUDIO_DEVICE={idx} is invalid ({e}).\n"
                "Unset AUDIO_DEVICE and use AUDIO_DEVICE_NAME=USB, or fix the index:\n"
                + _portaudio_devices_debug()
            ) from e
        if d.get("max_input_channels", 0) <= 0:
            raise RuntimeError(
                f"AUDIO_DEVICE={idx} has no input channels.\n"
                + _portaudio_devices_debug()
            )
        return idx, d["name"], min(d.get("max_input_channels", 1), 2)

    if AUDIO_DEVICE_NAME:
        needle = AUDIO_DEVICE_NAME.lower()
        primary = []
        for i, d in enumerate(devs):
            inch = int(d.get("max_input_channels", 0) or 0)
            if inch <= 0:
                continue
            nm = (d.get("name") or "")
            if not _looks_like_placeholder_capture(nm):
                primary.append((i, d))

        if primary:
            for i, d in primary:
                if needle in (d.get("name", "").lower()):
                    return i, d["name"], min(d.get("max_input_channels", 1), 2)
            footer = (
                "\nUnset AUDIO_DEVICE_NAME to auto-pick (when PortAudio already lists inputs), "
                "or set AUDIO_DEVICE=<index>."
            )
            if len(devs) == 0:
                footer = (
                    "\nClearing AUDIO_DEVICE_NAME will not help while no devices appear above — "
                    "fix ALSA/USB until `arecord -l` shows a capture card, then rerun."
                )
            raise RuntimeError(
                f'No input device name contains "{AUDIO_DEVICE_NAME}".\n'
                "PortAudio sees:\n"
                + _portaudio_devices_debug()
                + footer
            )

        # Only ALSA placeholders (snd_dummy etc.) exposed — substring match is pointless.
        return pick_input_device()

    return pick_input_device()

DEVICE_INDEX, DEVICE_NAME, DEVICE_CHANNELS = choose_input_device()

# Lock for thread-safe parameter updates between encoder_reader and OLED loop
import threading
_param_lock = threading.Lock()


def _apply_encoder_delta(enc_idx, direction):
    """Apply a single encoder quadrature step immediately (called from encoder_reader at 200Hz).
    
    This decouples parameter updates from the OLED refresh rate for snappier feel.
    Encoders 3–5 (trigger, release, brightness) use a fixed step of one OLED unit per tick
    (no velocity scaling on those parameters).
    
    Args:
        enc_idx: 1=Freq/Q, 2=Thresh, 3=Release/ReleaseMode, 4=Brightness
        direction: 1 for CW, -1 for CCW
    """
    global BRIGHTNESS, RELEASE_MODE_INDEX
    global _brightness_target, _discrete_last_change
    global _reactive_brightness_scale, _effective_brightness_display, _brightness_knob_last_turn
    global _effective_release_display, _release_knob_last_turn
    global ambient_speed, ambient_fade_time
    
    if DEV_NO_HW:
        return
    if time.time() < IGNORE_KNOBS_UNTIL:
        return
    
    with _param_lock:
        base_delta = -1 if direction > 0 else 1  # hardware CW produces inverted quadrature signal
        
        # ===== Encoder 5 (enc_idx=4): Brightness — 1 display unit per click =====
        if enc_idx == 4:
            if not _brightness_off:
                bright_int = max(0, min(99, int(BRIGHTNESS * 99) + base_delta))
                BRIGHTNESS = bright_int / 99.0
                _brightness_target = BRIGHTNESS
                if RELEASE_MODES[RELEASE_MODE_INDEX] in ("bright", "both"):
                    _reactive_brightness_scale = BRIGHTNESS
                    _effective_brightness_display = bright_int
                    _brightness_knob_last_turn = time.time()
            return

        # ===== Encoder 2 (enc_idx=1): Frequency/Q or Speed in AMBIENT mode =====
        if enc_idx == 1:
            if BASE_PROGRAM == 6:
                ambient_speed = max(0.2, min(8.0, ambient_speed + base_delta * 0.1))
            elif _home_enc2_alt:
                # Range (Q): one quadrature step = one OLED unit (0–99), no velocity.
                # Use a stored integer step index so band.q round-trip cannot collapse two ticks to one.
                global _home_enc2_range_pct
                q_min = get_q_min(band.center)
                if _home_enc2_range_pct is None:
                    q_clamped = max(q_min, min(Q_MAX, band.q))
                    q_ratio = math.log(Q_MAX / q_clamped) / math.log(Q_MAX / q_min)
                    _home_enc2_range_pct = int(
                        round(max(0.0, min(1.0, q_ratio)) * 99.0)
                    )
                    _home_enc2_range_pct = max(0, min(99, _home_enc2_range_pct))
                _home_enc2_range_pct = max(0, min(99, _home_enc2_range_pct + base_delta))
                t = _home_enc2_range_pct / 99.0
                band.q = max(q_min, min(Q_MAX, Q_MAX * (q_min / Q_MAX) ** t))
            else:
                # 1Hz per click: snap to integer Hz then step.
                band.center = float(max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, int(band.center) + base_delta)))
            return

        # ===== Encoder 3 (enc_idx=2): Trigger threshold — 1 display unit per click =====
        if enc_idx == 2:
            if BASE_PROGRAM == 6:
                return
            thresh_int = max(0, min(99, int(band.thresh * 99) + base_delta))
            band.thresh = thresh_int / 99.0
            return

        # ===== Encoder 4 (enc_idx=3): Release/ReleaseMode or Fade in AMBIENT mode =====
        if enc_idx == 3:
            if BASE_PROGRAM == 6:
                ambient_fade_time = max(
                    0.1,
                    min(10.0, ambient_fade_time + base_delta * ENC_STEP_AMBIENT_FADE_S),
                )
            elif _home_enc4_alt:
                now = time.time()
                elapsed_ms = (now - _discrete_last_change[3]) * 1000
                if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                    delta = 1 if direction > 0 else -1
                    new_idx = max(0, min(len(RELEASE_MODES) - 1, RELEASE_MODE_INDEX + delta))
                    if new_idx != RELEASE_MODE_INDEX:
                        RELEASE_MODE_INDEX = new_idx
                        _discrete_last_change[3] = now
            else:
                # 1ms per click: snap to integer ms then step.
                decay_int = max(40, min(5000, int(band.decay_ms) + base_delta))
                band.decay_ms = float(decay_int)
                release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                if release_mode in ("react", "rand", "both"):
                    _effective_release_display = decay_int
                    _release_knob_last_turn = time.time()
            return


def update_encoders():
    """Handle time-based animations (brightness fade) for the OLED loop.
    
    Parameter updates are now handled immediately in _apply_encoder_delta() which is
    called directly from encoder_reader() at 200Hz for snappier response.
    
    This function only handles:
    - Brightness fade animation (time-based, needs to run at OLED rate)
    - Clearing any legacy _enc_delta writes for backward compatibility
    """
    global BRIGHTNESS, _enc_delta, _brightness_fading
    
    if DEV_NO_HW:
        return
    
    # Clear any legacy delta writes (parameter updates now happen in _apply_encoder_delta)
    _enc_delta = [0, 0, 0, 0, 0]
    
    # Animate brightness fade (time-based linear interpolation)
    if _brightness_fading:
        now = time.time()
        elapsed = now - _brightness_fade_start_time
        t = min(1.0, elapsed / BRIGHTNESS_FADE_DURATION)  # 0.0 to 1.0 over duration
        
        # Use lock for thread-safe BRIGHTNESS update (also modified by _apply_encoder_delta)
        with _param_lock:
            # Linear interpolation from start value to target
            BRIGHTNESS = _brightness_fade_start_value + t * (_brightness_target - _brightness_fade_start_value)
            
            if t >= 1.0:
                BRIGHTNESS = _brightness_target
                _brightness_fading = False
        
        # Show "OFF" message outside lock (ui_flash doesn't need protection)
        if t >= 1.0 and _brightness_off and _brightness_target == 0.0:
            ui_flash("Brightness: OFF", 0.8)

def toggle_brightness():
    """Toggle brightness between current value and zero with fade animation."""
    global _brightness_saved, _brightness_off, _brightness_target, _brightness_fading
    global _brightness_fade_start_time, _brightness_fade_start_value
    
    # Record fade start state
    _brightness_fade_start_time = time.time()
    _brightness_fade_start_value = BRIGHTNESS
    
    if _brightness_off:
        # Fade back to saved brightness
        _brightness_target = _brightness_saved
        _brightness_off = False
        _brightness_fading = True
        ui_flash(f"Brightness: {int(_brightness_saved * 100)}%", 0.8)
    else:
        # Save current brightness and fade to zero
        _brightness_saved = BRIGHTNESS if BRIGHTNESS > 0.05 else _brightness_saved
        _brightness_target = 0.0
        _brightness_off = True
        _brightness_fading = True

# ===================== GPIO / Rotary Encoders =====================

def save_current_as_default():
    """Save current band params and modes as the selected default preset.
    Called when 'saved!' appears after 1..2..3 countdown - captures release mode
    (RELEASE_MODE_INDEX) at that moment. Threshold mode is always fixed (0)."""
    mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
    # Update in-memory preset with all 6 values (thresh_mode slot always 0 = fixed)
    DEFAULTS_PRESETS[mode_name] = (band.center, band.thresh, band.decay_ms, band.q,
                                    0, RELEASE_MODE_INDEX)
    # Persist to config file
    save_preset_values(mode_name, band.center, band.thresh, band.decay_ms, band.q,
                       0, RELEASE_MODE_INDEX)

def setup_gpio_inputs():
    """Initialize GPIO pins for encoders and reset button.
    
    Returns True on success, False on failure (with error set for TUI display).
    """
    global _mcp, _mcp_pins

    if DEV_NO_HW:
        return True
    if GPIO is None:
        set_error("RPi.GPIO not available - run with DEV_NO_HW=1")
        return False

    try:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        # Direct Pi GPIO inputs: ENC5 switch, reset button, extra button
        GPIO.setup(ENC5_SW_GPIO,      GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(RESET_BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(EXTRA_BUTTON_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    except Exception as e:
        error_str = str(e)
        if "busy" in error_str.lower():
            set_error(f"GPIO busy - kill other processes: sudo pkill -f python")
            print(f"\n[ERROR] GPIO busy - another process is using GPIO pins.")
            print(f"        Fix: sudo pkill -f dmx_audio_react.py && sudo pkill -f board_bringup.py")
            print(f"        Or reboot: sudo reboot\n")
        else:
            set_error(f"GPIO error: {error_str[:50]}")
            print(f"\n[ERROR] GPIO setup failed: {e}\n")
        return False

    if not _MCP_AVAILABLE:
        set_error("adafruit-circuitpython-mcp230xx not installed")
        print("[ERROR] MCP23017 library missing. Install: pip install adafruit-circuitpython-mcp230xx")
        return False

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        _mcp = MCP23017(i2c, address=MCP23017_ADDRESS)

        mcp_input_pins = [
            ENC1_CLK, ENC1_DT, ENC1_SW,
            ENC2_CLK, ENC2_DT, ENC2_SW,
            ENC3_CLK, ENC3_DT, ENC3_SW,
            ENC4_CLK, ENC4_DT, ENC4_SW,
            ENC5_CLK, ENC5_DT,
        ]
        for idx in mcp_input_pins:
            pin = _mcp.get_pin(idx)
            pin.direction = Direction.INPUT
            pin.pull = Pull.UP
            _mcp_pins[idx] = pin

        print(f"[OK] MCP23017 initialized at 0x{MCP23017_ADDRESS:02X}, {len(mcp_input_pins)} encoder pins configured")
        return True

    except Exception as e:
        set_error(f"MCP23017 error: {str(e)[:50]}")
        print(f"\n[ERROR] MCP23017 init failed: {e}\n")
        return False

_enc_last_update_time = [0.0, 0.0, 0.0, 0.0, 0.0]  # Time when delta was last consumed
_enc_update_velocity = [0.0, 0.0, 0.0, 0.0, 0.0]   # Smoothed velocity based on update intervals

def _calc_velocity_multiplier(enc_idx, max_mult=10, min_mult=1.0):
    """Calculate velocity multiplier based on time between update_encoders() calls.
    
    This measures the time between when deltas are CONSUMED (physical detent rate),
    not the internal click rate which is much faster.
    
    Args:
        enc_idx: Encoder index for tracking timing
        max_mult: Maximum multiplier for fast spinning
        min_mult: Minimum multiplier for slow turning (< 1.0 enables precision mode)
    
    Returns min_mult for very slow turning, 1.0 for normal slow turning, 
    up to max_mult for fast spinning."""
    global _enc_last_update_time, _enc_update_velocity
    import math
    
    now = time.time()
    last_update = _enc_last_update_time[enc_idx]
    
    # First update or no history - return 1x
    if last_update == 0:
        _enc_last_update_time[enc_idx] = now
        _enc_update_velocity[enc_idx] = 0
        return 1.0
    
    # Calculate time since last update with non-zero delta
    delta_s = now - last_update
    _enc_last_update_time[enc_idx] = now
    
    # If it's been a while since last update, reset velocity
    if delta_s > 0.8:  # 800ms pause = reset velocity, return 1x
        _enc_update_velocity[enc_idx] = 0
        return 1.0
    
    # Calculate updates per second (physical detent rate)
    if delta_s <= 0:
        return 1.0
    
    updates_per_sec = 1.0 / delta_s
    
    # Exponential smoothing: blend new reading with history
    alpha = 0.5
    _enc_update_velocity[enc_idx] = alpha * updates_per_sec + (1 - alpha) * _enc_update_velocity[enc_idx]
    
    velocity = _enc_update_velocity[enc_idx]
    
    # Map velocity to multiplier with logarithmic scaling
    # Based on PHYSICAL detent rate (updates per second), not internal clicks
    # Velocity thresholds tuned for responsive acceleration:
    PRECISION_VELOCITY = 1.0  # updates/sec - below this = min_mult (>1000ms between detents)
    SLOW_VELOCITY = 2.0       # updates/sec - at this point = 1x (500ms between detents)
    FAST_VELOCITY = 12.0      # updates/sec - above this = max (83ms between detents)
    
    if min_mult < 1.0 and velocity <= PRECISION_VELOCITY:
        # Precision mode: very slow turning gets sub-1x multiplier
        mult = min_mult
    elif velocity <= SLOW_VELOCITY:
        if min_mult < 1.0:
            # Interpolate between min_mult and 1.0 in the precision-to-slow zone
            ratio = (velocity - PRECISION_VELOCITY) / (SLOW_VELOCITY - PRECISION_VELOCITY)
            ratio = max(0.0, min(1.0, ratio))
            mult = min_mult + ratio * (1.0 - min_mult)
        else:
            mult = 1.0
    elif velocity >= FAST_VELOCITY:
        mult = float(max_mult)
    else:
        # Logarithmic interpolation from 1x to max_mult feels more natural
        log_slow = math.log(SLOW_VELOCITY)
        log_fast = math.log(FAST_VELOCITY)
        log_vel = math.log(velocity)
        ratio = (log_vel - log_slow) / (log_fast - log_slow)
        mult = 1.0 + ratio * (max_mult - 1.0)
    
    return max(min_mult, min(float(max_mult), mult))


# ===== gpiozero-based encoder handling (interrupt-driven, more reliable) =====
# NOTE: We import gpiozero lazily inside setup functions to avoid any
# pin-factory initialization side effects at module load time.

# MCP23017 globals — set up in setup_gpio_inputs()
_mcp = None
_mcp_pins = {}  # {mcp_pin_index: digitalio pin object} — used for initial config only
_mcp_snapshot = 0xFFFF  # Last bulk read of all 16 MCP pins (bits 0-15 = GPA0-GPB7)


def _mcp_snapshot_update():
    """Read both GPIO ports in one I2C transaction and cache the result."""
    global _mcp_snapshot
    try:
        _mcp_snapshot = _mcp.gpio  # 16-bit: bits 0-7=GPIOA, bits 8-15=GPIOB
    except Exception:
        pass  # keep previous snapshot on error


def _mcp_read(idx):
    """Read MCP23017 pin from cached snapshot. Returns 1 (HIGH) or 0 (LOW)."""
    return (_mcp_snapshot >> idx) & 1


def _read_encoder_quadrature(enc_idx, clk_idx, dt_idx):
    """Read encoder via MCP23017 polling. Returns 1 (CW), -1 (CCW), 0 (no change)."""
    global _enc_state
    if _mcp is None:
        return 0
    try:
        clk = _mcp_read(clk_idx)
        dt  = _mcp_read(dt_idx)
    except Exception:
        return 0

    new_state = (clk << 1) | dt
    old_state = _enc_state[enc_idx]
    if new_state == old_state:
        return 0
    _enc_state[enc_idx] = new_state

    # Quadrature state machine: CW: 3->1->0->2->3  CCW: 3->2->0->1->3
    if old_state == 0:
        if new_state == 2:
            return 1
        elif new_state == 1:
            return -1
    return 0


def _read_enc1_quadrature(clk_idx, dt_idx):
    """Read Encoder 1 via MCP23017 polling. Returns 1 (CW), -1 (CCW), 0 (no change)."""
    global _enc1_state
    if _mcp is None:
        return 0
    try:
        clk = _mcp_read(clk_idx)
        dt  = _mcp_read(dt_idx)
    except Exception:
        return 0

    new_state = (clk << 1) | dt
    old_state = _enc1_state
    if new_state == old_state:
        return 0
    _enc1_state = new_state

    if old_state == 3:
        if new_state == 1:
            return 0
        elif new_state == 2:
            return 0
    elif old_state == 1:
        if new_state == 0:
            return 0
    elif old_state == 2:
        if new_state == 0:
            return 0
    elif old_state == 0:
        if new_state == 2:
            return 1
        elif new_state == 1:
            return -1
    return 0


def _handle_submenu_value_change(direction):
    """Handle value changes when encoder 1 is in editing mode for submenu columns."""
    global BASE_PROGRAM, CYCLES_BETWEEN_INDEX, CYCLES_BETWEEN, CYCLE_TRIGGER_COUNT, CYCLE_PHASE
    global INPUT_GAIN_DB, DMX_OUTPUT_MODE, DMX_CHANNEL_COUNT
    
    tab = SUBMENU_TABS[submenu_tab]
    col = submenu_column
    
    if tab == "Presets":
        if col == 0:
            # Preset selection (1-6)
            BASE_PROGRAM = max(1, min(6, BASE_PROGRAM + direction))
            CYCLE_TRIGGER_COUNT = 0
            CYCLE_PHASE = 0
            # If AMBIENT preset selected, force mode to "off"
            if BASE_PROGRAM == 6:
                CYCLES_BETWEEN_INDEX = 0  # "off"
            ui_flash(f"Preset: {PROGRAM_NAMES[BASE_PROGRAM-1]}", 0.5)
        elif col == 1:
            # Cycle mode
            CYCLES_BETWEEN_INDEX = max(0, min(len(CYCLES_BETWEEN_MODES) - 1, CYCLES_BETWEEN_INDEX + direction))
            # Reset cycle state when changing modes
            CYCLE_PHASE = 0
            CYCLE_TRIGGER_COUNT = 0
            # For rnd/amb and random modes, ensure we're not on AMBIENT preset
            new_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
            if new_mode in ("rnd/amb", "rnd") and BASE_PROGRAM == 6:
                BASE_PROGRAM = random.randint(1, 5)
            ui_flash(f"Mode: {new_mode}", 0.5)
        elif col == 2:
            # Beat cycles
            global CYCLE_STEPS_INDEX, CYCLE_STEPS
            # If mode is "off", scrolling right enables x+1 at lowest beat value
            if CYCLES_BETWEEN_INDEX == 0:  # "off"
                if direction > 0:  # Scrolling right - enable cycling at 4
                    CYCLES_BETWEEN_INDEX = 1  # "x+1"
                    CYCLE_STEPS_INDEX = 0  # Start at 4
                    CYCLE_STEPS = CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX]
                    CYCLE_PHASE = 0
                    CYCLE_TRIGGER_COUNT = 0
                    ui_flash(f"Beats: {CYCLE_STEPS}", 0.5)
                # Scrolling left when off does nothing
                return
            
            new_index = CYCLE_STEPS_INDEX + direction
            # Scrolling left past min (4) turns off cycling
            if new_index < 0:
                CYCLES_BETWEEN_INDEX = 0  # "off"
                CYCLE_PHASE = 0
                CYCLE_TRIGGER_COUNT = 0
                ui_flash("Beats: --", 0.5)
            elif new_index < len(CYCLE_STEPS_OPTIONS):
                CYCLE_STEPS_INDEX = new_index
                CYCLE_STEPS = CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX]
                ui_flash(f"Beats: {CYCLE_STEPS}", 0.5)
    elif tab == "Settings":
        if col == 0:
            # Input gain in dB (-24 to +24, step by 1dB) - saved to config for persistence
            INPUT_GAIN_DB = max(INPUT_GAIN_MIN_DB, min(INPUT_GAIN_MAX_DB, INPUT_GAIN_DB + direction))
            save_input_gain(INPUT_GAIN_DB)
            ui_flash(f"Gain: {format_input_gain_display()}", 0.5)
        elif col == 1:
            # Reset - cycle through presets (clamped, no looping)
            global DEFAULTS_MODE_INDEX
            new_idx = max(0, min(len(DEFAULTS_MODES) - 1, DEFAULTS_MODE_INDEX + direction))
            DEFAULTS_MODE_INDEX = new_idx
            mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
            preset = DEFAULTS_PRESETS[mode_name]
            if len(preset) >= 6:
                center_hz, thresh, decay_ms, q_factor, thresh_mode, release_mode = preset
                global THRESH_MODE_INDEX, RELEASE_MODE_INDEX
                THRESH_MODE_INDEX = 0
                RELEASE_MODE_INDEX = release_mode
            else:
                center_hz, thresh, decay_ms, q_factor = preset[:4]
            band.center = center_hz
            band.thresh = thresh
            band.decay_ms = decay_ms
            band.q = q_factor
            global _home_enc2_range_pct
            _home_enc2_range_pct = None  # Resync Range ladder from restored band.q
            save_defaults_mode(DEFAULTS_MODE_INDEX)
            ui_flash(f"Reset: {mode_name}", 0.5)
        elif col == 2:
            # Detect column removed (mode locked to "old"). Navigation is also
            # capped at col=1 for Settings, so this branch is defensive only.
            pass
    elif tab == "Setup":
        if col == 0:
            # Output mode (Dimmer/DMX)
            DMX_OUTPUT_MODE = max(0, min(1, DMX_OUTPUT_MODE + direction))
            ui_flash(f"Output: {DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]}", 0.5)
        elif col == 1:
            # Channel count (4-24) - saved to config for persistence
            DMX_CHANNEL_COUNT = max(4, min(24, DMX_CHANNEL_COUNT + direction))
            save_dmx_channel_count(DMX_CHANNEL_COUNT)
            ui_flash(f"Chans: {DMX_CHANNEL_COUNT}", 0.5)
        elif col == 2:
            # Column 3 is blank - no action
            pass


def encoder_reader():
    """Read all 5 rotary encoders for page selection, parameters, and brightness.
    
    Encoder 5's switch toggles brightness on/off with a fade animation.
    Uses non-blocking state machine for button release tracking.
    """
    global encoder1_value, encoder1_button
    global current_page
    global _enc_last_clk, _enc_last_dt, _enc_last_sw, _enc_delta, _reset_last_state, _reset_press_time
    global _enc_state, _enc_count, _enc_last_click_time, _enc_velocity_mult
    global _home_enc2_alt, _home_enc4_alt
    global _enc2_press_time, _enc2_saving, _enc2_save_complete
    global submenu_tab, submenu_column, submenu_editing
    global _enc_awaiting_release
    global _setup_enc3_detect, _setup_enc4_channels
    global _brightness_click_flash, _brightness_enc5sw_state
    global _extra_last_state

    if DEV_NO_HW:
        return
    
    # Initialize all encoder states
    enc_pins = [
        (ENC1_CLK, ENC1_DT),
        (ENC2_CLK, ENC2_DT),
        (ENC3_CLK, ENC3_DT),
        (ENC4_CLK, ENC4_DT),
        (ENC5_CLK, ENC5_DT),
    ]

    for i, (clk_idx, dt_idx) in enumerate(enc_pins):
        try:
            clk = _mcp_read(clk_idx)
            dt  = _mcp_read(dt_idx)
            _enc_state[i] = (clk << 1) | dt
        except Exception:
            _enc_state[i] = 3
        _enc_rotation_dir[i] = 0
        _enc_count[i] = 0

    # Initialize Encoder 1 dedicated state
    global _enc1_state, _enc1_rotation_dir, _enc1_last_click
    try:
        clk = _mcp_read(ENC1_CLK)
        dt  = _mcp_read(ENC1_DT)
        _enc1_state = (clk << 1) | dt
    except Exception:
        _enc1_state = 3
    _enc1_rotation_dir = 0
    _enc1_last_click = 0.0

    # Initialize switch states
    _enc_last_sw[0] = _mcp_read(ENC1_SW)
    _enc_last_sw[1] = _mcp_read(ENC2_SW)
    _enc_last_sw[2] = _mcp_read(ENC3_SW)
    _enc_last_sw[3] = _mcp_read(ENC4_SW)
    try:
        _enc_last_sw[4] = GPIO.input(ENC5_SW_GPIO)
    except Exception:
        _enc_last_sw[4] = 1

    _reset_last_state = GPIO.input(RESET_BUTTON_GPIO)
    _reset_press_time = 0  # Track how long reset button is held
    _extra_last_state = GPIO.input(EXTRA_BUTTON_GPIO)
    
    try:
        while not STOP_THREADS:
            try:
                # Bulk-read all MCP pins in one I2C transaction
                _mcp_snapshot_update()

                # ===== Encoder 1 - Freq / Range =====
                direction = _read_enc1_quadrature(ENC1_CLK, ENC1_DT)
                if direction != 0:
                    _apply_encoder_delta(1, direction)

                enc1_sw = _mcp_read(ENC1_SW)
                if _enc_awaiting_release[0]:
                    if enc1_sw == 1:
                        _enc_awaiting_release[0] = False
                elif enc1_sw == 0 and _enc_last_sw[0] == 1:
                    global _home_enc2_range_pct
                    _home_enc2_alt = not _home_enc2_alt
                    _home_enc2_range_pct = None
                    ui_flash("Mode: Range" if _home_enc2_alt else "Mode: Freq", 0.5)
                    _enc_awaiting_release[0] = True
                _enc_last_sw[0] = enc1_sw

                # ===== Encoder 2 - Threshold =====
                direction = _read_encoder_quadrature(1, ENC2_CLK, ENC2_DT)
                if direction != 0:
                    _apply_encoder_delta(2, direction)

                enc2_sw = _mcp_read(ENC2_SW)
                if _enc_awaiting_release[1]:
                    if enc2_sw == 1:
                        _enc_awaiting_release[1] = False
                elif enc2_sw == 0 and _enc_last_sw[1] == 1:
                    _enc_awaiting_release[1] = True
                _enc_last_sw[1] = enc2_sw

                # ===== Encoder 3 - Release / Release-Mode =====
                direction = _read_encoder_quadrature(2, ENC3_CLK, ENC3_DT)
                if direction != 0:
                    _apply_encoder_delta(3, direction)

                enc3_sw = _mcp_read(ENC3_SW)
                if _enc_awaiting_release[2]:
                    if enc3_sw == 1:
                        _enc_awaiting_release[2] = False
                elif enc3_sw == 0 and _enc_last_sw[2] == 1:
                    global _setup_enc4_channels
                    if SUBMENU_TABS[submenu_tab] == "Settings":
                        _setup_enc4_channels = not _setup_enc4_channels
                        ui_flash("Mode: Chans" if _setup_enc4_channels else "Mode: Setup", 0.5)
                    else:
                        _home_enc4_alt = not _home_enc4_alt
                        ui_flash("Mode: R-Mode" if _home_enc4_alt else "Mode: Release", 0.5)
                    _enc_awaiting_release[2] = True
                _enc_last_sw[2] = enc3_sw

                # ===== Encoder 4 - Brightness =====
                direction = _read_encoder_quadrature(3, ENC4_CLK, ENC4_DT)
                if direction != 0:
                    _apply_encoder_delta(4, direction)

                enc4_sw = _mcp_read(ENC4_SW)
                _brightness_enc5sw_state = enc4_sw
                if enc4_sw == 0 and _enc_last_sw[3] == 1:
                    _brightness_click_flash = 1.0
                    toggle_brightness()
                _enc_last_sw[3] = enc4_sw

                # ===== Encoder 5 - Edit highlighted section value =====
                direction = _read_encoder_quadrature(4, ENC5_CLK, ENC5_DT)
                if direction != 0:
                    _handle_submenu_value_change(direction)

                # Enc5 switch (GPIO17): short press = cycle tabs, long press = save preset
                try:
                    enc5_sw = GPIO.input(ENC5_SW_GPIO)
                except Exception:
                    enc5_sw = 1
                global _enc1_press_time, _enc1_save_progress, _enc1_save_complete
                if _enc_awaiting_release[4]:
                    if enc5_sw == 1:
                        _enc_awaiting_release[4] = False
                else:
                    is_settings_reset = (SUBMENU_TABS[submenu_tab] == "Settings" and
                                         submenu_column == 1)
                    if enc5_sw == 0 and _enc_last_sw[4] == 1:
                        _enc1_press_time = time.time()
                        _enc1_save_progress = 0.0
                    elif enc5_sw == 0 and _enc_last_sw[4] == 0:
                        if _enc1_press_time > 0 and is_settings_reset:
                            hold_duration = time.time() - _enc1_press_time
                            if hold_duration >= 0.15:
                                adjusted_duration = hold_duration - 0.15
                                _enc1_save_progress = min(1.0, adjusted_duration / ENC1_SAVE_HOLD_DURATION)
                            if hold_duration >= (ENC1_SAVE_HOLD_DURATION + 0.15):
                                save_current_as_default()
                                _enc1_press_time = 0.0
                                _enc1_save_progress = 2.0
                                _enc1_save_complete = time.time()
                                _enc_awaiting_release[4] = True
                    elif enc5_sw == 1 and _enc_last_sw[4] == 0:
                        if _enc1_press_time > 0:
                            if _enc1_save_progress == 0:
                                # Short press: cycle tabs
                                submenu_tab = (submenu_tab + 1) % len(SUBMENU_TABS)
                                submenu_column = 0
                                ui_flash(f"Tab: {SUBMENU_TABS[submenu_tab]}", 0.5)
                        _enc1_press_time = 0.0
                        _enc1_save_progress = 0.0
                _enc_last_sw[4] = enc5_sw

                # ===== Reset button (GPIO25): long press (2s) = reset band to defaults =====
                reset_btn = GPIO.input(RESET_BUTTON_GPIO)
                if reset_btn == 0 and _reset_last_state == 1:
                    _reset_press_time = time.time()
                elif reset_btn == 0 and _reset_last_state == 0:
                    if _reset_press_time > 0:
                        hold_duration = time.time() - _reset_press_time
                        if hold_duration >= 2.0:
                            global _fft_reset_msg_until
                            mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
                            preset = DEFAULTS_PRESETS[mode_name]
                            if len(preset) >= 6:
                                center_hz, thresh, decay_ms, q_factor, _, release_mode = preset
                                global RELEASE_MODE_INDEX
                                RELEASE_MODE_INDEX = release_mode
                            else:
                                center_hz, thresh, decay_ms, q_factor = preset[:4]
                            band.center = center_hz
                            band.thresh = thresh
                            band.decay_ms = decay_ms
                            band.q = q_factor
                            _home_enc2_range_pct = None
                            _fft_reset_msg_until = time.time() + 1.0
                            ui_flash("Reset to defaults", 1.0)
                            _reset_press_time = 0
                elif reset_btn == 1 and _reset_last_state == 0:
                    if _reset_press_time > 0:
                        hold_duration = time.time() - _reset_press_time
                        if hold_duration < 2.0:
                            # Short press: cycle tabs
                            submenu_tab = (submenu_tab + 1) % len(SUBMENU_TABS)
                            submenu_column = 0
                            ui_flash(f"Page: {SUBMENU_TABS[submenu_tab]}", 0.5)
                    _reset_press_time = 0
                _reset_last_state = reset_btn

                # ===== Extra button (GPIO7): cycle section within current tab =====
                extra_btn = GPIO.input(EXTRA_BUTTON_GPIO)
                if extra_btn == 0 and _extra_last_state == 1:
                    _tab_name = SUBMENU_TABS[submenu_tab]
                    _max_col = 1 if _tab_name in ("Setup", "Settings") else 2
                    submenu_column = (submenu_column + 1) % (_max_col + 1)
                    _col_label = SUBMENU_LABELS.get(_tab_name, [""])[submenu_column]
                    ui_flash(_col_label if _col_label else _tab_name, 0.4)
                _extra_last_state = extra_btn

            except RuntimeError:
                break

            time.sleep(0.005)  # 5ms polling - balanced for all encoders
    finally:
        pass

# ===================== Audio loop =====================

live_band_env   = 0.0
live_threshold  = band.thresh
input_rms       = 0.0
last_trigger_ts = 0.0
chase_idx       = 0
group34_phase   = 0  # For 1+4/2+3 (program 3)
group12_phase   = 0  # For 1+2/3+4 (program 4)

# Ambient mode state (sized for max 24 channels)
ambient_targets = [0.0] * 24  # Target brightness for each channel (0=off, 1=on)
ambient_current = [0.0] * 24  # Current brightness for each channel
ambient_last_change = [0.0] * 24  # Time of last target change
ambient_speed = 1.0  # Speed multiplier - default 1x (range 0.2x to 8x)
ambient_fade_time = 1.0  # Fade time in seconds - default 1s (range 0.1s to 10s)
_ambient_next_time = 0.0  # Time when next channel switch happens

# Trigger indicator for UI
trigger_flash = 0.0  # Timestamp when last triggered (time.time())
TRIGGER_FLASH_DURATION = 0.08  # How long the flash stays visible (seconds)

bp   = None
envd = None
agc  = Agc(target=AGC_TARGET, tau=0.95, max_gain=AGC_MAX_GAIN)

# Classic detection (bandpass + envelope + AGC): absolute smoothed envelope before UI scaling
classic_abs_ema = 0.0

def get_band_energy(fft_magnitudes, freqs, low_hz, high_hz):
    """Get energy in a frequency band from FFT data.
    Uses interpolation for bands that fall between FFT bins."""
    mask = (freqs >= low_hz) & (freqs < high_hz)
    if np.any(mask):
        return np.mean(fft_magnitudes[mask])
    
    # If no bins in range, interpolate from nearest bins
    center_hz = (low_hz + high_hz) / 2
    # Find the two closest frequency bins
    idx = np.searchsorted(freqs, center_hz)
    if idx == 0:
        return float(fft_magnitudes[0])
    if idx >= len(freqs):
        return float(fft_magnitudes[-1])
    
    # Linear interpolation between adjacent bins
    f_low, f_high = freqs[idx-1], freqs[idx]
    m_low, m_high = fft_magnitudes[idx-1], fft_magnitudes[idx]
    t = (center_hz - f_low) / (f_high - f_low + 1e-10)
    return float(m_low + t * (m_high - m_low))

def audio_loop():
    global bp, envd, live_band_env, live_threshold, input_rms
    global last_trigger_ts, chase_idx, group34_phase, group12_phase
    global PROGRAM, BASE_PROGRAM, CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE, CYCLE_AMBIENT_START
    global APP_STATE, APP_ERROR
    global fft_bands, fft_peaks, fft_peak_times, fft_recent_max
    global three_band_detector, _last_3band_update

    global classic_abs_ema

    bp   = BiquadBandpass(SR, band.center, band.q)
    envd = EnvDetector(SR, attack_ms=8.0, release_ms=80.0)
    classic_abs_ema = 0.0

    # Initialize 3-band onset detector
    three_band_detector = ThreeBandOnsetDetector(SR, n_fft=FFT_SIZE)

    band.attack_ms = DEFAULT_ATTACK_MS
    frame_dt_ms = (HOP / SR) * 1000.0
    was_above = False
    _hysteresis_armed = True
    _adapt_armed = True
    _q_band_smooth = 0.0

    def cb(indata, frames, time_info, status):
        nonlocal was_above, _hysteresis_armed, _adapt_armed, _q_band_smooth
        global live_band_env, live_threshold, input_rms
        global last_trigger_ts, chase_idx, group34_phase, group12_phase
        global PROGRAM, BASE_PROGRAM, CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE, CYCLE_AMBIENT_START
        global fft_bands, fft_peaks, fft_peak_times, fft_recent_max
        global fft_display_bands
        global _recent_min, _effective_thresh
        global _reactive_brightness_scale, _effective_release_display, _effective_brightness_display
        global _trigger_speed_multiplier
        global prev_band_energies, fft_flux, band_running_mean, band_running_max
        global _q_band_running_avg, _flux_recent_max
        global _q_band_recent_max, _q_band_recent_min
        global _fft_hp_y, _fft_hp_x_prev
        global _fft_low_smooth
        global classic_abs_ema

        if not RUNNING:
            return

        # Mono vs stereo — channel selectable via AUDIO_INPUT_CHANNEL (see module constant)
        if indata.shape[1] >= 2:
            if AUDIO_INPUT_CHANNEL_MODE == "left":
                x = indata[:, 0].astype(np.float32)
            elif AUDIO_INPUT_CHANNEL_MODE == "mix":
                x = (0.5 * (indata[:, 0].astype(np.float32) + indata[:, 1].astype(np.float32)))
            else:
                x = indata[:, 1].astype(np.float32)
        else:
            x = indata[:, 0].astype(np.float32)
        x = x * db_to_linear(INPUT_GAIN_DB)  # Apply input gain (dB)
        # Optional high-pass before FFT (off by default for full-range EQ-style display)
        if FFT_HPF_HZ > 0:
            x, _fft_hp_y, _fft_hp_x_prev = apply_fft_highpass(x, SR, FFT_HPF_HZ, _fft_hp_y, _fft_hp_x_prev)
        input_rms = float(np.sqrt(np.mean(x*x)) + 1e-12)
        # Diagnostic: log raw audio levels every ~1 sec when AUDIO_DEBUG=1.
        # Helps verify that audio is actually reaching the FFT pipeline.
        if AUDIO_DEBUG:
            global _audio_dbg_count
            _audio_dbg_count += 1
            if _audio_dbg_count % 40 == 0:
                raw_peak = float(np.max(np.abs(indata.astype(np.float32))))
                raw_l_peak = float(np.max(np.abs(indata[:, 0].astype(np.float32)))) if indata.shape[1] >= 1 else 0.0
                raw_r_peak = float(np.max(np.abs(indata[:, 1].astype(np.float32)))) if indata.shape[1] >= 2 else 0.0
                x_peak_val = float(np.max(np.abs(x)))
                # Convert to dBFS for easier reading
                raw_l_db = 20 * np.log10(raw_l_peak + 1e-10)
                x_peak_db = 20 * np.log10(x_peak_val + 1e-10)
                print(
                    f"[AUDIO_DBG] ch={AUDIO_INPUT_CHANNEL_MODE} "
                    f"raw_L={raw_l_db:.1f}dB x_peak={x_peak_db:.1f}dB "
                    f"gain={INPUT_GAIN_DB}dB indata.shape={indata.shape} "
                    f"status={status}",
                    flush=True,
                )

        # FFT analysis for display - zero-padded for better frequency resolution
        global _HANNING_WINDOW
        if _HANNING_WINDOW is None or len(_HANNING_WINDOW) != len(x):
            _HANNING_WINDOW = np.hanning(len(x)).astype(np.float32)
        padded = np.zeros(FFT_SIZE, dtype=np.float32)
        padded[:len(x)] = x * _HANNING_WINDOW
        fft = np.fft.rfft(padded)
        fft_mag = np.abs(fft) / len(x)  # Normalize by original length
        
        # Pre-compute bin indices on first call (once only)
        global _FFT_BIN_INDICES
        if _FFT_BIN_INDICES is None:
            freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SR)
            _precompute_bin_indices(freqs)
        
        now = time.time()
        
        # Calculate FFT band energies with compensation (VECTORIZED)
        band_energies = get_all_band_energies_vectorized(fft_mag)
        band_energies *= np.array(FFT_COMPENSATION, dtype=np.float32)
        with np.errstate(divide='ignore', invalid='ignore'):
            db_vals = np.where(band_energies > 1e-10, 20.0 * np.log10(band_energies + 1e-10), -100.0)
        raw_levels = np.clip((db_vals + 60.0) / 50.0, 0.0, None)
        
        fft_flux = get_spectral_flux(raw_levels, prev_band_energies)
        if FFT_FLUX_DEADZONE > 0:
            fft_flux = np.maximum(0.0, fft_flux - FFT_FLUX_DEADZONE)
        prev_band_energies = raw_levels.copy()
        
        # Apply per-band normalization (spectral whitening) - SKIPPED for performance
        # The raw_levels are already compensated and work well enough
        whitened_levels = raw_levels  # Use raw levels directly
        
        # Auto-normalize FFT (vectorized for performance)
        current_max = float(np.max(raw_levels)) if len(raw_levels) > 0 else 0
        if current_max > fft_recent_max:
            # Rate-limit scale jumps: avoids full spectrum “twitch” when max bin changes abruptly (common on kicks)
            jump_cap = fft_recent_max * FFT_SCALE_MAX_JUMP_RATIO + FFT_SCALE_MAX_JUMP_ABS
            fft_recent_max = min(current_max, jump_cap)
        else:
            fft_recent_max = fft_recent_max * fft_max_decay
            # Peak collapsed (typical after a kick): ease divisor down so normalized lows aren’t glued / stair-stepping
            if fft_recent_max > 0.12 and current_max < fft_recent_max * FFT_SCALE_COLLAPSE_RATIO:
                floor_from_peak = max(current_max * FFT_SCALE_COLLAPSE_FLOOR_MUL, 0.08)
                fft_recent_max = max(floor_from_peak, fft_recent_max * FFT_SCALE_COLLAPSE_PULL)
        
        norm_factor = max(0.1, fft_recent_max)
        normalized = np.minimum(1.0, raw_levels / norm_factor)
        if FFT_SPECTRAL_FLOOR > 0:
            normalized = np.maximum(0.0, normalized - FFT_SPECTRAL_FLOOR)
        
        if FFT_ANALYZER_RAW:
            # Direct per-hop levels after auto-scale (no EQ-style inertia on the bars).
            fft_bands[:] = normalized
            fft_display_bands[:] = fft_bands
        else:
            # Bar motion: snappier attack so short kicks still reach threshold; smooth decay (EQ-like tail)
            attack_mask = normalized > fft_bands
            fft_bands[attack_mask] = 0.68 * normalized[attack_mask] + 0.32 * fft_bands[attack_mask]
            # Slightly faster decay on lowest bands — they share few FFT bins and otherwise lag the rest of the spectrum
            n_low_dec = min(FFT_LOW_DECAY_BANDS, len(fft_bands))
            if n_low_dec > 0:
                idx = np.arange(len(fft_bands), dtype=np.int32)
                low_m = idx < n_low_dec
                fall_m = (~attack_mask) & low_m
                rest_fall = (~attack_mask) & (~low_m)
                fft_bands[fall_m] = FFT_LOW_DECAY_MULT * fft_bands[fall_m]
                fft_bands[rest_fall] = 0.92 * fft_bands[rest_fall]
            else:
                fft_bands[~attack_mask] = 0.92 * fft_bands[~attack_mask]
            
            # Soften lowest columns only (sub/kick share few bins; auto-scale + leakage = visible flicker)
            nls = min(FFT_LOW_BAND_COUNT, len(fft_bands))
            if nls > 0:
                if _fft_low_smooth.shape[0] != fft_bands.shape[0]:
                    _fft_low_smooth = np.zeros_like(fft_bands)
                c = FFT_LOW_SMOOTH_COEF
                _fft_low_smooth[:nls] = c * _fft_low_smooth[:nls] + (1.0 - c) * fft_bands[:nls]
                fft_bands[:nls] = _fft_low_smooth[:nls]
            
            # Display smoothing: EMA so drawn bars are less choppy (triggers still use raw fft_bands)
            if FFT_DISPLAY_SMOOTH < 1.0:
                fft_display_bands[:] = FFT_DISPLAY_SMOOTH * fft_display_bands + (1.0 - FFT_DISPLAY_SMOOTH) * fft_bands
            else:
                fft_display_bands[:] = fft_bands
        
        # ===== 3-Band Onset Detector ===== (after fft_bands updated this hop)
        global _last_3band_update
        if three_band_detector is not None:
            dt = now - _last_3band_update
            if dt >= 1.0 / THREEBAND_UPDATE_HZ:
                three_band_detector.update_from_fft_bands(fft_bands, FFT_BANDS, dt)
                _last_3band_update = now
        
        # Vectorized peak tracking
        new_peak_mask = fft_bands > fft_peaks
        fft_peaks[new_peak_mask] = fft_bands[new_peak_mask]
        fft_peak_times[new_peak_mask] = now
        
        decay_mask = (~new_peak_mask) & ((now - fft_peak_times) > PEAK_HOLD_TIME)
        fft_peaks[decay_mask] = np.maximum(fft_peaks[decay_mask] * 0.98, fft_bands[decay_mask])

        # Calculate Q range for frequency targeting
        clamped_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center))
        bandwidth = clamped_center / max(0.1, band.q)
        low_freq = max(FFT_MIN_FREQ, clamped_center - bandwidth / 2)
        high_freq = min(FFT_MAX_FREQ, clamped_center + bandwidth / 2)
        
        # Use the SAME fft_bands values that are displayed on screen.
        # Mean across columns whose centers fall inside the Q window — wide bands no longer
        # misfire from a single spiky bin (max was too twitchy); threshold compares to average energy.
        sum_level_in_q = 0.0
        sum_flux_in_q = 0.0
        n_overlap = 0
        for i, (band_low, band_high) in enumerate(FFT_BANDS):
            band_center_freq = math.sqrt(band_low * band_high)
            if low_freq <= band_center_freq <= high_freq:
                sum_level_in_q += float(fft_bands[i])
                sum_flux_in_q += float(fft_flux[i])
                n_overlap += 1
        if n_overlap > 0:
            display_mean_in_q = sum_level_in_q / n_overlap
            q_flux_mean = sum_flux_in_q / n_overlap
        else:
            display_mean_in_q = 0.0
            q_flux_mean = 0.0

        # Per-Q-band normalization: auto-scale the targeted frequency range independently
        # so triggers are consistent regardless of overall mix level changes.
        global _q_band_recent_max, _q_band_recent_min
        raw_q_level = display_mean_in_q
        if Q_BAND_NORM_ENABLED and raw_q_level > 0:
            # Update running max (fast attack, slow decay)
            if raw_q_level > _q_band_recent_max:
                _q_band_recent_max = Q_BAND_NORM_ATTACK * raw_q_level + (1 - Q_BAND_NORM_ATTACK) * _q_band_recent_max
            else:
                _q_band_recent_max = Q_BAND_NORM_DECAY * _q_band_recent_max
            _q_band_recent_max = max(0.05, _q_band_recent_max)  # Floor to prevent division issues
            
            # Update running floor (very slow adaptation)
            if raw_q_level < _q_band_recent_min:
                _q_band_recent_min = Q_BAND_NORM_FLOOR_DECAY * _q_band_recent_min + (1 - Q_BAND_NORM_FLOOR_DECAY) * raw_q_level
            else:
                _q_band_recent_min = min(_q_band_recent_min * 1.001, raw_q_level * 0.3)
            _q_band_recent_min = max(0.001, min(_q_band_recent_min, _q_band_recent_max * 0.5))
            
            # Normalize: map [floor, max] to [0, 1] with some headroom
            q_range = max(0.02, _q_band_recent_max - _q_band_recent_min)
            normalized_q = (raw_q_level - _q_band_recent_min) / q_range
            display_mean_in_q = min(1.0, max(0.0, normalized_q))

        detect_mode = DETECT_MODES[DETECT_MODE_INDEX]
        # Per-mode trigger pipeline (each mode keeps its own running state in module globals).
        # FFT spectrum display above is unaffected; only the trigger compare changes.
        if detect_mode == "compander":
            live_band_env, trigger_score = _trigger_compander(x, bp, envd)
            effective_thresh = band.thresh
        elif detect_mode == "kick":
            live_band_env, trigger_score = _trigger_kick(x, bp)
            # Kick mode: noise-floor-aware effective threshold (never fires below floor)
            effective_thresh = _trigger_kick_effective_thresh()
        elif detect_mode == "old":
            # FFT-window level detector: source is the mean of normalized FFT bands
            # inside band.center ± bandwidth/2 — identical to the highlighted bars
            # on the visible spectrum. Out-of-band content cannot drive the trigger.
            live_band_env, trigger_score = _trigger_old(display_mean_in_q)
            effective_thresh = band.thresh
        else:  # "classic" (default / index 0)
            live_band_env, trigger_score = _trigger_classic(x, bp, envd, agc)
            effective_thresh = band.thresh

        live_threshold = band.thresh

        # Update tracking variable for adaptive threshold display only (no longer drives triggers)
        if live_band_env < _recent_min:
            _recent_min = ADAPT_FLOOR_EMA * _recent_min + (1.0 - ADAPT_FLOOR_EMA) * live_band_env
        else:
            _recent_min = min(_recent_min * 1.005, live_band_env)  # Slowly drift up when env above floor

        above = (trigger_score >= effective_thresh)
        can_fire = ((now - last_trigger_ts)*1000.0 >= REFRACTORY_MS)

        current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
        if current_mode == "x+1":
            # x+1 mode: toggle between base and neighbor preset
            p_base, p_neighbor = program_pair_for_base(BASE_PROGRAM)
            active_prog = p_base if CYCLE_PHASE == 0 else p_neighbor
        elif current_mode == "rnd":
            # Rnd mode: BASE_PROGRAM is the active program (changes on beat)
            active_prog = BASE_PROGRAM
        elif current_mode == "rnd/amb":
            # rnd/amb mode: alternate between random preset and ambient
            if CYCLE_PHASE == 0:
                active_prog = BASE_PROGRAM  # Random preset phase
            else:
                active_prog = 6  # Ambient phase
        else:
            # Mode off - no cycling
            active_prog = BASE_PROGRAM

        PROGRAM = active_prog

        # Decay brightness click indicator (trigger_flash is now time-based, no decay needed)
        global trigger_flash, _brightness_click_flash
        _brightness_click_flash = _brightness_click_flash * 0.9  # Decay click indicator

        # Determine trigger: edge detect on raw trigger_score vs effective_thresh + refractory.
        # The compander/onset/peak-hold pipelines already condition the score so the simple compare is enough.
        thresh_mode = "fixed"
        should_trigger = False

        if BEAT_DETECT_METHOD == 1 and three_band_detector is not None:
            # 3BAND_DETECT mode (legacy alternate): trigger from selected band
            should_trigger = three_band_detector.trigger[THREEBAND_SELECTED]
            _effective_thresh = three_band_detector.get_adaptive_threshold(THREEBAND_SELECTED)
        else:
            _effective_thresh = effective_thresh
            should_trigger = above and not was_above and can_fire

        if should_trigger and active_prog in (1, 2, 3, 4, 5):
            # Calculate time since last trigger for speed multiplier
            time_since_last_ms = (now - last_trigger_ts) * 1000.0
            
            # Speed multiplier vs trigger spacing (TRIG_DEBUG; "both"/"bright" brightness uses threshold excess)
            # Slow triggers (> 1000ms apart) = max multiplier (2.0x)
            # Fast triggers (< 200ms apart) = min multiplier (0.3x)
            if time_since_last_ms >= TRIGGER_SPEED_SLOW_MS:
                _trigger_speed_multiplier = TRIGGER_SPEED_MAX_MULT
            elif time_since_last_ms <= TRIGGER_SPEED_FAST_MS:
                _trigger_speed_multiplier = TRIGGER_SPEED_MIN_MULT
            else:
                # Linear interpolation between min and max multiplier
                speed_range = TRIGGER_SPEED_SLOW_MS - TRIGGER_SPEED_FAST_MS
                t = (time_since_last_ms - TRIGGER_SPEED_FAST_MS) / speed_range
                _trigger_speed_multiplier = TRIGGER_SPEED_MIN_MULT + t * (TRIGGER_SPEED_MAX_MULT - TRIGGER_SPEED_MIN_MULT)
            
            last_trigger_ts = now
            trigger_flash = now  # Record trigger time for flash
            if TRIG_DEBUG:
                print(f"[TRIG] mode={thresh_mode} env={live_band_env:.5f} thr={band.thresh:.5f} prog={active_prog} mult={_trigger_speed_multiplier:.2f}")

            # Calculate effective decay based on release mode
            release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
            effective_decay = band.decay_ms
            
            # Signal level for react release accent; bright/both use threshold excess separately
            boost_amount = live_band_env
            bright_excess = bright_excess_normalized(
                live_band_env, thresh_mode, band, _effective_thresh
            )

            if release_mode == "react":
                # Reactive: release length follows trigger spacing (sparse = long, dense = short),
                # with a small extra from hit strength. Same ms breakpoints as bright-mode speed.
                if (now - _release_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    scale = react_release_scale_from_interval_ms(time_since_last_ms, boost_amount)
                    effective_decay = max(40.0, min(5000.0, band.decay_ms * scale))
                    _effective_release_display = int(effective_decay)
            elif release_mode == "bright":
                # Reactive brightness: further above threshold → more lift from base brightness
                if (now - _brightness_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    boost = bright_excess * BRIGHT_THRESHOLD_EXCESS_MAX_DELTA
                    _reactive_brightness_scale = min(1.0, BRIGHTNESS + boost)
                    _effective_brightness_display = int(_reactive_brightness_scale * 99)
                    _effective_brightness_display = max(0, min(99, _effective_brightness_display))
            elif release_mode == "both":
                # Spacing-based release (same as "react") + threshold-based brightness (same as "bright")
                if (now - _release_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    scale = react_release_scale_from_interval_ms(time_since_last_ms, boost_amount)
                    effective_decay = max(40.0, min(5000.0, band.decay_ms * scale))
                    _effective_release_display = int(effective_decay)
                if (now - _brightness_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    boost = bright_excess * BRIGHT_THRESHOLD_EXCESS_MAX_DELTA
                    _reactive_brightness_scale = min(1.0, BRIGHTNESS + boost)
                    _effective_brightness_display = int(_reactive_brightness_scale * 99)
                    _effective_brightness_display = max(0, min(99, _effective_brightness_display))
            elif release_mode == "rand":
                # Random: add/subtract random value between -500ms and +500ms from current release
                # Check if we're still in the buffer period after knob turn
                if (now - _release_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    # Add random offset between -500ms and +500ms
                    rand_offset = random.randint(-500, 500)
                    effective_decay = max(40.0, min(5000.0, band.decay_ms + rand_offset))
                    # Update display value (in ms)
                    _effective_release_display = int(effective_decay)

            if active_prog == 1:
                # ALL - trigger all configured channels
                trigger_idxs(list(range(DMX_CHANNEL_COUNT)), band.attack_ms, effective_decay)
            elif active_prog == 2:
                # CHASE - cycle through all channels sequentially
                trigger_idxs([chase_idx], band.attack_ms, effective_decay)
                chase_idx = (chase_idx + 1) % DMX_CHANNEL_COUNT
            elif active_prog == 3:
                # GROUPS - first half alternate with second half
                half = DMX_CHANNEL_COUNT // 2
                if group12_phase == 0:
                    trigger_idxs(list(range(0, half)), band.attack_ms, effective_decay)
                    group12_phase = 1
                else:
                    trigger_idxs(list(range(half, DMX_CHANNEL_COUNT)), band.attack_ms, effective_decay)
                    group12_phase = 0
            elif active_prog == 4:
                # ODD/EVEN - odd indices (0,2,4...) alternate with even indices (1,3,5...)
                if group34_phase == 0:
                    trigger_idxs(list(range(0, DMX_CHANNEL_COUNT, 2)), band.attack_ms, effective_decay)
                    group34_phase = 1
                else:
                    trigger_idxs(list(range(1, DMX_CHANNEL_COUNT, 2)), band.attack_ms, effective_decay)
                    group34_phase = 0
            elif active_prog == 5:
                # RANDOM - trigger a random channel from all configured
                random_idx = random.randint(0, DMX_CHANNEL_COUNT - 1)
                trigger_idxs([random_idx], band.attack_ms, effective_decay)
            # Note: active_prog == 6 (AMBIENT) doesn't trigger here - it's handled separately

            # Count beats and cycle presets based on mode
            if current_mode == "x+1":
                CYCLE_TRIGGER_COUNT += 1
                if CYCLE_TRIGGER_COUNT >= CYCLE_STEPS:
                    CYCLE_TRIGGER_COUNT = 0
                    CYCLE_PHASE = 1 - CYCLE_PHASE
            elif current_mode == "rnd":
                CYCLE_TRIGGER_COUNT += 1
                if CYCLE_TRIGGER_COUNT >= CYCLE_STEPS:
                    CYCLE_TRIGGER_COUNT = 0
                    # Randomly select preset 1-5 (excluding AMBIENT)
                    BASE_PROGRAM = random.randint(1, 5)
            elif current_mode == "rnd/amb" and CYCLE_PHASE == 0:
                # rnd/amb preset phase - count beats
                CYCLE_TRIGGER_COUNT += 1
                if CYCLE_TRIGGER_COUNT >= CYCLE_STEPS:
                    CYCLE_TRIGGER_COUNT = 0
                    CYCLE_PHASE = 1  # Switch to ambient phase
                    CYCLE_AMBIENT_START = time.time()

        # Handle rnd/amb ambient phase timing
        if current_mode == "rnd/amb" and CYCLE_PHASE == 1:
            elapsed_seconds = now - CYCLE_AMBIENT_START
            if elapsed_seconds >= CYCLE_STEPS:
                CYCLE_PHASE = 0  # Switch back to preset phase
                BASE_PROGRAM = random.randint(1, 5)  # Pick new random preset

        # Handle AMBIENT mode separately (non-audio-reactive)
        if active_prog == 6:
            update_ambient_mode(frame_dt_ms)
        
        send_dmx(update_lights(frame_dt_ms))
        was_above = above

    # Try to open audio stream, falling back to 1 channel if multi-channel fails
    stream_opened = False
    last_error = None
    channels_to_try = [DEVICE_CHANNELS] if DEVICE_CHANNELS == 1 else [DEVICE_CHANNELS, 1]
    
    for try_channels in channels_to_try:
        if stream_opened:
            break
        try:
            with sd.InputStream(device=DEVICE_INDEX, channels=try_channels, samplerate=SR, blocksize=HOP, callback=cb):
                stream_opened = True
                APP_STATE = "ready"
                if try_channels >= 2:
                    cap = f"stereo→{AUDIO_INPUT_CHANNEL_MODE} (env AUDIO_INPUT_CHANNEL=left|right|mix)"
                else:
                    cap = "mono single channel index 0 only (AUDIO_INPUT_CHANNEL ignored)"
                print(
                    f"[AUDIO] device {DEVICE_INDEX}: {DEVICE_NAME} ({try_channels}ch) {cap}",
                    flush=True,
                )
                while not STOP_THREADS:
                    time.sleep(0.05)
        except Exception as e:
            last_error = e
            if try_channels > 1:
                print(f"[AUDIO] Failed with {try_channels}ch, trying 1ch...", file=sys.stderr)
            continue
    
    if not stream_opened:
        APP_STATE = "error"
        APP_ERROR = f"Audio init failed: {last_error}"
        print(f"[AUDIO][ERROR] {APP_ERROR}", file=sys.stderr, flush=True)
        _set_stop(True)
        _set_run(False)

# ===================== OLED UI (SPI) with FFT =====================

class OledUI:
    """
    SPI OLED display with FFT spectrum analyzer
    Layout:
      - Top half: FFT spectrum (32 bands, full width)
      - Bottom half: Global controls | Page tabs + pot values
    """
    def __init__(self, width=128, height=64, fps=15):
        self.enabled = False
        self.width = width
        self.height = height
        self.period = 1.0 / max(1, fps)
        self._font = None
        self._font_small = None
        self.device = None
        
        if DEV_NO_HW or not _OLED_AVAILABLE:
            return
        try:
            serial = luma_spi(
                device=OLED_SPI_DEV,
                port=0,
                bus_speed_hz=8000000,  # 8MHz SPI (reduce to 4MHz if display glitches)
                gpio_DC=OLED_DC_PIN,
                gpio_RST=OLED_RST_PIN,
            )
            self.device = ssd1322(serial, width=width, height=height, rotate=2, framebuffer=luma_full_frame())
            # luma's default is contrast(0x7F); SSD1322 often looks washed out. Max by default, override with OLED_CONTRAST=0-255
            _oc = os.environ.get("OLED_CONTRAST", "255").strip() or "255"
            try:
                _ocv = int(_oc, 0)
            except ValueError:
                _ocv = 255
            try:
                self.device.contrast(max(0, min(255, _ocv)))
            except Exception:
                pass
            try:
                self._font = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 9
                )
                self._font_small = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 8
                )
            except Exception:
                self._font = ImageFont.load_default()
                self._font_small = self._font
            self.enabled = True
        except Exception as e:
            print(f"[OLED] Init failed: {e}")
            self.enabled = False

    def clear(self):
        if not self.enabled or self.device is None:
            return
        try:
            self.device.clear()
        except Exception:
            pass

    def _draw_text_kerned(self, draw, pos, text, font, fill=OLED_WHITE, kerning=1):
        """Draw text with custom letter spacing (kerning).
        
        Args:
            draw: ImageDraw object
            pos: (x, y) tuple for starting position
            text: String to draw
            font: Font to use
            fill: Fill color (OLED_WHITE for white on OLED)
            kerning: Extra pixels between each character (can be negative)
        """
        x, y = pos
        for char in text:
            draw.text((x, y), char, font=font, fill=fill)
            # Get character width and add kerning
            bbox = font.getbbox(char)
            char_width = bbox[2] - bbox[0] if bbox else 5
            x += char_width + kerning
        return x  # Return final x position

    def _freq_to_x(self, freq, x_start, width):
        """Convert frequency to x position (warped log scale).
        
        Uses the same warping as generate_log_bands() to compress low frequencies.
        """
        if freq <= FFT_MIN_FREQ:
            return x_start
        if freq >= FFT_MAX_FREQ:
            return x_start + width - 1
        
        # Same warp factor as generate_log_bands
        warp = 0.3
        
        log_min = math.log10(FFT_MIN_FREQ)
        log_max = math.log10(FFT_MAX_FREQ)
        log_freq = math.log10(freq)
        
        # Get position in log space (0-1)
        t = (log_freq - log_min) / (log_max - log_min)
        
        # Inverse warp: convert from log position to display position
        # Since bands use t^(1-warp), we need t^(1/(1-warp)) to invert
        t_display = t ** (1.0 / (1.0 - warp))
        
        return int(x_start + t_display * (width - 1))

    def _draw_fft_contrast_vline(self, draw, x, y0, y1, x_min, x_max):
        """Single-pixel vertical dashed marker (visible on black and over white bars)."""
        x = int(round(x))
        x = max(x_min, min(x_max, x))
        y0, y1 = int(y0), int(y1)
        if y0 > y1:
            y0, y1 = y1, y0
        for yy in range(y0, y1 + 1):
            fill = OLED_BLACK if yy % 3 == 2 else OLED_WHITE
            draw.point((x, yy), fill=fill)

    def _draw_fft_contrast_hline(
        self, draw, x0, x1, y, y_min, y_max, x_min, x_max, emphasis=False
    ):
        """Dashed threshold ruler; when emphasis (level above line), thicker 2-row inverse band."""
        y = int(round(y))
        if y < y_min or y > y_max:
            return
        x0, x1 = int(x0), int(x1)
        if x0 > x1:
            x0, x1 = x1, x0
        x0 = max(x_min, x0)
        x1 = min(x_max, x1)
        period = 4 if emphasis else 7
        gap = 2 if emphasis else 2  # black run length at end of period
        for px in range(x0, x1 + 1):
            pos = (px + y) % period
            main_white = pos < period - gap
            draw.point((px, y), fill=OLED_WHITE if main_white else OLED_BLACK)
            if emphasis and y - 1 >= y_min:
                # Inverted row above: reads as a 2px-tall zipper, obvious vs idle single row
                draw.point(
                    (px, y - 1), fill=OLED_BLACK if main_white else OLED_WHITE
                )

    def _draw_env_meter_h(self, draw, x, y, width, height, env_val, thr_eff):
        """Horizontal bar under spectrum: white fill from left = envelope; dashed | = threshold."""
        if width < 8 or height < 3:
            return
        draw.rectangle((x, y, x + width - 1, y + height - 1), outline=OLED_WHITE)
        inner_x0 = x + 1
        inner_x1 = x + width - 2
        inner_y0 = y + 1
        inner_y1 = y + height - 2
        iw = inner_x1 - inner_x0 + 1
        ih = inner_y1 - inner_y0 + 1
        if iw < 1 or ih < 1:
            return
        ev = max(0.0, min(1.0, float(env_val)))
        tw = int(ev * iw)
        if tw > 0:
            draw.rectangle(
                (inner_x0, inner_y0, inner_x0 + tw - 1, inner_y1), fill=OLED_WHITE
            )
        te = max(0.0, min(1.0, float(thr_eff)))
        tx = inner_x0 + int(te * iw)
        tx = max(inner_x0, min(inner_x1, tx))
        env_edge = inner_x0 + tw - 1 if tw > 0 else inner_x0 - 1
        for py in range(inner_y0, inner_y1 + 1):
            if (py + tx) % 2 != 0:
                continue
            on_white = tw > 0 and inner_x0 <= tx <= env_edge
            draw.point((tx, py), fill=OLED_BLACK if on_white else OLED_WHITE)

    def _draw_fft_spectrum(self, draw, x, y, width, height):
        """Draw FFT spectrum with Q band highlighting.
        - Env meter is drawn below this region (see render_once / _draw_env_meter_h).
        - Q styling: white bars inside the selected Freq/Q range; gray outside (vertical markers show edges).
        - Q range boundaries: thin dashed vertical markers
        - Optional dashed threshold ruler (env FFT_SPECTRUM_THRESH_LINE=1); default off — use env meter below."""
        
        # FFT spectrum uses full width
        fft_x = x
        fft_width = width
        
        num_bands = len(fft_bands)
        
        # Calculate Q bandwidth for highlighting
        clamped_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center))
        bandwidth = clamped_center / max(0.1, band.q)
        low_freq = max(FFT_MIN_FREQ, clamped_center - bandwidth / 2)
        high_freq = min(FFT_MAX_FREQ, clamped_center + bandwidth / 2)
        
        low_x = self._freq_to_x(low_freq, fft_x, fft_width)
        high_x = self._freq_to_x(high_freq, fft_x, fft_width)
        q_x0 = min(low_x, high_x)
        q_x1 = max(low_x, high_x)
        # Threshold line position: _effective_thresh is 0.0-1.0, UI shows 0-99
        # thresh=0 (UI 0) = line at bottom, thresh=1.0 (UI 99) = line at top
        thresh_y = y + height - int(_effective_thresh * height)
        
        # Use display-smoothed bands (temporal EMA + optional spatial); triggers still use raw fft_bands
        draw_bands = _smooth_fft_display_bands(fft_display_bands)
        bar_step = fft_width / num_bands
        any_q_above_thresh = False

        for i, level in enumerate(draw_bands):
            bx_start = fft_x + int(i * bar_step)
            bx_end = fft_x + int((i + 1) * bar_step) - 1

            if bx_end >= fft_x + fft_width:
                bx_end = fft_x + fft_width - 1
            if bx_start >= fft_x + fft_width:
                continue

            bar_h = int(level * height * FFT_VISUAL_GAIN)
            if bar_h <= 0:
                continue

            bar_h = min(bar_h, height)

            bar_top = y + height - bar_h
            bar_bottom = y + height - 1

            sx = max(bx_start, q_x0)
            ex = min(bx_end, q_x1)
            q_overlap = sx <= ex

            # Outside selected frequency range: gray. Inside range: white.
            if bx_start < q_x0:
                left_end = min(bx_end, q_x0 - 1)
                if bx_start <= left_end:
                    draw.rectangle(
                        (bx_start, bar_top, left_end, bar_bottom), fill=OLED_GRAY
                    )
            if bx_end > q_x1:
                right_start = max(bx_start, q_x1 + 1)
                if right_start <= bx_end:
                    draw.rectangle(
                        (right_start, bar_top, bx_end, bar_bottom), fill=OLED_GRAY
                    )

            if q_overlap:
                if bar_top < thresh_y:
                    any_q_above_thresh = True
                draw.rectangle((sx, bar_top, ex, bar_bottom), fill=OLED_WHITE)
        
        x_right = fft_x + fft_width - 1
        y_bottom = y + height - 1
        # Q range boundaries: contrast stripes (plain white vlines vanish on white bars)
        self._draw_fft_contrast_vline(draw, low_x, y, y_bottom, fft_x, x_right)
        self._draw_fft_contrast_vline(draw, high_x, y, y_bottom, fft_x, x_right)
        
        # Threshold ruler (visual only; gray/white Q fill still uses thresh_y above)
        if FFT_SPECTRUM_THRESH_LINE and y <= thresh_y <= y_bottom:
            self._draw_fft_contrast_hline(
                draw,
                q_x0,
                q_x1,
                thresh_y,
                y,
                y_bottom,
                fft_x,
                x_right,
                emphasis=any_q_above_thresh,
            )

    def _format_freq_range(self, f_lo, f_hi):
        """Format frequency range for display (e.g., '2.5k-4.5k')."""
        def fmt(f):
            if f >= 1000:
                val = f"{f/1000:.1f}k"
                # Remove trailing .0 but keep other decimals
                if val.endswith('.0k'):
                    val = val[:-3] + 'k'
                return val
            return str(int(f))
        return f"{fmt(f_lo)}-{fmt(f_hi)}"

    def _draw_3band_vu(self, draw, x, y, width, height):
        """Draw 3-band visualization.
        
        View 0 (Spectrum): FFT with selected band highlighted
        View 1 (Bands): Three rectangles with LOW/MID/HIGH text, trigger border
        View 2 (Detail): Selected band detail with all parameters
        """
        if three_band_detector is None:
            return
        
        if THREEBAND_VIEW_MODE == 0:
            self._draw_3band_spectrum_view(draw, x, y, width, height)
        elif THREEBAND_VIEW_MODE == 1:
            self._draw_3band_rectangles_view(draw, x, y, width, height)
        else:
            self._draw_3band_detail_view(draw, x, y, width, height)
    
    def _draw_3band_spectrum_view(self, draw, x, y, width, height):
        """FFT spectrum with selected band highlighted, plus onset meter showing what triggers.
        
        Layout:
        - Left: Small onset meter (shows actual trigger signal with threshold)
        - Right: FFT spectrum with selected band range highlighted
        """
        # Get selected band info
        sel = THREEBAND_SELECTED
        sel_f_lo = three_band_detector.bands[sel].f_lo
        sel_f_hi = three_band_detector.bands[sel].f_hi
        # Use display_thresh which moves with threshold adjustment
        threshold = three_band_detector.display_thresh[sel]
        onset = three_band_detector.display_flux[sel]  # Use display_flux to match threshold scale
        triggered = three_band_detector.is_flash_active(sel)
        
        # === Left side: Small onset meter (10px wide) ===
        meter_width = 10
        meter_x = x + 1
        meter_height = height - 2
        meter_y = y + 1
        
        # Draw meter outline
        draw.rectangle((meter_x, meter_y, meter_x + meter_width - 1, meter_y + meter_height - 1), outline=OLED_WHITE)
        
        # Draw onset level (filled from bottom) - this is what actually triggers
        onset_height = int(min(1.0, onset) * (meter_height - 2))
        if onset_height > 0:
            fill_y = meter_y + meter_height - 1 - onset_height
            if triggered:
                # Solid fill with inverted top portion when triggered (faster than checkerboard)
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=OLED_WHITE)
                # Add horizontal lines for visual distinction
                for py in range(fill_y, meter_y + meter_height - 1, 3):
                    draw.line((meter_x + 1, py, meter_x + meter_width - 2, py), fill=OLED_BLACK)
            else:
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=OLED_WHITE)
        
        # No threshold line in 3-band mode (TouchDesigner-style detection doesn't use visual threshold)
        
        # === Right side: FFT spectrum ===
        fft_x = meter_x + meter_width + 3
        fft_width = width - meter_width - 4  # Use more of the available width
        fft_height = height
        num_bands = len(fft_bands)
        draw_bands = _smooth_fft_display_bands(fft_display_bands)
        
        # Calculate bar positions to fill the entire width (no gaps)
        bar_step = fft_width / num_bands
        
        # Get x positions for selected band boundaries (relative to fft_x)
        sel_low_x = self._freq_to_x(sel_f_lo, fft_x, fft_width)
        sel_high_x = self._freq_to_x(sel_f_hi, fft_x, fft_width)
        
        # Draw FFT bars
        for i, level in enumerate(draw_bands):
            bx_start = fft_x + int(i * bar_step)
            bx_end = fft_x + int((i + 1) * bar_step) - 1
            
            if bx_end >= fft_x + fft_width:
                bx_end = fft_x + fft_width - 1
            if bx_start >= fft_x + fft_width:
                continue
            
            bar_h = int(level * fft_height * FFT_VISUAL_GAIN)
            if bar_h <= 0:
                continue
            
            # Clip to view height (bars can exceed view with visual gain)
            bar_h = min(bar_h, fft_height)
            
            # Get the center frequency of this FFT band
            band_low, band_high = FFT_BANDS[i]
            band_center = math.sqrt(band_low * band_high)
            
            # Check if this FFT band is within the selected 3-band range
            in_selected_range = (band_center >= sel_f_lo and band_center <= sel_f_hi)
            
            bar_top = y + fft_height - bar_h
            bar_bottom = y + fft_height - 1
            
            if in_selected_range:
                # Solid fill for bars in selected range (no threshold crosshatch in 3-band mode)
                draw.rectangle((bx_start, bar_top, bx_end, bar_bottom), fill=OLED_WHITE)
            else:
                # Outline only for bars outside selected range
                draw.line((bx_start, bar_top, bx_end, bar_top), fill=OLED_WHITE)
                if bar_h > 1:
                    draw.line((bx_start, bar_top, bx_start, bar_bottom), fill=OLED_WHITE)
                    draw.line((bx_end, bar_top, bx_end, bar_bottom), fill=OLED_WHITE)
        
        fft_x_right = fft_x + fft_width - 1
        fft_y_bottom = y + fft_height - 1
        self._draw_fft_contrast_vline(
            draw, sel_low_x, y, fft_y_bottom, fft_x, fft_x_right
        )
        self._draw_fft_contrast_vline(
            draw, sel_high_x, y, fft_y_bottom, fft_x, fft_x_right
        )
        
        # No threshold line in 3-band mode
        
        # Draw trigger flash at top of selected range
        if triggered:
            flash_height = 3
            draw.rectangle((sel_low_x + 1, y, sel_high_x - 1, y + flash_height), fill=OLED_WHITE)
    
    def _draw_3band_rectangles_view(self, draw, x, y, width, height):
        """Three rectangles with LOW/MID/HIGH text, selected has border, trigger fills inside."""
        band_names = ["LOW", "MID", "HIGH"]
        
        # Calculate rectangle dimensions with padding for selection border
        padding = 2  # Space for selection border on edges
        gap = 3  # Gap between rectangles
        usable_width = width - padding * 2  # Leave room for selection border on left/right
        rect_width = (usable_width - gap * 2) // 3
        rect_height = height - 6  # Leave margin for selection border top/bottom
        rect_y = y + 3
        
        for i in range(3):
            rect_x = x + padding + i * (rect_width + gap)
            is_selected = (i == THREEBAND_SELECTED)
            triggered = three_band_detector.is_flash_active(i)
            
            # Selected band: double border (outer indicator)
            if is_selected:
                draw.rectangle((rect_x - 2, rect_y - 2, rect_x + rect_width + 1, rect_y + rect_height + 1), outline=OLED_WHITE)
                draw.rectangle((rect_x, rect_y, rect_x + rect_width - 1, rect_y + rect_height - 1), outline=OLED_WHITE)
            else:
                # Non-selected: single outline
                draw.rectangle((rect_x, rect_y, rect_x + rect_width - 1, rect_y + rect_height - 1), outline=OLED_WHITE)
            
            # Trigger: fill inside the rectangle
            if triggered:
                draw.rectangle((rect_x + 2, rect_y + 2, rect_x + rect_width - 3, rect_y + rect_height - 3), fill=OLED_WHITE)
            
            # Draw band name centered in rectangle
            label = band_names[i]
            text_width = len(label) * 5
            text_x = rect_x + (rect_width - text_width) // 2
            text_y = rect_y + (rect_height - 8) // 2
            
            # Invert text color when triggered (so it's visible on filled background)
            fill_color = OLED_BLACK if triggered else OLED_WHITE
            draw.text((text_x, text_y), label, font=self._font_small, fill=fill_color)
    
    def _draw_3band_detail_view(self, draw, x, y, width, height):
        """Selected band detail view with VU meter, info, and running line graph.
        
        Layout:
        - Left: VU meter (onset level)
        - Middle: Info (band name, freq range)
        - Right: Running line graph showing onset over time with trigger markers
        """
        band_names = ["LOW", "MID", "HIGH"]
        sel = THREEBAND_SELECTED
        band_cfg = three_band_detector.bands[sel]
        
        # Get display values
        onset = three_band_detector.display_flux[sel]
        f_lo = band_cfg.f_lo
        f_hi = band_cfg.f_hi
        triggered = three_band_detector.is_flash_active(sel)
        
        # === Left side: VU meter (12px wide) ===
        meter_width = 12
        meter_x = x + 2
        meter_height = height - 2
        meter_y = y + 1
        
        # Draw meter outline
        draw.rectangle((meter_x, meter_y, meter_x + meter_width - 1, meter_y + meter_height - 1), outline=OLED_WHITE)
        
        # Draw onset level (filled from bottom)
        onset_height = int(min(1.0, onset) * (meter_height - 2))
        if onset_height > 0:
            fill_y = meter_y + meter_height - 1 - onset_height
            if triggered:
                # Solid fill with horizontal lines when triggered (faster than checkerboard)
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=OLED_WHITE)
                for py in range(fill_y, meter_y + meter_height - 1, 3):
                    draw.line((meter_x + 1, py, meter_x + meter_width - 2, py), fill=OLED_BLACK)
            else:
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=OLED_WHITE)
        
        # No threshold line in 3-band mode (TouchDesigner-style detection doesn't use visual threshold)
        
        # === Middle: Info ===
        info_x = meter_x + meter_width + 4
        
        # Row 1: Band name + trigger indicator
        draw.text((info_x, y), band_names[sel], font=self._font_small, fill=OLED_WHITE)
        trig_box_x = info_x + 22
        trig_box_size = 6
        if triggered:
            draw.rectangle((trig_box_x, y, trig_box_x + trig_box_size, y + trig_box_size), fill=OLED_WHITE)
        else:
            draw.rectangle((trig_box_x, y, trig_box_x + trig_box_size, y + trig_box_size), outline=OLED_WHITE)
        
        # Row 2: Frequency range
        range_str = self._format_freq_range(f_lo, f_hi)
        draw.text((info_x, y + 9), range_str, font=self._font_small, fill=OLED_WHITE)
        
        # === Right side: Running line graph ===
        graph_width = 64  # Wider graph
        graph_x = x + width - graph_width - 2
        graph_y = y + 1
        graph_height = height - 2
        
        # Draw graph outline
        draw.rectangle((graph_x, graph_y, graph_x + graph_width - 1, graph_y + graph_height - 1), outline=OLED_WHITE)
        
        # No threshold line in 3-band mode (TouchDesigner-style detection doesn't use visual threshold)
        
        # Get history data (convert deque to list for faster indexed access)
        onset_history = list(three_band_detector.onset_history[sel])
        trigger_history = list(three_band_detector.trigger_history[sel])
        
        # Calculate how many samples to show (fit to graph width)
        num_samples = min(len(onset_history), graph_width - 2)
        start_idx = len(onset_history) - num_samples
        
        # Pre-calculate y-scale factor
        y_scale = graph_height - 3
        base_y = graph_y + graph_height - 2
        trig_top = graph_y + 1
        
        # Draw the onset line graph
        prev_px, prev_py = None, None
        for i in range(num_samples):
            hist_idx = start_idx + i
            onset_val = onset_history[hist_idx]
            if onset_val > 1.0:
                onset_val = 1.0
            
            px = graph_x + 1 + i
            py = base_y - int(onset_val * y_scale)
            
            # Draw line segment from previous point
            if prev_px is not None:
                draw.line((prev_px, prev_py, px, py), fill=OLED_WHITE)
            
            # Draw trigger marker (vertical line from bottom when triggered)
            if trigger_history[hist_idx]:
                draw.line((px, base_y, px, trig_top), fill=OLED_WHITE)
            
            prev_px, prev_py = px, py

    def _draw_sun_icon(self, draw, x, y, size=7):
        """Draw sun icon for brightness."""
        cx, cy = x + size // 2, y + size // 2
        draw.rectangle((cx - 1, cy - 1, cx, cy), fill=OLED_WHITE)
        draw.point((cx, y), fill=OLED_WHITE)
        draw.point((cx, y + size - 1), fill=OLED_WHITE)
        draw.point((x, cy), fill=OLED_WHITE)
        draw.point((x + size - 1, cy), fill=OLED_WHITE)
        draw.point((x + 1, y + 1), fill=OLED_WHITE)
        draw.point((x + size - 2, y + 1), fill=OLED_WHITE)
        draw.point((x + 1, y + size - 2), fill=OLED_WHITE)
        draw.point((x + size - 2, y + size - 2), fill=OLED_WHITE)

    def _draw_global_controls(self, draw, x, y):
        """Draw program number and brightness percentage."""
        # Program number - show cycling state if active
        current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
        if current_mode == "x+1" and CYCLE_PHASE == 1:
            _, neighbor = program_pair_for_base(BASE_PROGRAM)
            draw.text((x, y), f"(P{neighbor})", font=self._font_small, fill=OLED_WHITE)
        elif current_mode == "rnd/amb" and CYCLE_PHASE == 1:
            # Show (P6) when in rnd/amb ambient phase
            draw.text((x, y), f"(P6)", font=self._font_small, fill=OLED_WHITE)
        else:
            draw.text((x, y), f"P{BASE_PROGRAM}", font=self._font_small, fill=OLED_WHITE)
        
        # Sun icon + brightness percentage (reactive bright/both: live value like release column)
        self._draw_sun_icon(draw, x, y + 10, size=7)
        release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
        base_brt = int(_display_bright * 100)
        if release_mode in ("bright", "both"):
            brt_pct = _effective_brightness_display
        else:
            brt_pct = base_brt
        draw.text((x + 9, y + 11), f"{brt_pct:2d}", font=self._font_small, fill=OLED_WHITE)
    
    def _draw_brightness_inline(self, draw, x, y):
        """Draw sun icon + brightness percentage inline."""
        self._draw_sun_icon(draw, x, y, size=7)
        release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
        base_brt = int(_display_bright * 100)
        if release_mode in ("bright", "both"):
            brt_pct = _effective_brightness_display
        else:
            brt_pct = base_brt
        draw.text((x + 9, y + 1), f"{brt_pct:2d}", font=self._font_small, fill=OLED_WHITE)
    
    def _draw_trigger_indicator(self, draw, x, y):
        """Draw trigger indicator dot at specified position."""
        flash_active = (time.time() - trigger_flash) < TRIGGER_FLASH_DURATION
        if flash_active:
            # Draw filled circle (trigger active)
            draw.ellipse((x, y, x + 6, y + 6), fill=OLED_WHITE)
        else:
            # Draw empty circle (trigger idle)
            draw.ellipse((x, y, x + 6, y + 6), outline=OLED_WHITE)

    def _draw_page_icon(self, draw, x, y, page_name, selected):
        """Draw a single page icon (11x11 box with 9x9 icon inside)."""
        box_size = 11
        
        # Draw box (filled if selected)
        if selected:
            draw.rectangle((x, y, x + box_size - 1, y + box_size - 1), outline=OLED_WHITE, fill=OLED_WHITE)
            fill_color = OLED_BLACK
        else:
            draw.rectangle((x, y, x + box_size - 1, y + box_size - 1), outline=OLED_WHITE, fill=OLED_BLACK)
            fill_color = OLED_WHITE
        
        # Draw icon pixels (offset by 1 to center in box)
        icon_coords = PAGE_ICONS.get(page_name, [])
        for px, py in icon_coords:
            draw.point((x + 1 + px, y + 1 + py), fill=fill_color)

    def _draw_page_tabs(self, draw, x, y):
        """Draw page tabs as icons (fixed size)."""
        icon_box_size = 11
        spacing = 2
        pages = get_pages()
        
        for i, page in enumerate(pages):
            ix = x + i * (icon_box_size + spacing)
            self._draw_page_icon(draw, ix, y, page, i == current_page)
    
    def _draw_page_tabs_wide(self, draw, x, y, total_width, reserved_right=0):
        """Draw page tabs as icons, expanded to fill total_width minus reserved space.
        Always uses _MAX_PAGES for consistent spacing regardless of current mode."""
        tabs_area_width = total_width - reserved_right
        pages = get_pages()
        # Always use _MAX_PAGES for consistent tab width
        tab_width = tabs_area_width // _MAX_PAGES
        icon_size = 9  # The actual icon is 9x9
        box_height = 11
        
        for i, page in enumerate(pages):
            tab_x = x + i * tab_width
            tab_end_x = tab_x + tab_width - 2  # -2 for 1px gap between tabs
            
            selected = (i == current_page)
            
            # Draw tab rectangle
            if selected:
                draw.rectangle((tab_x, y, tab_end_x, y + box_height - 1), outline=OLED_WHITE, fill=OLED_WHITE)
                fill_color = OLED_BLACK
            else:
                draw.rectangle((tab_x, y, tab_end_x, y + box_height - 1), outline=OLED_WHITE, fill=OLED_BLACK)
                fill_color = OLED_WHITE
            
            # Center the icon within the tab
            icon_offset_x = (tab_width - 2 - icon_size) // 2
            icon_offset_y = (box_height - icon_size) // 2
            
            # Draw icon pixels
            icon_coords = PAGE_ICONS.get(page, [])
            for px, py in icon_coords:
                draw.point((tab_x + icon_offset_x + px, y + icon_offset_y + py), fill=fill_color)

    def _draw_pot_values(self, draw, x, y, width):
        """Draw pot values for current page with appropriate formatting.
        Uses locked display values when pots are stable."""
        global _display_freq, _display_thresh, _display_q, _display_bright, _display_q_pct, _display_release
        
        pages = get_pages()
        page_name = pages[current_page]
        labels = PAGE_POT_LABELS[page_name]
        
        # Override labels for HOME page when in AMBIENT mode
        if page_name == "HOME" and BASE_PROGRAM == 6:
            labels = ["Speed", "--", "Fade"]
        # Override labels for HOME page in 3BAND mode
        elif page_name == "HOME" and BEAT_DETECT_METHOD == 1:
            labels = [
                "Range" if _3band_enc2_range_mode else "Band",
                "Gain" if _3band_enc3_gain_mode else "Trigger",
                "R-Mode" if _home_enc4_alt else "Release"
            ]
        # Override labels for HOME page based on encoder toggle states (FFT mode)
        elif page_name == "HOME":
            labels = [
                "Range" if _home_enc2_alt else "Freq",
                "Thresh",
                "R-Mode" if _home_enc4_alt else "Release"
            ]
        # Override labels for SET page based on encoder toggle states
        elif page_name == "SET":
            labels = [
                labels[0],  # Default
                "Thresh",
                "Chans" if _setup_enc4_channels else "Setup"
            ]
        # Override labels for COLOR page based on encoder 3 toggle state
        elif page_name == "COLOR":
            labels = [
                "Lights",
                "Temp" if _color_enc3_temp_mode else "Hue",
                "Sat"
            ]
        
        # Use actual values directly - pot smoothing handles stability
        _display_freq = band.center
        _display_thresh = band.thresh
        _display_q = band.q
        _display_bright = BRIGHTNESS
        
        # Draw labels and values with even spacing
        # Each pot gets width/3 space, but we add padding between them
        spacing = 2  # Extra pixels between columns
        col_width = (width - spacing * 2) // 3
        
        # Kerning value for letter spacing (1 = 1 extra pixel between chars)
        # Use tighter kerning (0) for longer labels/values to fit on screen
        # Use extra tight kerning (-1) for very long values
        tight_labels = {"R-Mode"}  # Labels that need tighter kerning
        tight_values = {"rnd/amb", "rnd"}  # Values that need tighter kerning (kern=0)
        extra_tight_values = set()  # Values that need extra tight kerning (kern=-1)
        
        for i in range(3):
            px = x + i * (col_width + spacing)
            label_kern = 0 if labels[i] in tight_labels else 1
            self._draw_text_kerned(draw, (px, y), labels[i], self._font_small, fill=OLED_WHITE, kerning=label_kern)
            
            # Format value based on page and pot (using smoothed display values)
            if page_name == "HOME":
                # Check if in AMBIENT mode
                if BASE_PROGRAM == 6:
                    if i == 0:  # Speed - show as multiplier
                        val_str = f"{ambient_speed:.1f}x"
                    elif i == 1:  # Nothing
                        val_str = "--"
                    else:  # Fade time - show in seconds
                        val_str = f"{ambient_fade_time:.1f}s"
                elif BEAT_DETECT_METHOD == 1 and three_band_detector is not None:
                    # 3BAND mode
                    band_cfg = three_band_detector.bands[THREEBAND_SELECTED]
                    if i == 0:  # Band name or Range (based on toggle)
                        if _3band_enc2_range_mode:
                            # Range mode - show frequency range
                            val_str = self._format_freq_range(band_cfg.f_lo, band_cfg.f_hi)
                        else:
                            # Band select mode - show band name
                            val_str = THREEBAND_NAMES[THREEBAND_SELECTED]
                    elif i == 1:  # Sensitivity or Gain (based on toggle)
                        if _3band_enc3_gain_mode:
                            # Gain mode - show in dB relative to new center (6.3x = 0dB)
                            # gain 6.3 = 0dB (center), gain 0.63 = -20dB, gain 63 = +20dB
                            gain_db = 20 * math.log10(max(0.001, band_cfg.gain / 6.3))
                            val_str = f"{gain_db:+.1f}dB"
                        else:
                            # Show trigger threshold as 0-99 (centered at 50 = 0.068)
                            # trigger_thresh 0.068 = 50 (center), lower thresh = higher display
                            # Range: 0.01 (very sensitive, display 99) to 0.5 (less sensitive, display 0)
                            # Linear mapping: 50 at 0.068, 99 at ~0.01, 0 at ~0.5
                            if band_cfg.trigger_thresh <= 0.068:
                                # More sensitive than center: 50-99
                                trig_pct = 50 + int((0.068 - band_cfg.trigger_thresh) / 0.058 * 49)
                            else:
                                # Less sensitive than center: 0-50
                                trig_pct = 50 - int((band_cfg.trigger_thresh - 0.068) / 0.432 * 50)
                            trig_pct = max(0, min(99, trig_pct))
                            val_str = f"{trig_pct}"
                    else:  # Release or ReleaseMode (based on toggle)
                        if _home_enc4_alt:
                            # ReleaseMode - show current mode name
                            val_str = RELEASE_MODES[RELEASE_MODE_INDEX]
                        else:
                            # Release - show in ms (or seconds if >= 1000ms)
                            release_ms = int(band.decay_ms)
                            release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                            if release_mode in ("react", "rand", "both"):
                                val_str = format_release_display(_effective_release_display)
                            else:
                                val_str = format_release_display(release_ms)
                else:
                    # FFT_STANDARD mode
                    if i == 0:  # Frequency or Q (based on toggle)
                        if _home_enc2_alt:
                            # Q / Range: show encoder ladder every detent (no hysteresis).
                            # _home_enc2_range_pct is authoritative after first turn in Range mode.
                            if _home_enc2_range_pct is not None:
                                _display_q_pct = _home_enc2_range_pct
                                val_str = f"{_home_enc2_range_pct}"
                            else:
                                q_min = get_q_min(_display_freq)
                                q_ratio = math.log(Q_MAX / max(q_min, _display_q)) / math.log(Q_MAX / q_min)
                                q_pct = round(q_ratio * 99)
                                q_pct = max(0, min(99, q_pct))
                                _display_q_pct = q_pct
                                val_str = f"{q_pct}"
                        else:
                            # Freq mode - show in Hz (with tenths for kHz)
                            freq_hz = _display_freq
                            if freq_hz >= 1000:
                                # Show to tenths place for kHz (e.g., "1.2kHz", "10.5kHz")
                                freq_khz = freq_hz / 1000.0
                                if freq_khz >= 10:
                                    val_str = f"{freq_khz:.1f}kHz"
                                else:
                                    val_str = f"{freq_khz:.1f}kHz"
                            else:
                                val_str = f"{int(freq_hz)}Hz"
                    elif i == 1:  # Threshold (fixed mode only)
                        val_str = f"{int(_display_thresh * 99)}"
                    else:  # Release or ReleaseMode (based on toggle)
                        if _home_enc4_alt:
                            # ReleaseMode - show current mode name
                            val_str = RELEASE_MODES[RELEASE_MODE_INDEX]
                        else:
                            # Release - show in ms (or seconds if >= 1000ms)
                            release_ms = int(band.decay_ms)
                            release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                            if release_mode in ("react", "rand", "both"):
                                val_str = format_release_display(_effective_release_display)
                            else:
                                val_str = format_release_display(release_ms)
            elif page_name == "ADV":
                if i == 0:  # Q factor - display as 0-99 (inverted: higher = wider range)
                    # Q ranges from Q_MAX (narrow) to Q_MIN (wide, frequency-dependent)
                    # Use logarithmic mapping for perceptual linearity
                    q_min = get_q_min(_display_freq)
                    q_ratio = math.log(Q_MAX / max(q_min, _display_q)) / math.log(Q_MAX / q_min)
                    q_pct = round(q_ratio * 99)
                    q_pct = max(0, min(99, q_pct))
                    # Only update display if changed by more than 1 (hysteresis)
                    if abs(q_pct - _display_q_pct) > 1:
                        _display_q_pct = q_pct
                    val_str = f"{_display_q_pct}"
                elif i == 2:  # Decay
                    val_str = f"{int(band.decay_ms)}"
                else:
                    val_str = "--"
            elif page_name == "PRE":
                if i == 0:  # Preset - show preset name
                    current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
                    if current_mode == "x+1" and CYCLE_PHASE == 1 and BASE_PROGRAM != 6:
                        # Show neighbor preset in parentheses when cycling (only for x+1 mode)
                        _, neighbor = program_pair_for_base(BASE_PROGRAM)
                        val_str = f"({PROGRAM_NAMES[neighbor - 1]})"
                    elif current_mode == "rnd/amb" and CYCLE_PHASE == 1:
                        # Show AMB in brackets when in rnd/amb ambient phase
                        val_str = "[AMB]"
                    else:
                        val_str = PROGRAM_NAMES[BASE_PROGRAM - 1]
                elif i == 1:  # Mode - Cycles Between mode
                    val_str = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
                else:  # Beat Cycles - show value or -- if mode is off
                    current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
                    if BASE_PROGRAM == 6 or current_mode == "off":
                        val_str = "--"
                    else:
                        steps = CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX]
                        val_str = f"{steps}"
            elif page_name == "SET":
                if i == 0:  # Defaults mode
                    if _enc2_saving:
                        # Show loader based on elapsed time (animate dots)
                        elapsed = time.time() - _enc2_press_time
                        dots = int(elapsed) % 4
                        val_str = "." * (dots + 1)  # 1-4 dots
                    elif time.time() - _enc2_save_complete < 1.0:
                        val_str = "Saved!"
                    else:
                        val_str = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
                elif i == 1:  # FFT threshold
                    val_str = f"{int(band.thresh * 99)}"
                else:  # DMX Output Mode or Channel Count (based on toggle)
                    if _setup_enc4_channels:
                        val_str = str(DMX_CHANNEL_COUNT)
                    else:
                        val_str = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
            elif page_name == "COLOR":
                if i == 0:  # Light/channel selection
                    if _color_light_selection == 0:
                        val_str = "all"
                    elif _color_light_selection == 1:
                        val_str = "odd"
                    elif _color_light_selection == 2:
                        val_str = "even"
                    else:
                        val_str = str(_color_light_selection - 2)  # 1, 2, 3, ... n
                elif i == 1:  # Hue or Temperature
                    if _color_enc3_temp_mode:
                        val_str = str(_color_temperature)
                    else:
                        val_str = str(_color_hue)
                else:  # Saturation
                    val_str = str(_color_saturation)
            else:
                # Fallback for any unhandled pages
                val_str = "--"
            
            if val_str in extra_tight_values:
                val_kern = -1
            elif val_str in tight_values:
                val_kern = 0
            else:
                val_kern = 1
            self._draw_text_kerned(draw, (px, y + 9), val_str, self._font_small, fill=OLED_WHITE, kerning=val_kern)

    def _draw_home_controls(self, draw, x, y, width):
        """Draw the 4-column HOME controls at the bottom: Freq, Trigger, Release, Brightness.
        
        These are always visible and controlled by encoders 2-5.
        In AMBIENT mode: Speed, --, Fade, Bright
        """
        global _display_freq, _display_thresh, _display_release, _display_bright, _display_q_pct
        
        # 4 columns with spacing
        spacing = 2
        col_width = (width - spacing * 3) // 4
        
        # Labels based on mode and encoder toggle states
        if BASE_PROGRAM == 6:
            # AMBIENT mode: Speed, blank, Fade, Bright
            labels = ["Speed", "--", "Fade", "Brightness"]
        else:
            labels = [
                "Range" if _home_enc2_alt else "Freq",
                "Trigger",
                "R-Mode" if _home_enc4_alt else "Release",
                "Brightness"
            ]
        
        # Update display values
        _display_freq = band.center
        _display_thresh = band.thresh
        _display_bright = BRIGHTNESS
        
        for i in range(4):
            px = x + i * (col_width + spacing)
            label_kern = 0 if labels[i] in {"R-Mode"} else 1
            self._draw_text_kerned(draw, (px, y), labels[i], self._font_small, fill=OLED_WHITE, kerning=label_kern)
            
            # Format value based on column
            if BASE_PROGRAM == 6:
                # AMBIENT mode values
                if i == 0:  # Speed
                    val_str = f"{ambient_speed:.1f}x"
                elif i == 1:  # Blank
                    val_str = "--"
                elif i == 2:  # Fade
                    val_str = f"{ambient_fade_time:.1f}s"
                else:  # Brightness
                    if _brightness_off and not _brightness_fading:
                        val_str = "OFF"
                    else:
                        val_str = f"{int(BRIGHTNESS * 100)}"
            else:
                # Normal mode values
                if i == 0:  # Frequency or Q
                    if _home_enc2_alt:
                        if _home_enc2_range_pct is not None:
                            _display_q_pct = _home_enc2_range_pct
                            val_str = f"{_home_enc2_range_pct}"
                        else:
                            q_min = get_q_min(_display_freq)
                            q_ratio = math.log(Q_MAX / max(q_min, band.q)) / math.log(Q_MAX / q_min)
                            q_pct = round(q_ratio * 99)
                            q_pct = max(0, min(99, q_pct))
                            _display_q_pct = q_pct
                            val_str = f"{q_pct}"
                    else:
                        freq_hz = _display_freq
                        if freq_hz >= 1000:
                            freq_khz = freq_hz / 1000.0
                            val_str = f"{freq_khz:.1f}kHz"
                        else:
                            val_str = f"{int(freq_hz)}Hz"
                elif i == 1:  # Trigger threshold
                    val_str = f"{int(_display_thresh * 99)}"
                elif i == 2:  # Release or mode
                    if _home_enc4_alt:
                        val_str = RELEASE_MODES[RELEASE_MODE_INDEX]
                    else:
                        release_ms = int(band.decay_ms)
                        release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                        if release_mode in ("react", "rand", "both"):
                            val_str = format_release_display(_effective_release_display)
                        else:
                            val_str = format_release_display(release_ms)
                else:  # Brightness
                    if _brightness_off and not _brightness_fading:
                        val_str = "OFF"
                    else:
                        rm = RELEASE_MODES[RELEASE_MODE_INDEX]
                        if rm in ("bright", "both"):
                            val_str = f"{_effective_brightness_display}"
                        else:
                            val_str = f"{int(BRIGHTNESS * 100)}"
            
            self._draw_text_kerned(draw, (px, y + 9), val_str, self._font_small, fill=OLED_WHITE, kerning=1)

    def _draw_submenu_tabs_and_border(self, draw, x, y, width, content_height):
        """Draw folder-style tabs with border enclosing content below."""
        tab_names = SUBMENU_TABS
        tab_width = width // len(tab_names)
        tab_height = 10
        border_y = y + tab_height  # Where the content border starts
        total_height = tab_height + content_height
        
        # Draw the content border (box below tabs)
        # Left edge
        draw.line((x, border_y, x, y + total_height - 1), fill=OLED_WHITE)
        # Bottom edge
        draw.line((x, y + total_height - 1, x + width - 1, y + total_height - 1), fill=OLED_WHITE)
        # Right edge
        draw.line((x + width - 1, border_y, x + width - 1, y + total_height - 1), fill=OLED_WHITE)
        
        # Draw tabs and connect selected tab to border
        tab_kerning = -1  # Negative kerning for tighter text
        char_width = 4  # Approximate width per character with kerning=-1
        
        for i, name in enumerate(tab_names):
            tx = x + i * tab_width
            tab_right = tx + tab_width - 3
            is_selected = (i == submenu_tab)
            
            # Calculate centered text position
            text_width = len(name) * char_width
            tab_inner_width = tab_right - tx - 2  # Usable width inside tab
            text_x = tx + (tab_inner_width - text_width) // 2 + 1
            
            if is_selected:
                # Selected tab: rounded top corners, connects to content box
                # Top-left corner (rounded)
                draw.point((tx + 1, y), fill=OLED_WHITE)
                draw.point((tx, y + 1), fill=OLED_WHITE)
                # Top edge
                draw.line((tx + 2, y, tab_right - 2, y), fill=OLED_WHITE)
                # Top-right corner (rounded)
                draw.point((tab_right - 1, y), fill=OLED_WHITE)
                draw.point((tab_right, y + 1), fill=OLED_WHITE)
                # Left edge of tab
                draw.line((tx, y + 2, tx, border_y - 1), fill=OLED_WHITE)
                # Right edge of tab
                draw.line((tab_right, y + 2, tab_right, border_y - 1), fill=OLED_WHITE)
                # Text (white on black background) - tight kerning, centered
                self._draw_text_kerned(draw, (text_x, y + 1), name, self._font_small, fill=OLED_WHITE, kerning=tab_kerning)
                
                # Connect border: draw line from content box edge to tab edges
                # Line from left edge to tab left
                if tx > x:
                    draw.line((x, border_y, tx - 1, border_y), fill=OLED_WHITE)
                # Line from tab right to right edge (or next tab)
                if tab_right < x + width - 1:
                    draw.line((tab_right + 1, border_y, x + width - 1, border_y), fill=OLED_WHITE)
            else:
                # Unselected tab: just text, sits on the border line - tight kerning, centered
                self._draw_text_kerned(draw, (text_x, y), name, self._font_small, fill=OLED_GRAY, kerning=tab_kerning)
                # Draw border line under unselected tab
                draw.line((tx, border_y, tx + tab_width - 3, border_y), fill=OLED_WHITE)

    def _draw_submenu_content(self, draw, x, y, width, height):
        """Draw the submenu content (3 columns of controls for current tab)."""
        global _enc1_save_progress
        tab = SUBMENU_TABS[submenu_tab]
        labels = SUBMENU_LABELS[tab]
        
        # 3 columns
        spacing = 2
        col_width = (width - spacing * 2) // 3
        
        for i in range(3):
            px = x + i * (col_width + spacing)
            is_selected = (i == submenu_column)
            
            # Draw label - invert when selected and NOT editing (column selection mode)
            label_str = labels[i]
            label_kern = 0  # Tighter kerning for labels
            if is_selected and not submenu_editing:
                # Column selection mode: invert the label (white bg, black text)
                text_width = len(label_str) * 5  # Tighter spacing
                draw.rectangle((px - 1, y - 1, px + text_width, y + 8), fill=OLED_WHITE)
                self._draw_text_kerned(draw, (px, y), label_str, self._font_small, fill=OLED_BLACK, kerning=label_kern)
            else:
                # Editing mode or unselected: normal colors
                label_color = OLED_WHITE if is_selected else OLED_GRAY
                self._draw_text_kerned(draw, (px, y), label_str, self._font_small, fill=label_color, kerning=label_kern)
            
            # Get value for this column
            if tab == "Presets":
                if i == 0:  # Preset - show cycling state with parentheses
                    current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
                    if current_mode == "x+1" and CYCLE_PHASE == 1 and BASE_PROGRAM != 6:
                        # Show neighbor preset in parentheses when in x+1 neighbor phase
                        _, neighbor = program_pair_for_base(BASE_PROGRAM)
                        val_str = f"({PROGRAM_NAMES[neighbor - 1]})"
                    elif current_mode == "rnd/amb" and CYCLE_PHASE == 1:
                        # Show AMB in brackets when in rnd/amb ambient phase
                        val_str = "[AMB]"
                    else:
                        # Normal: show base preset name
                        val_str = PROGRAM_NAMES[BASE_PROGRAM - 1]
                elif i == 1:  # Mode
                    val_str = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
                else:  # Beats
                    if CYCLES_BETWEEN_INDEX == 0:
                        val_str = "--"
                    else:
                        val_str = f"{CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX]}"
            elif tab == "Settings":
                if i == 0:  # Gain (dB)
                    val_str = format_input_gain_display()
                elif i == 1:  # Reset - show current preset name or countdown
                    # Check if "saved!" should be shown (progress=2.0 for 0.8s after save)
                    if _enc1_save_progress >= 2.0 and (time.time() - _enc1_save_complete) < 0.8:
                        val_str = "saved!"
                    elif _enc1_save_progress > 0 and _enc1_save_progress < 2.0 and is_selected and submenu_editing:
                        # Show countdown replacing preset name: 3 -> 2 -> 1 based on progress
                        if _enc1_save_progress < 0.33:
                            val_str = "3..."
                        elif _enc1_save_progress < 0.67:
                            val_str = "2.."
                        else:
                            val_str = "1."
                    else:
                        # Clear saved state after display timeout
                        if _enc1_save_progress >= 2.0 and (time.time() - _enc1_save_complete) >= 0.8:
                            _enc1_save_progress = 0.0
                        val_str = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
                else:  # Column 3 is blank (Detect mode locked to "old")
                    val_str = ""
            elif tab == "Setup":
                if i == 0:  # Output
                    val_str = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
                elif i == 1:  # Channels
                    val_str = str(DMX_CHANNEL_COUNT)
                else:  # Column 3 is blank
                    val_str = ""
            else:
                val_str = "--"
            
            # Draw value - invert when editing (white bg, black text), otherwise white/gray
            val_y = y + 11  # Extra spacing between label and value rows
            val_kern = 0  # Tighter kerning for values
            if is_selected and submenu_editing:
                # Editing mode: invert colors (white background, black text)
                # Get text width for background rectangle
                text_width = len(val_str) * 5  # Tighter spacing
                draw.rectangle((px - 1, val_y - 1, px + text_width, val_y + 8), fill=OLED_WHITE)
                self._draw_text_kerned(draw, (px, val_y), val_str, self._font_small, fill=OLED_BLACK, kerning=val_kern)
            else:
                # Selection mode or unselected: normal colors
                val_color = OLED_WHITE if is_selected else OLED_GRAY
                self._draw_text_kerned(draw, (px, val_y), val_str, self._font_small, fill=val_color, kerning=val_kern)

    def _draw_reset_modal(self, draw):
        """Centered bordered panel over the main UI when long-press reset completes."""
        if time.time() >= _fft_reset_msg_until:
            return
        W, H = self.width, self.height
        msg = "reset!"
        try:
            bbox = draw.textbbox((0, 0), msg, font=self._font_small)
            l, t, r, b = bbox[0], bbox[1], bbox[2], bbox[3]
            tw, th = r - l, b - t
        except (TypeError, AttributeError):
            l, t, r, b = 0, 0, 30, 8
            tw, th = 30, 8
        pad_x, pad_y = 10, 5
        border = 2
        box_w = tw + pad_x * 2 + border * 2
        box_h = th + pad_y * 2 + border * 2
        bx0 = (W - box_w) // 2
        by0 = (H - box_h) // 2
        bx1 = bx0 + box_w - 1
        by1 = by0 + box_h - 1
        draw.rectangle((bx0, by0, bx1, by1), fill=OLED_WHITE)
        ix0, iy0 = bx0 + border, by0 + border
        ix1, iy1 = bx1 - border, by1 - border
        draw.rectangle((ix0, iy0, ix1, iy1), fill=OLED_BLACK)
        text_x = ix0 + pad_x - l
        text_y = iy0 + pad_y - t
        draw.text((text_x, text_y), msg, font=self._font_small, fill=OLED_WHITE)

    def render_once(self):
        if not self.enabled or self.device is None:
            return
        
        W, H = self.width, self.height
        image = Image.new("RGB", (W, H), OLED_BLACK)
        draw = ImageDraw.Draw(image)

        if APP_STATE == "error":
            draw.text((0, 0), "ERROR", font=self._font, fill=OLED_WHITE)
            draw.text((0, 14), (APP_ERROR or "See logs")[:20], font=self._font, fill=OLED_WHITE)
        else:
            # Trigger border: 1px thick with 1px gap to content
            border_thickness = 1
            content_inset = 2  # 1px border + 1px gap
            
            # Check if flash is active (time-based)
            flash_active = (time.time() - trigger_flash) < TRIGGER_FLASH_DURATION
            
            if flash_active:
                # Draw 1px border on all 4 sides
                draw.line((0, 0, W - 1, 0), fill=OLED_WHITE)  # Top
                draw.line((0, H - 1, W - 1, H - 1), fill=OLED_WHITE)  # Bottom
                draw.line((0, 0, 0, H - 1), fill=OLED_WHITE)  # Left
                draw.line((W - 1, 0, W - 1, H - 1), fill=OLED_WHITE)  # Right
            
            # Content area inset by border + gap
            cx = content_inset  # Content x start
            cy = content_inset  # Content y start
            cw = W - content_inset * 2  # Content width
            ch = H - content_inset * 2  # Content height
            
            # Layout inside content area
            # Top section (FFT + Submenu): ~38px tall
            # Bottom section (HOME controls): ~22px tall
            
            half_width = cw // 2  # ~125px
            top_height = 38  # Taller top section for submenu content
            
            # Top-left: FFT (squashed) + horizontal env vs threshold — same outer box as before
            draw.rectangle((cx, cy, cx + half_width - 1, cy + top_height - 1), outline=OLED_WHITE)
            inner_x = cx + 1
            inner_y = cy + 1
            inner_w = half_width - 2
            inner_h = top_height - 2
            meter_h = 6
            gap_y = 1
            fft_h = inner_h - meter_h - gap_y
            fft_h = max(8, fft_h)
            self._draw_fft_spectrum(draw, inner_x, inner_y, inner_w, fft_h)
            self._draw_env_meter_h(
                draw,
                inner_x,
                inner_y + fft_h + gap_y,
                inner_w,
                meter_h,
                live_band_env,
                _effective_thresh,
            )
            
            # Top-right: Submenu area (taller to fit 3 lines)
            submenu_x = cx + half_width + 2
            submenu_width = half_width - 4
            content_height = top_height - 11  # Height of content area below tabs
            
            # Draw folder-style tabs with border enclosing content
            self._draw_submenu_tabs_and_border(draw, submenu_x, cy, submenu_width, content_height)
            
            # Submenu content inside the bordered area
            self._draw_submenu_content(draw, submenu_x + 2, cy + 12, submenu_width - 4, content_height - 2)
            
            # Bottom: HOME controls (4 columns) - pushed down
            self._draw_home_controls(draw, cx, cy + top_height + 2, cw)
            self._draw_reset_modal(draw)

        try:
            self.device.display(image)
        except Exception as e:
            # Don't disable on transient SPI errors, just skip this frame
            pass

    def loop(self):
        while not STOP_THREADS:
            start = time.monotonic()
            update_encoders()
            self.render_once()
            
            elapsed = time.monotonic() - start
            sleep_time = self.period - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        self.clear()

# ===================== TUI =====================

def safe_addstr(stdscr, y, x, s):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w: return
    if x < 0:
        s = s[-x:]; x = 0
    maxlen = w - x
    if maxlen > 0:
        stdscr.addnstr(y, x, s, maxlen)

def draw_band_bar(stdscr, y, x, width, center, q):
    left_hz, right_hz = MIN_CENTER_HZ, MAX_CENTER_HZ
    warp = 0.3  # Same warp factor as generate_log_bands
    def hz_to_col(f):
        lf = math.log10(max(left_hz, min(right_hz, f)))
        lmin, lmax = math.log10(left_hz), math.log10(right_hz)
        t = (lf - lmin) / (lmax - lmin)
        t_display = t ** (1.0 / (1.0 - warp))
        return int(t_display * (width-1))
    bw   = center/max(1e-6, q)
    f_lo = max(left_hz,  center - 0.5*bw)
    f_hi = min(right_hz, center + 0.5*bw)
    c0   = x + hz_to_col(f_lo)
    c1   = x + hz_to_col(f_hi)
    c0, c1 = min(c0, x+width-1), min(c1, x+width-1)
    safe_addstr(stdscr, y, x, "─"*width)
    for col in range(c0, c1+1):
        safe_addstr(stdscr, y, col, "━")
    safe_addstr(stdscr, y+1, x, f"{int(f_lo)} Hz ← band → {int(f_hi)} Hz".ljust(width))

def draw_threshold_meter(stdscr, y, x, width, env_val, thr):
    m = 1.0
    e = max(0.0, min(1.0, env_val/m))
    t = max(0.0, min(1.0, thr/m))
    bar = ["-"]*width
    thr_col = min(width-1, int(t*(width-1)))
    env_col = min(width-1, int(e*(width-1)))
    for i in range(env_col+1):
        bar[i] = "#"
    bar[thr_col] = "|"
    safe_addstr(stdscr, y, x, "".join(bar))

def wrap_text(text: str, width: int) -> list:
    """Wrap text to fit within a given width, preserving words where possible."""
    if width <= 0:
        return []
    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        if len(word) > width:
            if current_line:
                lines.append(current_line)
                current_line = ""
            for i in range(0, len(word), width):
                lines.append(word[i:i+width])
        elif not current_line:
            current_line = word
        elif len(current_line) + 1 + len(word) <= width:
            current_line += " " + word
        else:
            lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines if lines else [""]

def draw_error_box(stdscr, error_msg: str, error_type: str, error_time: float):
    """Draw a visually elegant error/warning/info box in the TUI.
    
    The box is centered horizontally and positioned near the bottom of the screen.
    It includes a border, icon, title, wrapped message, age indicator, and dismiss hint.
    """
    h, w = stdscr.getmaxyx()
    
    # Configuration based on error type
    config = {
        "error": {
            "icon": "✖",
            "title": "ERROR",
            "color_pair": 1,  # Red
            "border_char": "═",
            "corner_tl": "╔", "corner_tr": "╗",
            "corner_bl": "╚", "corner_br": "╝",
            "side": "║"
        },
        "warning": {
            "icon": "⚠",
            "title": "WARNING",
            "color_pair": 2,  # Yellow
            "border_char": "─",
            "corner_tl": "┌", "corner_tr": "┐",
            "corner_bl": "└", "corner_br": "┘",
            "side": "│"
        },
        "info": {
            "icon": "ℹ",
            "title": "INFO",
            "color_pair": 3,  # Green
            "border_char": "─",
            "corner_tl": "┌", "corner_tr": "┐",
            "corner_bl": "└", "corner_br": "┘",
            "side": "│"
        }
    }
    cfg = config.get(error_type, config["error"])
    
    # Calculate box dimensions
    max_box_width = min(60, w - 4)
    min_box_width = 30
    content_width = max(min_box_width - 4, min(max_box_width - 4, len(error_msg) + 2))
    box_width = content_width + 4
    
    # Wrap the message text
    wrapped_lines = wrap_text(error_msg, content_width)
    max_lines = 4
    if len(wrapped_lines) > max_lines:
        wrapped_lines = wrapped_lines[:max_lines-1] + [wrapped_lines[max_lines-1][:content_width-3] + "..."]
    
    # Calculate age string
    age_secs = time.time() - error_time
    if age_secs < 60:
        age_str = f"{int(age_secs)}s ago"
    elif age_secs < 3600:
        age_str = f"{int(age_secs/60)}m ago"
    else:
        age_str = f"{int(age_secs/3600)}h ago"
    
    # Box height: top border + title + blank + message lines + blank + hint + bottom border
    box_height = 3 + len(wrapped_lines) + 2
    
    # Position: centered horizontally, near bottom
    start_y = max(0, h - box_height - 3)
    start_x = max(0, (w - box_width) // 2)
    
    # Ensure we have room
    if start_y < 0 or start_x < 0 or box_height > h or box_width > w:
        # Fallback to simple display
        try:
            stdscr.attron(curses.color_pair(cfg["color_pair"]) | curses.A_BOLD)
            safe_addstr(stdscr, h - 3, 0, f"{cfg['icon']} {cfg['title']}: {error_msg}"[:w-1])
            stdscr.attroff(curses.color_pair(cfg["color_pair"]) | curses.A_BOLD)
        except Exception:
            safe_addstr(stdscr, h - 3, 0, f"{cfg['title']}: {error_msg}"[:w-1])
        return
    
    try:
        color = curses.color_pair(cfg["color_pair"])
        
        # Draw top border with title
        top_border = cfg["corner_tl"] + cfg["border_char"] * (box_width - 2) + cfg["corner_tr"]
        stdscr.attron(color | curses.A_BOLD)
        safe_addstr(stdscr, start_y, start_x, top_border)
        
        # Title line with icon
        title_text = f" {cfg['icon']} {cfg['title']} "
        title_line = cfg["side"] + title_text.center(box_width - 2) + cfg["side"]
        safe_addstr(stdscr, start_y + 1, start_x, title_line)
        stdscr.attroff(color | curses.A_BOLD)
        
        # Separator line
        sep_line = cfg["side"] + "─" * (box_width - 2) + cfg["side"]
        stdscr.attron(color)
        safe_addstr(stdscr, start_y + 2, start_x, sep_line)
        stdscr.attroff(color)
        
        # Message lines
        for i, line in enumerate(wrapped_lines):
            msg_line = cfg["side"] + " " + line.ljust(content_width) + " " + cfg["side"]
            stdscr.attron(color)
            safe_addstr(stdscr, start_y + 3 + i, start_x, cfg["side"])
            safe_addstr(stdscr, start_y + 3 + i, start_x + box_width - 1, cfg["side"])
            stdscr.attroff(color)
            safe_addstr(stdscr, start_y + 3 + i, start_x + 1, " " + line.ljust(content_width) + " ")
        
        # Hint line with age
        hint_row = start_y + 3 + len(wrapped_lines)
        hint_text = f"[c] dismiss"
        age_text = age_str
        padding = box_width - 4 - len(hint_text) - len(age_text)
        hint_content = " " + hint_text + " " * max(1, padding) + age_text + " "
        stdscr.attron(color)
        safe_addstr(stdscr, hint_row, start_x, cfg["side"])
        safe_addstr(stdscr, hint_row, start_x + box_width - 1, cfg["side"])
        stdscr.attroff(color)
        stdscr.attron(curses.A_DIM)
        safe_addstr(stdscr, hint_row, start_x + 1, hint_content[:box_width-2])
        stdscr.attroff(curses.A_DIM)
        
        # Bottom border
        bottom_border = cfg["corner_bl"] + cfg["border_char"] * (box_width - 2) + cfg["corner_br"]
        stdscr.attron(color)
        safe_addstr(stdscr, hint_row + 1, start_x, bottom_border)
        stdscr.attroff(color)
        
    except Exception:
        # Fallback if box drawing fails
        safe_addstr(stdscr, h - 3, 0, f"{cfg['title']}: {error_msg}"[:w-1])

def tui(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(33)
    
    # Initialize color pairs for error display
    curses.start_color()
    curses.use_default_colors()
    try:
        curses.init_pair(1, curses.COLOR_RED, -1)      # Red text on default bg
        curses.init_pair(2, curses.COLOR_YELLOW, -1)   # Yellow text on default bg
        curses.init_pair(3, curses.COLOR_GREEN, -1)    # Green text on default bg
    except Exception:
        pass  # Color not available
    
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        bar_width = max(20, min(65, w - 2))

        safe_addstr(stdscr, 0, 0,
            f"Page={get_pages()[current_page]}  Preset={BASE_PROGRAM}  P{PROGRAM}  RUN={'ON' if RUNNING else 'PAUSE'}  "
            f"Device={DEVICE_NAME}  DMX={DMX_BACKEND}"
        )
        safe_addstr(stdscr, 1, 0,
            f"Center={band.center:.0f}Hz  Q={band.q:.1f}  Thresh={band.thresh:.2f}  "
            f"Decay={band.decay_ms:.0f}ms  Bright={BRIGHTNESS:.0%}"
        )
        bright_status = "OFF" if _brightness_off else f"{int(BRIGHTNESS*100)}%"
        click_indicator = " [CLICK!]" if _brightness_click_flash > 0.5 else ""
        safe_addstr(stdscr, 2, 0, f"Brightness: {bright_status}  (saved: {int(_brightness_saved*100)}%)  E5SW(GPIO17)={_brightness_enc5sw_state}{click_indicator}")

        row = 4
        safe_addstr(stdscr, row, 0, "Band Env vs Threshold (| is threshold):")
        draw_threshold_meter(stdscr, row + 1, 0, bar_width, live_band_env, band.thresh)
        safe_addstr(stdscr, row + 2, 0, f"env={live_band_env:.4f}")

        safe_addstr(stdscr, row + 4, 0, "Targeted Frequency Band:")
        draw_band_bar(stdscr, row + 5, 0, bar_width, band.center, band.q)

        safe_addstr(stdscr, row + 7, 0, "Channels:")
        for i, s in enumerate(states, start=1):
            safe_addstr(stdscr, row + 7 + i, 1, f"ch{i}: env={s.env:.3f} post={s.post:.3f} {'ON' if s.active else 'off'}")

        # Display persistent error in elegant box at bottom of screen
        error_msg, error_time, error_type = get_error()
        if error_msg:
            draw_error_box(stdscr, error_msg, error_type, error_time)

        # Flash message (temporary) - shown at very bottom
        if time.time() < _ui_flash_until and _ui_flash_msg:
            msg = _ui_flash_msg
            x = max(0, (w - len(msg)) // 2)
            stdscr.addnstr(h - 1, x, msg, max(0, w - x))

        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord('q'), ord('Q'), 27):
            _set_stop(True)
            _set_run(False)
            break
        elif ch in (ord('c'), ord('C')):
            # Dismiss error message
            clear_error()

# ===================== Main =====================

def main():
    print(f"[OK] Using input: {DEVICE_INDEX} - {DEVICE_NAME} ({DEVICE_CHANNELS}ch)")
    print(f"[OK] DMX backend: {DMX_BACKEND} (Universe {UNIVERSE}, Channels 1..{DMX_CHANNEL_COUNT})")
    if DEV_NO_HW:
        print("[OK] DEV_NO_HW=1: skipping GPIO, OLED.")
    else:
        print("[OK] Hardware mode: GPIO + OLED enabled.")

    global APP_STATE, IGNORE_KNOBS_UNTIL
    APP_STATE = "loading"
    IGNORE_KNOBS_UNTIL = time.time() + 0.3

    # init GPIO if enabled
    gpio_ok = True
    if not DEV_NO_HW:
        gpio_ok = setup_gpio_inputs()
        if not gpio_ok:
            print("[WARN] GPIO/MCP23017 init failed - continuing without hardware controls")

    # DMX backend + sender thread
    dmx_backend = make_dmx_backend()
    threading.Thread(target=lambda: dmx_sender_loop(dmx_backend), daemon=True).start()

    # OLED UI (SPI) with FFT display - EastRising 3.2" SSD1322 256x64
    oled_ui = OledUI(width=OLED_WIDTH, height=OLED_HEIGHT, fps=15)
    if getattr(oled_ui, "enabled", False):
        threading.Thread(target=oled_ui.loop, daemon=True).start()
        print("[OK] OLED UI: 128x64 SPI with FFT display")
    else:
        print("[INFO] OLED UI not available (skipping).")

    # Encoder reader thread (GPIO) — only if GPIO setup succeeded (SPI/OLED may still work otherwise)
    if not DEV_NO_HW and gpio_ok:
        threading.Thread(target=encoder_reader, daemon=True).start()
        print("[OK] Encoders: E1-E4 A/B/SW + E5 A/B via MCP23017; E5 SW GPIO17; Reset GPIO25; Extra GPIO7")
        print("     MCP23017@0x20: E1(8,9,10) E2(11,12,13) E3(14,0,1) E4(2,3,4) E5(5,6,-)")
        print("     Reset(GPIO25): short=tab cycle, long(>2s)=reset to defaults")
    elif not DEV_NO_HW and not gpio_ok:
        print("[INFO] Encoders disabled (GPIO init failed); OLED/audio still run if available.")

    # Audio thread
    threading.Thread(target=audio_loop, daemon=True).start()

    # TUI — run in the main thread so curses.wrapper owns terminal teardown.
    # Running it in a daemon thread caused terminal corruption on exit: the main
    # thread called curses.endwin() while the TUI thread was still active.
    use_tui = sys.stdout.isatty() and os.environ.get("ENABLE_TUI", "1") != "0"
    if not use_tui:
        print("[INFO] No TTY detected (or ENABLE_TUI=0). Running headless.")

    try:
        if use_tui:
            # curses.wrapper blocks until tui() returns; it calls endwin() for us
            # in its own finally, even on KeyboardInterrupt.
            curses.wrapper(tui)
        else:
            while not STOP_THREADS:
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        _set_stop(True)
        _set_run(False)
        time.sleep(0.15)

        # endwin is only needed in headless mode (wrapper already called it in TUI mode).
        # Call it unconditionally — it's a no-op if curses was never fully initialized.
        try:
            curses.endwin()
        except Exception:
            pass

        print("\nAll channels off. Bye.")

        # Turn off OLED display
        try:
            if oled_ui and oled_ui.device is not None:
                oled_ui.device.hide()
                oled_ui.device.cleanup()
        except Exception:
            pass

        try:
            if (not DEV_NO_HW) and (GPIO is not None):
                GPIO.cleanup()
        except Exception:
            pass

if __name__ == "__main__":
    main()
