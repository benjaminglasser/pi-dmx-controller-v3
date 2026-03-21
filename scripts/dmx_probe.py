#!/usr/bin/env python3
"""
DMX hardware probe: tries several frame lengths and break styles.
Stop pi-dmx first: sudo systemctl stop pi-dmx.service

Chauvet @ address 1, channels 1–4. Watch for ANY flicker during each 4s ON phase.
If nothing ever responds, suspect transceiver power, DE/RE, A/B swap, or wrong UART pin.
"""
import os
import sys
import time
import fcntl

import serial

DEVICE = os.environ.get("DMX_UART_DEVICE", "/dev/serial0")
BAUD = 250000
TIOCSBRK = 0x5427
TIOCCBRK = 0x5428

CHANS = int(os.environ.get("DMX_TEST_CHANS", "4"))


def send_break(ser: serial.Serial, use_ioctl: bool) -> None:
    fd = ser.fileno()
    if use_ioctl:
        try:
            fcntl.ioctl(fd, TIOCSBRK, 0)
            time.sleep(0.000092)
            fcntl.ioctl(fd, TIOCCBRK, 0)
            time.sleep(0.000012)
            return
        except OSError:
            pass
    ser.baudrate = 9600
    ser.write(b"\x00")
    ser.flush()
    time.sleep(0.001)
    ser.baudrate = BAUD


def send_frame(ser: serial.Serial, ch_values: list[int], min_slots: int, use_ioctl_break: bool) -> None:
    n = len(ch_values)
    pad = max(n, min_slots)
    buf = bytearray(1 + pad)
    buf[0] = 0x00
    for i in range(n):
        buf[1 + i] = max(0, min(255, int(ch_values[i])))
    send_break(ser, use_ioctl_break)
    ser.write(buf)
    ser.flush()


def main() -> None:
    vals_on = [255] * CHANS
    vals_off = [0] * CHANS

    try:
        ser = serial.Serial(
            port=DEVICE,
            baudrate=BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
            write_timeout=0,
            exclusive=True,
        )
    except serial.SerialException as e:
        print(f"Cannot open {DEVICE}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Device: {DEVICE}  channels tested: 1..{CHANS}  dimmer address should be 1\n")
    print("Each line = 4 seconds FULL ON, then dim. Note if ANY channel flickers.\n")

    try:
        for min_sl in (24, 64, 128, 256, 512):
            for brk in (True, False):
                tag = f"min_slots={min_sl}  break={'ioctl' if brk else 'baud9600'}"
                print(f">>> {tag}")
                t_end = time.time() + 4.0
                while time.time() < t_end:
                    send_frame(ser, vals_on, min_sl, brk)
                    time.sleep(0.04)
                for _ in range(25):
                    send_frame(ser, vals_off, min_sl, brk)
                    time.sleep(0.04)
                print("    (off)\n")
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        try:
            send_frame(ser, vals_off, 512, True)
        except Exception:
            pass
        ser.close()
        print("Done. Send zeros and closed port.")


if __name__ == "__main__":
    main()
