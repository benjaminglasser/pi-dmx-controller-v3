#!/usr/bin/env python3
# dmx_audio_react.py (v2: NO OLA) + DEV_NO_HW + Plug&Play Audio + FFT OLED Display
#
# Audio-reactive DMX with optional hardware:
#   - 5 rotary encoders with push buttons (no MCP3008)
#   - SPI OLED display with FFT spectrum (CE1)
#
# Hardware Wiring:
#   SPI OLED:
#     RST: GPIO 12
#     DC:  GPIO 24
#     CS:  CE1 (GPIO 7)
#
#   Rotary Encoder 1 (Page selection):
#     CLK: GPIO 5,  DT: GPIO 6,  SW: GPIO 13
#
#   Rotary Encoder 2 (Param A - Freq/Speed/Preset):
#     CLK: GPIO 17, DT: GPIO 27, SW: GPIO 22
#
#   Rotary Encoder 3 (Param B - Thresh/Beats):
#     CLK: GPIO 19, DT: GPIO 26, SW: GPIO 23
#
#   Rotary Encoder 4 (Param C - Release/Mode):
#     CLK: GPIO 16, DT: GPIO 20, SW: GPIO 21
#
#   Rotary Encoder 5 (Brightness):
#     CLK: GPIO 4,  DT: GPIO 18, SW: GPIO 8
#
#   Reset Button: GPIO 25
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

import os, sys, time, math, threading, curses, re, random
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
    from luma.oled.device import ssd1309  # Waveshare 2.42" uses SSD1309
    _OLED_AVAILABLE = True
except Exception:
    _OLED_AVAILABLE = False

# ===================== Config =====================

DEV_NO_HW = os.environ.get("DEV_NO_HW", "0").strip() == "1"

DMX_BACKEND = os.environ.get("DMX_BACKEND", "uart").strip().lower()  # Default to uart for RS485 transceiver
DMX_UART_DEVICE = os.environ.get("DMX_UART_DEVICE", "/dev/serial0")
DMX_UART_BAUD = 250000  # DMX: 250k 8N2

UNIVERSE   = 0
DMX_CHANS  = 24  # Max supported channels (actual count controlled by DMX_CHANNEL_COUNT)

# DMX Output Mode: 0=Dimmer, 1=DMX (DMX mode disabled for now)
DMX_OUTPUT_MODE = 0
DMX_OUTPUT_MODES = ["Dimmer", "(DMX)"]  # DMX in parentheses = disabled/not yet implemented

# DMX Channel Count (4-24, affects all preset patterns)
DMX_CHANNEL_COUNT = 4

# Startup defaults (used by "LOW" mode)
DEFAULT_CENTER_HZ = 120.0   # 120 Hz
DEFAULT_Q         = 4.24    # 60 on 0-99 scale ((10-4.24)/9.5*99 = 60)
DEFAULT_THRESH    = 0.61    # 60 on 0-99 scale (0.61 * 99 = 60.39 → 60)
DEFAULT_ATTACK_MS = 10.0
DEFAULT_DECAY_MS  = 542.0   # 10 on 0-99 scale ((542-40)/4960*99 = 10.02 → 10)
DEFAULT_BRIGHT    = 0.5

# Defaults modes: LOW, MID, HIGH are built-in presets, USR 1-3 are user-saveable slots
# Each mode has (center_hz, thresh, decay_ms, q, thresh_mode, release_mode)
# Q display mapping: 0 = narrow (Q=8), 99 = wide (Q=0.5), so display 96 ≈ Q=0.74
# thresh_mode: 0=fixed, 1=adapt | release_mode: 0=fixed, 1=react, 2=bright, 3=both, 4=rand
DEFAULTS_MODES = ["LOW", "MID", "HIGH", "USR 1", "USR 2", "USR 3"]
DEFAULTS_PRESETS = {
    #           (center_hz, thresh, decay_ms, q_factor, thresh_mode, release_mode)
    "LOW":   (120.0,  0.40, 542.0, 2.0, 0, 0),    # Low frequencies ~120Hz, thresh=40
    "MID":   (1000.0, 0.41, 542.0, 1.5, 0, 0),    # Mid frequencies ~1kHz, thresh=40
    "HIGH":  (5000.0, 0.25, 542.0, 0.82, 0, 0),   # High frequencies ~5kHz, thresh=25, Q display=90
    "USR 1": (1200.0, 0.40, 542.0, 0.65, 0, 0),   # User preset 1: 1.2kHz, Q display=99
    "USR 2": (1200.0, 0.40, 542.0, 0.65, 0, 0),   # User preset 2: 1.2kHz, Q display=99
    "USR 3": (1200.0, 0.40, 542.0, 0.65, 0, 0),   # User preset 3: 1.2kHz, Q display=99
}

# Config file for persisting settings
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dmx_config")

def load_defaults_mode():
    """Load defaults mode, DMX output mode, channel count, and any custom preset values from config."""
    global DEFAULTS_PRESETS, DMX_OUTPUT_MODE, DMX_CHANNEL_COUNT
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
                    elif "=" in line:
                        # Parse preset override: LOW=120.0,0.40,542.0,2.0,0,0
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            parts = val.split(",")
                            if len(parts) >= 4:
                                base = tuple(float(p) for p in parts[:4])
                                if len(parts) >= 6:
                                    # New format with thresh_mode and release_mode
                                    DEFAULTS_PRESETS[key] = base + (int(parts[4]), int(parts[5]))
                                else:
                                    # Old format - default modes to 0 (fixed)
                                    DEFAULTS_PRESETS[key] = base + (0, 0)
    except Exception:
        pass
    return mode_idx  # Default to LOW (0)

