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

DMX_BACKEND = os.environ.get("DMX_BACKEND", "uart").strip().lower()
DMX_UART_DEVICE = os.environ.get("DMX_UART_DEVICE", "/dev/serial0")
DMX_UART_BAUD = 250000  # DMX: 250k 8N2

UNIVERSE   = 0
DMX_CHANS  = 4

# Startup defaults (used by "LOW" mode)
DEFAULT_CENTER_HZ = 120.0   # 120 Hz
DEFAULT_Q         = 4.24    # 60 on 0-99 scale ((10-4.24)/9.5*99 = 60)
DEFAULT_THRESH    = 0.61    # 60 on 0-99 scale (0.61 * 99 = 60.39 → 60)
DEFAULT_ATTACK_MS = 10.0
DEFAULT_DECAY_MS  = 542.0   # 10 on 0-99 scale ((542-40)/4960*99 = 10.02 → 10)
DEFAULT_BRIGHT    = 0.5

# Defaults modes: LOW, MID, HIGH target different frequency ranges
# Each mode has (center_hz, thresh, decay_ms, q) - Q varies by range
DEFAULTS_MODES = ["LOW", "MID", "HIGH", "USR1", "USR2", "USR3"]
DEFAULTS_PRESETS = {
    #           (center_hz, thresh, decay_ms, q_factor)
    "LOW":  (120.0,  0.61, 542.0, 2.0),    # Low frequencies ~120Hz, thresh=60
    "MID":  (1000.0, 0.41, 542.0, 1.5),    # Mid frequencies ~1kHz, thresh=40
    "HIGH": (5000.0, 0.25, 542.0, 1.0),    # High frequencies ~5kHz, thresh=25
    "USR1": (120.0,  0.61, 542.0, 2.0),    # User preset 1 (not active yet)
    "USR2": (120.0,  0.61, 542.0, 2.0),    # User preset 2 (not active yet)
    "USR3": (120.0,  0.61, 542.0, 2.0),    # User preset 3 (not active yet)
}

# Config file for persisting settings
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dmx_config")

