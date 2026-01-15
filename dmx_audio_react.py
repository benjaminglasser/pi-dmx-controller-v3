#!/usr/bin/env python3
# dmx_audio_react.py (v2: NO OLA) + DEV_NO_HW + Plug&Play Audio + FFT OLED Display
#
# Audio-reactive DMX with optional hardware:
#   - 3 pots + 1 slider (MCP3008 via SPI0 CE0, CH0-CH3)
#   - 2 rotary encoders with push buttons
#   - SPI OLED display with FFT spectrum (shares MOSI/CLK with MCP3008, uses CE1)
#
# Hardware Wiring:
#   MCP3008 (SPI0 CE0):
#     CH0: Pot 1 (Center Freq on HOME, reserved on ADV)
#     CH1: Pot 2 (Threshold on HOME, Q on ADV)
#     CH2: Pot 3 (Release on HOME, Decay on ADV)
#     CH3: Slider (Brightness - always)
#
#   SPI OLED (shares MOSI/CLK):
#     RST: GPIO 12
#     DC:  GPIO 24
#     CS:  CE1 (GPIO 7)
#
#   Rotary Encoder 1 (Page selection):
#     CLK: GPIO 5
#     DT:  GPIO 6
#     SW:  GPIO 13 (Reset button)
#
#   Rotary Encoder 2 (Preset selection):
#     CLK: GPIO 17
#     DT:  GPIO 27
#     SW:  GPIO 22
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

import os, sys, time, math, threading, curses, re
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

# Startup defaults
DEFAULT_CENTER_HZ = 120.0   # 120 Hz
DEFAULT_Q         = 2.0
DEFAULT_THRESH    = 0.60    # 60 on 0-99 scale
DEFAULT_ATTACK_MS = 10.0
DEFAULT_DECAY_MS  = 536.0   # ~10 on 0-99 scale (40 + 0.10 * 4960 = 536ms)
DEFAULT_BRIGHT    = 0.5

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

# Rotary Encoder 1 (Page selection + Reset button)
ENC1_CLK = 5
ENC1_DT  = 6
ENC1_SW  = 13

# Rotary Encoder 2 (Preset selection)
ENC2_CLK = 17
ENC2_DT  = 27
ENC2_SW  = 22

# SPI OLED pins (Waveshare 2.42" SSD1309 on CE1)
OLED_SPI_DEV = 1   # CE1 (GPIO 7)
OLED_RST_PIN = 12
OLED_DC_PIN  = 24
OLED_WIDTH   = 128
OLED_HEIGHT  = 64

# MCP3008 on SPI0 CE0
SPI_BUS, SPI_DEV = 0, 0

# ===================== Page System =====================

