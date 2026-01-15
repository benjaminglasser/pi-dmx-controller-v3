#!/usr/bin/env python3
"""
FFT Spectrum Analyzer with Page System
Displays real-time audio visualization on the Waveshare 2.42" OLED

Screen Layout:
  - Top: FFT spectrum (16 bands) with threshold line
  - Bottom: Page tabs + page-specific controls

Pages (controlled by Encoder 1):
  - HOME: Freq Center, Threshold, Release
  - ADV:  Q Factor, Attack, Decay  
  - PRESET: Preset selection controls
  - COLOR: Color/program controls

Pot assignments:
  - Pot 1-3: Page-dependent (see above)
  - Slider (CH3): Always Brightness

Run: python tests/vu_meter_test.py
Press Ctrl+C to exit.
"""

import time
import sys
import threading
import numpy as np
import math

# ============ Audio ============
try:
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except ImportError:
    print("ERROR: sounddevice not installed. Run: pip install sounddevice")
    sys.exit(1)

# ============ OLED ============
try:
    from PIL import Image, ImageDraw, ImageFont
    from luma.core.interface.serial import spi as luma_spi
    from luma.oled.device import ssd1309
    OLED_AVAILABLE = True
except ImportError:
    print("ERROR: OLED libs not installed. Run: pip install luma.oled pillow")
    sys.exit(1)

# ============ GPIO ============
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# ============ SPI / MCP3008 ============
try:
    import spidev
    SPI_AVAILABLE = True
except ImportError:
    SPI_AVAILABLE = False

# ============ Configuration ============

# Audio settings
SAMPLE_RATE = 44100
BLOCK_SIZE = 512  # Smaller for more responsive FFT

# OLED settings
OLED_SPI_DEV = 1
OLED_RST_PIN = 12
OLED_DC_PIN = 24
OLED_WIDTH = 128
OLED_HEIGHT = 64

# MCP3008 settings
MCP_SPI_BUS = 0
MCP_SPI_DEV = 0

# Encoder 1 (page selection)
ENC1_CLK = 5
ENC1_DT = 6
ENC1_SW = 13

# Encoder 2 (preset selection)
ENC2_CLK = 17
ENC2_DT = 27
ENC2_SW = 22

# FFT settings - 32 bands for clean display
# Range: 80Hz to 12kHz (practical range for most audio sources)
# Starting at 80Hz avoids the gap from sub-bass that most mics/inputs don't capture
FFT_MIN_FREQ = 80
FFT_MAX_FREQ = 12000

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

FFT_BANDS = generate_log_bands(32, FFT_MIN_FREQ, FFT_MAX_FREQ)

# Pages - only 3 pots are page-specific now
# Slider (CH3) = Brightness (global)
# Encoder 2 = Preset (global)
PAGES = ["HOME", "ADV", "COLOR", "SET"]
PAGE_POT_LABELS = {
    "HOME": ["Frq", "Thr", "Rel"],
    "ADV": ["--", "Q", "Dcy"],
    "COLOR": ["Prg", "Spd", "Hue"],
    "SET": ["Op1", "Op2", "Op3"],
}

# Tab labels (short text for each page)
PAGE_TAB_LABELS = ["HOM", "ADV", "CLR", "SET"]

# Global parameters (not affected by page)
brightness = 0.5  # Controlled by slider (CH3)
preset_num = 1    # Controlled by encoder 2

# ============ Global State ============
running = True
current_page = 0  # Index into PAGES

# FFT data
fft_bands = [0.0] * len(FFT_BANDS)
fft_peaks = [0.0] * len(FFT_BANDS)
fft_peak_times = [0.0] * len(FFT_BANDS)
PEAK_HOLD_TIME = 0.4

# Auto-normalization: track recent max for dynamic range
fft_recent_max = 0.3  # Start with reasonable default
fft_max_decay = 0.995  # Slow decay of max tracker

# Frequency compensation curve - boost highs, cut lows
# This compensates for the 1/f characteristic of most audio content
def calculate_freq_compensation():
    """Calculate per-band gain compensation to flatten the spectrum visually."""
    compensation = []
    for low, high in FFT_BANDS:
        center = math.sqrt(low * high)  # Geometric mean
        # Reference frequency is ~800Hz (no boost/cut)
        ref_freq = 800.0
        # +5dB per octave above ref, -5dB per octave below (stronger compensation)
        octaves_from_ref = math.log2(center / ref_freq)
        db_adjustment = octaves_from_ref * 5.0
        # Convert dB to linear gain
        gain = 10 ** (db_adjustment / 20.0)
        # Clamp to reasonable range
        gain = max(0.15, min(6.0, gain))
        compensation.append(gain)
    return compensation