def save_defaults_mode(idx):
    """Save the defaults mode to config file, preserving preset overrides, DMX output mode, and channel count."""
    try:
        mode_name = DEFAULTS_MODES[idx]
        # Read existing preset overrides and DMX settings
        preset_overrides = {}
        dmx_output = DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]
        channel_count = DMX_CHANNEL_COUNT
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
                    elif "=" in line and not line.startswith("defaults_mode="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated mode
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={mode_name}\n")
            f.write(f"dmx_output_mode={dmx_output}\n")
            f.write(f"dmx_channel_count={channel_count}\n")
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
                    elif "=" in line and not line.startswith("dmx_output_mode="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated DMX output mode
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={defaults_mode}\n")
            f.write(f"dmx_output_mode={output_mode}\n")
            f.write(f"dmx_channel_count={channel_count}\n")
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
        preset_overrides = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("defaults_mode="):
                        defaults_mode = line.split("=")[1]
                    elif line.startswith("dmx_output_mode="):
                        output_mode = line.split("=")[1]
                    elif "=" in line and not line.startswith("dmx_channel_count="):
                        key, val = line.split("=", 1)
                        if key in DEFAULTS_PRESETS:
                            preset_overrides[key] = val
        # Write back with updated channel count
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={defaults_mode}\n")
            f.write(f"dmx_output_mode={output_mode}\n")
            f.write(f"dmx_channel_count={count}\n")
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
SR  = 44100
HOP = 512  # Smaller for more responsive FFT
_HANNING_WINDOW = None  # Pre-computed, initialized on first use

# Detection / logic
ENV_EMA       = 0.55
AGC_ON        = True
AGC_TARGET    = 0.020
REFRACTORY_MS = 110.0
WEIGHTING_ON  = False
INPUT_GAIN    = 0.64  # Default gain for 3-band mode (was 0.16, now 4x higher as new center)
INPUT_VOLUME  = 50  # 0-99, centered at 50=0.64x gain (exponential: 0=0.04x, 50=0.64x, 99=10x)
BRIGHTNESS    = DEFAULT_BRIGHT

# Threshold detection modes
THRESH_MODES = ["fixed", "adapt"]
THRESH_MODE_INDEX = 0  # Default to fixed threshold (current behavior)
_recent_min = 1.0           # Tracks recent minimum for adaptive mode
_effective_thresh = 0.3     # Effective threshold for display (varies by mode)

# Release modes
RELEASE_MODES = ["fixed", "react", "bright", "both", "rand"]
RELEASE_MODE_INDEX = 0  # Default to fixed (current behavior)
_reactive_brightness_scale = 1.0  # For bright mode: scales brightness by level above threshold
_effective_release_display = 0  # For displaying reactive release values (0-99)
_effective_brightness_display = 50  # For displaying reactive brightness (0-99, 50 = default)
_brightness_knob_last_turn = 0.0  # Timestamp of last brightness knob turn
_release_knob_last_turn = 0.0  # Timestamp of last release knob turn
REACTIVE_BUFFER_SECONDS = 2.0  # Seconds to wait after knob turn before reactivity kicks in
_trigger_speed_multiplier = 1.0  # 0.3 to 2.0, fast = lower, slow = higher
TRIGGER_SPEED_FAST_MS = 200.0  # Triggers faster than this = min multiplier (0.3x)
TRIGGER_SPEED_SLOW_MS = 1000.0  # Triggers slower than this = max multiplier (2.0x)
TRIGGER_SPEED_MIN_MULT = 0.3  # Minimum multiplier for fast triggers (dampens effect)
TRIGGER_SPEED_MAX_MULT = 2.0  # Maximum multiplier for slow triggers

# Detection modes for FFT analysis
# - "level": Uses absolute energy level (original behavior)
# - "flux": Uses spectral flux (onset detection, better for drums/transients)
# - "hybrid": Combines level with flux boost (default, best of both)
DETECT_MODES = ["level", "flux", "hybrid"]
DETECT_MODE_INDEX = 2  # Default to hybrid mode

# Beat detection method: 0 = FFT_STANDARD (existing Q-band), 1 = 3BAND_DETECT (new)
# Now always using 3BAND mode
BEAT_DETECT_METHOD = 1
BEAT_DETECT_METHODS = ["FFT", "3BAND"]

# 3-Band detector state
THREEBAND_SELECTED = 0  # Selected band for DMX trigger (0=LOW, 1=MID, 2=HIGH)
THREEBAND_NAMES = ["LOW", "MID", "HIGH"]

# 3-Band view mode: 0 = FFT spectrum view, 1 = Three rectangles view, 2 = Selected band detail view
THREEBAND_VIEW_MODE = 0

# Encoder mode toggles for 3BAND_DETECT (HOME page only)
_3band_enc2_range_mode = False  # False=band select, True=range/width adjust
_3band_enc3_gain_mode = False   # False=threshold, True=gain adjust

# 3-band detector update rate
_last_3band_update = 0.0
THREEBAND_UPDATE_HZ = 50  # Run detector at 50 Hz (reduced from 100 for Pi performance)

# Program state
PROGRAM      = 1
BASE_PROGRAM = 1

RUNNING      = True
STOP_THREADS = False

# When > time.time(), knob readings are ignored
IGNORE_KNOBS_UNTIL = 0.0

# Preferred device name patterns (fallback)
PREFERRED_INPUTS = [
    r"hifiberry", r"dac\+adc", r"scarlett", r"usb audio", r"codec", r"line", r"pulse"
]

# Rotary Encoder 1 (Page selection)
ENC1_CLK = 5
ENC1_DT  = 6
ENC1_SW  = 13

# Rotary Encoder 2 - Param A (Freq/Speed/Preset depending on page)
ENC2_CLK = 17
ENC2_DT  = 27
ENC2_SW  = 22

# Rotary Encoder 3 - Param B (Thresh/Beats depending on page)
ENC3_CLK = 19
ENC3_DT  = 26
ENC3_SW  = 23

# Rotary Encoder 4 - Param C (Release/Mode depending on page)
ENC4_CLK = 16
ENC4_DT  = 20
ENC4_SW  = 21

# Rotary Encoder 5 - Brightness (global)
# NOTE: GPIO8 is SPI CE0, conflicts with OLED. SW for Enc5 is disabled.
ENC5_CLK = 4
ENC5_DT  = 18
ENC5_SW  = 8  # Brightness toggle (physical pin 24)
ENC5_SW_DISABLED = False  # ENABLED - brightness toggle on GPIO8

# Reset button (separate from encoder buttons)
RESET_PIN = 25

# SPI OLED pins (Waveshare 2.42" SSD1309 on CE1)
OLED_SPI_DEV = 1   # CE1 (GPIO 7)
OLED_RST_PIN = 12
OLED_DC_PIN  = 24
OLED_WIDTH   = 128
OLED_HEIGHT  = 64

# ===================== Page System =====================

# All available pages (COLOR comes after SET, only shown in DMX mode)
_ALL_PAGES = ["HOME", "PRE", "SET", "COLOR"]
_DIMMER_PAGES = ["HOME", "PRE", "SET"]  # No COLOR page for dimmer mode
_MAX_PAGES = 4  # Always reserve space for 4 pages for consistent icon spacing

def get_pages():
    """Get available pages based on DMX output mode. COLOR page only available in DMX mode."""
    if DMX_OUTPUT_MODE == 1:  # DMX mode
        return _ALL_PAGES
    return _DIMMER_PAGES

# For backward compatibility, PAGES is now a function call
PAGES = _DIMMER_PAGES  # Default, will be updated dynamically

PAGE_POT_LABELS = {
    "HOME": ["Freq", "Thresh", "Rels"],      # Freq center, Threshold, Release
    "ADV": ["Q", "ThPre", "RelMd"],           # Q, Threshold Preset, Release Mode
    "PRE": ["Preset", "Mode", "Beats"],       # Preset selection, Cycle mode, Beat cycles
    "COLOR": ["Light", "HSV", "Sat"],         # Light select, HSV, Saturation
    "SET": ["Reset", "Gain", "Setup"],        # Reset defaults, Input Gain, DMX Output Mode
}

# HOME page encoder toggle states (False = primary, True = alternate)
_home_enc2_alt = False  # False = Freq, True = Q
_home_enc3_alt = False  # False = Thresh, True = ThreshMode
_home_enc4_alt = False  # False = Release, True = ReleaseMode

# SET page encoder toggle states
_setup_enc4_channels = False  # False = Dimmer/DMX mode, True = Channel count (4-24)
_setup_enc3_detect = False    # False = Input Volume, True = Detection Mode

# COLOR page state
_color_light_selection = 0  # 0=all, 1=odd, 2=even, 3+=individual light (1-indexed)
_color_hue = 50            # 0-99 hue value (maps to 0-360 degrees)
_color_saturation = 99     # 0-99 saturation (0=white, 99=full color)
_color_temperature = 50    # 0-99 temperature (0=cool white, 99=warm white)
_color_enc3_temp_mode = False  # False=Hue mode, True=Temperature mode

# Page icons - pixel art as coordinate lists (drawn in 9x9 space)
PAGE_ICONS = {
    "HOME": [  # House shape
        (4, 0),  # roof peak
        (3, 1), (5, 1),
        (2, 2), (6, 2),
        (1, 3), (7, 3),
        (0, 4), (8, 4),  # roof base
        (1, 4), (2, 4), (3, 4), (4, 4), (5, 4), (6, 4), (7, 4),
        (2, 5), (2, 6), (2, 7), (2, 8),  # left wall
        (6, 5), (6, 6), (6, 7), (6, 8),  # right wall
        (2, 8), (3, 8), (4, 8), (5, 8), (6, 8),  # floor
        (4, 6), (4, 7), (4, 8),  # door
    ],
    "ADV": [  # Plus sign
        (4, 1), (4, 2), (4, 3), (4, 4), (4, 5), (4, 6), (4, 7),  # vertical
        (1, 4), (2, 4), (3, 4), (5, 4), (6, 4), (7, 4),  # horizontal
    ],
    "PRE": [  # Number/list icon (1 2 3)
        (1, 1), (2, 1), (2, 2), (2, 3),  # "1"
        (4, 1), (5, 1), (6, 1), (6, 2), (5, 3), (4, 4), (4, 5), (5, 5), (6, 5),  # "2" simplified
        (1, 7), (2, 7), (3, 7), (5, 7), (6, 7), (7, 7),  # dots/lines
    ],
    "COLOR": [  # Palette/droplet
        (4, 0), (3, 1), (5, 1),
        (2, 2), (6, 2),
        (1, 3), (7, 3),
        (1, 4), (7, 4),
        (1, 5), (7, 5),
        (2, 6), (6, 6),
        (3, 7), (4, 7), (5, 7),
        (3, 3), (5, 4),  # inner dots
    ],
    "SET": [  # Gear icon
        (4, 0),
        (3, 1), (4, 1), (5, 1),
        (2, 2), (6, 2),
        (1, 3), (2, 3), (3, 3), (5, 3), (6, 3), (7, 3),
        (0, 4), (1, 4), (3, 4), (5, 4), (7, 4), (8, 4),
        (1, 5), (2, 5), (3, 5), (5, 5), (6, 5), (7, 5),
        (2, 6), (6, 6),
        (3, 7), (4, 7), (5, 7),
        (4, 8),
    ],
}

current_page = 0  # Index into PAGES

# Program names for display
# Program 1: ALL - all channels trigger together
# Program 2: CHASE - sequential single channel cycling through all channels
# Program 3: GROUPS - first half of channels alternate with second half
# Program 4: ODD/EVEN - odd channels (1,3,5...) alternate with even (2,4,6...)
# Program 5: RANDOM - random channel each trigger
# Program 6: AMBIENT - non-audio-reactive random fading
PROGRAM_NAMES = ["ALL", "CHASE", "GROUPS", "ODD/EVEN", "RANDOM", "AMBIENT"]

# ===================== FFT Display =====================

# FFT settings - 32 bands, 100Hz to 10kHz
FFT_MIN_FREQ = 100
FFT_MAX_FREQ = 10000
FFT_NUM_BANDS = 64
FFT_SIZE = 2048  # Zero-pad to 2048 for ~21Hz resolution (4x better than 512 samples)

def generate_log_bands(num_bands, min_freq, max_freq):
    """Generate logarithmically spaced frequency bands."""
    bands = []
    log_min = math.log10(min_freq)
    log_max = math.log10(max_freq)
    step = (log_max - log_min) / num_bands
    for i in range(num_bands):
        low = 10 ** (log_min + i * step)
        high = 10 ** (log_min + (i + 1) * step)
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

def _precompute_bin_indices(freqs):
    """Pre-compute start/end bin indices for each FFT band."""
    global _FFT_BIN_INDICES
    indices = []
    for low_hz, high_hz in FFT_BANDS:
        # Find bin indices for this band
        start_idx = np.searchsorted(freqs, low_hz)
        end_idx = np.searchsorted(freqs, high_hz)
        if end_idx <= start_idx:
            end_idx = start_idx + 1  # At least one bin
        indices.append((start_idx, min(end_idx, len(freqs))))
    _FFT_BIN_INDICES = indices
    return indices

def get_band_energy_fast(fft_magnitudes, band_idx):
    """Fast band energy using pre-computed bin indices."""
    start, end = _FFT_BIN_INDICES[band_idx]
    if end > start:
        return float(np.mean(fft_magnitudes[start:end]))
    return float(fft_magnitudes[start]) if start < len(fft_magnitudes) else 0.0

# Visual gain for FFT display (makes bars appear taller within fixed view)
FFT_VISUAL_GAIN = 1.0

# FFT state (numpy arrays for faster vectorized operations)
fft_bands = np.zeros(len(FFT_BANDS), dtype=np.float32)
fft_peaks = np.zeros(len(FFT_BANDS), dtype=np.float32)
fft_peak_times = np.zeros(len(FFT_BANDS), dtype=np.float64)
PEAK_HOLD_TIME = 0.4
fft_recent_max = 0.3
fft_max_decay = 0.995

# Spectral flux state for onset/transient detection
prev_band_energies = [0.0] * len(FFT_BANDS)
fft_flux = [0.0] * len(FFT_BANDS)  # Per-band spectral flux values

# Per-band normalization state (spectral whitening)
band_running_mean = [0.01] * len(FFT_BANDS)
band_running_max = [0.1] * len(FFT_BANDS)
BAND_NORM_DECAY = 0.995

def get_spectral_flux(current_energies, prev_energies):
    """Calculate half-wave rectified spectral flux per band.
    Returns only positive changes (onsets), which helps detect transients."""
    flux = []
    for curr, prev in zip(current_energies, prev_energies):
        diff = curr - prev
        flux.append(max(0, diff))  # Only positive changes (onsets)
    return flux

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
    Low frequencies need slower attack to avoid false triggers from transient leakage,
    while highs need faster response."""
    if center_hz < 200:      # Lows
        return (12.0, 100.0)  # Slower attack, longer release
    elif center_hz < 2000:   # Mids
        return (8.0, 80.0)    # Default
    else:                    # Highs
        return (4.0, 50.0)    # Fast attack, quick release

# ===================== Encoder / Pot State =====================

encoder1_value = 0
encoder1_button = False
_reset_last_state = 1  # Reset button state (1 = not pressed)

# Encoder state for all 5 encoders
# Encoders 1-4: Page, Param A, Param B, Param C (indices 0-3)
# Encoder 5: Brightness (index 4)
_enc_last_clk = [None, None, None, None, None]
_enc_last_dt = [None, None, None, None, None]  # Track DT state too for quadrature
_enc_last_sw = [1, 1, 1, 1, 1]  # Switch states (1 = not pressed)
# Encoder deltas - accumulated since last update call
_enc_delta = [0, 0, 0, 0, 0]

# Quadrature state machine for reliable direction detection
# State is encoded as (CLK << 1) | DT, giving values 0-3
# Valid transitions: 0->1->3->2->0 (CW) or 0->2->3->1->0 (CCW)
_enc_state = [0, 0, 0, 0, 0]
_enc_count = [0, 0, 0, 0, 0]  # Raw quadrature counts (4 counts per detent)

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

# Preset toggle state for encoder 2 on PRE page (toggle to/from ambient)
_last_preset_before_ambient = 1  # Stores the preset to return to when toggling from ambient

# Simple velocity parameters - just max multiplier per parameter type
# Velocity is calculated as clicks-per-second, then mapped logarithmically
VELOCITY_MAX_FREQ = 25        # Frequency: large range, high acceleration
VELOCITY_MAX_THRESH = 20      # Threshold: 0-99 range
VELOCITY_MAX_DECAY = 8        # Decay/Release: reduced for more precision
VELOCITY_MAX_Q = 20           # Q factor: 0-99 range
VELOCITY_MAX_BRIGHTNESS = 18  # Brightness: 0-99%
VELOCITY_MAX_PRESET = 1       # Presets: no acceleration (always 1x)
VELOCITY_MAX_PAGE = 1         # Pages: no acceleration (always 1x)
VELOCITY_MAX_AMBIENT = 10     # Ambient params: moderate acceleration

# Minimum velocity multiplier for precision mode (sub-1x for slow turning)
# Set to 1.0 to disable precision mode, lower values = more precise at slow speeds
VELOCITY_MIN_DECAY = 0.1      # Decay/Release: 10x more precise when turning slowly

# Brightness fade toggle state
_brightness_saved = DEFAULT_BRIGHT  # Saved brightness before fade-out
_brightness_fading = False  # True while fading
_brightness_target = DEFAULT_BRIGHT  # Target for fade animation
_brightness_off = False  # True when faded to zero
BRIGHTNESS_FADE_SPEED = 0.05  # How fast to fade (per frame)
_brightness_click_flash = 0.0  # Decays over time, >0 means click detected recently
_brightness_gpio8_state = 1  # Current GPIO8 state for debug display

# Display-specific smoothed values (separate from control values)
_display_freq = DEFAULT_CENTER_HZ
_display_thresh = DEFAULT_THRESH
_display_q = DEFAULT_Q
_display_bright = DEFAULT_BRIGHT
_display_release = int((DEFAULT_DECAY_MS - 40.0) / 4960.0 * 99)  # Release display value
_display_q_pct = 50  # Q display value (0-99), will be recalculated dynamically

# DMX throttling (44Hz is near max for DMX512 protocol)
DMX_RATE_HZ       = 44.0
_DMX_MIN_INTERVAL = 1.0 / DMX_RATE_HZ

# --- Plug & Play Audio Selection ---
AUDIO_DEVICE      = os.environ.get("AUDIO_DEVICE", "").strip()
AUDIO_DEVICE_NAME = os.environ.get("AUDIO_DEVICE_NAME", "").strip()

AUDIO_DEBUG = os.environ.get("AUDIO_DEBUG", "0").strip() == "1"
TRIG_DEBUG  = os.environ.get("TRIG_DEBUG",  "0").strip() == "1"

# --- TUI flash message ---
_ui_flash_msg   = ""
_ui_flash_until = 0.0

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
CYCLE_STEPS         = 0
CYCLE_TRIGGER_COUNT = 0
CYCLE_PHASE         = 0
CYCLE_STEPS_INDEX   = 0  # Index into CYCLE_STEPS_OPTIONS (default 4)
CYCLE_AMBIENT_START = 0  # Timestamp when ambient phase started (for rnd/amb mode)

# Cycles between modes: off disables cycling, random/x+1/rnd/amb enable it
CYCLES_BETWEEN_MODES = ["off", "random", "x+1", "rnd/amb"]
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
        )
        print(f"[DMX] Backend: uart ({device}) @ {DMX_UART_BAUD} 8N2")

    def _send_break(self):
        try:
            self.serial.baudrate = 9600
            self.serial.write(b"\x00")
            self.serial.flush()
            time.sleep(0.001)
        finally:
            self.serial.baudrate = DMX_UART_BAUD

    def send(self, vals):
        # Build buffer dynamically based on vals length
        buf = bytearray(1 + len(vals))
        buf[0] = 0x00  # DMX start code
        for i, v in enumerate(vals):
            buf[1 + i] = max(0, min(255, int(v)))
        self._send_break()
        self.serial.write(buf)

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
            THRESH_MODE_INDEX = thresh_mode
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
    
    # Calculate base release display value
    base_release_display = int((band.decay_ms - 40.0) / 4960.0 * 99)
    base_release_display = max(0, min(99, base_release_display))
    
    # Calculate base brightness display value
    base_brightness_display = int(BRIGHTNESS * 99)
    
    if release_mode in ("bright", "both"):
        # Bright/both mode: use the reactive brightness scale (set on each trigger, stays until next)
        effective_brightness = _reactive_brightness_scale
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
    def __init__(self, target=0.02, tau=0.95):
        self.target=target
        self.gain  = 1.0
        self.tau   = tau
    def update(self, env_mean):
        eps=1e-6
        desired=self.target/max(eps, env_mean)
        desired=max(0.1, min(20.0, desired))
        self.gain=self.tau*self.gain+(1.0-self.tau)*desired
        return self.gain

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
            # trigger_thresh 0.068 = very sensitive (UI shows 50 at this value)
            # lag_attack 0.4 = follow rising signal at 40% per frame
            # lag_decay 0.015 = slow decay (1.5% per frame) for sawtooth shape
            # gain 6.3 = +16dB boost (UI shows 0dB at this value as new center)
            BandConfig("LOW", 40, 150, 0.068, 200, 0.4, 0.015, 6.3),
            # MID: Snare - similar tuning
            BandConfig("MID", 1000, 4000, 0.068, 120, 0.35, 0.02, 6.3),
            # HIGH: Hats - similar tuning
            BandConfig("HIGH", 6000, 16000, 0.068, 50, 0.5, 0.03, 6.3),
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
        self.trigger_flash = [0.0, 0.0, 0.0]
        self.last_trigger_time = [0.0, 0.0, 0.0]
        
        # Display values for UI
        self.display_flux = [0.0, 0.0, 0.0]     # Normalized slope for display (0-1)
        self.display_thresh = [0.068, 0.068, 0.068]  # Trigger threshold for display (new center)
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
                self.trigger_flash[i] = 1.0
                self.last_trigger_time[i] = now
                self.suppression_time[i] = now  # Start suppressing other bands
            else:
                self.trigger[i] = False
            
            # #region agent log
            # Log kick detection details (band 0 = LOW)
            if i == 0 and (self.normalized_slope[i] > 0.05 or self.trigger[i]):
                import json as _json
                with open("/home/benglasser/.cursor/debug.log", "a") as _f:
                    _f.write(_json.dumps({
                        "ts": int(now*1000), "type": "TD_KICK",
                        "energy": round(float(self.energy[i]), 4),
                        "lagged": round(float(self.lagged_energy[i]), 4),
                        "slope": round(float(self.slope[i]), 4),
                        "norm_slope": round(float(self.normalized_slope[i]), 3),
                        "thresh": round(float(band.trigger_thresh), 2),
                        "cooldown_ok": cooldown_ok,
                        "trig": self.trigger[i]
                    }) + "\n")
            # #endregion
            
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
            
            # Decay flash for UI (~100ms visible at 50Hz update rate)
            if not self.trigger[i]:
                self.trigger_flash[i] *= 0.78
            
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
                self.trigger_flash[i] = 1.0
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
            
            # Decay flash for UI (~100ms visible at 50Hz update rate)
            if not self.trigger[i]:
                self.trigger_flash[i] *= 0.78
            
            self.onset_history[i].append(self.scaled_onset[i])
            self.trigger_history[i].append(self.trigger[i])
        
        return [(self.energy[i], self.flux[i], self.scaled_onset[i], self.trigger[i]) 
                for i in range(3)]
    
    def get_adaptive_threshold(self, band_idx):
        """Get the current trigger threshold for a band (for UI display)."""
        band = self.bands[band_idx]
        return band.trigger_thresh
    
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

def pick_input_device():
    devs = sd.query_devices()
    for pat in PREFERRED_INPUTS:
        rx = re.compile(pat, re.I)
        for i, d in enumerate(devs):
            if d.get("max_input_channels",0) >= 1 and rx.search(d.get("name","")):
                return i, d["name"]
    for i, d in enumerate(devs):
        if d.get("max_input_channels",0) >= 1:
            return i, d["name"]
    raise RuntimeError("No suitable input device (>=1ch) found")

def choose_input_device():
    devs = sd.query_devices()

    if AUDIO_DEVICE:
        idx = int(AUDIO_DEVICE)
        d = sd.query_devices(idx)
        if d.get("max_input_channels", 0) <= 0:
            raise RuntimeError(f"AUDIO_DEVICE={idx} has no input channels")
        return idx, d["name"]

    if AUDIO_DEVICE_NAME:
        needle = AUDIO_DEVICE_NAME.lower()
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0 and needle in d.get("name", "").lower():
                return i, d["name"]
        raise RuntimeError(f'No input device name contains "{AUDIO_DEVICE_NAME}"')

    return pick_input_device()

DEVICE_INDEX, DEVICE_NAME = choose_input_device()


def update_encoders():
    """Apply encoder deltas based on current page and handle brightness.
    Uses per-parameter velocity sensitivity for acceleration.
    Discrete controls (presets, modes) use debouncing for precise single-detent changes."""
    global BRIGHTNESS, BASE_PROGRAM, CYCLES_BETWEEN_INDEX, THRESH_MODE_INDEX
    global ambient_speed, ambient_fade_time, DEFAULTS_MODE_INDEX
    global _enc_delta, _brightness_target, _brightness_fading
    global _discrete_last_change
    global current_page, encoder1_value
    global RELEASE_MODE_INDEX
    global _effective_release_display, _release_knob_last_turn
    global THREEBAND_SELECTED, _3band_enc2_range_mode, _3band_enc3_gain_mode
    
    if DEV_NO_HW:
        return
    if time.time() < IGNORE_KNOBS_UNTIL:
        return
    
    # Get raw encoder deltas (direction only: -1, 0, or 1 per click)
    # Indices: 1=Param A, 2=Param B, 3=Param C, 4=Brightness
    raw_deltas = _enc_delta[1:4]  # Param A, B, C deltas
    brightness_raw = _enc_delta[4]
    _enc_delta = [0, 0, 0, 0, 0]
    
    # Handle brightness encoder (Encoder 5) with its own velocity
    # Direct update (no lerp) when turning - lerp only on click toggle
    if brightness_raw != 0 and not _brightness_off:
        base_delta = 1 if brightness_raw > 0 else -1
        mult = _calc_velocity_multiplier(4, VELOCITY_MAX_BRIGHTNESS)
        delta = base_delta * mult
        # Small range (0-100%), ~1% per slow click
        BRIGHTNESS = max(0.0, min(0.99, BRIGHTNESS + delta * 0.01))
        _brightness_target = BRIGHTNESS  # Keep target in sync
        # In bright/both mode, snap reactive brightness back to base when knob is turned
        # and set buffer timestamp to delay reactivity
        global _reactive_brightness_scale, _effective_brightness_display, _brightness_knob_last_turn
        if RELEASE_MODES[RELEASE_MODE_INDEX] in ("bright", "both"):
            _reactive_brightness_scale = BRIGHTNESS
            _effective_brightness_display = int(BRIGHTNESS * 99)
            _brightness_knob_last_turn = time.time()
    
    # Animate brightness fade
    if _brightness_fading:
        diff = _brightness_target - BRIGHTNESS
        if abs(diff) < BRIGHTNESS_FADE_SPEED:
            BRIGHTNESS = _brightness_target
            _brightness_fading = False
        else:
            BRIGHTNESS += BRIGHTNESS_FADE_SPEED if diff > 0 else -BRIGHTNESS_FADE_SPEED
    
    # Update parameters based on current page using encoder deltas with per-param velocity
    pages = get_pages()
    page_name = pages[current_page]
    
    if page_name == "HOME":
        # Check if we're in AMBIENT mode (preset 6) - different encoder behavior
        if BASE_PROGRAM == 6:
            # AMBIENT mode: Enc A = Speed, Enc B = nothing, Enc C = Fade time
            if raw_deltas[0] != 0:
                global _ambient_next_time
                base_delta = 1 if raw_deltas[0] > 0 else -1
                mult = _calc_velocity_multiplier(1, VELOCITY_MAX_AMBIENT)
                delta = base_delta * mult
                # Range (0.2-8.0), fine control: 0.02x per slow click (~390 steps)
                ambient_speed = max(0.2, min(8.0, ambient_speed + delta * 0.02))
                # Reset timer so new speed takes effect immediately
                _ambient_next_time = 0.0
            if raw_deltas[2] != 0:
                base_delta = 1 if raw_deltas[2] > 0 else -1
                mult = _calc_velocity_multiplier(3, VELOCITY_MAX_AMBIENT)
                delta = base_delta * mult
                # Range (0.1-10s), fine control: 0.05s per slow click (~200 steps)
                ambient_fade_time = max(0.1, min(10.0, ambient_fade_time + delta * 0.05))
        elif BEAT_DETECT_METHOD == 1:
            # ===== 3BAND_DETECT mode =====
            # Enc A: Band select OR Range/width adjust (toggle with click)
            if raw_deltas[0] != 0:
                if _3band_enc2_range_mode:
                    # Range/width adjust for selected band
                    if three_band_detector is not None:
                        # 5% width change per detent
                        three_band_detector.adjust_width(THREEBAND_SELECTED, raw_deltas[0] * 5)
                else:
                    # Band select (cycle through LOW/MID/HIGH)
                    now = time.time()
                    elapsed_ms = (now - _discrete_last_change[1]) * 1000
                    if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                        delta = 1 if raw_deltas[0] > 0 else -1
                        THREEBAND_SELECTED = (THREEBAND_SELECTED + delta) % 3
                        _discrete_last_change[1] = now
                        ui_flash(f"Band: {THREEBAND_NAMES[THREEBAND_SELECTED]}", 0.5)
            
            # Enc B: Threshold OR Gain adjust (toggle with click)
            if raw_deltas[1] != 0:
                if three_band_detector is not None:
                    band_cfg = three_band_detector.bands[THREEBAND_SELECTED]
                    base_delta = 1 if raw_deltas[1] > 0 else -1
                    mult = _calc_velocity_multiplier(2, VELOCITY_MAX_THRESH)
                    delta = base_delta * mult
                    
                    if _3band_enc3_gain_mode:
                        # Gain adjust: per-band gain multiplier (0.63 to 63.0, center at 6.3)
                        # This gives +/- 20dB range from center
                        band_cfg.gain = max(0.63, min(63.0, band_cfg.gain * (1.05 ** delta)))
                    else:
                        # Trigger threshold adjust (0.01 to 0.5, center at 0.068)
                        # Lower value = more sensitive, higher = less sensitive
                        # Right (positive delta) = more sensitive (lower thresh), Left = less sensitive
                        band_cfg.trigger_thresh = max(0.01, min(0.5, band_cfg.trigger_thresh - delta * 0.01))
            
            # Enc C: Release (global, same as FFT mode) OR ReleaseMode (toggle with click)
            if raw_deltas[2] != 0:
                if _home_enc4_alt:
                    # ReleaseMode: cycle through release modes (clamped, not wrapping)
                    now = time.time()
                    elapsed_ms = (now - _discrete_last_change[3]) * 1000
                    if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                        delta = 1 if raw_deltas[2] > 0 else -1
                        new_idx = max(0, min(len(RELEASE_MODES) - 1, RELEASE_MODE_INDEX + delta))
                        if new_idx != RELEASE_MODE_INDEX:
                            RELEASE_MODE_INDEX = new_idx
                            _discrete_last_change[3] = now
                else:
                    # Release mode: (40-5000ms, display 0-99)
                    base_delta = 1 if raw_deltas[2] > 0 else -1
                    mult = _calc_velocity_multiplier(3, VELOCITY_MAX_DECAY, VELOCITY_MIN_DECAY)
                    delta = base_delta * mult
                    band.decay_ms = max(40.0, min(5000.0, band.decay_ms + delta * 50.0))
                    release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                    if release_mode in ("react", "rand", "both"):
                        _effective_release_display = int((band.decay_ms - 40.0) / 4960.0 * 99)
                        _effective_release_display = max(0, min(99, _effective_release_display))
                        _release_knob_last_turn = time.time()
        else:
            # ===== FFT_STANDARD mode (existing behavior) =====
            # Enc A: Center frequency OR Q (toggle with click)
            if raw_deltas[0] != 0:
                # Clamp base delta to ±1, velocity multiplier handles acceleration
                base_delta = 1 if raw_deltas[0] > 0 else -1
                if _home_enc2_alt:
                    # Q mode: Q factor - logarithmic scaling for perceptual linearity
                    mult = _calc_velocity_multiplier(1, VELOCITY_MAX_Q)
                    delta = base_delta * mult
                    # Use logarithmic scaling: ~2% change per click for consistent feel across range
                    # Negative delta = CW rotation = decrease Q (wider)
                    factor = 1.02 ** (-delta)
                    q_min = get_q_min(band.center)
                    band.q = max(q_min, min(Q_MAX, band.q * factor))
                else:
                    # Freq mode: Center frequency (log scale)
                    mult = _calc_velocity_multiplier(1, VELOCITY_MAX_FREQ)
                    delta = base_delta * mult
                    # Large range (20Hz-20kHz), ~0.8% per slow click
                    factor = 1.008 ** delta
                    new_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center * factor))
                    print(f"[FREQ] delta={delta} mult={mult} factor={factor:.3f} {band.center:.0f}Hz -> {new_center:.0f}Hz")
                    band.center = new_center
            # Enc B: Threshold OR ThreshMode (toggle with click)
            if raw_deltas[1] != 0:
                if _home_enc3_alt:
                    # ThreshMode: cycle through threshold detection modes
                    now = time.time()
                    elapsed_ms = (now - _discrete_last_change[2]) * 1000
                    if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                        delta = 1 if raw_deltas[1] > 0 else -1
                        new_idx = max(0, min(len(THRESH_MODES) - 1, THRESH_MODE_INDEX + delta))
                        if new_idx != THRESH_MODE_INDEX:
                            THRESH_MODE_INDEX = new_idx
                            _discrete_last_change[2] = now
                else:
                    # Threshold mode: (0-1, display 0-99)
                    # Clamp base delta to ±1, velocity multiplier handles acceleration
                    base_delta = 1 if raw_deltas[1] > 0 else -1
                    mult = _calc_velocity_multiplier(2, VELOCITY_MAX_THRESH)
                    delta = base_delta * mult
                    # Medium range (0-99), ~1 display unit per slow click
                    band.thresh = max(0.0, min(1.0, band.thresh + delta * 0.01))
            # Enc C: Release/Decay OR ReleaseMode (toggle with click)
            if raw_deltas[2] != 0:
                if _home_enc4_alt:
                    # ReleaseMode: cycle through release modes (clamped, not wrapping)
                    now = time.time()
                    elapsed_ms = (now - _discrete_last_change[3]) * 1000
                    if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                        delta = 1 if raw_deltas[2] > 0 else -1
                        new_idx = max(0, min(len(RELEASE_MODES) - 1, RELEASE_MODE_INDEX + delta))
                        if new_idx != RELEASE_MODE_INDEX:
                            RELEASE_MODE_INDEX = new_idx
                            _discrete_last_change[3] = now
                else:
                    # Release mode: (40-5000ms, display 0-99)
                    # Clamp base delta to ±1, velocity multiplier handles acceleration
                    base_delta = 1 if raw_deltas[2] > 0 else -1
                    mult = _calc_velocity_multiplier(3, VELOCITY_MAX_DECAY, VELOCITY_MIN_DECAY)
                    delta = base_delta * mult
                    # Medium range, ~1 display unit per slow click (50ms step)
                    # With precision mode (min_mult=0.2), slow turns give ~10ms steps
                    band.decay_ms = max(40.0, min(5000.0, band.decay_ms + delta * 50.0))
                    # In react/rand/both mode, snap effective release back to base when knob is turned
                    # and set buffer timestamp to delay reactivity
                    release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                    if release_mode in ("react", "rand", "both"):
                        _effective_release_display = int((band.decay_ms - 40.0) / 4960.0 * 99)
                        _effective_release_display = max(0, min(99, _effective_release_display))
                        _release_knob_last_turn = time.time()
                
    elif page_name == "ADV":
        # Enc A: Q factor (display 0-99 inverted) - logarithmic scaling
        if raw_deltas[0] != 0:
            base_delta = 1 if raw_deltas[0] > 0 else -1
            mult = _calc_velocity_multiplier(1, VELOCITY_MAX_Q)
            delta = base_delta * mult
            # Use logarithmic scaling: ~6% change per click (3x HOME page) for consistent feel
            # Negative delta = CW rotation = decrease Q (wider)
            factor = 1.06 ** (-delta)
            q_min = get_q_min(band.center)
            band.q = max(q_min, min(Q_MAX, band.q * factor))
        # Enc C: Decay (40-5000ms, display 0-99)
        if raw_deltas[2] != 0:
            base_delta = 1 if raw_deltas[2] > 0 else -1
            mult = _calc_velocity_multiplier(3, VELOCITY_MAX_DECAY, VELOCITY_MIN_DECAY)
            delta = base_delta * mult
            # Medium range, ~1 display unit per slow click (50ms step)
            # With precision mode (min_mult=0.2), slow turns give ~10ms steps
            band.decay_ms = max(40.0, min(5000.0, band.decay_ms + delta * 50.0))
            
    elif page_name == "PRE":
        # Enc A: Preset selection (1-6) - debounced discrete control
        if raw_deltas[0] != 0:
            now = time.time()
            elapsed_ms = (now - _discrete_last_change[1]) * 1000
            if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                delta = 1 if raw_deltas[0] > 0 else -1
                # Limit to presets 1-5 (AMBIENT only via button click)
                current = BASE_PROGRAM if BASE_PROGRAM <= 5 else 1  # If on AMBIENT, start from 1
                new_preset = max(1, min(5, current + delta))
                if new_preset != BASE_PROGRAM:
                    global CYCLE_TRIGGER_COUNT, CYCLE_PHASE
                    BASE_PROGRAM = new_preset
                    # Reset cycle state when preset changes
                    CYCLE_TRIGGER_COUNT = 0
                    CYCLE_PHASE = 0
                    _discrete_last_change[1] = now
                    ui_flash(f"Preset: {PROGRAM_NAMES[new_preset-1]}", 0.8)
        # Enc B: Cycle mode - debounced discrete control
        if raw_deltas[1] != 0:
            now = time.time()
            elapsed_ms = (now - _discrete_last_change[2]) * 1000
            if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                delta = 1 if raw_deltas[1] > 0 else -1
                new_idx = max(0, min(len(CYCLES_BETWEEN_MODES) - 1, CYCLES_BETWEEN_INDEX + delta))
                if new_idx != CYCLES_BETWEEN_INDEX:
                    old_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
                    CYCLES_BETWEEN_INDEX = new_idx
                    new_mode = CYCLES_BETWEEN_MODES[new_idx]
                    _discrete_last_change[2] = now
                    # When switching from off to a mode, set beats to 4 (index 0)
                    if old_mode == "off" and new_mode != "off":
                        set_cycle_steps_by_index(0)  # Default to 4 beats
                        # If on AMBIENT, jump to ALL preset
                        if BASE_PROGRAM == 6:
                            BASE_PROGRAM = 1
                            CYCLE_TRIGGER_COUNT = 0
                            CYCLE_PHASE = 0
                            ui_flash(f"Preset: ALL", 0.8)
        # Enc C: Beat Cycles index - debounced discrete control (disabled for AMBIENT or mode off)
        current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
        if raw_deltas[2] != 0 and BASE_PROGRAM != 6 and current_mode != "off":
            now = time.time()
            elapsed_ms = (now - _discrete_last_change[3]) * 1000
            if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                delta = 1 if raw_deltas[2] > 0 else -1
                new_idx = max(0, min(len(CYCLE_STEPS_OPTIONS) - 1, CYCLE_STEPS_INDEX + delta))
                if new_idx != CYCLE_STEPS_INDEX:
                    _discrete_last_change[3] = now
                    set_cycle_steps_by_index(new_idx)
            
    elif page_name == "SET":
        # Enc A: Defaults mode (0-5 for LOW/MID/HIGH/USR1/USR2/USR3) - debounced discrete control
        if raw_deltas[0] != 0:
            now = time.time()
            elapsed_ms = (now - _discrete_last_change[1]) * 1000
            if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                delta = 1 if raw_deltas[0] > 0 else -1
                new_idx = max(0, min(5, DEFAULTS_MODE_INDEX + delta))
                if new_idx != DEFAULTS_MODE_INDEX:
                    DEFAULTS_MODE_INDEX = new_idx
                    _discrete_last_change[1] = now
                    mode_name = DEFAULTS_MODES[new_idx]
                    preset = DEFAULTS_PRESETS[mode_name]
                    # Apply the defaults immediately (only if preset has values)
                    if preset is not None:
                        if len(preset) >= 6:
                            center_hz, thresh, decay_ms, q_factor, thresh_mode, release_mode = preset
                            THRESH_MODE_INDEX = thresh_mode
                            RELEASE_MODE_INDEX = release_mode
                        else:
                            center_hz, thresh, decay_ms, q_factor = preset[:4]
                        band.center   = center_hz
                        band.thresh   = thresh
                        band.decay_ms = decay_ms
                        band.q        = q_factor
                    # Save to config file for persistence
                    save_defaults_mode(new_idx)
                    ui_flash(f"Defaults: {mode_name}", 0.8)
        
        # Enc B: Input Volume (0-99) OR Detection Mode (toggle with click)
        if raw_deltas[1] != 0:
            if _setup_enc3_detect:
                # Detection Mode: cycle through level/flux/hybrid
                global DETECT_MODE_INDEX
                now = time.time()
                elapsed_ms = (now - _discrete_last_change[2]) * 1000
                if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                    delta = 1 if raw_deltas[1] > 0 else -1
                    new_idx = max(0, min(len(DETECT_MODES) - 1, DETECT_MODE_INDEX + delta))
                    if new_idx != DETECT_MODE_INDEX:
                        DETECT_MODE_INDEX = new_idx
                        _discrete_last_change[2] = now
                        ui_flash(f"Detect: {DETECT_MODES[new_idx]}", 0.8)
            else:
                # Input Volume mode
                global INPUT_VOLUME, INPUT_GAIN
                new_vol = max(0, min(99, INPUT_VOLUME + raw_deltas[1]))
                if new_vol != INPUT_VOLUME:
                    INPUT_VOLUME = new_vol
                    # Exponential mapping centered at 50=0.64x (recalibrated for 3-band mode)
                    # 0=0.04x, 50=0.64x, 99=10x (4 octaves each direction from center)
                    INPUT_GAIN = 0.64 * (16 ** ((new_vol - 50) / 49.5))
                    ui_flash(f"Input Vol: {INPUT_VOLUME}", 0.8)
        
        # Enc C: DMX Output Mode OR Channel Count (toggle with click) - debounced discrete control
        if raw_deltas[2] != 0:
            global DMX_OUTPUT_MODE, DMX_CHANNEL_COUNT
            now = time.time()
            elapsed_ms = (now - _discrete_last_change[3]) * 1000
            if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                delta = 1 if raw_deltas[2] > 0 else -1
                if _setup_enc4_channels:
                    # Channel count mode: 4-24 channels
                    new_count = max(4, min(24, DMX_CHANNEL_COUNT + delta))
                    if new_count != DMX_CHANNEL_COUNT:
                        DMX_CHANNEL_COUNT = new_count
                        _discrete_last_change[3] = now
                        save_dmx_channel_count(new_count)
                        ui_flash(f"Channels: {new_count}", 0.8)
                else:
                    # Output mode: Dimmer / (DMX) - can scroll to see both options
                    # (DMX) is shown in parentheses to indicate it's not fully implemented yet
                    new_mode = max(0, min(1, DMX_OUTPUT_MODE + delta))
                    if new_mode != DMX_OUTPUT_MODE:
                        DMX_OUTPUT_MODE = new_mode
                        _discrete_last_change[3] = now
                        save_dmx_output_mode(new_mode)
                        ui_flash(f"Output: {DMX_OUTPUT_MODES[new_mode]}", 0.8)

    elif page_name == "COLOR":
        global _color_light_selection, _color_hue, _color_saturation, _color_temperature
        
        # Enc A: Light selection (all, odd, even, 1, 2, 3, ... n)
        # Each channel is treated as an individual light
        if raw_deltas[0] != 0:
            now = time.time()
            elapsed_ms = (now - _discrete_last_change[1]) * 1000
            if elapsed_ms >= DISCRETE_DEBOUNCE_MS:
                delta = 1 if raw_deltas[0] > 0 else -1
                # all(0), odd(1), even(2), then 1..DMX_CHANNEL_COUNT
                max_selection = 2 + DMX_CHANNEL_COUNT
                new_sel = max(0, min(max_selection, _color_light_selection + delta))
                if new_sel != _color_light_selection:
                    _color_light_selection = new_sel
                    _discrete_last_change[1] = now
        
        # Enc B: Hue (0-99) or Temperature (0-99) based on toggle
        if raw_deltas[1] != 0:
            base_delta = 1 if raw_deltas[1] > 0 else -1
            mult = _calc_velocity_multiplier(2, VELOCITY_MAX_THRESH)
            delta = base_delta * mult
            if _color_enc3_temp_mode:
                _color_temperature = max(0, min(99, _color_temperature + delta))
            else:
                _color_hue = max(0, min(99, _color_hue + delta))
        
        # Enc C: Saturation (0-99)
        if raw_deltas[2] != 0:
            base_delta = 1 if raw_deltas[2] > 0 else -1
            mult = _calc_velocity_multiplier(3, VELOCITY_MAX_THRESH)
            delta = base_delta * mult
            _color_saturation = max(0, min(99, _color_saturation + delta))

def toggle_brightness():
    """Toggle brightness between current value and zero with fade animation."""
    global _brightness_saved, _brightness_off, _brightness_target, _brightness_fading
    
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
        ui_flash("Brightness: OFF", 0.8)

# ===================== GPIO / Rotary Encoders =====================

def reset_to_defaults():
    """Toggle between FFT and 3BAND audio analysis modes."""
    global BEAT_DETECT_METHOD
    
    # Toggle between FFT (0) and 3BAND (1) modes
    BEAT_DETECT_METHOD = 1 - BEAT_DETECT_METHOD
    mode_name = BEAT_DETECT_METHODS[BEAT_DETECT_METHOD]
    ui_flash(f"Mode: {mode_name}", 1.0)

def save_current_as_default():
    """Save current band params and modes as the selected default preset."""
    mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
    # Update in-memory preset with all 6 values
    DEFAULTS_PRESETS[mode_name] = (band.center, band.thresh, band.decay_ms, band.q,
                                    THRESH_MODE_INDEX, RELEASE_MODE_INDEX)
    # Persist to config file
    save_preset_values(mode_name, band.center, band.thresh, band.decay_ms, band.q,
                       THRESH_MODE_INDEX, RELEASE_MODE_INDEX)

def setup_gpio_inputs():
    if DEV_NO_HW:
        return
    if GPIO is None:
        raise RuntimeError("RPi.GPIO not available")
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    
    # Rotary Encoder 1 (Page selection)
    GPIO.setup(ENC1_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC1_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC1_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Rotary Encoder 2 (Param A - Freq/Speed/Preset)
    GPIO.setup(ENC2_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC2_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC2_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Rotary Encoder 3 (Param B - Thresh/Beats)
    GPIO.setup(ENC3_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC3_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC3_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Rotary Encoder 4 (Param C - Release/Mode)
    GPIO.setup(ENC4_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC4_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC4_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Rotary Encoder 5 (Brightness)
    GPIO.setup(ENC5_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC5_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    if not ENC5_SW_DISABLED:
        try:
            GPIO.setup(ENC5_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        except Exception as e:
            print(f"[GPIO] ENC5_SW (GPIO8) setup failed: {e} - disabling")
    
    # Reset button
    GPIO.setup(RESET_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

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
    # Velocity thresholds for different speed zones:
    PRECISION_VELOCITY = 0.8  # updates/sec - below this = min_mult (>1250ms between detents)
    SLOW_VELOCITY = 1.5       # updates/sec - at this point = 1x (>660ms between physical detents)
    FAST_VELOCITY = 15.0      # updates/sec - above this = max (<67ms between physical detents)
    
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


def _read_encoder_quadrature(enc_idx, clk_pin, dt_pin):
    """Read encoder using quadrature state machine for reliable direction detection.
    Returns direction: 1 for CW, -1 for CCW, 0 for no change.
    Uses sub-count threshold of 2 for more responsive feel.
    Note: Velocity multiplier is applied later in update_encoders() per-parameter."""
    global _enc_state, _enc_count, _enc_last_click_time, _enc_prev_click_time
    
    clk = GPIO.input(clk_pin)
    dt = GPIO.input(dt_pin)
    
    # Encode current state as 2-bit value: (CLK << 1) | DT
    # State 0 = 00 (both low)
    # State 1 = 01 (CLK low, DT high)
    # State 2 = 10 (CLK high, DT low)
    # State 3 = 11 (both high) - detent/rest position
    new_state = (clk << 1) | dt
    old_state = _enc_state[enc_idx]
    
    if new_state == old_state:
        return 0  # No change
    
    _enc_state[enc_idx] = new_state
    
    # Quadrature transition table:
    # CW sequence:  3 -> 1 -> 0 -> 2 -> 3 (or 3 -> 2 -> 0 -> 1 -> 3 depending on encoder)
    # CCW sequence: 3 -> 2 -> 0 -> 1 -> 3 (or reverse)
    #
    # Direction lookup: [old_state][new_state] -> direction
    # +1 = CW, -1 = CCW, 0 = invalid/skip
    transition = [
        # new:  0   1   2   3
        [  0, -1,  1,  0],  # old = 0
        [  1,  0,  0, -1],  # old = 1
        [ -1,  0,  0,  1],  # old = 2
        [  0,  1, -1,  0],  # old = 3
    ]
    
    direction = transition[old_state][new_state]
    
    if direction != 0:
        _enc_count[enc_idx] += direction
        
        # Using threshold of 2 for responsive velocity-based controls
        # Discrete controls use debouncing in update_encoders() instead
        if _enc_count[enc_idx] >= 2:
            _enc_count[enc_idx] = 0
            # Record click times for velocity calculation
            # Move last click to prev BEFORE updating last click
            _enc_prev_click_time[enc_idx] = _enc_last_click_time[enc_idx]
            _enc_last_click_time[enc_idx] = time.time()
            return 1  # CW click
        elif _enc_count[enc_idx] <= -2:
            _enc_count[enc_idx] = 0
            # Record click times for velocity calculation
            # Move last click to prev BEFORE updating last click
            _enc_prev_click_time[enc_idx] = _enc_last_click_time[enc_idx]
            _enc_last_click_time[enc_idx] = time.time()
            return -1  # CCW click
    
    return 0  # Not enough transitions yet


# Page encoder state (quadrature with debouncing)
# Uses full quadrature state machine for reliable direction detection
_page_enc_state = 3  # Initial state (both high = rest)
_page_enc_count = 0  # Raw quadrature count (4 counts per detent)
_page_enc_clk_buffer = [1, 1, 1]  # Debounce buffer for CLK
_page_enc_dt_buffer = [1, 1, 1]   # Debounce buffer for DT

# Quadrature transition table: [old_state][new_state] -> direction
# State encoding: (CLK << 1) | DT
# State 0 = both low, State 1 = CLK low/DT high, State 2 = CLK high/DT low, State 3 = both high (rest)
_PAGE_QUAD_TRANSITION = [
    [  0, -1,  1,  0],  # old = 0 (both low)
    [  1,  0,  0, -1],  # old = 1 (CLK low, DT high)
    [ -1,  0,  0,  1],  # old = 2 (CLK high, DT low)
    [  0,  1, -1,  0],  # old = 3 (both high - rest/detent)
]

def _read_page_encoder(clk_pin, dt_pin):
    """Read page encoder with debounced quadrature state machine.
    Returns -1, 0, or 1 for exactly one page change per physical detent.
    
    Uses 3-sample majority voting to filter electrical noise and a full
    quadrature state machine for reliable direction detection."""
    global _page_enc_state, _page_enc_count
    global _page_enc_clk_buffer, _page_enc_dt_buffer
    
    # Read and debounce using majority voting (2 of 3 samples)
    _page_enc_clk_buffer.pop(0)
    _page_enc_clk_buffer.append(GPIO.input(clk_pin))
    _page_enc_dt_buffer.pop(0)
    _page_enc_dt_buffer.append(GPIO.input(dt_pin))
    
    clk = 1 if sum(_page_enc_clk_buffer) >= 2 else 0
    dt = 1 if sum(_page_enc_dt_buffer) >= 2 else 0
    
    new_state = (clk << 1) | dt
    old_state = _page_enc_state
    
    if new_state != old_state:
        _page_enc_state = new_state
        direction = _PAGE_QUAD_TRANSITION[old_state][new_state]
        _page_enc_count += direction
        
        # 4 quadrature transitions = 1 physical detent
        # Using threshold of 2 for responsive feel (half-detent)
        if _page_enc_count >= 2:
            _page_enc_count = 0
            return 1
        elif _page_enc_count <= -2:
            _page_enc_count = 0
            return -1
    
    return 0


def encoder_reader():
    """Read all 5 rotary encoders for page selection, parameters, and brightness.
    
    Encoder 5's switch toggles brightness on/off with a fade animation.
    """
    global encoder1_value, encoder1_button
    global current_page
    global _enc_last_clk, _enc_last_dt, _enc_last_sw, _enc_delta, _reset_last_state
    global _enc_state, _enc_count, _enc_last_click_time, _enc_velocity_mult
    global _home_enc2_alt, _home_enc3_alt, _home_enc4_alt
    global _enc2_press_time, _enc2_saving, _enc2_save_complete
    global _3band_enc2_range_mode, _3band_enc3_gain_mode, THREEBAND_VIEW_MODE
    
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
    
    for i, (clk_pin, dt_pin) in enumerate(enc_pins):
        try:
            clk = GPIO.input(clk_pin)
            dt = GPIO.input(dt_pin)
            _enc_state[i] = (clk << 1) | dt
            _enc_count[i] = 0
        except Exception:
            _enc_state[i] = 3  # Default to rest position (both high)
            _enc_count[i] = 0
    
    # Initialize switch states
    _enc_last_sw[0] = GPIO.input(ENC1_SW)
    _enc_last_sw[1] = GPIO.input(ENC2_SW)
    _enc_last_sw[2] = GPIO.input(ENC3_SW)
    _enc_last_sw[3] = GPIO.input(ENC4_SW)
    if not ENC5_SW_DISABLED:
        try:
            _enc_last_sw[4] = GPIO.input(ENC5_SW)
        except Exception:
            _enc_last_sw[4] = 1
    else:
        _enc_last_sw[4] = 1
    
    _reset_last_state = GPIO.input(RESET_PIN)
    _reset_press_time = 0  # Track how long reset button is held
    
    try:
        while not STOP_THREADS:
            try:
                # ===== Encoder 1 - Page selection (uses detent-based detection) =====
                direction = _read_page_encoder(ENC1_CLK, ENC1_DT)
                if direction != 0:
                    pages = get_pages()
                    encoder1_value += direction
                    encoder1_value = max(0, min(len(pages) - 1, encoder1_value))
                    if current_page != encoder1_value:
                        current_page = encoder1_value
                        # Reset HOME page encoder toggles when changing pages
                        _home_enc2_alt = False
                        _home_enc3_alt = False
                        _home_enc4_alt = False
                
                enc1_sw = GPIO.input(ENC1_SW)
                if enc1_sw == 0 and _enc_last_sw[0] == 1:
                    time.sleep(0.02)  # Debounce
                    if GPIO.input(ENC1_SW) == 0:
                        # Cycle through 3-band view modes (0 -> 1 -> 2 -> 0)
                        global THREEBAND_VIEW_MODE
                        THREEBAND_VIEW_MODE = (THREEBAND_VIEW_MODE + 1) % 3
                        view_names = ["Spectrum", "Bands", "Detail"]
                        ui_flash(f"View: {view_names[THREEBAND_VIEW_MODE]}", 0.5)
                _enc_last_sw[0] = enc1_sw
                
                # ===== Encoder 2 - Param A (Freq/Speed/Preset) =====
                direction = _read_encoder_quadrature(1, ENC2_CLK, ENC2_DT)
                if direction != 0:
                    _enc_delta[1] += direction
                
                enc2_sw = GPIO.input(ENC2_SW)
                if enc2_sw == 0 and _enc_last_sw[1] == 1:
                    # Button just pressed - start timing
                    time.sleep(0.02)  # Debounce
                    if GPIO.input(ENC2_SW) == 0:
                        _enc2_press_time = time.time()
                        if get_pages()[current_page] == "SET":
                            _enc2_saving = True  # Start showing loader
                elif enc2_sw == 1 and _enc_last_sw[1] == 0:
                    # Button released
                    press_duration = time.time() - _enc2_press_time
                    if _enc2_saving:
                        # Was in save mode but released early - cancel
                        _enc2_saving = False
                    elif press_duration < 3.0:
                        # Short press - toggle Freq/Q mode on HOME page (or range mode in 3BAND)
                        pages = get_pages()
                        if pages[current_page] == "HOME":
                            if BEAT_DETECT_METHOD == 1:
                                # 3BAND mode: toggle range/width adjust mode
                                _3band_enc2_range_mode = not _3band_enc2_range_mode
                                if _3band_enc2_range_mode:
                                    ui_flash("Mode: Range", 0.5)
                                else:
                                    ui_flash("Mode: Band", 0.5)
                            else:
                                # FFT mode: toggle Freq/Q
                                _home_enc2_alt = not _home_enc2_alt
                        # Short press - toggle to/from ambient on PRE page
                        elif pages[current_page] == "PRE":
                            global _last_preset_before_ambient, BASE_PROGRAM, CYCLE_TRIGGER_COUNT, CYCLE_PHASE, CYCLES_BETWEEN_INDEX
                            if BASE_PROGRAM == 6:
                                # Currently on ambient - jump back to last preset (mode stays off)
                                BASE_PROGRAM = _last_preset_before_ambient
                                # Reset cycle state when preset changes
                                CYCLE_TRIGGER_COUNT = 0
                                CYCLE_PHASE = 0
                                ui_flash(f"Preset: {PROGRAM_NAMES[BASE_PROGRAM-1]}", 0.8)
                            else:
                                # Not on ambient - save current and jump to ambient
                                _last_preset_before_ambient = BASE_PROGRAM
                                BASE_PROGRAM = 6
                                # Reset cycle state and set mode to off
                                CYCLE_TRIGGER_COUNT = 0
                                CYCLE_PHASE = 0
                                CYCLES_BETWEEN_INDEX = 0  # Set mode to "off"
                                ui_flash("Preset: AMBIENT", 0.8)
                elif enc2_sw == 0 and _enc2_saving:
                    # Button still held on SET page - check if 3 seconds reached
                    if time.time() - _enc2_press_time >= 3.0:
                        _enc2_saving = False
                        save_current_as_default()  # Save and show "Saved"
                        _enc2_save_complete = time.time()
                _enc_last_sw[1] = enc2_sw
                
                # ===== Encoder 3 - Param B (Thresh/Beats) =====
                direction = _read_encoder_quadrature(2, ENC3_CLK, ENC3_DT)
                if direction != 0:
                    _enc_delta[2] += direction
                
                enc3_sw = GPIO.input(ENC3_SW)
                if enc3_sw == 0 and _enc_last_sw[2] == 1:
                    time.sleep(0.02)
                    if GPIO.input(ENC3_SW) == 0:
                        # Toggle Thresh/ThreshMode on HOME page (or gain mode in 3BAND)
                        pages = get_pages()
                        if pages[current_page] == "HOME":
                            if BEAT_DETECT_METHOD == 1:
                                # 3BAND mode: toggle gain adjust mode
                                _3band_enc3_gain_mode = not _3band_enc3_gain_mode
                                if _3band_enc3_gain_mode:
                                    ui_flash("Mode: Gain", 0.5)
                                else:
                                    ui_flash("Mode: Thresh", 0.5)
                            else:
                                # FFT mode: toggle Thresh/ThreshMode
                                _home_enc3_alt = not _home_enc3_alt
                        # Turn Mode OFF on PRE page
                        elif pages[current_page] == "PRE":
                            if CYCLES_BETWEEN_INDEX != 0:
                                CYCLES_BETWEEN_INDEX = 0
                                ui_flash("Mode: off", 0.8)
                        # Toggle Input Volume / Detection Mode on SET page
                        elif pages[current_page] == "SET":
                            global _setup_enc3_detect
                            _setup_enc3_detect = not _setup_enc3_detect
                            if _setup_enc3_detect:
                                ui_flash(f"Detect: {DETECT_MODES[DETECT_MODE_INDEX]}", 0.8)
                            else:
                                ui_flash(f"Input Vol: {INPUT_VOLUME}", 0.8)
                        # Toggle Hue/Temp mode on COLOR page
                        elif pages[current_page] == "COLOR":
                            global _color_enc3_temp_mode
                            _color_enc3_temp_mode = not _color_enc3_temp_mode
                            if _color_enc3_temp_mode:
                                ui_flash("Mode: Temp", 0.8)
                            else:
                                ui_flash("Mode: Hue", 0.8)
                _enc_last_sw[2] = enc3_sw
                
                # ===== Encoder 4 - Param C (Release/Mode) =====
                direction = _read_encoder_quadrature(3, ENC4_CLK, ENC4_DT)
                if direction != 0:
                    _enc_delta[3] += direction
                
                enc4_sw = GPIO.input(ENC4_SW)
                if enc4_sw == 0 and _enc_last_sw[3] == 1:
                    time.sleep(0.02)
                    if GPIO.input(ENC4_SW) == 0:
                        # Toggle Release/ReleaseMode on HOME page
                        pages = get_pages()
                        if pages[current_page] == "HOME":
                            _home_enc4_alt = not _home_enc4_alt
                        # Toggle Output/Channels mode on SET page
                        elif pages[current_page] == "SET":
                            global _setup_enc4_channels
                            _setup_enc4_channels = not _setup_enc4_channels
                            if _setup_enc4_channels:
                                ui_flash(f"Channels: {DMX_CHANNEL_COUNT}", 0.8)
                            else:
                                ui_flash(f"Output: {DMX_OUTPUT_MODES[DMX_OUTPUT_MODE]}", 0.8)
                        # No action on PRE page for encoder 4 button
                _enc_last_sw[3] = enc4_sw
                
                # ===== Encoder 5 - Brightness =====
                direction = _read_encoder_quadrature(4, ENC5_CLK, ENC5_DT)
                if direction != 0:
                    _enc_delta[4] += direction
                
                # Encoder 5 switch - toggle brightness on/off with fade
                global _brightness_click_flash, _brightness_gpio8_state
                if not ENC5_SW_DISABLED:
                    try:
                        enc5_sw = GPIO.input(ENC5_SW)
                    except Exception as e:
                        enc5_sw = 1
                    _brightness_gpio8_state = enc5_sw  # Update for UI display
                    if enc5_sw == 0 and _enc_last_sw[4] == 1:
                        _brightness_click_flash = 1.0  # Flash indicator in UI
                        time.sleep(0.02)  # Debounce
                        try:
                            enc5_sw_after = GPIO.input(ENC5_SW)
                        except Exception:
                            enc5_sw_after = 1
                        if enc5_sw_after == 0:
                            toggle_brightness()
                    _enc_last_sw[4] = enc5_sw
                
                # ===== Reset button (GPIO 25) =====
                # Press = toggle between FFT and 3BAND audio analysis modes
                reset_btn = GPIO.input(RESET_PIN)
                if reset_btn == 0 and _reset_last_state == 1:
                    # Button just pressed
                    time.sleep(0.02)  # Debounce
                    if GPIO.input(RESET_PIN) == 0:
                        reset_to_defaults()
                _reset_last_state = reset_btn
                
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
trigger_flash = 0.0  # Decays over time, >0 means recently triggered
TRIGGER_FLASH_DECAY = 0.86  # Decay rate for ~100ms visible at 86Hz audio callback rate

bp   = None
envd = None
agc  = Agc(target=AGC_TARGET, tau=0.95)

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

    bp   = BiquadBandpass(SR, band.center, band.q)
    envd = EnvDetector(SR, attack_ms=8.0, release_ms=80.0)
    
    # Initialize 3-band onset detector
    three_band_detector = ThreeBandOnsetDetector(SR, n_fft=FFT_SIZE)

    band.attack_ms = DEFAULT_ATTACK_MS
    frame_dt_ms = (HOP / SR) * 1000.0
    was_above = False

    # #region agent log
    _cb_slow_count = [0]
    # #endregion

    def cb(indata, frames, time_info, status):
        nonlocal was_above
        global live_band_env, live_threshold, input_rms
        global last_trigger_ts, chase_idx, group34_phase, group12_phase
        global PROGRAM, BASE_PROGRAM, CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE, CYCLE_AMBIENT_START
        global fft_bands, fft_peaks, fft_peak_times, fft_recent_max
        global _recent_min, _effective_thresh
        global _reactive_brightness_scale, _effective_release_display, _effective_brightness_display
        global _trigger_speed_multiplier
        global prev_band_energies, fft_flux, band_running_mean, band_running_max

        if not RUNNING:
            return

        # #region agent log
        _cb_start = time.time()
        # #endregion

        # Use channel 1 (Input 2 on Scarlett Solo - the line input on back)
        # Change to indata[:, 0] for Input 1 (front XLR/inst jack)
        x = indata[:, 1].astype(np.float32)
        x = x * INPUT_GAIN  # Apply input volume control
        input_rms = float(np.sqrt(np.mean(x*x)) + 1e-12)

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
        
        # Calculate FFT band energies with compensation (OPTIMIZED)
        raw_levels = []
        for i in range(len(FFT_BANDS)):
            energy = get_band_energy_fast(fft_mag, i)
            energy *= FFT_COMPENSATION[i]
            if energy > 1e-10:
                db = 20 * math.log10(energy + 1e-10)
                normalized = max(0, (db + 60) / 50)
            else:
                normalized = 0
            raw_levels.append(normalized)
        
        # Calculate spectral flux (onset detection) before updating prev_band_energies
        fft_flux = get_spectral_flux(raw_levels, prev_band_energies)
        prev_band_energies = raw_levels.copy()
        
        # ===== 3-Band Onset Detector =====
        # Use the SAME normalized fft_bands that the display uses
        # This ensures the VU meter matches what you see on the FFT
        global _last_3band_update
        if three_band_detector is not None:
            # Run detector at THREEBAND_UPDATE_HZ (100 Hz)
            dt = now - _last_3band_update
            if dt >= 1.0 / THREEBAND_UPDATE_HZ:
                # Use normalized fft_bands (0-1 scale) instead of raw FFT
                # #region agent log
                _3band_start = time.time()
                # #endregion
                three_band_detector.update_from_fft_bands(fft_bands, FFT_BANDS, dt)
                _last_3band_update = now
                # #region agent log
                _3band_elapsed = time.time() - _3band_start
                if _3band_elapsed > 0.005:  # >5ms is slow for this operation
                    import json as _json
                    with open("/home/benglasser/.cursor/debug.log", "a") as _f:
                        _f.write(_json.dumps({"ts": int(now*1000), "type": "SLOW_3BAND", "ms": int(_3band_elapsed*1000)}) + "\n")
                # #endregion
        
        # Apply per-band normalization (spectral whitening) - SKIPPED for performance
        # The raw_levels are already compensated and work well enough
        whitened_levels = raw_levels  # Use raw levels directly
        
        # Auto-normalize FFT (vectorized for performance)
        raw_arr = np.array(raw_levels, dtype=np.float32)
        current_max = float(np.max(raw_arr)) if len(raw_arr) > 0 else 0
        if current_max > fft_recent_max:
            fft_recent_max = current_max
        else:
            fft_recent_max = fft_recent_max * fft_max_decay
        
        norm_factor = max(0.1, fft_recent_max)
        normalized = np.minimum(1.0, raw_arr / norm_factor)
        
        # Vectorized attack/decay
        attack_mask = normalized > fft_bands
        fft_bands[attack_mask] = 0.7 * normalized[attack_mask] + 0.3 * fft_bands[attack_mask]
        fft_bands[~attack_mask] = 0.88 * fft_bands[~attack_mask]
        
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
        
        # Use the SAME fft_bands values that are displayed on screen
        # This ensures the trigger matches exactly what you see
        display_max_in_q = 0.0
        q_flux_max = 0.0
        for i, (band_low, band_high) in enumerate(FFT_BANDS):
            band_center_freq = math.sqrt(band_low * band_high)
            if low_freq <= band_center_freq <= high_freq:
                display_max_in_q = max(display_max_in_q, fft_bands[i])
                q_flux_max = max(q_flux_max, fft_flux[i])
        
        # Combine level and flux based on detection mode
        detect_mode = DETECT_MODES[DETECT_MODE_INDEX]
        
        if detect_mode == "level":
            # Pure level mode - use display values directly (what you see = what triggers)
            q_band_max = display_max_in_q
        elif detect_mode == "flux":
            # Pure flux mode - uses spectral flux for onset/transient detection
            # Better for drums and percussive sounds
            q_band_max = min(1.0, q_flux_max * 3.0)  # Scale flux to usable range
        else:  # "hybrid" (default)
            # Hybrid mode - use display level but boost with flux for transients
            flux_boost = min(0.15, q_flux_max * 1.5)  # Flux adds up to 0.15 boost
            q_band_max = min(1.0, display_max_in_q + flux_boost)
        
        # Get frequency-dependent envelope smoothing
        attack_ms, release_ms = get_envelope_times(clamped_center)
        # Calculate EMA coefficient based on attack time (faster attack = lower coefficient)
        freq_ema = math.exp(-1.0 / (max(1e-3, attack_ms) * 1e-3 * SR / HOP))
        
        # Smooth the trigger envelope with frequency-appropriate response
        if q_band_max > live_band_env:
            # Attack: use frequency-dependent faster response
            v = freq_ema * live_band_env + (1.0 - freq_ema) * q_band_max
        else:
            # Release: use standard smoothing
            v = ENV_EMA * live_band_env + (1.0 - ENV_EMA) * q_band_max
        live_band_env = v
        live_threshold = band.thresh

        # Update tracking variable for adaptive threshold mode
        _recent_min = min(_recent_min * 1.005, live_band_env)  # Slowly rise back up

        above = (live_band_env >= band.thresh)
        can_fire = ((now - last_trigger_ts)*1000.0 >= REFRACTORY_MS)

        current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
        if current_mode == "x+1":
            # x+1 mode: toggle between base and neighbor preset
            p_base, p_neighbor = program_pair_for_base(BASE_PROGRAM)
            active_prog = p_base if CYCLE_PHASE == 0 else p_neighbor
        elif current_mode == "random":
            # Random mode: BASE_PROGRAM is the active program (changes on beat)
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

        # Decay trigger flash indicator
        global trigger_flash, _brightness_click_flash
        trigger_flash = trigger_flash * TRIGGER_FLASH_DECAY
        _brightness_click_flash = _brightness_click_flash * 0.9  # Decay click indicator

        # Determine trigger based on detection method and threshold mode
        thresh_mode = THRESH_MODES[THRESH_MODE_INDEX]
        should_trigger = False
        
        # Check if using 3BAND detection mode
        if BEAT_DETECT_METHOD == 1 and three_band_detector is not None:
            # 3BAND_DETECT mode: use trigger from selected band
            should_trigger = three_band_detector.trigger[THREEBAND_SELECTED]
            # Use the selected band's adaptive threshold for display
            _effective_thresh = three_band_detector.get_adaptive_threshold(THREEBAND_SELECTED)
        elif thresh_mode == "fixed":
            # FFT_STANDARD: Fixed edge-triggered (only triggers once when crossing above threshold)
            _effective_thresh = band.thresh
            should_trigger = above and not was_above and can_fire
        elif thresh_mode == "adapt":
            # FFT_STANDARD: Adaptive - trigger on rise above recent minimum
            # Scale threshold: 0=very sensitive (0.02 rise), 99=less sensitive (0.6 rise)
            adapt_thresh = 0.02 + band.thresh * 0.58
            relative_rise = live_band_env - _recent_min
            _effective_thresh = min(1.0, _recent_min + adapt_thresh)  # Show where trigger point is
            should_trigger = relative_rise >= adapt_thresh and can_fire
            if should_trigger:
                _recent_min = live_band_env  # Reset after trigger

        if should_trigger and active_prog in (1, 2, 3, 4, 5):
            # Calculate time since last trigger for speed multiplier
            time_since_last_ms = (now - last_trigger_ts) * 1000.0
            
            # Update speed multiplier based on trigger interval
            # Slow triggers (> 1000ms apart) = max multiplier (2.0x) - boosts effect
            # Fast triggers (< 200ms apart) = min multiplier (0.3x) - dampens effect
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
            trigger_flash = 1.0  # Flash on trigger
            if TRIG_DEBUG:
                print(f"[TRIG] mode={thresh_mode} env={live_band_env:.5f} thr={band.thresh:.5f} prog={active_prog} mult={_trigger_speed_multiplier:.2f}")

            # Calculate effective decay based on release mode
            release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
            effective_decay = band.decay_ms
            
            # Calculate boost amount from signal energy directly
            # live_band_env is 0-1, representing the smoothed amplitude of the frequency band
            # Louder/more energetic = higher value = more boost
            # This works the same regardless of threshold mode
            boost_amount = live_band_env
            
            # Multiply amplitude by speed multiplier
            # Fast triggers (< 200ms) = 1.0x (just amplitude)
            # Slow triggers (> 1000ms) = 2.0x (amplitude doubled)
            combined_boost = boost_amount * _trigger_speed_multiplier
            
            if release_mode == "react":
                # Reactive: release scales up from set value based on signal strength + speed
                # Check if we're still in the buffer period after knob turn
                if (now - _release_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    # Max 300% boost based on combined signal strength and trigger speed
                    scale = 1.0 + min(3.0, combined_boost * 3.0)  # 1x to 4x
                    effective_decay = band.decay_ms * scale
                    # Update display value (0-99 scale)
                    _effective_release_display = int((effective_decay - 40.0) / 4960.0 * 99)
                    _effective_release_display = max(0, min(99, _effective_release_display))
            elif release_mode == "bright":
                # Reactive brightness: brightness scales up from set value based on signal strength + speed
                # Check if we're still in the buffer period after knob turn
                if (now - _brightness_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    # Max 50% boost based on combined signal strength and trigger speed
                    boost = min(0.5, combined_boost * 0.5) * BRIGHTNESS  # Up to 50% of set brightness
                    _reactive_brightness_scale = min(1.0, BRIGHTNESS + boost)
                    # Update display value (0-99 scale)
                    _effective_brightness_display = int(_reactive_brightness_scale * 99)
                    _effective_brightness_display = max(0, min(99, _effective_brightness_display))
            elif release_mode == "both":
                # Both: combines react (release scaling) and bright (brightness scaling)
                # Check release buffer
                if (now - _release_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    # Max 300% boost based on combined signal strength and trigger speed
                    scale = 1.0 + min(3.0, combined_boost * 3.0)  # 1x to 4x
                    effective_decay = band.decay_ms * scale
                    # Update display value (0-99 scale)
                    _effective_release_display = int((effective_decay - 40.0) / 4960.0 * 99)
                    _effective_release_display = max(0, min(99, _effective_release_display))
                # Check brightness buffer
                if (now - _brightness_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    # Max 50% boost based on combined signal strength and trigger speed
                    boost = min(0.5, combined_boost * 0.5) * BRIGHTNESS  # Up to 50% of set brightness
                    _reactive_brightness_scale = min(1.0, BRIGHTNESS + boost)
                    # Update display value (0-99 scale)
                    _effective_brightness_display = int(_reactive_brightness_scale * 99)
                    _effective_brightness_display = max(0, min(99, _effective_brightness_display))
            elif release_mode == "rand":
                # Random: add/subtract random value between -20 and +20 from current release
                # Check if we're still in the buffer period after knob turn
                if (now - _release_knob_last_turn) >= REACTIVE_BUFFER_SECONDS:
                    # Calculate base release display value (0-99)
                    base_release = int((band.decay_ms - 40.0) / 4960.0 * 99)
                    base_release = max(0, min(99, base_release))
                    # Add random offset between -20 and +20
                    rand_offset = random.randint(-20, 20)
                    new_release_display = max(0, min(99, base_release + rand_offset))
                    # Convert back to decay_ms
                    effective_decay = 40.0 + (new_release_display / 99.0) * 4960.0
                    # Update display value
                    _effective_release_display = new_release_display

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
            elif current_mode == "random":
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

        # #region agent log
        _cb_elapsed = time.time() - _cb_start
        if _cb_elapsed > 0.012 and _cb_slow_count[0] < 30:  # >12ms is slow (buffer is 11.6ms)
            _cb_slow_count[0] += 1
            import json as _json
            with open("/home/benglasser/.cursor/debug.log", "a") as _f:
                _f.write(_json.dumps({"ts": int(_cb_start*1000), "type": "SLOW_CB", "ms": int(_cb_elapsed*1000)}) + "\n")
        # #endregion

    try:
        with sd.InputStream(device=DEVICE_INDEX, channels=2, samplerate=SR, blocksize=HOP, callback=cb):
            APP_STATE = "ready"
            if AUDIO_DEBUG:
                print(f"[AUDIO] Using device {DEVICE_INDEX}: {DEVICE_NAME}")
            while not STOP_THREADS:
                time.sleep(0.05)
    except Exception as e:
        APP_STATE = "error"
        APP_ERROR = f"Audio init failed: {e}"
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
                bus_speed_hz=4000000,  # 4MHz - faster OLED updates
                gpio_DC=OLED_DC_PIN,
                gpio_RST=OLED_RST_PIN,
            )
            self.device = ssd1309(serial, width=width, height=height, rotate=0)
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

    def _draw_text_kerned(self, draw, pos, text, font, fill=1, kerning=1):
        """Draw text with custom letter spacing (kerning).
        
        Args:
            draw: ImageDraw object
            pos: (x, y) tuple for starting position
            text: String to draw
            font: Font to use
            fill: Fill color (1 for white on OLED)
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
        """Convert frequency to x position (log scale)."""
        if freq <= FFT_MIN_FREQ:
            return x_start
        if freq >= FFT_MAX_FREQ:
            return x_start + width - 1
        log_min = math.log10(FFT_MIN_FREQ)
        log_max = math.log10(FFT_MAX_FREQ)
        log_freq = math.log10(freq)
        ratio = (log_freq - log_min) / (log_max - log_min)
        return int(x_start + ratio * (width - 1))

    def _draw_fft_spectrum(self, draw, x, y, width, height):
        """Draw FFT spectrum with Q band highlighting.
        - Left: Vertical meter showing band envelope vs threshold
        - Bars inside Q range above threshold: crosshatch/dashed (triggering zone)
        - Bars inside Q range below threshold: solid fill
        - Bars outside Q range: single-pixel outline
        - Q range boundaries: vertical lines
        - Threshold line: horizontal within Q range"""
        
        # === Left side: Band envelope meter (10px wide) ===
        meter_width = 10
        meter_x = x + 1
        meter_height = height - 2
        meter_y = y + 1
        
        # Draw meter outline
        draw.rectangle((meter_x, meter_y, meter_x + meter_width - 1, meter_y + meter_height - 1), outline=1)
        
        # Draw band envelope level (filled from bottom)
        env_height = int(min(1.0, live_band_env) * (meter_height - 2))
        if env_height > 0:
            fill_y = meter_y + meter_height - 1 - env_height
            triggered = trigger_flash > 0.2
            if triggered:
                # Solid fill with horizontal lines when triggered
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=1)
                for py in range(fill_y, meter_y + meter_height - 1, 3):
                    draw.line((meter_x + 1, py, meter_x + meter_width - 2, py), fill=0)
            else:
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=1)
        
        # Draw threshold line on meter
        thresh_meter_y = meter_y + meter_height - 1 - int(_effective_thresh * (meter_height - 2))
        draw.line((meter_x, thresh_meter_y, meter_x + meter_width - 1, thresh_meter_y), fill=1)
        
        # === Right side: FFT spectrum ===
        fft_x = meter_x + meter_width + 3
        fft_width = width - meter_width - 4
        
        num_bands = len(fft_bands)
        
        # Calculate Q bandwidth for highlighting
        clamped_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center))
        bandwidth = clamped_center / max(0.1, band.q)
        low_freq = max(FFT_MIN_FREQ, clamped_center - bandwidth / 2)
        high_freq = min(FFT_MAX_FREQ, clamped_center + bandwidth / 2)
        
        low_x = self._freq_to_x(low_freq, fft_x, fft_width)
        high_x = self._freq_to_x(high_freq, fft_x, fft_width)
        # Use effective threshold for display (varies by threshold mode)
        thresh_y = y + height - int(_effective_thresh * height)
        
        # Calculate bar positions to fill the entire width (no gaps)
        bar_step = fft_width / num_bands
        
        for i, level in enumerate(fft_bands):
            bx_start = fft_x + int(i * bar_step)
            bx_end = fft_x + int((i + 1) * bar_step) - 1
            
            if bx_end >= fft_x + fft_width:
                bx_end = fft_x + fft_width - 1
            if bx_start >= fft_x + fft_width:
                continue
            
            bar_h = int(level * height * FFT_VISUAL_GAIN)
            if bar_h <= 0:
                continue
            
            # Clip to view height (bars can exceed view with visual gain)
            bar_h = min(bar_h, height)
            
            # Get the center frequency of this band
            band_low, band_high = FFT_BANDS[i]
            band_center = math.sqrt(band_low * band_high)
            
            # Check if this band is within the Q range
            in_q_range = (band_center >= low_freq and band_center <= high_freq)
            
            bar_top = y + height - bar_h
            bar_bottom = y + height - 1
            
            if in_q_range:
                # Check if bar crosses threshold
                if bar_top < thresh_y:
                    # Part above threshold - crosshatch (triggering zone)
                    # Leave 1px gap above threshold line for distinction
                    above_top = bar_top
                    above_bottom = min(thresh_y - 2, bar_bottom)  # -2 leaves 1px gap
                    if above_top <= above_bottom:
                        for py in range(above_top, above_bottom + 1):
                            for px in range(bx_start, bx_end + 1):
                                if (px + py) % 2 == 0:
                                    draw.point((px, py), fill=1)
                    
                    # Part below threshold - solid fill (start 1px below threshold line)
                    if thresh_y + 1 <= bar_bottom:
                        draw.rectangle((bx_start, thresh_y + 1, bx_end, bar_bottom), fill=1)
                else:
                    # Entirely below threshold - solid fill
                    draw.rectangle((bx_start, bar_top, bx_end, bar_bottom), fill=1)
            else:
                # Single pixel outline for bands outside Q range
                draw.line((bx_start, bar_top, bx_end, bar_top), fill=1)
                if bar_h > 1:
                    draw.line((bx_start, bar_top, bx_start, bar_bottom), fill=1)
                    draw.line((bx_end, bar_top, bx_end, bar_bottom), fill=1)
        
        # Draw Q range boundary lines (vertical)
        draw.line((low_x, y, low_x, y + height - 1), fill=1)
        draw.line((high_x, y, high_x, y + height - 1), fill=1)
        
        # Threshold line (horizontal within Q range)
        if y <= thresh_y < y + height:
            draw.line((low_x, thresh_y, high_x, thresh_y), fill=1)
        
        # Trigger flash - filled bar at top of Q range when triggered
        if trigger_flash > 0.2:
            flash_height = 3
            draw.rectangle((low_x + 1, y, high_x - 1, y + flash_height), fill=1)

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
        triggered = three_band_detector.trigger_flash[sel] > 0.3
        
        # === Left side: Small onset meter (10px wide) ===
        meter_width = 10
        meter_x = x + 1
        meter_height = height - 2
        meter_y = y + 1
        
        # Draw meter outline
        draw.rectangle((meter_x, meter_y, meter_x + meter_width - 1, meter_y + meter_height - 1), outline=1)
        
        # Draw onset level (filled from bottom) - this is what actually triggers
        onset_height = int(min(1.0, onset) * (meter_height - 2))
        if onset_height > 0:
            fill_y = meter_y + meter_height - 1 - onset_height
            if triggered:
                # Solid fill with inverted top portion when triggered (faster than checkerboard)
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=1)
                # Add horizontal lines for visual distinction
                for py in range(fill_y, meter_y + meter_height - 1, 3):
                    draw.line((meter_x + 1, py, meter_x + meter_width - 2, py), fill=0)
            else:
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=1)
        
        # No threshold line in 3-band mode (TouchDesigner-style detection doesn't use visual threshold)
        
        # === Right side: FFT spectrum ===
        fft_x = meter_x + meter_width + 3
        fft_width = width - meter_width - 4  # Use more of the available width
        fft_height = height
        num_bands = len(fft_bands)
        
        # Calculate bar positions to fill the entire width (no gaps)
        bar_step = fft_width / num_bands
        
        # Get x positions for selected band boundaries (relative to fft_x)
        sel_low_x = self._freq_to_x(sel_f_lo, fft_x, fft_width)
        sel_high_x = self._freq_to_x(sel_f_hi, fft_x, fft_width)
        
        # Draw FFT bars
        for i, level in enumerate(fft_bands):
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
                draw.rectangle((bx_start, bar_top, bx_end, bar_bottom), fill=1)
            else:
                # Outline only for bars outside selected range
                draw.line((bx_start, bar_top, bx_end, bar_top), fill=1)
                if bar_h > 1:
                    draw.line((bx_start, bar_top, bx_start, bar_bottom), fill=1)
                    draw.line((bx_end, bar_top, bx_end, bar_bottom), fill=1)
        
        # Draw vertical lines at band boundaries
        draw.line((sel_low_x, y, sel_low_x, y + fft_height - 1), fill=1)
        draw.line((sel_high_x, y, sel_high_x, y + fft_height - 1), fill=1)
        
        # No threshold line in 3-band mode
        
        # Draw trigger flash at top of selected range
        if triggered:
            flash_height = 3
            draw.rectangle((sel_low_x + 1, y, sel_high_x - 1, y + flash_height), fill=1)
    
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
            triggered = three_band_detector.trigger_flash[i] > 0.3
            
            # Selected band: double border (outer indicator)
            if is_selected:
                draw.rectangle((rect_x - 2, rect_y - 2, rect_x + rect_width + 1, rect_y + rect_height + 1), outline=1)
                draw.rectangle((rect_x, rect_y, rect_x + rect_width - 1, rect_y + rect_height - 1), outline=1)
            else:
                # Non-selected: single outline
                draw.rectangle((rect_x, rect_y, rect_x + rect_width - 1, rect_y + rect_height - 1), outline=1)
            
            # Trigger: fill inside the rectangle
            if triggered:
                draw.rectangle((rect_x + 2, rect_y + 2, rect_x + rect_width - 3, rect_y + rect_height - 3), fill=1)
            
            # Draw band name centered in rectangle
            label = band_names[i]
            text_width = len(label) * 5
            text_x = rect_x + (rect_width - text_width) // 2
            text_y = rect_y + (rect_height - 8) // 2
            
            # Invert text color when triggered (so it's visible on filled background)
            fill_color = 0 if triggered else 1
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
        triggered = three_band_detector.trigger_flash[sel] > 0.3
        
        # === Left side: VU meter (12px wide) ===
        meter_width = 12
        meter_x = x + 2
        meter_height = height - 2
        meter_y = y + 1
        
        # Draw meter outline
        draw.rectangle((meter_x, meter_y, meter_x + meter_width - 1, meter_y + meter_height - 1), outline=1)
        
        # Draw onset level (filled from bottom)
        onset_height = int(min(1.0, onset) * (meter_height - 2))
        if onset_height > 0:
            fill_y = meter_y + meter_height - 1 - onset_height
            if triggered:
                # Solid fill with horizontal lines when triggered (faster than checkerboard)
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=1)
                for py in range(fill_y, meter_y + meter_height - 1, 3):
                    draw.line((meter_x + 1, py, meter_x + meter_width - 2, py), fill=0)
            else:
                draw.rectangle((meter_x + 1, fill_y, meter_x + meter_width - 2, meter_y + meter_height - 2), fill=1)
        
        # No threshold line in 3-band mode (TouchDesigner-style detection doesn't use visual threshold)
        
        # === Middle: Info ===
        info_x = meter_x + meter_width + 4
        
        # Row 1: Band name + trigger indicator
        draw.text((info_x, y), band_names[sel], font=self._font_small, fill=1)
        trig_box_x = info_x + 22
        trig_box_size = 6
        if triggered:
            draw.rectangle((trig_box_x, y, trig_box_x + trig_box_size, y + trig_box_size), fill=1)
        else:
            draw.rectangle((trig_box_x, y, trig_box_x + trig_box_size, y + trig_box_size), outline=1)
        
        # Row 2: Frequency range
        range_str = self._format_freq_range(f_lo, f_hi)
        draw.text((info_x, y + 9), range_str, font=self._font_small, fill=1)
        
        # === Right side: Running line graph ===
        graph_width = 64  # Wider graph
        graph_x = x + width - graph_width - 2
        graph_y = y + 1
        graph_height = height - 2
        
        # Draw graph outline
        draw.rectangle((graph_x, graph_y, graph_x + graph_width - 1, graph_y + graph_height - 1), outline=1)
        
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
                draw.line((prev_px, prev_py, px, py), fill=1)
            
            # Draw trigger marker (vertical line from bottom when triggered)
            if trigger_history[hist_idx]:
                draw.line((px, base_y, px, trig_top), fill=1)
            
            prev_px, prev_py = px, py

    def _draw_sun_icon(self, draw, x, y, size=7):
        """Draw sun icon for brightness."""
        cx, cy = x + size // 2, y + size // 2
        draw.rectangle((cx - 1, cy - 1, cx, cy), fill=1)
        draw.point((cx, y), fill=1)
        draw.point((cx, y + size - 1), fill=1)
        draw.point((x, cy), fill=1)
        draw.point((x + size - 1, cy), fill=1)
        draw.point((x + 1, y + 1), fill=1)
        draw.point((x + size - 2, y + 1), fill=1)
        draw.point((x + 1, y + size - 2), fill=1)
        draw.point((x + size - 2, y + size - 2), fill=1)

    def _draw_global_controls(self, draw, x, y):
        """Draw program number and brightness percentage."""
        # Program number - show cycling state if active
        current_mode = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
        if current_mode == "x+1" and CYCLE_PHASE == 1:
            _, neighbor = program_pair_for_base(BASE_PROGRAM)
            draw.text((x, y), f"(P{neighbor})", font=self._font_small, fill=1)
        elif current_mode == "rnd/amb" and CYCLE_PHASE == 1:
            # Show (P6) when in rnd/amb ambient phase
            draw.text((x, y), f"(P6)", font=self._font_small, fill=1)
        else:
            draw.text((x, y), f"P{BASE_PROGRAM}", font=self._font_small, fill=1)
        
        # Sun icon + brightness percentage (use smoothed display value)
        self._draw_sun_icon(draw, x, y + 10, size=7)
        release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
        base_brt = int(_display_bright * 100)
        if release_mode in ("bright", "both") and _effective_brightness_display > base_brt:
            # Show effective brightness when boosted in bright/both mode
            brt_pct = _effective_brightness_display
            draw.text((x + 9, y + 11), f"{brt_pct:2d}", font=self._font_small, fill=1)
        else:
            draw.text((x + 9, y + 11), f"{base_brt:2d}", font=self._font_small, fill=1)
    
    def _draw_brightness_inline(self, draw, x, y):
        """Draw sun icon + brightness percentage inline."""
        self._draw_sun_icon(draw, x, y, size=7)
        release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
        base_brt = int(_display_bright * 100)
        if release_mode in ("bright", "both") and _effective_brightness_display > base_brt:
            # Show effective brightness when boosted in bright/both mode
            brt_pct = _effective_brightness_display
            draw.text((x + 9, y + 1), f"{brt_pct:2d}", font=self._font_small, fill=1)
        else:
            draw.text((x + 9, y + 1), f"{base_brt:2d}", font=self._font_small, fill=1)
    
    def _draw_trigger_indicator(self, draw, x, y):
        """Draw trigger indicator dot at specified position."""
        if trigger_flash > 0.2:
            # Draw filled circle (trigger active)
            draw.ellipse((x, y, x + 6, y + 6), fill=1)
        else:
            # Draw empty circle (trigger idle)
            draw.ellipse((x, y, x + 6, y + 6), outline=1)

    def _draw_page_icon(self, draw, x, y, page_name, selected):
        """Draw a single page icon (11x11 box with 9x9 icon inside)."""
        box_size = 11
        
        # Draw box (filled if selected)
        if selected:
            draw.rectangle((x, y, x + box_size - 1, y + box_size - 1), outline=1, fill=1)
            fill_color = 0
        else:
            draw.rectangle((x, y, x + box_size - 1, y + box_size - 1), outline=1, fill=0)
            fill_color = 1
        
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
                draw.rectangle((tab_x, y, tab_end_x, y + box_height - 1), outline=1, fill=1)
                fill_color = 0
            else:
                draw.rectangle((tab_x, y, tab_end_x, y + box_height - 1), outline=1, fill=0)
                fill_color = 1
            
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
                "Q" if _home_enc2_alt else "Freq",
                "Th-Mode" if _home_enc3_alt else "Thresh",
                "R-Mode" if _home_enc4_alt else "Release"
            ]
        # Override labels for SET page based on encoder toggle states
        elif page_name == "SET":
            labels = [
                labels[0],  # Default
                "Detect" if _setup_enc3_detect else "Gain",
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
        tight_labels = {"Th-Mode", "R-Mode"}  # Labels that need tighter kerning
        tight_values = {"rnd/amb", "random", "ODD/EVEN"}  # Values that need tighter kerning (kern=0)
        extra_tight_values = set()  # Values that need extra tight kerning (kern=-1)
        
        for i in range(3):
            px = x + i * (col_width + spacing)
            label_kern = 0 if labels[i] in tight_labels else 1
            self._draw_text_kerned(draw, (px, y), labels[i], self._font_small, fill=1, kerning=label_kern)
            
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
                            # Release - 0-99 (based on decay_ms: 40-5000ms range)
                            release_pct = int((band.decay_ms - 40.0) / 4960.0 * 99)
                            release_pct = max(0, min(99, release_pct))
                            if abs(release_pct - _display_release) > 1:
                                _display_release = release_pct
                            release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                            if release_mode in ("react", "rand", "both"):
                                val_str = f"{_effective_release_display}"
                            else:
                                val_str = f"{_display_release}"
                else:
                    # FFT_STANDARD mode
                    if i == 0:  # Frequency or Q (based on toggle)
                        if _home_enc2_alt:
                            # Q mode - display as 0-99 (inverted: higher = wider range)
                            # Use logarithmic mapping for perceptual linearity
                            # Q_MAX (8.0) -> 0, Q_MIN (frequency-dependent) -> 99
                            q_min = get_q_min(_display_freq)
                            q_ratio = math.log(Q_MAX / max(q_min, _display_q)) / math.log(Q_MAX / q_min)
                            q_pct = round(q_ratio * 99)
                            q_pct = max(0, min(99, q_pct))
                            if abs(q_pct - _display_q_pct) > 1:
                                _display_q_pct = q_pct
                            val_str = f"{_display_q_pct}"
                        else:
                            # Freq mode - show in Hz (with tenths for kHz)
                            freq_hz = _display_freq
                            if freq_hz >= 1000:
                                # Show to tenths place for kHz (e.g., "1.2k", "10.5k")
                                freq_khz = freq_hz / 1000.0
                                if freq_khz >= 10:
                                    val_str = f"{freq_khz:.1f}k"
                                else:
                                    val_str = f"{freq_khz:.1f}k"
                            else:
                                val_str = f"{int(freq_hz)}"
                    elif i == 1:  # Threshold or ThreshMode (based on toggle)
                        if _home_enc3_alt:
                            # ThreshMode - show current mode name
                            val_str = THRESH_MODES[THRESH_MODE_INDEX]
                        else:
                            # Threshold - 0-99
                            val_str = f"{int(_display_thresh * 99)}"
                    else:  # Release or ReleaseMode (based on toggle)
                        if _home_enc4_alt:
                            # ReleaseMode - show current mode name
                            val_str = RELEASE_MODES[RELEASE_MODE_INDEX]
                        else:
                            # Release - 0-99 (based on decay_ms: 40-5000ms range)
                            # Convert decay_ms back to 0-99 scale
                            release_pct = int((band.decay_ms - 40.0) / 4960.0 * 99)
                            release_pct = max(0, min(99, release_pct))
                            # Only update display if changed by more than 1
                            if abs(release_pct - _display_release) > 1:
                                _display_release = release_pct
                            
                            # Show effective value for react/rand/both modes, base value otherwise
                            release_mode = RELEASE_MODES[RELEASE_MODE_INDEX]
                            if release_mode in ("react", "rand", "both"):
                                val_str = f"{_effective_release_display}"
                            else:
                                val_str = f"{_display_release}"
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
                        # Show AMBIENT in parentheses when in rnd/amb ambient phase
                        val_str = "(AMBIENT)"
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
                        val_str = "Saved"
                    else:
                        val_str = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
                elif i == 1:  # Input Volume or Detection Mode (based on toggle)
                    if _setup_enc3_detect:
                        val_str = DETECT_MODES[DETECT_MODE_INDEX]
                    else:
                        val_str = str(INPUT_VOLUME)
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
            self._draw_text_kerned(draw, (px, y + 9), val_str, self._font_small, fill=1, kerning=val_kern)

    def render_once(self):
        if not self.enabled or self.device is None:
            return
        
        W, H = self.width, self.height
        image = Image.new("1", (W, H))
        draw = ImageDraw.Draw(image)

        if APP_STATE == "error":
            draw.text((0, 0), "ERROR", font=self._font, fill=1)
            draw.text((0, 14), (APP_ERROR or "See logs")[:20], font=self._font, fill=1)
        else:
            # Top half: Visualization based on detection mode
            if BEAT_DETECT_METHOD == 1:
                # 3BAND mode: show 3-band visualization
                self._draw_3band_vu(draw, 0, 0, W, 32)
            else:
                # FFT mode: show FFT spectrum with Q-band and threshold
                self._draw_fft_spectrum(draw, 0, 0, W, 32)
            
            # Bottom half layout:
            # Row 1: [Page tabs] [Brightness]
            # Row 2: [Enc2,3,4 values - full width]
            
            # Row 1: Page tabs + brightness (inline)
            brightness_width = 25  # sun icon (7) + space (2) + 2-digit number (~16)
            self._draw_page_tabs_wide(draw, 0, 33, W, reserved_right=brightness_width)
            self._draw_brightness_inline(draw, W - brightness_width + 2, 34)
            
            # Row 2: Pot values (full width)
            self._draw_pot_values(draw, 0, 46, W)

        try:
            self.device.display(image)
        except Exception as e:
            # Don't disable on transient SPI errors, just skip this frame
            pass

    def loop(self):
        target_fps = 12  # Reduced from 15 for smoother Pi performance
        frame_time = 1.0 / target_fps
        # #region agent log
        _oled_slow_count = [0]
        # #endregion
        
        while not STOP_THREADS:
            start = time.time()
            update_encoders()
            self.render_once()
            
            elapsed = time.time() - start
            # #region agent log
            if elapsed > 0.12 and _oled_slow_count[0] < 30:  # >120ms is a freeze
                _oled_slow_count[0] += 1
                import json as _json
                with open("/home/benglasser/.cursor/debug.log", "a") as _f:
                    _f.write(_json.dumps({"ts": int(start*1000), "type": "SLOW_OLED", "ms": int(elapsed*1000)}) + "\n")
            # #endregion
            sleep_time = frame_time - elapsed
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
    def hz_to_col(f):
        lf = math.log10(max(left_hz, min(right_hz, f)))
        lmin, lmax = math.log10(left_hz), math.log10(right_hz)
        return int((lf - lmin)/(lmax-lmin) * (width-1))
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

def tui(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(33)
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
        safe_addstr(stdscr, 2, 0, f"Brightness: {bright_status}  (saved: {int(_brightness_saved*100)}%)  GPIO8={_brightness_gpio8_state}{click_indicator}")

        row = 4
        safe_addstr(stdscr, row, 0, "Band Env vs Threshold (| is threshold):")
        draw_threshold_meter(stdscr, row+1, 0, bar_width, live_band_env, band.thresh)
        safe_addstr(stdscr, row+2, 0, f"env={live_band_env:.4f}")

        safe_addstr(stdscr, row+4, 0, "Targeted Frequency Band:")
        draw_band_bar(stdscr, row+5, 0, bar_width, band.center, band.q)

        safe_addstr(stdscr, row+7, 0, "Channels:")
        for i, s in enumerate(states, start=1):
            safe_addstr(stdscr, row+7+i, 1, f"ch{i}: env={s.env:.3f} post={s.post:.3f} {'ON' if s.active else 'off'}")

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

# ===================== Main =====================

def main():
    print(f"[OK] Using input: {DEVICE_INDEX} - {DEVICE_NAME}")
    print(f"[OK] DMX backend: {DMX_BACKEND} (Universe {UNIVERSE}, Channels 1..4)")
    if DEV_NO_HW:
        print("[OK] DEV_NO_HW=1: skipping GPIO, OLED.")
    else:
        print("[OK] Hardware mode: GPIO + OLED enabled.")

    global APP_STATE, IGNORE_KNOBS_UNTIL
    APP_STATE = "loading"
    IGNORE_KNOBS_UNTIL = time.time() + 0.3

    # init GPIO if enabled
    if not DEV_NO_HW:
        setup_gpio_inputs()

    # DMX backend + sender thread
    dmx_backend = make_dmx_backend()
    threading.Thread(target=lambda: dmx_sender_loop(dmx_backend), daemon=True).start()

    # OLED UI (SPI) with FFT display
    oled_ui = OledUI(width=128, height=64, fps=15)
    if getattr(oled_ui, "enabled", False):
        threading.Thread(target=oled_ui.loop, daemon=True).start()
        print("[OK] OLED UI: 128x64 SPI with FFT display")
    else:
        print("[INFO] OLED UI not available (skipping).")

    # Encoder reader thread (GPIO)
    if not DEV_NO_HW:
        threading.Thread(target=encoder_reader, daemon=True).start()
        print("[OK] Encoders: 5 rotary encoders (E5 SW disabled - SPI conflict)")
        print("     E1(5,6,13) E2(17,27,22) E3(19,26,23) E4(16,20,21) E5(4,18,-)")
        print("     Reset(25): short=reset, long(>1s)=brightness toggle")

    # Audio thread
    threading.Thread(target=audio_loop, daemon=True).start()

    # TUI thread (enabled by default if running in a TTY)
    use_tui = sys.stdout.isatty() and os.environ.get("ENABLE_TUI", "1") != "0"
    if use_tui:
        threading.Thread(target=lambda: curses.wrapper(tui), daemon=True).start()
    else:
        print("[INFO] No TTY detected (or ENABLE_TUI=0). Running headless.")

    # Keep main thread alive
    try:
        while not STOP_THREADS:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        _set_stop(True)
        _set_run(False)
        time.sleep(0.1)

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
