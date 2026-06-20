#!/usr/bin/env python3
"""
test_fft_oled.py — standalone audio→FFT→OLED test.

Reads left channel from the HiFiBerry ADC, computes a 64-band FFT,
draws the spectrum on the SSD1322 OLED, and prints a terminal meter.

Use this to confirm the full pipeline works independently of dmx_audio_react.py.
Press Ctrl+C to quit.
"""
import sys, time, threading
import numpy as np

# ── audio ────────────────────────────────────────────────────────────────────
try:
    import sounddevice as sd
except ModuleNotFoundError:
    sys.exit("sounddevice not found — run with .venv/bin/python3")

# ── OLED ─────────────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont
    from luma.core.interface.serial import spi as luma_spi
    from luma.core.framebuffer import full_frame as luma_full_frame
    from luma.oled.device import ssd1322
    OLED_OK = True
except Exception as e:
    print(f"[WARN] OLED libs not available: {e}")
    OLED_OK = False

# ── config ───────────────────────────────────────────────────────────────────
AUDIO_DEVICE  = 2          # 'hifiberry' ALSA alias
SR            = 48000
BLOCKSIZE     = 1024
FFT_SIZE      = 1024
NUM_BANDS     = 64
FREQ_MIN      = 20
FREQ_MAX      = 16000

VISUAL_BOOST  = 2.5   # multiply bar height — raise if bars look too short

OLED_SPI_DEV  = 0
OLED_DC_PIN   = 23
OLED_RST_PIN  = 24
OLED_W        = 256
OLED_H        = 64

WHITE = (255, 255, 255)
GRAY  = (100, 100, 100)
BLACK = (0, 0, 0)

# ── log-spaced bands ─────────────────────────────────────────────────────────
def log_bands(n, fmin, fmax):
    edges = np.logspace(np.log10(fmin), np.log10(fmax), n + 1)
    return [(edges[i], edges[i+1]) for i in range(n)]

BANDS = log_bands(NUM_BANDS, FREQ_MIN, FREQ_MAX)

def band_energy(mag, freqs, lo, hi):
    m = (freqs >= lo) & (freqs < hi)
    return float(np.mean(mag[m])) if m.any() else 0.0

# ── shared state ─────────────────────────────────────────────────────────────
bars        = np.zeros(NUM_BANDS, dtype=np.float32)   # 0..1 display level
running_max = 0.05
lock        = threading.Lock()
stop_evt    = threading.Event()

# ── audio callback ────────────────────────────────────────────────────────────
def audio_cb(indata, frames, time_info, status):
    global running_max
    if status:
        print(f"[AUDIO] {status}", file=sys.stderr)

    # mono from left channel
    x = indata[:, 0].astype(np.float32)
    rms = float(np.sqrt(np.mean(x**2)))

    # FFT
    win    = np.hanning(len(x)).astype(np.float32)
    padded = np.zeros(FFT_SIZE, dtype=np.float32)
    padded[:len(x)] = x * win
    mag    = np.abs(np.fft.rfft(padded)) / len(x)
    freqs  = np.fft.rfftfreq(FFT_SIZE, 1.0 / SR)

    # band energies → dB → 0..1
    raw = np.array([band_energy(mag, freqs, lo, hi) for lo, hi in BANDS], dtype=np.float32)
    with np.errstate(divide='ignore', invalid='ignore'):
        db  = np.where(raw > 1e-10, 20 * np.log10(raw + 1e-10), -100.0)
    levels = np.clip((db + 60.0) / 50.0, 0.0, None)

    # auto-normalize so the loudest band fills the display
    cur_max = float(np.max(levels))
    with lock:
        if cur_max > running_max:
            running_max = cur_max
        else:
            running_max = max(0.05, running_max * 0.995)
        norm = levels / max(running_max, 0.05)

        # smooth bars: fast attack, slower decay
        attack = norm > bars
        bars[attack]  = 0.7 * norm[attack]  + 0.3 * bars[attack]
        bars[~attack] = 0.92 * bars[~attack]