FFT_COMPENSATION = calculate_freq_compensation()

# Pot values (0-1 normalized)
pot_values = [0.5, 0.5, 0.5, 0.5]  # CH0, CH1, CH2, CH3 (slider)
pot_display = [50, 50, 50, 50]  # Display values (0-100, with hysteresis)

# Threshold (controlled by pot on HOME page)
threshold = 0.3

# Center frequency and Q for bandpass visualization
# These are set by pots and persist across page changes
center_freq = 500.0  # Hz (controlled by pot 1 on HOME page)
q_factor = 2.0       # Q factor (controlled by pot 2 on ADV page - persists!)

# Encoder state
encoder1_value = 0  # Page selection
encoder2_value = 1  # Preset number (1-8)
encoder1_last_clk = None
encoder2_last_clk = None

# ============ Audio Processing ============

def get_band_energy(fft_magnitudes, freqs, low_hz, high_hz):
    """Get energy in a frequency band from FFT data."""
    mask = (freqs >= low_hz) & (freqs < high_hz)
    if not np.any(mask):
        return 0.0
    return np.mean(fft_magnitudes[mask])

def audio_callback(indata, frames, time_info, status):
    global fft_bands, fft_peaks, fft_peak_times, fft_recent_max
    
    if not running:
        return
    
    audio = indata[:, 0].astype(np.float32)
    
    # FFT analysis
    window = np.hanning(len(audio))
    fft = np.fft.rfft(audio * window)
    fft_mag = np.abs(fft) / len(audio)
    freqs = np.fft.rfftfreq(len(audio), 1.0 / SAMPLE_RATE)
    
    now = time.time()
    
    # First pass: calculate raw energies with compensation
    raw_levels = []
    for i, (low, high) in enumerate(FFT_BANDS):
        energy = get_band_energy(fft_mag, freqs, low, high)
        energy *= FFT_COMPENSATION[i]
        
        # Convert to dB-like scale
        if energy > 1e-10:
            db = 20 * math.log10(energy + 1e-10)
            # Map dB to 0-1 range (adjust range for better sensitivity)
            normalized = max(0, (db + 60) / 50)  # -60dB to -10dB maps to 0-1
        else:
            normalized = 0
        raw_levels.append(normalized)
    
    # Track the recent maximum for auto-normalization
    current_max = max(raw_levels) if raw_levels else 0
    if current_max > fft_recent_max:
        fft_recent_max = current_max  # Instant attack
    else:
        fft_recent_max = fft_recent_max * fft_max_decay  # Slow decay
    
    # Ensure minimum floor to avoid division issues
    norm_factor = max(0.1, fft_recent_max)
    
    # Second pass: normalize and smooth
    for i, raw in enumerate(raw_levels):
        # Auto-normalize so max fills the display
        normalized = min(1.0, raw / norm_factor)
        
        # Heavy smoothing for stable display
        if normalized > fft_bands[i]:
            fft_bands[i] = 0.4 * normalized + 0.6 * fft_bands[i]  # Faster attack
        else:
            fft_bands[i] = 0.95 * fft_bands[i]  # Smooth decay
        
        # Peak hold
        if fft_bands[i] > fft_peaks[i]:
            fft_peaks[i] = fft_bands[i]
            fft_peak_times[i] = now
        elif now - fft_peak_times[i] > PEAK_HOLD_TIME:
            fft_peaks[i] = max(fft_peaks[i] * 0.98, fft_bands[i])

# ============ MCP3008 ============

mcp_spi = None
mcp_lock = threading.Lock()
pot_ema = [None, None, None, None]
EMA_ALPHA = 0.15  # Lower = smoother (was 0.3)
HYSTERESIS = 2    # Don't update display unless change > this %

def init_mcp3008():
    global mcp_spi
    if not SPI_AVAILABLE:
        return False
    try:
        mcp_spi = spidev.SpiDev()
        mcp_spi.open(MCP_SPI_BUS, MCP_SPI_DEV)
        mcp_spi.max_speed_hz = 1000000
        mcp_spi.mode = 0
        return True
    except:
        return False

def read_mcp3008(channel):
    if mcp_spi is None:
        return 512
    cmd = [1, (8 + channel) << 4, 0]
    with mcp_lock:
        try:
            resp = mcp_spi.xfer2(cmd)
        except:
            return 512
    return ((resp[1] & 3) << 8) | resp[2]

