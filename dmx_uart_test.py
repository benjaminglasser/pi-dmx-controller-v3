#!/usr/bin/env python3
"""Hardware smoke test for GPIO UART DMX (e.g. Chauvet DMX-4). Stop pi-dmx first: sudo systemctl stop pi-dmx.service

Cycles channel 1..4 full ON (others 0) five times per second. Watch the dimmer pack.
Uses same break / padding / flush as dmx_audio_react UartDmx.
"""
import os
import time
import fcntl

DMX_UART_DEVICE = os.environ.get("DMX_UART_DEVICE", "/dev/serial0")
DMX_UART_BAUD = 250000
CHANS = int(os.environ.get("DMX_TEST_CHANS", "4"))
MIN_SLOTS = max(CHANS, int(os.environ.get("DMX_UART_MIN_SLOTS", "256")))
TIOCSBRK = 0x5427
TIOCCBRK = 0x5428

import serial

ser = serial.Serial(
    port=DMX_UART_DEVICE,
    baudrate=DMX_UART_BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_TWO,
    timeout=0,
    write_timeout=0,
    exclusive=True,
)


def send_break():
    fd = ser.fileno()
    try:
        fcntl.ioctl(fd, TIOCSBRK, 0)
        time.sleep(0.000092)
        fcntl.ioctl(fd, TIOCCBRK, 0)
        time.sleep(0.000012)
    except OSError:
        ser.baudrate = 9600
        ser.write(b"\x00")
        ser.flush()
        time.sleep(0.001)
        ser.baudrate = DMX_UART_BAUD


def send(vals):
    n = len(vals)
    pad = max(n, MIN_SLOTS)
    buf = bytearray(1 + pad)
    buf[0] = 0x00
    for i in range(n):
        buf[1 + i] = max(0, min(255, int(vals[i])))
    send_break()
    ser.write(buf)
    ser.flush()


print(f"DMX UART test: {DMX_UART_DEVICE} min_slots={MIN_SLOTS} chans={CHANS}")
print("Chase 1..4 @ 5 Hz. Ctrl+C to stop. If no light: check DMX cable, A/B, dimmer address=1.")

i = 0
try:
    while True:
        vals = [0] * CHANS
        vals[i % CHANS] = 255
        send(vals)
        i += 1
        time.sleep(0.20)
except KeyboardInterrupt:
    print("\nStopping, sending zeros...")
finally:
    send([0] * CHANS)
    ser.close()