PAGES = ["HOME", "ADV", "PRE", "COLOR", "SET"]
PAGE_POT_LABELS = {
    "HOME": ["Freq", "Thresh", "Rels"],      # Freq center, Threshold, Release
    "ADV": ["Q", "ThPre", "RelMd"],           # Q, Threshold Preset, Release Mode
    "PRE": ["BtCyc", "CycBt", "Reset"],       # Beat cycles, Cycles between, Reset mode
    "COLOR": ["Light", "HSV", "Sat"],         # Light select, HSV, Saturation
    "SET": ["--", "--", "--"],                # TBD
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
PROGRAM_NAMES = ["ALL", "1+2/3+4", "1+3/2+4", "CHASE"]

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
encoder2_value = 1  # Program selection (1-4)
encoder1_button = False
encoder2_button = False
_enc1_last_clk = None
_enc2_last_clk = None

# Pot values (0-1 normalized)
pot_values = [0.5, 0.5, 0.5, 0.5]
pot_display = [50, 50, 50, 50]
_pot_raw = [0.5, 0.5, 0.5, 0.5]  # Raw readings for change detection
POT_EMA_ALPHA = 0.15  # Moderate smoothing for responsiveness
POT_CHANGE_THRESHOLD = 0.03  # 3% change needed to take over
POT_STABLE_FRAMES = 0  # Counter for stability

# Pot takeover system - pots don't control until moved significantly
# This allows script defaults to be used on startup
_pot_has_taken_over = [False, False, False, False]
_pot_initial_reading = [None, None, None, None]
POT_TAKEOVER_THRESHOLD = 0.05  # 5% movement from initial to take over

# Display-specific smoothed values (separate from control values)
_display_freq = DEFAULT_CENTER_HZ
_display_thresh = DEFAULT_THRESH
_display_q = DEFAULT_Q
_display_bright = DEFAULT_BRIGHT
_display_release = int((DEFAULT_DECAY_MS - 40.0) / 4960.0 * 99)  # Release display value
_display_values_locked = [None, None, None, None]  # Lock display when stable
DISPLAY_LOCK_THRESHOLD = 0.005  # Lock if change < 0.5%

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

CYCLE_STEPS_OPTIONS = [0, 4, 8, 16, 32, 64, 128, 256]
CYCLE_STEPS         = 0
CYCLE_TRIGGER_COUNT = 0
CYCLE_PHASE         = 0

def program_pair_for_base(base: int):
    if base == 1: return (1, 2)
    if base == 2: return (2, 3)
    if base == 3: return (3, 4)
    return (4, 1)

def set_cycle_steps(steps: int):
    global CYCLE_STEPS, CYCLE_TRIGGER_COUNT, CYCLE_PHASE
    CYCLE_STEPS         = int(steps)
    CYCLE_TRIGGER_COUNT = 0
    CYCLE_PHASE         = 0

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
        self.center    = DEFAULT_CENTER_HZ
        self.q         = DEFAULT_Q
        self.thresh    = DEFAULT_THRESH
        self.attack_ms = DEFAULT_ATTACK_MS
        self.decay_ms  = DEFAULT_DECAY_MS

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

# ===================== MCP3008 (knobs) =====================

spi = None
spi_lock = threading.Lock()
_pot_ema = [None] * 4

def init_spi():
    global spi
    if DEV_NO_HW:
        return
    if spidev is None:
        raise RuntimeError("spidev module not available")
    s = spidev.SpiDev()
    s.open(SPI_BUS, SPI_DEV)
    s.max_speed_hz = 1000000
    s.mode = 0
    spi = s

def read_mcp3008(ch: int) -> int:
    if DEV_NO_HW or spi is None:
        return 512
    cmd = [1, (8+ch) << 4, 0]
    with spi_lock:
        try:
            resp = spi.xfer2(cmd)
        except OSError:
            time.sleep(0.02)
            try:
                spi.close()
            except Exception:
                pass
            spi.open(SPI_BUS, SPI_DEV)
            spi.max_speed_hz = 1000000
            spi.mode = 0
            resp = spi.xfer2(cmd)
        # Small delay to let SPI bus settle before OLED uses it
        time.sleep(0.0005)  # 0.5ms
    return ((resp[1] & 3) << 8) | resp[2]

_pot_stable_count = [0, 0, 0, 0]  # Count consecutive stable readings

def read_pot_smoothed(channel):
    """Read pot with takeover system - defaults used until pot is moved significantly."""
    global _pot_raw, _display_values_locked, _pot_has_taken_over, _pot_initial_reading, _pot_stable_count
    
    raw = read_mcp3008(channel) / 1023.0
    
    # First reading - store initial position
    if _pot_initial_reading[channel] is None:
        _pot_initial_reading[channel] = raw
        _pot_ema[channel] = raw
        _pot_raw[channel] = raw
        _pot_stable_count[channel] = 0
        return raw
    
    # Check if pot has taken over yet
    if not _pot_has_taken_over[channel]:
        # Check if pot moved enough from initial position to take over
        if abs(raw - _pot_initial_reading[channel]) > POT_TAKEOVER_THRESHOLD:
            _pot_has_taken_over[channel] = True
            _pot_ema[channel] = raw
            _pot_raw[channel] = raw
            _display_values_locked[channel] = None
            _pot_stable_count[channel] = 0
        else:
            # Pot hasn't taken over - don't update
            return _pot_ema[channel]
    
    # Pot has taken over - normal smoothing
    change = abs(raw - _pot_raw[channel])
    _pot_raw[channel] = raw
    
    if change > POT_CHANGE_THRESHOLD:
        # Pot is moving - be responsive
        _pot_ema[channel] = 0.4 * raw + 0.6 * _pot_ema[channel]
        _display_values_locked[channel] = None
        _pot_stable_count[channel] = 0
    else:
        # Pot is stable - smooth very heavily
        _pot_ema[channel] = 0.02 * raw + 0.98 * _pot_ema[channel]
        _pot_stable_count[channel] += 1
        
        # Lock display after 5 consecutive stable readings
        if _pot_stable_count[channel] >= 5 and _display_values_locked[channel] is None:
            _display_values_locked[channel] = _pot_ema[channel]
    
    if _display_values_locked[channel] is not None:
        return _display_values_locked[channel]
    return _pot_ema[channel]

def update_pots():
    """Read pots and update parameters based on current page.
    Uses takeover system - defaults are used until pot is moved significantly."""
    global pot_values, pot_display, BRIGHTNESS
    
    if DEV_NO_HW:
        return
    if time.time() < IGNORE_KNOBS_UNTIL:
        return
    
    for i in range(4):
        pot_values[i] = read_pot_smoothed(i)
        new_pct = int(pot_values[i] * 100)
        if new_pct != pot_display[i]:
            pot_display[i] = new_pct
    
    # Slider (CH3) is always brightness (global) - only if taken over
    if _pot_has_taken_over[3]:
        BRIGHTNESS = pot_values[3]
    
    # Update parameters based on current page - only if pot has taken over
    page_name = PAGES[current_page]
    if page_name == "HOME":
        # Pot 0: Center frequency (log scale)
        if _pot_has_taken_over[0]:
            log_min = math.log10(FFT_MIN_FREQ)
            log_max = math.log10(FFT_MAX_FREQ)
            band.center = 10 ** (log_min + pot_values[0] * (log_max - log_min))
        # Pot 1: Threshold
        if _pot_has_taken_over[1]:
            band.thresh = pot_values[1]
        # Pot 2: Release/Decay (50ms to 5000ms range)
        if _pot_has_taken_over[2]:
            band.decay_ms = 40.0 + pot_values[2] * 4960.0
    elif page_name == "ADV":
        # Pot 0: Q factor - inverted so higher value = wider range (lower Q)
        # pot 0 = Q of 10 (narrow), pot 99 = Q of 0.5 (wide)
        if _pot_has_taken_over[0]:
            band.q = 10.0 - pot_values[0] * 9.5  # Inverted: 10 down to 0.5
        # Pot 2: Decay (50ms to 5000ms range)
        if _pot_has_taken_over[2]:
            band.decay_ms = 40.0 + pot_values[2] * 4960.0

# ===================== GPIO / Rotary Encoders =====================

def reset_to_defaults(channel=None):
    """Reset HOME page parameters (Freq, Threshold, Release) to defaults."""
    global IGNORE_KNOBS_UNTIL, _pot_has_taken_over, _pot_initial_reading
    # Only reset HOME page parameters
    band.center    = DEFAULT_CENTER_HZ
    band.thresh    = DEFAULT_THRESH
    # Release is not currently stored separately, but we reset decay
    band.decay_ms  = DEFAULT_DECAY_MS
    
    # Reset pot takeover state so defaults are used until pots are moved again
    for i in range(4):
        _pot_ema[i] = None
        _pot_has_taken_over[i] = False
        _pot_initial_reading[i] = None
    IGNORE_KNOBS_UNTIL = time.time() + 0.3
    ui_flash("HOME defaults reset", 1.0)

def setup_gpio_inputs():
    if DEV_NO_HW:
        return
    if GPIO is None:
        raise RuntimeError("RPi.GPIO not available")
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    
    # Rotary Encoder 1
    GPIO.setup(ENC1_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC1_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC1_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Rotary Encoder 2
    GPIO.setup(ENC2_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC2_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(ENC2_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def encoder_reader():
    """Read rotary encoders for page and program selection."""
    global encoder1_value, encoder2_value, encoder1_button, encoder2_button
    global _enc1_last_clk, _enc2_last_clk
    global current_page, BASE_PROGRAM
    
    if DEV_NO_HW:
        return
    
    _enc1_last_clk = GPIO.input(ENC1_CLK)
    _enc2_last_clk = GPIO.input(ENC2_CLK)
    last_enc1_btn = 1
    
    try:
        while not STOP_THREADS:
            try:
                # Encoder 1 - page selection
                enc1_clk = GPIO.input(ENC1_CLK)
                enc1_dt = GPIO.input(ENC1_DT)
                enc1_sw = GPIO.input(ENC1_SW)
                
                # Only trigger on falling edge of CLK (0) to avoid double-counting
                if enc1_clk == 0 and _enc1_last_clk == 1:
                    if enc1_dt == 1:  # CW
                        encoder1_value += 1
                    else:  # CCW
                        encoder1_value -= 1
                    # Clamp page (no wrap around)
                    encoder1_value = max(0, min(len(PAGES) - 1, encoder1_value))
                    current_page = encoder1_value
                _enc1_last_clk = enc1_clk

                # Encoder 1 button (reset)
                encoder1_button = (enc1_sw == 0)
                if last_enc1_btn == 1 and enc1_sw == 0:
                    time.sleep(0.02)
                    if GPIO.input(ENC1_SW) == 0:
                        reset_to_defaults()
                last_enc1_btn = enc1_sw
                
                # Encoder 2 - program selection (1-4)
                enc2_clk = GPIO.input(ENC2_CLK)
                enc2_dt = GPIO.input(ENC2_DT)
                enc2_sw = GPIO.input(ENC2_SW)
                
                # Only trigger on falling edge of CLK (0) to avoid double-counting
                if enc2_clk == 0 and _enc2_last_clk == 1:
                    if enc2_dt == 1:  # CW
                        encoder2_value += 1
                    else:  # CCW
                        encoder2_value -= 1
                    # Clamp program to 1-4
                    BASE_PROGRAM = max(1, min(4, encoder2_value))
                    encoder2_value = BASE_PROGRAM
                    ui_flash(f"Program: {PROGRAM_NAMES[BASE_PROGRAM-1]}", 0.8)
                _enc2_last_clk = enc2_clk
                
                encoder2_button = (enc2_sw == 0)
                
            except RuntimeError:
                break

            time.sleep(0.002)
    finally:
        pass

# ===================== Audio loop =====================

live_band_env   = 0.0
live_threshold  = band.thresh
input_rms       = 0.0
last_trigger_ts = 0.0
chase_idx       = 0
group34_phase   = 0

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

            if CYCLE_STEPS > 0:
                CYCLE_TRIGGER_COUNT += 1
                if CYCLE_TRIGGER_COUNT >= CYCLE_STEPS:
                    CYCLE_TRIGGER_COUNT = 0
                    CYCLE_PHASE = 1 - CYCLE_PHASE

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
                if i == 0:  # Frequency - show in Hz
                    freq_hz = int(_display_freq)
                    if freq_hz >= 1000:
                        val_str = f"{freq_hz//1000}k"
                    else:
                        val_str = f"{freq_hz}"
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
                    # Q ranges from 10 (narrow) to 0.5 (wide)
                    # Convert to 0-99 where 99 = widest (Q=0.5), 0 = narrowest (Q=10)
                    q_pct = int((10.0 - _display_q) / 9.5 * 99)
                    q_pct = max(0, min(99, q_pct))
                    val_str = f"{q_pct}"
                elif i == 2:  # Decay
                    val_str = f"{int(band.decay_ms)}"
                else:
                    val_str = "--"
            else:
                val_str = f"{pot_display[i]}" if labels[i] != "--" else "--"
            
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
            update_pots()
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
            f"Page={PAGES[current_page]}  Preset={preset_num}  P{PROGRAM}  RUN={'ON' if RUNNING else 'PAUSE'}  "
            f"Device={DEVICE_NAME}  DMX={DMX_BACKEND}"
        )
        safe_addstr(stdscr, 1, 0,
            f"Center={band.center:.0f}Hz  Q={band.q:.1f}  Thresh={band.thresh:.2f}  "
            f"Decay={band.decay_ms:.0f}ms  Bright={BRIGHTNESS:.0%}"
        )
        safe_addstr(stdscr, 2, 0, f"Pots: {pot_display[0]:3d} {pot_display[1]:3d} {pot_display[2]:3d} {pot_display[3]:3d}")

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
        print("[OK] DEV_NO_HW=1: skipping SPI/MCP3008, GPIO, OLED.")
    else:
        print("[OK] Hardware mode: SPI + GPIO enabled.")

    global APP_STATE, IGNORE_KNOBS_UNTIL
    APP_STATE = "loading"
    IGNORE_KNOBS_UNTIL = time.time() + 0.3

    # init SPI/GPIO if enabled
    if not DEV_NO_HW:
        init_spi()
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
        print("[OK] Encoders: E1(GPIO5,6,13) E2(GPIO17,27,22)")

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
            if spi is not None:
                spi.close()
        except Exception:
            pass
        try:
            if (not DEV_NO_HW) and (GPIO is not None):
                GPIO.cleanup()
        except Exception:
            pass

if __name__ == "__main__":
    main()
