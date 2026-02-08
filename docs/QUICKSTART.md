# Quick Start Guide - DMX Audio-Reactive Light Controller

This guide walks you through building a complete audio-reactive DMX lighting controller from scratch. Follow these steps in order and you'll have a working system.

---

## Table of Contents

1. [Parts List](#1-parts-list)
2. [Raspberry Pi Setup](#2-raspberry-pi-setup)
3. [Hardware Wiring](#3-hardware-wiring)
4. [Software Installation](#4-software-installation)
5. [Testing](#5-testing)
6. [Usage](#6-usage)
7. [Development Mode](#7-development-mode)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Parts List

### Required Components

| Component | Quantity | Purpose | Notes |
|-----------|----------|---------|-------|
| Raspberry Pi 4 or 5 | 1 | Main controller | 2GB+ RAM recommended |
| MicroSD Card | 1 | OS storage | 16GB+ recommended |
| Power Supply | 1 | Pi power | 5V 3A USB-C |
| SPI OLED Display 128x64 | 1 | UI display | SSD1309 controller (Waveshare 2.42") |
| Rotary Encoders with Push Button | 5 | Parameter controls | KY-040 or similar |
| MAX485 RS485 Transceiver | 1 | DMX output | Or similar RS485 module |
| 3-pin or 5-pin XLR Connector | 1 | DMX output | Female for fixture connection |
| Push Button | 1 | Reset button | Momentary, normally open |
| Jumper Wires | ~40 | Connections | Male-female and male-male |
| Breadboard or PCB | 1 | Mounting | For prototyping or permanent install |

### Audio Input (choose one)

| Option | Notes |
|--------|-------|
| HiFiBerry DAC+ADC | Best quality, stacks on Pi GPIO |
| USB Audio Interface | Scarlett, Behringer, etc. |
| USB Sound Card | Budget option with line input |

### DMX Fixtures

Any DMX512-compatible fixtures. The controller supports 4-24 channels.

---

## 2. Raspberry Pi Setup

### 2.1 Flash the Operating System

1. Download and install [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Insert your MicroSD card
3. Open Raspberry Pi Imager and select:
   - **Device**: Your Pi model
   - **OS**: Raspberry Pi OS (32-bit) - Bookworm
   - **Storage**: Your MicroSD card
4. Click the gear icon (⚙️) for advanced options:
   - Enable SSH
   - Set username: `benglasser` (or your preferred username)
   - Set password
   - Configure WiFi (optional but recommended)
   - Set locale/timezone
5. Click **Write** and wait for completion

### 2.2 First Boot

1. Insert the SD card into your Pi
2. Connect power
3. Wait 2-3 minutes for first boot
4. SSH into your Pi:
   ```bash
   ssh benglasser@raspberrypi.local
   ```
   (Replace `benglasser` with your username)

### 2.3 Initial System Update

Run these commands:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git
sudo reboot
```

Wait for reboot, then SSH back in.

---

## 3. Hardware Wiring

### GPIO Pin Reference (BCM Numbering)

The Raspberry Pi uses BCM (Broadcom) pin numbering. Here's the complete pinout:

```
                    3.3V  (1) (2)  5V
          (SDA) GPIO 2  (3) (4)  5V
          (SCL) GPIO 3  (5) (6)  GND
               GPIO 4  (7) (8)  GPIO 14 (TXD) --> DMX
                  GND  (9) (10) GPIO 15 (RXD)
    [Enc1 CLK] GPIO 17 (11) (12) GPIO 18 [Enc5 DT]
    [Enc1 DT]  GPIO 27 (13) (14) GND
    [Enc1 SW]  GPIO 22 (15) (16) GPIO 23 [Enc3 SW]
                 3.3V (17) (18) GPIO 24 [OLED DC]
   [SPI MOSI] GPIO 10 (19) (20) GND
   [SPI MISO]  GPIO 9 (21) (22) GPIO 25 [Reset Button]
   [SPI SCLK] GPIO 11 (23) (24) GPIO 8  [Enc5 SW]
                  GND (25) (26) GPIO 7  [OLED CS/CE1]
               GPIO 0 (27) (28) GPIO 1
    [Enc1 CLK] GPIO 5 (29) (30) GND
    [Enc1 DT]  GPIO 6 (31) (32) GPIO 12 [OLED RST]
    [Enc1 SW] GPIO 13 (33) (34) GND
   [Enc3 CLK] GPIO 19 (35) (36) GPIO 16 [Enc4 CLK]
   [Enc3 DT]  GPIO 26 (37) (38) GPIO 20 [Enc4 DT]
                  GND (39) (40) GPIO 21 [Enc4 SW]
```

### 3.1 SPI OLED Display (128x64 SSD1309)

| OLED Pin | Connect To | Pi Physical Pin |
|----------|------------|-----------------|
| VCC | 3.3V | Pin 1 |
| GND | GND | Pin 6 |
| DIN (MOSI) | GPIO 10 | Pin 19 |
| CLK (SCLK) | GPIO 11 | Pin 23 |
| CS | GPIO 7 (CE1) | Pin 26 |
| DC | GPIO 24 | Pin 18 |
| RST | GPIO 12 | Pin 32 |

### 3.2 Rotary Encoders

Each encoder has 5 pins: CLK, DT, SW (button), VCC (+), GND (-)

**Encoder 1 - Page Selection**
| Encoder Pin | Connect To | Pi Physical Pin |
|-------------|------------|-----------------|
| CLK | GPIO 5 | Pin 29 |
| DT | GPIO 6 | Pin 31 |
| SW | GPIO 13 | Pin 33 |
| + | 3.3V | Pin 1 or 17 |
| GND | GND | Pin 6, 9, 14, etc. |

**Encoder 2 - Parameter A (Frequency/Preset)**
| Encoder Pin | Connect To | Pi Physical Pin |
|-------------|------------|-----------------|
| CLK | GPIO 17 | Pin 11 |
| DT | GPIO 27 | Pin 13 |
| SW | GPIO 22 | Pin 15 |
| + | 3.3V | Pin 1 or 17 |
| GND | GND | Any GND pin |

**Encoder 3 - Parameter B (Threshold/Beats)**
| Encoder Pin | Connect To | Pi Physical Pin |
|-------------|------------|-----------------|
| CLK | GPIO 19 | Pin 35 |
| DT | GPIO 26 | Pin 37 |
| SW | GPIO 23 | Pin 16 |
| + | 3.3V | Pin 1 or 17 |
| GND | GND | Any GND pin |

**Encoder 4 - Parameter C (Release/Mode)**
| Encoder Pin | Connect To | Pi Physical Pin |
|-------------|------------|-----------------|
| CLK | GPIO 16 | Pin 36 |
| DT | GPIO 20 | Pin 38 |
| SW | GPIO 21 | Pin 40 |
| + | 3.3V | Pin 1 or 17 |
| GND | GND | Any GND pin |

**Encoder 5 - Brightness**
| Encoder Pin | Connect To | Pi Physical Pin |
|-------------|------------|-----------------|
| CLK | GPIO 4 | Pin 7 |
| DT | GPIO 18 | Pin 12 |
| SW | GPIO 8 | Pin 24 |
| + | 3.3V | Pin 1 or 17 |
| GND | GND | Any GND pin |

### 3.3 Reset Button

| Button Pin | Connect To | Pi Physical Pin |
|------------|------------|-----------------|
| Terminal 1 | GPIO 25 | Pin 22 |
| Terminal 2 | GND | Any GND pin |

The Pi uses internal pull-up resistors. Pressing the button pulls GPIO 25 LOW.

### 3.4 DMX Output (RS485)

**MAX485 Module Wiring:**

| MAX485 Pin | Connect To | Notes |
|------------|------------|-------|
| VCC | 3.3V or 5V | Check module specs |
| GND | GND | Common ground |
| DI (Data In) | GPIO 14 (TXD) | Pi UART TX |
| RO (Receive Out) | Not connected | Optional |
| DE (Driver Enable) | 3.3V | Tie high for TX-only |
| RE (Receive Enable) | 3.3V | Tie high (inverted) |
| A (D+) | XLR Pin 3 | DMX Data+ |
| B (D-) | XLR Pin 2 | DMX Data- |

**XLR Connector Pinout (looking at solder side of female connector):**

```
    Pin 1 = Ground (shield)
    Pin 2 = Data- (cold)
    Pin 3 = Data+ (hot)
```

### 3.5 Audio Input

**Option A: HiFiBerry DAC+ADC**
- Simply stack the HiFiBerry board on top of the Pi's GPIO header
- No additional wiring needed
- Add to `/boot/firmware/config.txt`:
  ```
  dtparam=audio=off
  dtoverlay=hifiberry-dacplusadc
  ```

**Option B: USB Audio Interface**
- Plug into any USB port
- Will be auto-detected

### 3.6 Wiring Diagram Summary

```
                           ┌─────────────────────┐
                           │   Raspberry Pi 4    │
                           │                     │
  ┌──────────┐             │  GPIO 5,6,13 ◄──────┼─── Encoder 1 (Page)
  │  OLED    │             │  GPIO 17,27,22 ◄────┼─── Encoder 2 (Freq)
  │ SSD1309  │◄────────────┼─ GPIO 7,10,11,12,24 │
  │ 128x64   │             │  GPIO 19,26,23 ◄────┼─── Encoder 3 (Thresh)
  └──────────┘             │  GPIO 16,20,21 ◄────┼─── Encoder 4 (Release)
                           │  GPIO 4,18,8 ◄──────┼─── Encoder 5 (Bright)
  ┌──────────┐             │                     │
  │  MAX485  │◄────────────┼─ GPIO 14 (TXD)      │
  │  RS485   │             │                     │
  └────┬─────┘             │  GPIO 25 ◄──────────┼─── Reset Button
       │                   │                     │
       ▼                   │  USB ◄──────────────┼─── Audio Interface
  ┌──────────┐             │                     │
  │   XLR    │             └─────────────────────┘
  │  (DMX)   │
  └────┬─────┘
       │
       ▼
  DMX Fixtures
```

---

## 4. Software Installation

### 4.1 Clone the Repository

```bash
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2
```

### 4.2 Run the Bootstrap Script

```bash
bash scripts/bootstrap_pi.sh
```

This script will:
- Update system packages
- Install Python, audio libraries, and dependencies
- Enable SPI and I2C interfaces
- Configure HiFiBerry overlay (if applicable)
- Create Python virtual environment
- Install all Python packages

**Wait for completion, then reboot:**

```bash
sudo reboot
```

### 4.3 Install Startup Services

After reboot, SSH back in and run:

```bash
cd ~/pi-dmx-controller-v2
sudo scripts/install_services.sh
```

This installs:
- **oled_splash.service** - Shows logo on boot (4 seconds)
- **dmx_audio_react.service** - Main controller (auto-starts)
- **dmx-dev** command - Development mode toggle

### 4.4 Verify Installation

```bash
# Check services are enabled
systemctl is-enabled oled_splash.service
systemctl is-enabled dmx_audio_react.service

# Check dmx-dev command
dmx-dev status
```

---

## 5. Testing

### 5.1 Test OLED Display

```bash
cd ~/pi-dmx-controller-v2
source .venv/bin/activate
python utils/oled_boot.py
```

You should see the logo on the OLED for 4 seconds.

### 5.2 Test Audio Input

```bash
# List audio devices
arecord -l

# Test recording (Ctrl+C to stop)
arecord -d 5 -f cd test.wav
aplay test.wav
rm test.wav
```

### 5.3 Test DMX Output

```bash
cd ~/pi-dmx-controller-v2
source .venv/bin/activate
python -c "
import serial
import time

# Open UART
ser = serial.Serial('/dev/serial0', 250000, stopbits=2)

# Send DMX break
ser.break_condition = True
time.sleep(0.001)
ser.break_condition = False
time.sleep(0.001)

# Send DMX data (start code + 4 channels at full)
ser.write(bytes([0, 255, 255, 255, 255]))
print('DMX sent: channels 1-4 at 255')
ser.close()
"
```

Your DMX fixtures should respond.

### 5.4 Full System Test

```bash
# Start the service
sudo systemctl start dmx_audio_react.service

# Check it's running
sudo systemctl status dmx_audio_react.service

# View live logs
sudo journalctl -u dmx_audio_react.service -f
```

Play some music into your audio input - you should see the FFT display respond and DMX output trigger!

---

## 6. Usage

### 6.1 Boot Sequence

1. Power on the Pi
2. OLED shows logo for 4 seconds
3. DMX controller starts automatically
4. OLED shows FFT spectrum and parameter display

### 6.2 Encoder Controls

| Encoder | Turn | Press |
|---------|------|-------|
| **Enc 1** | Change page | (reserved) |
| **Enc 2** | Adjust param A | Toggle alternate param |
| **Enc 3** | Adjust param B | Toggle alternate param |
| **Enc 4** | Adjust param C | Toggle alternate param |
| **Enc 5** | Adjust brightness | Toggle brightness mode |

### 6.3 Pages

| Page | Enc 2 | Enc 3 | Enc 4 |
|------|-------|-------|-------|
| **HOME** | Frequency (press: Q) | Threshold (press: Mode) | Release (press: Mode) |
| **PRE** | Preset | Program | Beats |
| **SET** | Reset | Input Gain | Output Mode |

### 6.4 Presets

| Preset | Frequency | Use Case |
|--------|-----------|----------|
| LOW | 120 Hz | Bass, kick drums |
| MID | 1000 Hz | Vocals, snare |
| HIGH | 5000 Hz | Hi-hats, cymbals |
| USR 1-3 | Custom | Your saved settings |

**Save a preset:** On PRE page, hold Encoder 2 for 3 seconds while on a USR slot.

### 6.5 Programs

| Program | Behavior |
|---------|----------|
| ALL | All channels trigger together |
| CHASE | Sequential single channel |
| GROUPS | First half vs second half |
| ODD/EVEN | Odd vs even channels |
| RANDOM | Random channel each trigger |
| AMBIENT | Non-reactive random fading |

---

## 7. Development Mode

### Enter Development Mode

To stop auto-start and work on the code:

```bash
dmx-dev disable
```

This:
- Stops the running service
- Prevents auto-start on boot
- Lets you run manually

### Run Manually

```bash
cd ~/pi-dmx-controller-v2
source .venv/bin/activate
python dmx_audio_react.py
```

Press `q` to exit.

### Exit Development Mode

```bash
dmx-dev enable
```

Service will auto-start on next boot. To start now:

```bash
sudo systemctl start dmx_audio_react.service
```

### Check Status

```bash
dmx-dev status
```

---

## 8. Troubleshooting

### OLED Not Displaying

1. Check SPI is enabled:
   ```bash
   ls /dev/spidev*
   # Should show: /dev/spidev0.0  /dev/spidev0.1
   ```

2. Check wiring (especially CS, DC, RST pins)

3. Test manually:
   ```bash
   cd ~/pi-dmx-controller-v2
   source .venv/bin/activate
   python utils/oled_boot.py
   ```

### No Audio Input Detected

1. List devices:
   ```bash
   arecord -l
   ```

2. If using HiFiBerry, check config:
   ```bash
   grep hifiberry /boot/firmware/config.txt
   ```

3. For USB audio, try different USB port

### DMX Not Working

1. Check UART is available:
   ```bash
   ls /dev/serial0
   ```

2. Verify RS485 wiring (DE and RE should be HIGH)

3. Check XLR polarity (swap pins 2 and 3 if needed)

4. Test with simple script (see section 5.3)

### Service Won't Start

1. Check logs:
   ```bash
   sudo journalctl -u dmx_audio_react.service -n 50
   ```

2. Common issues:
   - No audio device: Connect audio interface
   - Permission error: Check user in service file
   - Import error: Reinstall requirements

### Encoders Not Responding

1. Check GPIO connections
2. Verify encoder common goes to GND
3. Test individual encoder:
   ```bash
   python -c "
   import RPi.GPIO as GPIO
   GPIO.setmode(GPIO.BCM)
   GPIO.setup(5, GPIO.IN, pull_up_down=GPIO.PUD_UP)
   print('Turn encoder 1...')
   import time
   for i in range(20):
       print(GPIO.input(5))
       time.sleep(0.1)
   "
   ```

### Reset to Factory

If things go wrong, you can always:

```bash
cd ~/pi-dmx-controller-v2
rm .dmx_config  # Reset saved settings
sudo systemctl restart dmx_audio_react.service
```

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────┐
│              DMX Audio-Reactive Controller              │
├─────────────────────────────────────────────────────────┤
│  ENCODERS:                                              │
│    Enc 1: Page select                                   │
│    Enc 2: Freq/Preset (press: Q/ThreshMode)            │
│    Enc 3: Thresh/Program (press: ThreshMode)           │
│    Enc 4: Release/Beats (press: ReleaseMode)           │
│    Enc 5: Brightness                                    │
│    Reset: Short press = reset to preset defaults        │
├─────────────────────────────────────────────────────────┤
│  COMMANDS:                                              │
│    dmx-dev disable  - Enter development mode            │
│    dmx-dev enable   - Exit development mode             │
│    dmx-dev status   - Check current mode                │
├─────────────────────────────────────────────────────────┤
│  SERVICE:                                               │
│    sudo systemctl start dmx_audio_react.service         │
│    sudo systemctl stop dmx_audio_react.service          │
│    sudo journalctl -u dmx_audio_react.service -f        │
└─────────────────────────────────────────────────────────┘
```

---

## Support

- GitHub: https://github.com/benjaminglasser/pi-dmx-controller-v2
- Issues: https://github.com/benjaminglasser/pi-dmx-controller-v2/issues

---

© 2025 Ben Glasser | MIT License