def read_pot_smoothed(channel):
    raw = read_mcp3008(channel) / 1023.0
    if pot_ema[channel] is None:
        pot_ema[channel] = raw
    else:
        pot_ema[channel] = EMA_ALPHA * raw + (1 - EMA_ALPHA) * pot_ema[channel]
    return pot_ema[channel]

def update_pots():
    global pot_values, pot_display, threshold, center_freq, q_factor, brightness
    for i in range(4):
        pot_values[i] = read_pot_smoothed(i)
        
        # Apply hysteresis to display value to prevent jitter
        new_pct = int(pot_values[i] * 100)
        if abs(new_pct - pot_display[i]) > HYSTERESIS:
            pot_display[i] = new_pct
    
    # Slider (CH3) is always brightness (global)
    brightness = pot_values[3]
    
    # Update parameters based on current page (pots 0-2 only)
    # Note: center_freq, threshold, and q_factor PERSIST across page changes
    # They are only updated when on their respective pages
    page_name = PAGES[current_page]
    if page_name == "HOME":
        # Pot 1: Center frequency (60Hz - 16kHz, log scale matching FFT range)
        log_min = math.log10(FFT_MIN_FREQ)
        log_max = math.log10(FFT_MAX_FREQ)
        center_freq = 10 ** (log_min + pot_values[0] * (log_max - log_min))
        # Pot 2: Threshold
        threshold = pot_values[1]
        # Pot 3: Release time (future use)
    elif page_name == "ADV":
        # Pot 1: (reserved)
        # Pot 2: Q factor (0.5 to 10) - THIS IS THE Q CONTROL
        q_factor = 0.5 + pot_values[1] * 9.5
        # Pot 3: Decay (future use)
    elif page_name == "COLOR":
        # Pot 1: Program (future use)
        # Pot 2: Speed (future use)
        # Pot 3: Hue (future use)
        pass
    elif page_name == "SET":
        # Settings page (future use)
        pass

# ============ GPIO / Encoder ============

