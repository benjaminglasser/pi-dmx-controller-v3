import os, time

DMX_UART_DEVICE = os.environ.get("DMX_UART_DEVICE", "/dev/serial0")
DMX_UART_BAUD = 250000

CHANS = 4

import serial
ser = serial.Serial(
    port=DMX_UART_DEVICE,
    baudrate=DMX_UART_BAUD,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_TWO,
    timeout=0,
    write_timeout=0,
)

buf = bytearray(1 + CHANS)
buf[0] = 0x00  # start code

def send(vals):
    # best-effort BREAK
    ser.baudrate = 9600
    ser.write(b"\x00")
    ser.flush()
    time.sleep(0.001)
    ser.baudrate = DMX_UART_BAUD

    for i in range(CHANS):
        buf[1+i] = max(0, min(255, int(vals[i])))
    ser.write(buf)

print("Sending DMX chase on channels 1..4. Ctrl+C to stop.")
i = 0
try:
    while True:
        vals = [0,0,0,0]
        vals[i % CHANS] = 255
        send(vals)
        i += 1
        time.sleep(0.10)
except KeyboardInterrupt:
    pass
finally:
    send([0,0,0,0])
    ser.close()