# ── OLED renderer ─────────────────────────────────────────────────────────────
def oled_loop(device):
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 8)
    except Exception:
        font = ImageFont.load_default()

    while not stop_evt.is_set():
        t0 = time.monotonic()
        img  = Image.new("RGB", (OLED_W, OLED_H), BLACK)
        draw = ImageDraw.Draw(img)

        with lock:
            b = bars.copy()

        # FFT spectrum — full width, top 48px
        fft_h = 48
        bar_w = OLED_W / NUM_BANDS
        for i, level in enumerate(b):
            bx0 = int(i * bar_w)
            bx1 = int((i + 1) * bar_w) - 1
            bh  = int(min(level * VISUAL_BOOST, 1.0) * fft_h)
            if bh < 1:
                continue
            by0 = fft_h - bh
            draw.rectangle((bx0, by0, bx1, fft_h - 1), fill=WHITE)

        # divider line
        draw.line((0, fft_h, OLED_W - 1, fft_h), fill=GRAY)

        # status row — bottom 14px
        rms_val = float(np.sqrt(np.mean(bars**2)))
        db_val  = 20 * np.log10(max(rms_val, 1e-6))
        draw.text((2, fft_h + 3), f"HiFiBerry L  {db_val:+.0f}dB", font=font, fill=WHITE)

        try:
            device.display(img)
        except Exception as e:
            print(f"[OLED] display error: {e}", file=sys.stderr)
            break

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1/30 - elapsed))  # ~30 fps

# ── terminal meter ─────────────────────────────────────────────────────────────
def term_loop():
    W = 50
    while not stop_evt.is_set():
        with lock:
            peak = float(np.max(bars))
            rms  = float(np.sqrt(np.mean(bars**2)))
        filled = int(peak * W)
        bar    = "[" + "#" * filled + "-" * (W - filled) + f"] peak={peak:.3f}  rms={rms:.3f}"
        print(f"\r{bar}", end="", flush=True)
        time.sleep(0.05)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print("=== FFT + OLED test (HiFiBerry left channel) ===")
    print(f"Audio: device={AUDIO_DEVICE}  sr={SR}  blocksize={BLOCKSIZE}")

    # OLED init
    oled_device = None
    if OLED_OK:
        try:
            serial     = luma_spi(device=OLED_SPI_DEV, port=0, bus_speed_hz=8000000,
                                  gpio_DC=OLED_DC_PIN, gpio_RST=OLED_RST_PIN)
            oled_device = ssd1322(serial, width=OLED_W, height=OLED_H, rotate=2,
                                  framebuffer=luma_full_frame())
            oled_device.contrast(255)
            print("[OK] OLED SSD1322 256x64 initialized")
        except Exception as e:
            print(f"[WARN] OLED init failed: {e}")
            oled_device = None
    else:
        print("[INFO] OLED libs missing — terminal-only mode")

    if oled_device:
        threading.Thread(target=oled_loop, args=(oled_device,), daemon=True).start()
    else:
        print("[INFO] No OLED — watch terminal bars below.")

    threading.Thread(target=term_loop, daemon=True).start()

    print("Playing audio into aux input — bars should move. Ctrl+C to quit.\n")
    try:
        with sd.InputStream(device=AUDIO_DEVICE, channels=2, samplerate=SR,
                            blocksize=BLOCKSIZE, callback=audio_cb):
            while True:
                time.sleep(0.1)
    except sd.PortAudioError as e:
        print(f"\n[ERROR] PortAudio: {e}")
        print("Available input devices:")
        for i, d in enumerate(sd.query_devices()):
            if d['max_input_channels'] > 0:
                print(f"  [{i}] {d['name']}")
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        time.sleep(0.2)
        if oled_device:
            try:
                oled_device.hide()
                oled_device.cleanup()
            except Exception:
                pass
        print("\nDone.")

if __name__ == "__main__":
    main()
