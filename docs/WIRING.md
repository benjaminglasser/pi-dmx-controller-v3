# Wiring Guide — Pi DMX Controller V3 PCB

All connections for the current V3 PCB: SSD1322 OLED, MCP23017 encoder expander, direct GPIO buttons, RS485 DMX UART, and audio.

---

## Raspberry Pi GPIO Pinout (relevant pins)

| BCM | Physical | Function |
|-----|----------|---------|
| 2 | 3 | I2C SDA — MCP23017 |
| 3 | 5 | I2C SCL — MCP23017 |
| 7 | 26 | Extra button (active low, internal pull-up) |
| 8 | 24 | SPI CE0 — OLED chip select |
| 9 | 21 | SPI MISO |
| 10 | 19 | SPI MOSI |
| 11 | 23 | SPI SCLK |
| 14 | 8 | UART TX → RS485 DI |
| 15 | 10 | UART RX ← RS485 RO |
| 17 | 11 | Encoder 5 push button (active low) |
| 23 | 16 | OLED DC |
| 24 | 18 | OLED RST |
| 25 | 22 | Reset button (active low, internal pull-up) |

---

## OLED Display — EastRising 3.2" SSD1322 (256×64 SPI)

| OLED Pin | Connects To | Notes |
|----------|-------------|-------|
| VCC | 3.3V (pin 1) | |
| GND | GND (pin 6) | |
| SCK | BCM 11 / pin 23 | SPI clock |
| SDA (MOSI) | BCM 10 / pin 19 | SPI data |
| CS | BCM 8 / pin 24 | CE0 |
| DC | BCM 23 / pin 16 | Data/command select |
| RST | BCM 24 / pin 18 | Reset |

The display is on **CE0** (`spidev0.0`). `config/boot/config.txt` enables SPI with a single chip select so GPIO8 is available.

---

## Rotary Encoders — via MCP23017 I2C Expander (addr 0x20)

Connect the MCP23017 to the Pi's I2C bus:

| MCP23017 Pin | Connects To |
|--------------|-------------|
| VDD | 3.3V |
| GND | GND |
| SDA | BCM 2 / pin 3 |
| SCL | BCM 3 / pin 5 |
| A0, A1, A2 | GND (sets I2C addr 0x20) |

Encoder wiring on the MCP23017:

| Encoder | Function | CLK Pin | DT Pin | SW Pin |
|---------|----------|---------|--------|--------|
| E1 | Submenu / column select | GPB0 | GPB1 | GPB2 |
| E2 | Parameter A | GPB3 | GPB4 | GPB5 |
| E3 | Parameter B | GPB6 | GPA0 | GPA1 |
| E4 | Parameter C | GPA2 | GPA3 | GPA4 |
| E5 | Brightness (rotation) | GPA5 | GPA6 | — |

Encoder 5 push button is wired **directly to Pi GPIO 17** (not through MCP23017).

Each encoder: common pin → GND. Internal pull-ups enabled in software.

---

## Buttons (Direct GPIO)

| Button | BCM | Physical | Notes |
|--------|-----|----------|-------|
| Encoder 5 push | 17 | pin 11 | Active low, internal pull-up |
| Reset | 25 | pin 22 | Active low, internal pull-up — restores default params |
| Extra | 7 | pin 26 | Active low, internal pull-up |

---

## DMX Output — GPIO UART → RS485 → XLR

The app sends DMX directly over `/dev/serial0` (UART on GPIO 14/15) through an RS485 transceiver (e.g. DMXKing, MAX485 module, or similar).

| Pi Pin | RS485 Module Pin | Notes |
|--------|-----------------|-------|
| BCM 14 / pin 8 | DI (data in) | UART TX |
| BCM 15 / pin 10 | RO (receiver out) | UART RX |
| 3.3V or 5V | VCC | Check module voltage |
| GND | GND | Common ground |
| DE + RE | 3.3V or tied together | Always-transmit mode |

RS485 A/B → XLR pin 3/2 (pin 1 = shield/ground).

> Keep ground common between Pi, RS485 module, and DMX fixtures.  
> Use shielded cable for the RS485 run.  
> If fixtures never respond: check A/B polarity (swap if needed), verify DE/RE are both high.

---

## Audio Input

**USB interface (default):** plug in any class-compliant USB audio interface or microphone. Use `arecord -l` to confirm it appears.

**HiFiBerry DAC+ ADC (optional HAT):** connects via I2S (standard 40-pin header). Follow HiFiBerry's mounting guide. Requires `dtoverlay=hifiberry-dacplusadc` in `config.txt` and `config/alsa/asound.conf` copied to `/etc/asound.conf` — see QUICKSTART Step 3.

---

## Tips

- All grounds must be common (Pi, RS485 module, encoders, OLED, audio interface).
- Use short wires for I2C and SPI runs; keep RS485 away from analog lines.
- Power down the Pi before rewiring GPIO pins.
- If encoders feel backwards, swap CLK/DT on that encoder's MCP23017 pins.
