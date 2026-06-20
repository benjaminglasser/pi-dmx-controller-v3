#!/usr/bin/env python3
"""
test_audio_input.py — quick sanity check for HiFiBerry ADC input.
Shows a live ASCII bar for each channel so you can confirm signal is reaching Python.
Press Ctrl+C to quit.
"""
import sys
import time

try:
    import sounddevice as sd
    import numpy as np
except ModuleNotFoundError as e:
    sys.exit(f"Missing dependency: {e}\nRun: .venv/bin/python3 scripts/test_audio_input.py")

SR       = 48000
BLOCKSIZE = 2048
BAR_W    = 50
DEVICE   = 2   # 'hifiberry' ALSA alias — change if needed

def bar(level, width=BAR_W):
    filled = int(level * width)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {level:.4f}"

def db(v):
    return 20 * np.log10(max(v, 1e-10))

print("=== HiFiBerry ADC input test ===")
print(f"Device index: {DEVICE}")
try:
    info = sd.query_devices(DEVICE)
    print(f"Device name:  {info['name']}")
    print(f"Max inputs:   {info['max_input_channels']}")
    print(f"Default SR:   {info['default_samplerate']}")
except Exception as e:
    print(f"(could not query device {DEVICE}: {e})")

print(f"\nSample rate: {SR} Hz   Blocksize: {BLOCKSIZE}")
print("Make noise into the aux input — you should see the bars move.\n")
print(f"{'CH0 (LEFT)':<12} {'':<{BAR_W+10}}  {'CH1 (RIGHT)':<12}")
print("-" * 80)

peak_l = peak_r = 0.0
PEAK_DECAY = 0.97

try:
    with sd.InputStream(device=DEVICE, channels=2, samplerate=SR, blocksize=BLOCKSIZE) as stream:
        while True:
            data, overflowed = stream.read(BLOCKSIZE)
            ch0 = np.abs(data[:, 0].astype(np.float32))
            ch1 = np.abs(data[:, 1].astype(np.float32))
            rms_l = float(np.sqrt(np.mean(ch0**2)))
            rms_r = float(np.sqrt(np.mean(ch1**2)))
            peak_l = max(float(np.max(ch0)), peak_l * PEAK_DECAY)
            peak_r = max(float(np.max(ch1)), peak_r * PEAK_DECAY)

            flag = " OVERFLOW" if overflowed else ""
            print(
                f"\rL: {bar(min(peak_l, 1.0))}  {db(peak_l):6.1f}dBFS  "
                f"R: {bar(min(peak_r, 1.0))}  {db(peak_r):6.1f}dBFS{flag}   ",
                end="", flush=True
            )
except sd.PortAudioError as e:
    print(f"\n\nPortAudio error: {e}")
    print("\nAll available input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0:
            print(f"  [{i}] {d['name']}  ({d['max_input_channels']} ch)")
    print(f"\nTry re-running with a different DEVICE index above.")
except KeyboardInterrupt:
    print("\n\nDone.")