def load_defaults_mode():
    """Load the defaults mode from config file."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                for line in f:
                    if line.startswith("defaults_mode="):
                        mode_name = line.strip().split("=")[1]
                        if mode_name in DEFAULTS_MODES:
                            return DEFAULTS_MODES.index(mode_name)
    except Exception:
        pass
    return 0  # Default to LOW

def save_defaults_mode(idx):
    """Save the defaults mode to config file."""
    try:
        mode_name = DEFAULTS_MODES[idx]
        with open(CONFIG_FILE, 'w') as f:
            f.write(f"defaults_mode={mode_name}\n")
    except Exception:
        pass

DEFAULTS_MODE_INDEX = load_defaults_mode()  # Load from config or default to LOW

# Q factor range: 0.5 (very wide) to 8.0 (very narrow)
# Display: 0 = narrow (Q=8), 99 = wide (Q=0.5)
Q_MIN = 0.5   # Widest (display 99)
Q_MAX = 8.0   # Narrowest (display 0)

THRESH_MIN = 0.001
THRESH_MAX = 1.0
MIN_CENTER_HZ = 80.0
MAX_CENTER_HZ = 12000.0

APP_STATE = "boot"   # "boot" | "loading" | "ready" | "error"
APP_ERROR = ""

# Audio
SR  = 44100
HOP = 512  # Smaller for more responsive FFT

# Detection / logic
ENV_EMA       = 0.55
AGC_ON        = True
AGC_TARGET    = 0.020
REFRACTORY_MS = 110.0
WEIGHTING_ON  = False
INPUT_GAIN    = 1.0
BRIGHTNESS    = DEFAULT_BRIGHT

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
ENC5_CLK = 4
ENC5_DT  = 18
ENC5_SW  = 8

# Reset button (separate from encoder buttons)
RESET_PIN = 25

# SPI OLED pins (Waveshare 2.42" SSD1309 on CE1)
OLED_SPI_DEV = 1   # CE1 (GPIO 7)
OLED_RST_PIN = 12
OLED_DC_PIN  = 24
OLED_WIDTH   = 128
OLED_HEIGHT  = 64

# ===================== Page System =====================

PAGES = ["HOME", "ADV", "PRE", "COLOR", "SET"]
PAGE_POT_LABELS = {
    "HOME": ["Freq", "Thresh", "Rels"],      # Freq center, Threshold, Release
    "ADV": ["Q", "ThPre", "RelMd"],           # Q, Threshold Preset, Release Mode
    "PRE": ["Preset", "Beats", "Mode"],       # Preset selection, Beat cycles, Cycle mode
    "COLOR": ["Light", "HSV", "Sat"],         # Light select, HSV, Saturation
    "SET": ["Dflt", "--", "--"],              # Defaults mode, TBD, TBD
}

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
PROGRAM_NAMES = ["ALL", "1+2/3+4", "1+3/2+4", "CHASE", "RANDOM", "AMBIENT"]

# ===================== FFT Display =====================

# FFT settings - 32 bands, 100Hz to 10kHz
FFT_MIN_FREQ = 100
FFT_MAX_FREQ = 10000
FFT_NUM_BANDS = 32

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

# FFT state
fft_bands = [0.0] * len(FFT_BANDS)
fft_peaks = [0.0] * len(FFT_BANDS)
fft_peak_times = [0.0] * len(FFT_BANDS)
PEAK_HOLD_TIME = 0.4
fft_recent_max = 0.3
fft_max_decay = 0.995

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

# Brightness fade toggle state
_brightness_saved = DEFAULT_BRIGHT  # Saved brightness before fade-out
_brightness_fading = False  # True while fading
_brightness_target = DEFAULT_BRIGHT  # Target for fade animation
_brightness_off = False  # True when faded to zero
BRIGHTNESS_FADE_SPEED = 0.05  # How fast to fade (per frame)

# Display-specific smoothed values (separate from control values)
_display_freq = DEFAULT_CENTER_HZ
_display_thresh = DEFAULT_THRESH
_display_q = DEFAULT_Q
_display_bright = DEFAULT_BRIGHT
_display_release = int((DEFAULT_DECAY_MS - 40.0) / 4960.0 * 99)  # Release display value
_display_q_pct = round((Q_MAX - DEFAULT_Q) / (Q_MAX - Q_MIN) * 99)  # Q display value (0-99)

# DMX throttling
DMX_RATE_HZ       = 25.0
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

# Beat cycle options: 0 = off, then 4, 8, 16, 32, 64, 128, 256, 512
CYCLE_STEPS_OPTIONS = [0, 4, 8, 16, 32, 64, 128, 256, 512]
CYCLE_STEPS         = 0
CYCLE_TRIGGER_COUNT = 0
CYCLE_PHASE         = 0
CYCLE_STEPS_INDEX   = 0  # Index into CYCLE_STEPS_OPTIONS (0 = off)

# Cycles between modes
CYCLES_BETWEEN_MODES = ["x+1"]  # For now just one mode
CYCLES_BETWEEN_INDEX = 0

# Number of presets (can be expanded later)
NUM_PRESETS = 6

def program_pair_for_base(base: int):
    """Get the pair of programs for cycling: current and next (wrapping)."""
    # base is 1-indexed (1 to NUM_PRESETS)
    # Returns (current, next) where next wraps around
    next_prog = (base % NUM_PRESETS) + 1  # 1->2, 2->3, 3->4, 4->1
    return (base, next_prog)

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

        self._buf = bytearray(1 + DMX_CHANS)
        self._buf[0] = 0x00

    def _send_break(self):
        try:
            self.serial.baudrate = 9600
            self.serial.write(b"\x00")
            self.serial.flush()
            time.sleep(0.001)
        finally:
            self.serial.baudrate = DMX_UART_BAUD

    def send(self, vals):
        for i in range(DMX_CHANS):
            self._buf[1 + i] = max(0, min(255, int(vals[i])))
        self._send_break()
        self.serial.write(self._buf)

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
        for i in range(DMX_CHANS):
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
                    dmx_backend.send(vals)
                except Exception as e:
                    APP_STATE = "error"
                    APP_ERROR = f"DMX send failed: {e}"
            time.sleep(0.002)
    finally:
        try:
            dmx_backend.send([0] * DMX_CHANS)
            time.sleep(0.05)
        except Exception:
            pass
        try:
            dmx_backend.close()
        except Exception:
            pass

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
        # Initialize from the loaded defaults mode (persisted from last session)
        mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
        center_hz, thresh, decay_ms, q_factor = DEFAULTS_PRESETS[mode_name]
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

def update_lights(dt_ms):
    a = max(1e-3, _runtime['attack_ms'])
    d = max(1e-3, _runtime['decay_ms'])
    vals = []
    for s in states:
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
        vals.append(int(255 * s.post * BRIGHTNESS))
    return vals

def update_ambient_mode(dt_ms):
    """Update ambient mode - non-audio-reactive random fading."""
    global ambient_targets, ambient_current, ambient_last_change
    global ambient_speed, ambient_fade_time
    
    now = time.time()
    dt_sec = dt_ms / 1000.0
    
    # Get speed and fade time from pots (when in ambient mode)
    # Speed: 0.2x to 5x (controlled by what would be Freq pot)
    # Fade time: 0.5s to 10s (controlled by what would be Release pot)
    
    for i in range(4):
        # Check if it's time to pick a new target for this channel
        time_since_change = now - ambient_last_change[i]
        # Random interval based on speed: faster speed = shorter intervals
        interval = (2.0 + random.random() * 3.0) / ambient_speed
        
        if time_since_change > interval:
            # Pick a new random target (0.0 to 1.0)
            ambient_targets[i] = random.random()
            ambient_last_change[i] = now
        
        # Smoothly fade current toward target
        diff = ambient_targets[i] - ambient_current[i]
        fade_rate = dt_sec / max(0.1, ambient_fade_time)
        if abs(diff) < fade_rate:
            ambient_current[i] = ambient_targets[i]
        else:
            ambient_current[i] += fade_rate if diff > 0 else -fade_rate
        
        # Apply to state (bypass normal trigger system)
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
        self.q      = max(0.3,           min(12.0,          float(q)))
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
    """Apply encoder deltas based on current page and handle brightness."""
    global BRIGHTNESS, BASE_PROGRAM, CYCLES_BETWEEN_INDEX
    global ambient_speed, ambient_fade_time, DEFAULTS_MODE_INDEX
    global _enc_delta, _brightness_target, _brightness_fading
    
    if DEV_NO_HW:
        return
    if time.time() < IGNORE_KNOBS_UNTIL:
        return
    
    # Get encoder deltas and reset them (indices 1-3 are param encoders, 4 is brightness)
    deltas = _enc_delta[1:4]  # Param A, B, C deltas
    brightness_delta = _enc_delta[4]
    _enc_delta = [0, 0, 0, 0, 0]
    
    # Handle brightness encoder (Encoder 5)
    if brightness_delta != 0 and not _brightness_off:
        # Each click changes brightness by 2%
        _brightness_target = max(0.0, min(1.0, _brightness_target + brightness_delta * 0.02))
        _brightness_fading = True
    
    # Animate brightness fade
    if _brightness_fading:
        diff = _brightness_target - BRIGHTNESS
        if abs(diff) < BRIGHTNESS_FADE_SPEED:
            BRIGHTNESS = _brightness_target
            _brightness_fading = False
        else:
            BRIGHTNESS += BRIGHTNESS_FADE_SPEED if diff > 0 else -BRIGHTNESS_FADE_SPEED
    
    # Update parameters based on current page using encoder deltas
    page_name = PAGES[current_page]
    
    if page_name == "HOME":
        # Check if we're in AMBIENT mode (preset 6) - different encoder behavior
        if BASE_PROGRAM == 6:
            # AMBIENT mode: Enc A = Speed, Enc B = nothing, Enc C = Fade time
            if deltas[0] != 0:
                # Speed: 0.2x to 5x, step by 0.1x per click
                ambient_speed = max(0.2, min(5.0, ambient_speed + deltas[0] * 0.1))
            if deltas[2] != 0:
                # Fade time: 0.5s to 10s, step by 0.2s per click
                ambient_fade_time = max(0.5, min(10.0, ambient_fade_time + deltas[2] * 0.2))
        else:
            # Normal audio-reactive mode
            # Enc A: Center frequency (log scale) - multiply/divide by factor per click
            if deltas[0] != 0:
                # Each click changes freq by ~5%
                factor = 1.05 ** deltas[0]
                new_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center * factor))
                print(f"[FREQ] delta={deltas[0]} factor={factor:.3f} {band.center:.0f}Hz -> {new_center:.0f}Hz (Q={band.q:.2f} unchanged)")
                band.center = new_center
            # Enc B: Threshold (0-1, display 0-99)
            if deltas[1] != 0:
                band.thresh = max(0.0, min(1.0, band.thresh + deltas[1] * 0.01))
            # Enc C: Release/Decay (40-5000ms)
            if deltas[2] != 0:
                # Step by ~50ms per click
                band.decay_ms = max(40.0, min(5000.0, band.decay_ms + deltas[2] * 50.0))
                
    elif page_name == "ADV":
        # Enc A: Q factor (0.5 to 8.0, display 0-99 inverted)
        if deltas[0] != 0:
            # Each click changes Q by 0.1 (inverted: CW = lower Q = wider)
            band.q = max(Q_MIN, min(Q_MAX, band.q - deltas[0] * 0.1))
        # Enc C: Decay (40-5000ms)
        if deltas[2] != 0:
            band.decay_ms = max(40.0, min(5000.0, band.decay_ms + deltas[2] * 50.0))
            
    elif page_name == "PRE":
        # Enc A: Preset selection (1-6)
        if deltas[0] != 0:
            new_preset = max(1, min(NUM_PRESETS, BASE_PROGRAM + deltas[0]))
            if new_preset != BASE_PROGRAM:
                BASE_PROGRAM = new_preset
                ui_flash(f"Preset: {PROGRAM_NAMES[new_preset-1]}", 0.8)
        # Enc B: Beat Cycles index (0-8)
        if deltas[1] != 0:
            new_idx = max(0, min(len(CYCLE_STEPS_OPTIONS) - 1, CYCLE_STEPS_INDEX + deltas[1]))
            set_cycle_steps_by_index(new_idx)
        # Enc C: Cycle mode (0 for now, only x+1)
        if deltas[2] != 0:
            new_idx = max(0, min(len(CYCLES_BETWEEN_MODES) - 1, CYCLES_BETWEEN_INDEX + deltas[2]))
            CYCLES_BETWEEN_INDEX = new_idx
            
    elif page_name == "SET":
        # Enc A: Defaults mode (0-2 for LOW/MID/HIGH)
        if deltas[0] != 0:
            new_idx = max(0, min(2, DEFAULTS_MODE_INDEX + deltas[0]))
            if new_idx != DEFAULTS_MODE_INDEX:
                DEFAULTS_MODE_INDEX = new_idx
                # Apply the new defaults immediately
                mode_name = DEFAULTS_MODES[new_idx]
                center_hz, thresh, decay_ms, q_factor = DEFAULTS_PRESETS[mode_name]
                band.center   = center_hz
                band.thresh   = thresh
                band.decay_ms = decay_ms
                band.q        = q_factor
                # Save to config file for persistence
                save_defaults_mode(new_idx)
                ui_flash(f"Defaults: {mode_name}", 0.8)

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
    """Reset HOME page parameters (Freq, Threshold, Release, Q) to current defaults mode."""
    global IGNORE_KNOBS_UNTIL
    
    # Get values from current defaults mode
    mode_name = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
    center_hz, thresh, decay_ms, q_factor = DEFAULTS_PRESETS[mode_name]
    
    # Reset HOME page parameters to the current defaults mode
    band.center   = center_hz
    band.thresh   = thresh
    band.decay_ms = decay_ms
    band.q        = q_factor
    
    IGNORE_KNOBS_UNTIL = time.time() + 0.3
    ui_flash(f"Reset to {mode_name}", 1.0)

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
    GPIO.setup(ENC5_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Reset button
    GPIO.setup(RESET_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def _read_encoder_quadrature(enc_idx, clk_pin, dt_pin):
    """Read encoder using quadrature state machine for reliable direction detection.
    Returns direction: 1 for CW, -1 for CCW, 0 for no change or invalid transition.
    Most encoders have 4 state changes per detent (click), so we accumulate and
    return direction only when a full detent is completed."""
    global _enc_state, _enc_count
    
    clk = GPIO.input(clk_pin)
    dt = GPIO.input(dt_pin)
    
    # Encode current state as 2-bit value
    new_state = (clk << 1) | dt
    old_state = _enc_state[enc_idx]
    
    if new_state == old_state:
        return 0  # No change
    
    _enc_state[enc_idx] = new_state
    
    # Quadrature state machine:
    # Valid CW sequence:  0 -> 1 -> 3 -> 2 -> 0 (binary: 00->01->11->10->00)
    # Valid CCW sequence: 0 -> 2 -> 3 -> 1 -> 0 (binary: 00->10->11->01->00)
    
    # Transition table: [old_state][new_state] -> direction (1=CW, -1=CCW, 0=invalid)
    # This handles all valid transitions in the quadrature sequence
    transition = [
        # new: 0   1   2   3
        [  0,  1, -1,  0],  # old = 0
        [ -1,  0,  0,  1],  # old = 1
        [  1,  0,  0, -1],  # old = 2
        [  0, -1,  1,  0],  # old = 3
    ]
    
    direction = transition[old_state][new_state]
    
    if direction != 0:
        _enc_count[enc_idx] += direction
        
        # Most encoders have 4 state changes per detent
        # Return direction only when we've completed a full detent
        # Note: Direction is inverted so CW = positive (increase values)
        if _enc_count[enc_idx] >= 4:
            _enc_count[enc_idx] = 0
            return -1  # CW click (inverted)
        elif _enc_count[enc_idx] <= -4:
            _enc_count[enc_idx] = 0
            return 1  # CCW click (inverted)
    
    return 0  # Not a full detent yet


# Page encoder uses a simpler "detent detection" approach
# Triggers once per click when returning to rest position (both signals high)
_page_enc_last_clk = 1
_page_enc_direction = 0  # Stores detected direction until rest position

def _read_page_encoder(clk_pin, dt_pin):
    """Read page encoder with detent-based detection.
    Returns direction only when encoder reaches rest position (click completed).
    This ensures exactly one page change per physical click."""
    global _page_enc_last_clk, _page_enc_direction
    
    clk = GPIO.input(clk_pin)
    dt = GPIO.input(dt_pin)
    
    # Detect falling edge of CLK to determine direction
    if clk == 0 and _page_enc_last_clk == 1:
        # CLK just went low - check DT to determine direction
        # DT high = CCW (left), DT low = CW (right)
        _page_enc_direction = 1 if dt == 1 else -1
    
    _page_enc_last_clk = clk
    
    # Only return direction when both signals are high (rest/detent position)
    # This is the "click" position where the encoder settles
    if clk == 1 and dt == 1 and _page_enc_direction != 0:
        direction = _page_enc_direction
        _page_enc_direction = 0  # Reset for next click
        return direction
    
    return 0


def encoder_reader():
    """Read all 5 rotary encoders for page selection, parameters, and brightness."""
    global encoder1_value, encoder1_button
    global current_page
    global _enc_last_clk, _enc_last_dt, _enc_last_sw, _enc_delta, _reset_last_state
    global _enc_state, _enc_count
    
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
        clk = GPIO.input(clk_pin)
        dt = GPIO.input(dt_pin)
        _enc_state[i] = (clk << 1) | dt
        _enc_count[i] = 0
    
    # Initialize all switch states
    _enc_last_sw[0] = GPIO.input(ENC1_SW)
    _enc_last_sw[1] = GPIO.input(ENC2_SW)
    _enc_last_sw[2] = GPIO.input(ENC3_SW)
    _enc_last_sw[3] = GPIO.input(ENC4_SW)
    _enc_last_sw[4] = GPIO.input(ENC5_SW)
    
    _reset_last_state = GPIO.input(RESET_PIN)
    
    try:
        while not STOP_THREADS:
            try:
                # ===== Encoder 1 - Page selection (uses detent-based detection) =====
                direction = _read_page_encoder(ENC1_CLK, ENC1_DT)
                if direction != 0:
                    encoder1_value += direction
                    encoder1_value = max(0, min(len(PAGES) - 1, encoder1_value))
                    if current_page != encoder1_value:
                        current_page = encoder1_value
                
                enc1_sw = GPIO.input(ENC1_SW)
                if enc1_sw == 0 and _enc_last_sw[0] == 1:
                    time.sleep(0.02)  # Debounce
                    if GPIO.input(ENC1_SW) == 0:
                        reset_to_defaults()
                _enc_last_sw[0] = enc1_sw
                
                # ===== Encoder 2 - Param A (Freq/Speed/Preset) =====
                direction = _read_encoder_quadrature(1, ENC2_CLK, ENC2_DT)
                if direction != 0:
                    _enc_delta[1] += direction
                
                enc2_sw = GPIO.input(ENC2_SW)
                if enc2_sw == 0 and _enc_last_sw[1] == 1:
                    time.sleep(0.02)
                    if GPIO.input(ENC2_SW) == 0:
                        pass  # Reserved for future use
                _enc_last_sw[1] = enc2_sw
                
                # ===== Encoder 3 - Param B (Thresh/Beats) =====
                direction = _read_encoder_quadrature(2, ENC3_CLK, ENC3_DT)
                if direction != 0:
                    _enc_delta[2] += direction
                
                enc3_sw = GPIO.input(ENC3_SW)
                if enc3_sw == 0 and _enc_last_sw[2] == 1:
                    time.sleep(0.02)
                    if GPIO.input(ENC3_SW) == 0:
                        pass  # Reserved for future use
                _enc_last_sw[2] = enc3_sw
                
                # ===== Encoder 4 - Param C (Release/Mode) =====
                direction = _read_encoder_quadrature(3, ENC4_CLK, ENC4_DT)
                if direction != 0:
                    _enc_delta[3] += direction
                
                enc4_sw = GPIO.input(ENC4_SW)
                if enc4_sw == 0 and _enc_last_sw[3] == 1:
                    time.sleep(0.02)
                    if GPIO.input(ENC4_SW) == 0:
                        pass  # Reserved for future use
                _enc_last_sw[3] = enc4_sw
                
                # ===== Encoder 5 - Brightness =====
                direction = _read_encoder_quadrature(4, ENC5_CLK, ENC5_DT)
                if direction != 0:
                    _enc_delta[4] += direction
                
                enc5_sw = GPIO.input(ENC5_SW)
                if enc5_sw == 0 and _enc_last_sw[4] == 1:
                    time.sleep(0.02)
                    if GPIO.input(ENC5_SW) == 0:
                        toggle_brightness()
                _enc_last_sw[4] = enc5_sw
                
                # ===== Reset button (GPIO 25) =====
                reset_btn = GPIO.input(RESET_PIN)
                if reset_btn == 0 and _reset_last_state == 1:
                    time.sleep(0.02)
                    if GPIO.input(RESET_PIN) == 0:
                        reset_to_defaults()
                _reset_last_state = reset_btn
                
            except RuntimeError:
                break

            time.sleep(0.001)  # Faster polling for better quadrature tracking
    finally:
        pass

# ===================== Audio loop =====================

live_band_env   = 0.0
live_threshold  = band.thresh
input_rms       = 0.0
last_trigger_ts = 0.0
chase_idx       = 0
group34_phase   = 0

# Ambient mode state
ambient_targets = [0.0, 0.0, 0.0, 0.0]  # Target brightness for each channel
ambient_current = [0.0, 0.0, 0.0, 0.0]  # Current brightness for each channel
ambient_last_change = [0.0, 0.0, 0.0, 0.0]  # Time of last target change
ambient_speed = 1.0  # Speed multiplier (controlled by Freq pot in ambient mode)
ambient_fade_time = 2.0  # Fade in/out time in seconds (controlled by Release pot)

# Trigger indicator for UI
trigger_flash = 0.0  # Decays over time, >0 means recently triggered
TRIGGER_FLASH_DECAY = 0.85

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
    global last_trigger_ts, chase_idx, group34_phase
    global PROGRAM, BASE_PROGRAM, CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE
    global APP_STATE, APP_ERROR
    global fft_bands, fft_peaks, fft_peak_times, fft_recent_max

    bp   = BiquadBandpass(SR, band.center, band.q)
    envd = EnvDetector(SR, attack_ms=8.0, release_ms=80.0)

    band.attack_ms = DEFAULT_ATTACK_MS
    frame_dt_ms = (HOP / SR) * 1000.0
    was_above = False

    def cb(indata, frames, time_info, status):
        nonlocal was_above
        global live_band_env, live_threshold, input_rms
        global last_trigger_ts, chase_idx, group34_phase
        global PROGRAM, BASE_PROGRAM, CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE
        global fft_bands, fft_peaks, fft_peak_times, fft_recent_max

        if not RUNNING:
            return

        x = indata[:, 0].astype(np.float32)
        input_rms = float(np.sqrt(np.mean(x*x)) + 1e-12)

        # FFT analysis for display
        window = np.hanning(len(x))
        fft = np.fft.rfft(x * window)
        fft_mag = np.abs(fft) / len(x)
        freqs = np.fft.rfftfreq(len(x), 1.0 / SR)
        
        now = time.time()
        
        # Calculate FFT band energies with compensation
        raw_levels = []
        for i, (low, high) in enumerate(FFT_BANDS):
            energy = get_band_energy(fft_mag, freqs, low, high)
            energy *= FFT_COMPENSATION[i]
            if energy > 1e-10:
                db = 20 * math.log10(energy + 1e-10)
                normalized = max(0, (db + 60) / 50)
            else:
                normalized = 0
            raw_levels.append(normalized)
        
        # Auto-normalize FFT
        current_max = max(raw_levels) if raw_levels else 0
        if current_max > fft_recent_max:
            fft_recent_max = current_max
        else:
            fft_recent_max = fft_recent_max * fft_max_decay
        
        norm_factor = max(0.1, fft_recent_max)
        
        for i, raw in enumerate(raw_levels):
            normalized = min(1.0, raw / norm_factor)
            if normalized > fft_bands[i]:
                fft_bands[i] = 0.4 * normalized + 0.6 * fft_bands[i]
            else:
                fft_bands[i] = 0.95 * fft_bands[i]
            
            if fft_bands[i] > fft_peaks[i]:
                fft_peaks[i] = fft_bands[i]
                fft_peak_times[i] = now
            elif now - fft_peak_times[i] > PEAK_HOLD_TIME:
                fft_peaks[i] = max(fft_peaks[i] * 0.98, fft_bands[i])

        # Use FFT bands within Q range for trigger detection
        # This makes the trigger match what you see on the display
        clamped_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center))
        bandwidth = clamped_center / max(0.1, band.q)
        low_freq = max(FFT_MIN_FREQ, clamped_center - bandwidth / 2)
        high_freq = min(FFT_MAX_FREQ, clamped_center + bandwidth / 2)
        
        # Find max level of FFT bands within Q range
        q_band_max = 0.0
        for i, (band_low, band_high) in enumerate(FFT_BANDS):
            band_center_freq = math.sqrt(band_low * band_high)
            if low_freq <= band_center_freq <= high_freq:
                q_band_max = max(q_band_max, fft_bands[i])
        
        # Smooth the trigger envelope
        v, a = live_band_env, ENV_EMA
        v = a * v + (1.0 - a) * q_band_max
        live_band_env = v
        live_threshold = band.thresh

        above = (live_band_env >= band.thresh)
        can_fire = ((now - last_trigger_ts)*1000.0 >= REFRACTORY_MS)

        if CYCLE_STEPS > 0:
            p_base, p_neighbor = program_pair_for_base(BASE_PROGRAM)
            active_prog = p_base if CYCLE_PHASE == 0 else p_neighbor
        else:
            active_prog = BASE_PROGRAM

        PROGRAM = active_prog

        # Decay trigger flash indicator
        global trigger_flash
        trigger_flash = trigger_flash * TRIGGER_FLASH_DECAY

        if above and not was_above and can_fire and active_prog in (1, 2, 3, 4):
            last_trigger_ts = now
            trigger_flash = 1.0  # Flash on trigger
            if TRIG_DEBUG:
                print(f"[TRIG] env={live_band_env:.5f} thr={band.thresh:.5f} prog={active_prog}")

            if active_prog == 1:
                trigger_idxs([0, 1, 2, 3], band.attack_ms, band.decay_ms)
            elif active_prog == 2:
                trigger_idxs([chase_idx], band.attack_ms, band.decay_ms)
                chase_idx = (chase_idx + 1) % 4
            elif active_prog == 3:
                if group34_phase == 0:
                    trigger_idxs([0, 3], band.attack_ms, band.decay_ms)
                    group34_phase = 1
                else:
                    trigger_idxs([1, 2], band.attack_ms, band.decay_ms)
                    group34_phase = 0
            elif active_prog == 4:
                if group34_phase == 0:
                    trigger_idxs([0, 1], band.attack_ms, band.decay_ms)
                    group34_phase = 1
                else:
                    trigger_idxs([2, 3], band.attack_ms, band.decay_ms)
                    group34_phase = 0
            elif active_prog == 5:
                # RANDOM - trigger a random channel each time
                random_idx = random.randint(0, 3)
                trigger_idxs([random_idx], band.attack_ms, band.decay_ms)
            # Note: active_prog == 6 (AMBIENT) doesn't trigger here - it's handled separately

            if CYCLE_STEPS > 0:
                CYCLE_TRIGGER_COUNT += 1
                if CYCLE_TRIGGER_COUNT >= CYCLE_STEPS:
                    CYCLE_TRIGGER_COUNT = 0
                    CYCLE_PHASE = 1 - CYCLE_PHASE

        # Handle AMBIENT mode separately (non-audio-reactive)
        if active_prog == 6:
            update_ambient_mode(frame_dt_ms)
        
        send_dmx(update_lights(frame_dt_ms))
        was_above = above

    try:
        with sd.InputStream(device=DEVICE_INDEX, channels=1, samplerate=SR, blocksize=HOP, callback=cb):
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
                bus_speed_hz=1000000,  # 1MHz for stability (reduced from 2MHz)
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
        - Bars inside Q range above threshold: crosshatch/dashed (triggering zone)
        - Bars inside Q range below threshold: solid fill
        - Bars outside Q range: single-pixel outline
        - Q range boundaries: vertical lines
        - Threshold line: horizontal within Q range"""
        num_bands = len(fft_bands)
        
        # Calculate Q bandwidth for highlighting
        clamped_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, band.center))
        bandwidth = clamped_center / max(0.1, band.q)
        low_freq = max(FFT_MIN_FREQ, clamped_center - bandwidth / 2)
        high_freq = min(FFT_MAX_FREQ, clamped_center + bandwidth / 2)
        
        low_x = self._freq_to_x(low_freq, x, width)
        high_x = self._freq_to_x(high_freq, x, width)
        thresh_y = y + height - int(band.thresh * height)
        
        # Calculate bar positions with 1px gap
        total_gaps = num_bands - 1
        total_bar_width = width - total_gaps
        bar_width = max(1, total_bar_width // num_bands)
        
        for i, level in enumerate(fft_bands):
            bx_start = x + i * (bar_width + 1)
            bx_end = bx_start + bar_width - 1
            
            if bx_end >= x + width:
                bx_end = x + width - 1
            if bx_start >= x + width:
                continue
            
            bar_h = int(level * height)
            if bar_h <= 0:
                continue
            
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
        # Program number (1-4)
        draw.text((x, y), f"P{BASE_PROGRAM}", font=self._font_small, fill=1)
        
        # Sun icon + brightness percentage (use smoothed display value)
        self._draw_sun_icon(draw, x, y + 10, size=7)
        brt_pct = int(_display_bright * 100)
        draw.text((x + 9, y + 11), f"{brt_pct:2d}", font=self._font_small, fill=1)
    
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
        
        for i, page in enumerate(PAGES):
            ix = x + i * (icon_box_size + spacing)
            self._draw_page_icon(draw, ix, y, page, i == current_page)
    
    def _draw_page_tabs_wide(self, draw, x, y, total_width):
        """Draw page tabs as icons, expanded to fill total_width."""
        num_pages = len(PAGES)
        # Calculate tab width to fill the space evenly
        tab_width = total_width // num_pages
        icon_size = 9  # The actual icon is 9x9
        box_height = 11
        
        for i, page in enumerate(PAGES):
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
        global _display_freq, _display_thresh, _display_q, _display_bright
        
        page_name = PAGES[current_page]
        labels = PAGE_POT_LABELS[page_name]
        
        # Override labels for HOME page when in AMBIENT mode
        if page_name == "HOME" and BASE_PROGRAM == 6:
            labels = ["Speed", "--", "Fade"]
        
        # Use actual values directly - pot smoothing handles stability
        _display_freq = band.center
        _display_thresh = band.thresh
        _display_q = band.q
        _display_bright = BRIGHTNESS
        
        # Draw labels and values with even spacing
        # Each pot gets width/3 space, but we add padding between them
        spacing = 2  # Extra pixels between columns
        col_width = (width - spacing * 2) // 3
        
        for i in range(3):
            px = x + i * (col_width + spacing)
            draw.text((px, y), f"{labels[i]}", font=self._font_small, fill=1)
            
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
                else:
                    # Normal audio-reactive mode
                    if i == 0:  # Frequency - show in Hz (with tenths for kHz)
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
                    elif i == 1:  # Threshold - 0-99
                        val_str = f"{int(_display_thresh * 99)}"
                    else:  # Release - 0-99 (based on decay_ms: 40-5000ms range)
                        global _display_release
                        # Convert decay_ms back to 0-99 scale
                        release_pct = int((band.decay_ms - 40.0) / 4960.0 * 99)
                        release_pct = max(0, min(99, release_pct))
                        # Only update display if changed by more than 1
                        if abs(release_pct - _display_release) > 1:
                            _display_release = release_pct
                        val_str = f"{_display_release}"
            elif page_name == "ADV":
                if i == 0:  # Q factor - display as 0-99 (inverted: higher = wider range)
                    global _display_q_pct
                    # Q ranges from Q_MAX (narrow) to Q_MIN (wide)
                    # Convert to 0-99 where 99 = widest (Q_MIN), 0 = narrowest (Q_MAX)
                    q_pct = round((Q_MAX - _display_q) / (Q_MAX - Q_MIN) * 99)
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
                    val_str = PROGRAM_NAMES[BASE_PROGRAM - 1]
                elif i == 1:  # Beat Cycles - show actual value (0=OFF, 4, 8, 16, etc)
                    steps = CYCLE_STEPS_OPTIONS[CYCLE_STEPS_INDEX]
                    val_str = "OFF" if steps == 0 else f"{steps}"
                else:  # Mode - Cycles Between mode
                    val_str = CYCLES_BETWEEN_MODES[CYCLES_BETWEEN_INDEX]
            elif page_name == "SET":
                if i == 0:  # Defaults mode
                    val_str = DEFAULTS_MODES[DEFAULTS_MODE_INDEX]
                else:  # TBD
                    val_str = "--"
            elif page_name == "COLOR":
                # COLOR page - not yet implemented
                val_str = "--"
            else:
                # Fallback for any unhandled pages
                val_str = "--"
            
            draw.text((px, y + 9), val_str, font=self._font_small, fill=1)

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
            # FFT Spectrum (top half, full width)
            self._draw_fft_spectrum(draw, 0, 0, W, 32)
            
            # Bottom half layout - two columns:
            # Column 1 (wide): [Page tabs] + [Pot values below]
            # Column 2 (narrow): [Preset #] + [Brightness]
            
            # Column 2 width - just enough for "P1" and brightness
            global_width = 22
            separator_x = W - global_width - 2
            
            # Column 1: Page tabs and pot values (takes remaining space)
            tabs_width = separator_x - 1
            self._draw_page_tabs_wide(draw, 0, 33, tabs_width)
            
            # Pot values below tabs
            self._draw_pot_values(draw, 0, 46, tabs_width)
            
            # Vertical separator between columns
            draw.line((separator_x, 33, separator_x, 63), fill=1)
            
            # Column 2: Global controls (Preset + Brightness)
            global_x = separator_x + 2
            self._draw_global_controls(draw, global_x, 34)

        try:
            self.device.display(image)
        except Exception as e:
            # Don't disable on transient SPI errors, just skip this frame
            pass

    def loop(self):
        target_fps = 15
        frame_time = 1.0 / target_fps
        
        while not STOP_THREADS:
            start = time.time()
            update_encoders()
            self.render_once()
            
            elapsed = time.time() - start
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
            f"Page={PAGES[current_page]}  Preset={BASE_PROGRAM}  P{PROGRAM}  RUN={'ON' if RUNNING else 'PAUSE'}  "
            f"Device={DEVICE_NAME}  DMX={DMX_BACKEND}"
        )
        safe_addstr(stdscr, 1, 0,
            f"Center={band.center:.0f}Hz  Q={band.q:.1f}  Thresh={band.thresh:.2f}  "
            f"Decay={band.decay_ms:.0f}ms  Bright={BRIGHTNESS:.0%}"
        )
        bright_status = "OFF" if _brightness_off else f"{int(BRIGHTNESS*100)}%"
        safe_addstr(stdscr, 2, 0, f"Brightness: {bright_status}  (saved: {int(_brightness_saved*100)}%)")

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
        print("[OK] Encoders: 5 rotary encoders with switches")
        print("     E1(5,6,13) E2(17,27,22) E3(19,26,23) E4(16,20,21) E5(4,18,8) RST(25)")

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
