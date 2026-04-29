#!/usr/bin/env python3
"""Simple standalone audio meter for the pi-dmx setup.

Uses sounddevice/PortAudio (same path as dmx_audio_react.py) so a successful
read here means the main app should also see audio. Prints a live stereo
peak/RMS meter in plain text. Press Ctrl+C to stop.

Usage:
    sudo .venv/bin/python scripts/audio_test.py
    sudo .venv/bin/python scripts/audio_test.py --device 0
    sudo .venv/bin/python scripts/audio_test.py --rate 44100 --duration 10
"""
from __future__ import annotations

import argparse
import math
import sys
import time

import numpy as np
import sounddevice as sd


def _db(x: float) -> str:
    if x <= 0.0:
        return "  -inf dBFS"
    return f"{20 * math.log10(x):6.1f} dBFS"


def _bar(level_db: float, width: int = 30, floor_db: float = -60.0) -> str:
    """Render a horizontal meter bar from level_db (dBFS) to width chars."""
    if level_db <= floor_db:
        n = 0
    elif level_db >= 0.0:
        n = width
    else:
        n = int(round(width * (1 - level_db / floor_db)))
    return "#" * n + "-" * (width - n)


def find_device(name_hint: str | None) -> int:
    """Return PortAudio device index matching name_hint (default: first card with 'wm8960')."""
    devices = sd.query_devices()
    if name_hint:
        for i, d in enumerate(devices):
            if name_hint.lower() in d["name"].lower() and d["max_input_channels"] > 0:
                return i
        raise SystemExit(f"No input device matching {name_hint!r}")
    for i, d in enumerate(devices):
        if "wm8960" in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and "hdmi" not in d["name"].lower():
            return i
    raise SystemExit("No suitable input device found")


def list_devices() -> None:
    print("Available input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  [{i:>2}] in={d['max_input_channels']:>2} sr={int(d['default_samplerate']):>6}  {d['name']!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone audio meter for pi-dmx")
    ap.add_argument("--device", help="device index (int) or substring of name")
    ap.add_argument("--rate", type=int, default=48000, help="sample rate (default 48000)")
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until Ctrl+C")
    ap.add_argument("--list", action="store_true", help="list input devices and exit")
    args = ap.parse_args()

    if args.list:
        list_devices()
        return 0

    if args.device is None:
        dev = find_device(None)
    else:
        try:
            dev = int(args.device)
        except ValueError:
            dev = find_device(args.device)

    info = sd.query_devices(dev)
    print(f"Using device [{dev}] {info['name']!r}")
    print(f"  channels={args.channels}  rate={args.rate}  dtype=int16")
    print()
    print("  Live meter (peak hold 1.5s)  -- Ctrl+C to stop")
    print("  L: |-30|----|-15|----|0    R: |-30|----|-15|----|0")
    print()

    peak_l = peak_r = 0.0
    held_l = held_r = -90.0
    held_l_t = held_r_t = 0.0
    total_l = total_r = 0.0
    n_blocks = 0

    def cb(indata: np.ndarray, frames: int, t, status) -> None:
        nonlocal peak_l, peak_r, total_l, total_r, n_blocks
        if status:
            print(f"  [stream status: {status}]")
        a = indata.astype(np.float32) / 32768.0
        L = a[:, 0]
        R = a[:, 1] if a.shape[1] > 1 else a[:, 0]
        peak_l = float(np.max(np.abs(L)))
        peak_r = float(np.max(np.abs(R)))
        rms_l = float(np.sqrt(np.mean(L * L)))
        rms_r = float(np.sqrt(np.mean(R * R)))
        total_l += peak_l
        total_r += peak_r
        n_blocks += 1
        return rms_l, rms_r  # (unused; kept for clarity)

    try:
        with sd.InputStream(
            device=dev, channels=args.channels, samplerate=args.rate,
            dtype="int16", blocksize=int(args.rate * 0.1), callback=cb,
        ):
            t0 = time.time()
            while True:
                if args.duration > 0 and time.time() - t0 > args.duration:
                    break
                pl_db = 20 * math.log10(peak_l) if peak_l > 0 else -90.0
                pr_db = 20 * math.log10(peak_r) if peak_r > 0 else -90.0
                now = time.time()
                if pl_db > held_l or now - held_l_t > 1.5:
                    held_l, held_l_t = pl_db, now
                if pr_db > held_r or now - held_r_t > 1.5:
                    held_r, held_r_t = pr_db, now
                bar_l = _bar(pl_db)
                bar_r = _bar(pr_db)
                clip_l = "CLIP" if peak_l >= 0.999 else "    "
                clip_r = "CLIP" if peak_r >= 0.999 else "    "
                sys.stdout.write(
                    f"\r  L [{bar_l}] {pl_db:6.1f} (hold {held_l:6.1f}) {clip_l}"
                    f"   R [{bar_r}] {pr_db:6.1f} (hold {held_r:6.1f}) {clip_r}"
                )
                sys.stdout.flush()
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    print()
    print()
    if n_blocks:
        avg_l = total_l / n_blocks
        avg_r = total_r / n_blocks
        print(f"  Summary over {n_blocks} block(s):")
        print(f"    avg block-peak L = {_db(avg_l)}    R = {_db(avg_r)}")
        if avg_l < 1e-6 and avg_r < 1e-6:
            print()
            print("  >>> ZERO signal on both channels. Likely codec lockup or wrong device.")
            print("  >>> Try: sudo /home/pi/pi-dmx-controller-v2/scripts/wm8960-rebind.sh")
            print("  >>>      then re-run this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