def init_gpio():
    if not GPIO_AVAILABLE:
        return False
    try:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        # Encoder 1 (page selection)
        GPIO.setup(ENC1_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC1_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC1_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        # Encoder 2 (preset selection)
        GPIO.setup(ENC2_CLK, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC2_DT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(ENC2_SW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        return True
    except:
        return False

def encoder_thread():
    global encoder1_value, encoder1_last_clk, current_page
    global encoder2_value, encoder2_last_clk, preset_num
    
    if not GPIO_AVAILABLE:
        return
    
    encoder1_last_clk = GPIO.input(ENC1_CLK)
    encoder2_last_clk = GPIO.input(ENC2_CLK)
    
    while running:
        try:
            # Encoder 1 - page selection
            clk1 = GPIO.input(ENC1_CLK)
            dt1 = GPIO.input(ENC1_DT)
            
            if clk1 != encoder1_last_clk:
                if dt1 != clk1:
                    encoder1_value += 1
                else:
                    encoder1_value -= 1
                
                # Update page (wrap around)
                current_page = encoder1_value % len(PAGES)
                if current_page < 0:
                    current_page += len(PAGES)
            
            encoder1_last_clk = clk1
            
            # Encoder 2 - preset selection (1-8)
            clk2 = GPIO.input(ENC2_CLK)
            dt2 = GPIO.input(ENC2_DT)
            
            if clk2 != encoder2_last_clk:
                if dt2 != clk2:
                    encoder2_value += 1
                else:
                    encoder2_value -= 1
                
                # Clamp preset to 1-8
                preset_num = max(1, min(8, encoder2_value))
                encoder2_value = preset_num  # Keep in sync
            
            encoder2_last_clk = clk2
            
            time.sleep(0.002)
        except:
            break

# ============ OLED Display ============

oled_device = None
font = None
font_small = None

def init_oled():
    global oled_device, font, font_small
    try:
        serial = luma_spi(
            device=OLED_SPI_DEV,
            port=0,
            bus_speed_hz=2000000,  # 2MHz for maximum stability
            gpio_DC=OLED_DC_PIN,
            gpio_RST=OLED_RST_PIN,
        )
        oled_device = ssd1309(serial, width=OLED_WIDTH, height=OLED_HEIGHT, rotate=0)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 9)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 8)
        except:
            font = ImageFont.load_default()
            font_small = font
        
        return True
    except Exception as e:
        print(f"OLED init failed: {e}")
        return False

def freq_to_x(freq, x_start, width):
    """Convert frequency to x position (log scale) using FFT range."""
    if freq <= FFT_MIN_FREQ:
        return x_start
    if freq >= FFT_MAX_FREQ:
        return x_start + width - 1
    log_min = math.log10(FFT_MIN_FREQ)
    log_max = math.log10(FFT_MAX_FREQ)
    log_freq = math.log10(freq)
    ratio = (log_freq - log_min) / (log_max - log_min)
    return int(x_start + ratio * (width - 1))

def draw_fft_spectrum(draw, x, y, width, height):
    """
    Draw FFT spectrum analyzer with vertical bars, threshold line,
    and Q bandwidth indicator. Auto-normalized to fill display.
    """
    num_bands = len(fft_bands)
    
    # Calculate bar positions with 1px gap between bars for distinction
    total_gaps = num_bands - 1
    total_bar_width = width - total_gaps  # Reserve 1px per gap
    bar_width = max(1, total_bar_width // num_bands)
    
    for i, level in enumerate(fft_bands):
        # Each bar: bar_width pixels, then 1px gap
        bx_start = x + i * (bar_width + 1)
        bx_end = bx_start + bar_width - 1
        
        # Don't exceed display width
        if bx_end >= x + width:
            bx_end = x + width - 1
        if bx_start >= x + width:
            continue
        
        bar_h = int(level * height)
        if bar_h > 0:
            draw.rectangle(
                (bx_start, y + height - bar_h, bx_end, y + height - 1),
                fill=1
            )
    
    # Q bandwidth markers - clamp to visible frequency range
    clamped_center = max(FFT_MIN_FREQ, min(FFT_MAX_FREQ, center_freq))
    bandwidth = clamped_center / max(0.1, q_factor)
    low_freq = max(FFT_MIN_FREQ, clamped_center - bandwidth / 2)
    high_freq = min(FFT_MAX_FREQ, clamped_center + bandwidth / 2)
    
    low_x = freq_to_x(low_freq, x, width)
    high_x = freq_to_x(high_freq, x, width)
    
    # Draw Q boundaries as solid lines
    draw.line((low_x, y, low_x, y + height - 1), fill=1)
    draw.line((high_x, y, high_x, y + height - 1), fill=1)
    
    # Threshold line (solid horizontal within Q range)
    thresh_y = y + height - int(threshold * height)
    if y <= thresh_y < y + height:
        draw.line((low_x, thresh_y, high_x, thresh_y), fill=1)

def draw_sun_icon(draw, x, y, size=7):
    """Draw a small sun icon for brightness."""
    cx, cy = x + size // 2, y + size // 2
    r = size // 2 - 1
    
    # Center circle (2x2)
    draw.rectangle((cx - 1, cy - 1, cx, cy), fill=1)
    
    # Rays (4 cardinal + 4 diagonal)
    # Top
    draw.point((cx, y), fill=1)
    # Bottom
    draw.point((cx, y + size - 1), fill=1)
    # Left
    draw.point((x, cy), fill=1)
    # Right
    draw.point((x + size - 1, cy), fill=1)
    # Diagonals
    draw.point((x + 1, y + 1), fill=1)
    draw.point((x + size - 2, y + 1), fill=1)
    draw.point((x + 1, y + size - 2), fill=1)
    draw.point((x + size - 2, y + size - 2), fill=1)

def draw_global_controls(draw, x, y, height):
    """Draw global controls (Preset, Brightness) in bottom left."""
    # Preset number
    draw.text((x, y), f"P{preset_num}", font=font_small, fill=1)
    
    # Sun icon for brightness
    draw_sun_icon(draw, x, y + 10, size=7)
    
    # Brightness bar (horizontal, next to sun)
    bar_x = x + 9
    bar_y = y + 11
    bar_width = 12
    bar_height = 5
    brt_fill = int(brightness * bar_width)
    
    # Draw brightness bar outline
    draw.rectangle((bar_x, bar_y, bar_x + bar_width, bar_y + bar_height), outline=1, fill=0)
    # Fill from left
    if brt_fill > 0:
        draw.rectangle((bar_x + 1, bar_y + 1, bar_x + brt_fill, bar_y + bar_height - 1), fill=1)

def draw_page_tabs(draw, x, y):
    """Draw page tabs as text labels."""
    tab_width = 22  # Width per tab
    tab_height = 10
    
    for i, label in enumerate(PAGE_TAB_LABELS):
        tx = x + i * tab_width
        
        # Highlight current page (inverted)
        if i == current_page:
            draw.rectangle((tx, y, tx + tab_width - 2, y + tab_height - 1), outline=1, fill=1)
            draw.text((tx + 2, y + 1), label, font=font_small, fill=0)
        else:
            draw.rectangle((tx, y, tx + tab_width - 2, y + tab_height - 1), outline=1, fill=0)
            draw.text((tx + 2, y + 1), label, font=font_small, fill=1)

def draw_pot_values(draw, x, y, width):
    """Draw pot values for current page (3 pots only)."""
    page_name = PAGES[current_page]
    labels = PAGE_POT_LABELS[page_name]
    
    # 3 pots spread across available width
    col_width = width // 3
    
    for i in range(3):
        px = x + i * col_width
        draw.text((px, y), f"{labels[i]}", font=font_small, fill=1)
        draw.text((px, y + 9), f"{pot_display[i]:3d}", font=font_small, fill=1)

def draw_display():
    """
    Main display drawing.
    Layout (128x64):
      - TOP HALF (y=0-31): FFT Spectrum (full width, 32px tall)
      - BOTTOM HALF (y=32-63):
        - Left: Global controls (Preset, Brightness) with vertical separator
        - Right: Page tabs + 3 pot values
    """
    if oled_device is None:
        return
    
    try:
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        
        # FFT Spectrum (top half, FULL WIDTH)
        draw_fft_spectrum(draw, 0, 0, OLED_WIDTH, 32)
        
        # Bottom half layout
        global_width = 24  # Width for preset + brightness
        separator_x = global_width + 1
        controls_x = separator_x + 3
        controls_width = OLED_WIDTH - controls_x
        
        # Global controls (bottom left)
        draw_global_controls(draw, 1, 34, 28)
        
        # Vertical separator line
        draw.line((separator_x, 33, separator_x, 63), fill=1)
        
        # Page tabs as icons (bottom right, top row)
        draw_page_tabs(draw, controls_x, 34)
        
        # Pot values (bottom right, below tabs)
        draw_pot_values(draw, controls_x, 46, controls_width)
        
        oled_device.display(image)
    except:
        pass

def display_loop():
    """Display update loop with consistent frame timing."""
    target_fps = 15  # 15 FPS for stable OLED
    frame_time = 1.0 / target_fps
    
    while running:
        start = time.time()
        update_pots()
        draw_display()
        
        # Sleep for remaining frame time
        elapsed = time.time() - start
        sleep_time = frame_time - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

# ============ Audio Device Selection ============

def choose_input_device():
    devs = sd.query_devices()
    preferred = ["usb", "hifiberry", "scarlett", "codec", "input"]
    
    for pat in preferred:
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) >= 1:
                if pat.lower() in d.get("name", "").lower():
                    return i, d["name"]
    
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) >= 1:
            return i, d["name"]
    
    raise RuntimeError("No audio input device found")

# ============ Main ============

def main():
    global running
    
    print("=" * 50)
    print("FFT Spectrum + Page System Test")
    print("=" * 50)
    print()
    
    # Initialize OLED
    if not init_oled():
        print("✗ OLED init failed")
        sys.exit(1)
    print("✓ OLED initialized")
    
    # Initialize MCP3008
    if init_mcp3008():
        print("✓ MCP3008 initialized")
    else:
        print("✗ MCP3008 not available (using defaults)")
    
    # Initialize GPIO
    if init_gpio():
        print("✓ GPIO initialized")
        enc_thread = threading.Thread(target=encoder_thread, daemon=True)
        enc_thread.start()
    else:
        print("✗ GPIO not available")
    
    # Find audio device
    try:
        device_idx, device_name = choose_input_device()
        print(f"✓ Audio: {device_name}")
    except Exception as e:
        print(f"✗ Audio error: {e}")
        sys.exit(1)
    
    print()
    print("Controls:")
    print("  Encoder 1: Change page (HOME/ADV/PRESET/COLOR)")
    print("  Pots 1-3:  Page-specific controls")
    print("  Slider:    Always brightness")
    print()
    print("Press Ctrl+C to exit.")
    print()
    
    # Start display thread
    display_thread = threading.Thread(target=display_loop, daemon=True)
    display_thread.start()
    
    # Start audio stream
    try:
        with sd.InputStream(
            device=device_idx,
            channels=1,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            callback=audio_callback
        ):
            print("Listening...")
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n\nExiting...")
    except Exception as e:
        print(f"Audio error: {e}")
    finally:
        running = False
        time.sleep(0.1)
        
        if oled_device:
            try:
                oled_device.clear()
                oled_device.hide()
            except:
                pass
        
        if mcp_spi:
            mcp_spi.close()
        
        if GPIO_AVAILABLE:
            GPIO.cleanup()
        
        print("Done.")

if __name__ == "__main__":
    main()
