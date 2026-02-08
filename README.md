# DMX Audio-Reactive Light Controller v2

A fully standalone, audio-reactive DMX lighting controller for Raspberry Pi with OLED display, rotary encoders, and UART-based DMX output.

## Features

### Audio Processing
- Real-time FFT spectrum analysis with 32 frequency bands
- Configurable center frequency (80Hz - 12kHz)
- Adjustable Q factor (bandwidth) with visual feedback
- Multiple threshold modes: Fixed and Adaptive
- Multiple release modes: Fixed, Reactive, Brightness-reactive, Both, Random

### DMX Output
- UART-based DMX512 output via RS485 transceiver (no USB adapter needed)
- 4-24 configurable DMX channels
- 6 program modes:
  - **ALL**: All channels trigger together
  - **CHASE**: Sequential single channel cycling
  - **GROUPS**: First half alternates with second half
  - **ODD/EVEN**: Odd channels alternate with even
  - **RANDOM**: Random channel each trigger
  - **AMBIENT**: Non-audio-reactive random fading

### Hardware Interface
- 128x64 SPI OLED display with live FFT visualization
- 5 rotary encoders with push buttons for parameter control
- Multi-page UI: HOME, PRE (presets), SET (settings), COLOR (DMX mode only)
- 6 preset slots (3 built-in: LOW/MID/HIGH, 3 user-saveable)
- Persistent settings saved to config file

### Boot & Startup
- 5-second splash screen on OLED at boot
- Auto-start on power-up via systemd
- Development mode toggle (`dmx-dev` command)
- Fast boot optimizations

---

## Quick Start Guide

This guide will take you from a blank SD card to a fully working DMX controller.

### What You'll Need

| Component | Purpose |
|-----------|---------|
| Raspberry Pi 4 or 5 | Main computer |
| HiFiBerry DAC+ADC (or USB audio interface) | Audio input |
| MAX485 or similar RS485 transceiver | DMX output |
| SPI OLED 128x64 (SSD1309) | Display |
| 5x Rotary encoders with push buttons | Controls |
| 3-pin or 5-pin XLR connector | DMX output |
| DMX fixtures | Lights! |

---

## Step 1: Prepare the Raspberry Pi

### 1.1 Flash the OS

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Flash **Raspberry Pi OS Bookworm (32-bit)** to your SD card
3. In Imager settings, enable SSH and set your username/password
4. Insert SD card and boot the Pi

### 1.2 Initial System Setup

SSH into your Pi and run:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip
sudo reboot
```

---

## Step 2: Clone and Install

### 2.1 Clone the Repository

```bash
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2
```

### 2.2 Run the Bootstrap Script

```bash
bash scripts/bootstrap_pi.sh
sudo reboot
```

The bootstrap script will:
- Install all required system packages
- Enable SPI and I2C interfaces
- Configure HiFiBerry DAC+ADC overlay (if using)
- Create Python virtual environment
- Install Python dependencies
- Configure OLA for DMX output

### 2.3 Install Startup Services

After reboot, install the systemd services:

```bash
cd ~/pi-dmx-controller-v2
sudo scripts/install_services.sh
```

This installs:
- **oled_splash.service** - Shows logo on OLED for 5 seconds at boot
- **dmx_audio_react.service** - Main DMX controller (auto-starts after splash)
- **dmx-dev** command - Toggle development mode

---

## Step 3: Hardware Wiring

### Pin Reference (BCM Numbering)

#### SPI OLED Display (128x64 SSD1309 on CE1)

| OLED Pin | Pi GPIO | Pi Pin |
|----------|---------|--------|
| VCC | 3.3V | Pin 1 |
| GND | GND | Pin 6 |
| DIN (MOSI) | BCM 10 | Pin 19 |
| CLK (SCLK) | BCM 11 | Pin 23 |
| CS | BCM 7 (CE1) | Pin 26 |
| DC | BCM 24 | Pin 18 |
| RST | BCM 12 | Pin 32 |

#### Rotary Encoders

| Encoder | Function | CLK | DT | SW (Button) |
|---------|----------|-----|-----|-------------|
| Encoder 1 | Page Selection | BCM 5 | BCM 6 | BCM 13 |
| Encoder 2 | Param A (Freq/Speed/Preset) | BCM 17 | BCM 27 | BCM 22 |
| Encoder 3 | Param B (Thresh/Beats) | BCM 19 | BCM 26 | BCM 23 |
| Encoder 4 | Param C (Release/Mode) | BCM 16 | BCM 20 | BCM 21 |
| Encoder 5 | Brightness | BCM 4 | BCM 18 | BCM 8 |

All encoder common pins connect to GND.

#### Reset Button

| Pin | Connection |
|-----|------------|
| BCM 25 | Button terminal 1 |
| GND | Button terminal 2 |

#### DMX Output (UART via RS485)

| Pi GPIO | RS485 Module | Notes |
|---------|--------------|-------|
| BCM 14 (TXD) | DI (Data In) | UART TX |
| BCM 15 (RXD) | RO (Receive Out) | Optional, not used |
| 3.3V | VCC | Power |
| GND | GND | Ground |
| 3.3V | DE + RE | Tied high for TX-only mode |

RS485 module output connects to DMX XLR:
- **A (D+)** → XLR Pin 3 (Data+)
- **B (D-)** → XLR Pin 2 (Data-)
- **GND** → XLR Pin 1 (Ground)

#### HiFiBerry DAC+ADC (if using)

The HiFiBerry connects via the 40-pin GPIO header. No additional wiring needed - just stack it on the Pi.

Add to `/boot/firmware/config.txt`:
```
dtparam=audio=off
dtoverlay=hifiberry-dacplusadc
dtparam=spi=on
```

---

## Step 4: Configuration

### 4.1 Audio Input

The controller auto-detects audio input devices. Priority order:
1. HiFiBerry DAC+ADC
2. USB audio interfaces (Scarlett, etc.)
3. Any device with input channels

To force a specific device, set environment variables:
```bash
export AUDIO_DEVICE=1
export AUDIO_DEVICE_NAME="USB Audio"
```

### 4.2 DMX Backend

Default is UART output. Options:

```bash
# UART (RS485 transceiver) - Default
export DMX_BACKEND=uart

# No DMX output (testing)
export DMX_BACKEND=null
```

### 4.3 Development Mode (No Hardware)

To run without hardware (for development/testing):

```bash
export DEV_NO_HW=1
python dmx_audio_react.py
```

---

## Step 5: Usage

### Boot Sequence

1. Power on the Pi
2. OLED shows splash logo for 5 seconds
3. DMX controller starts automatically
4. OLED shows FFT spectrum and controls

### UI Pages

Navigate pages with **Encoder 1** (turn to switch, press to confirm):

| Page | Encoder 2 | Encoder 3 | Encoder 4 |
|------|-----------|-----------|-----------|
| **HOME** | Frequency (press: Q) | Threshold (press: Thresh Mode) | Release (press: Release Mode) |
| **PRE** | Preset Select | Program Mode | Beat Cycles |
| **SET** | Reset Defaults | Input Gain | Output Mode / Channel Count |
| **COLOR** | Light Select | Hue/Temp | Saturation |

**Encoder 5** always controls brightness.

### Presets

| Preset | Center Freq | Description |
|--------|-------------|-------------|
| LOW | 120 Hz | Bass/kick drums |
| MID | 1000 Hz | Vocals/snare |
| HIGH | 5000 Hz | Hi-hats/cymbals |
| USR 1-3 | Custom | User-saveable slots |

**Saving Presets:** Long-press Encoder 2 on the PRE page to save current settings to a USR slot.

Each preset stores:
- Center frequency
- Q factor (bandwidth)
- Threshold level
- Release time
- Threshold mode (fixed/adapt)
- Release mode (fixed/react/bright/both/rand)

### Release Modes

| Mode | Behavior |
|------|----------|
| **fixed** | Constant release time from knob |
| **react** | Release time varies with trigger speed |
| **bright** | Brightness varies with signal level |
| **both** | Both reactive release and brightness |
| **rand** | Random release time each trigger |

### Threshold Modes

| Mode | Behavior |
|------|----------|
| **fixed** | Constant threshold from knob |
| **adapt** | Threshold adapts to signal level |

---

## Development Mode

### Enter Development Mode

Stop the auto-starting service and prevent it from restarting:

```bash
dmx-dev disable
```

This:
- Stops the running DMX service
- Creates a flag file that prevents auto-start on boot
- Lets you run the script manually for development

### Run Manually

```bash
cd ~/pi-dmx-controller-v2
source .venv/bin/activate
python dmx_audio_react.py
```

Press `q` to exit the TUI.

### Exit Development Mode

Re-enable auto-start:

```bash
dmx-dev enable
```

The service will auto-start on next boot. To start immediately:

```bash
sudo systemctl start dmx_audio_react.service
```

### Check Status

```bash
dmx-dev status
```

---

## Service Management

### Systemd Services

| Service | Purpose |
|---------|---------|
| `oled_splash.service` | Shows boot logo on OLED |
| `dmx_audio_react.service` | Main DMX controller |

### Common Commands

```bash
# Check service status
sudo systemctl status dmx_audio_react.service

# View logs
sudo journalctl -u dmx_audio_react.service -f

# Restart service
sudo systemctl restart dmx_audio_react.service

# Stop service
sudo systemctl stop dmx_audio_react.service

# Disable auto-start
sudo systemctl disable dmx_audio_react.service

# Re-enable auto-start
sudo systemctl enable dmx_audio_react.service
```

---

## File Structure

```
pi-dmx-controller-v2/
├── dmx_audio_react.py      # Main application
├── requirements.txt        # Python dependencies
├── .dmx_config            # Persistent settings (auto-created)
├── assets/
│   ├── csw.svg            # Boot splash logo (optional)
│   ├── logo.jpg           # Fallback boot logo
│   └── logo.BMP           # Alternative logo format
├── config/
│   └── firmware-config.snippet.txt  # /boot/firmware/config.txt additions
├── docs/
│   └── WIRING.md          # Detailed wiring guide
├── scripts/
│   ├── bootstrap_pi.sh    # Initial setup script
│   ├── install_services.sh # Install systemd services
│   ├── dmx-dev            # Development mode toggle
│   └── verify_universe.sh # OLA/DMX verification
├── systemd/
│   ├── dmx_audio_react.service  # Main service definition
│   └── oled_splash.service      # Boot splash service
├── utils/
│   └── oled_boot.py       # Boot splash screen script
└── tests/
    ├── hardware_test.py   # Hardware verification
    ├── mcp3008_test.py    # ADC test (legacy)
    └── vu_meter_test.py   # Audio level test
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No OLED display | Check SPI wiring, verify `dtparam=spi=on` in config.txt |
| No audio input | Run `arecord -l` to list devices, check HiFiBerry overlay |
| No DMX output | Check RS485 wiring, verify UART is enabled |
| Service won't start | Check logs: `journalctl -u dmx_audio_react.service -n 50` |
| Encoders not responding | Check GPIO wiring, verify pull-ups |
| Boot splash missing | Check `csw.svg` or `logo.jpg` in assets folder |
| "No suitable input device" | Connect audio interface or set `DEV_NO_HW=1` |

### Verify Hardware

```bash
# Check SPI devices
ls /dev/spidev*

# Check I2C devices
sudo i2cdetect -y 1

# Check audio devices
arecord -l

# Check UART
ls /dev/serial*

# Test DMX output
cd ~/pi-dmx-controller-v2
source .venv/bin/activate
python -c "from dmx_audio_react import *; dmx_send([255,0,0,0])"
```

---

## Boot Speed Optimizations

For fastest boot, add to `/boot/firmware/cmdline.txt`:
```
quiet loglevel=3
```

Disable unnecessary services:
```bash
sudo systemctl disable NetworkManager-wait-online.service
sudo systemctl disable bluetooth
sudo systemctl disable avahi-daemon
```

Expected boot times:
- OLED logo appears: ~2-3 seconds
- Controller fully running: ~8-10 seconds

---

## Custom Splash Screen

To use your own boot splash:

1. Create an SVG file sized for 128x32 pixels (or it will be scaled)
2. Save as `assets/csw.svg`
3. The system will use it automatically on next boot

Supported formats: SVG (preferred), JPG, PNG, BMP

---

## Dependencies

### System Packages
- python3, python3-venv, python3-pip
- alsa-utils, libportaudio2, portaudio19-dev
- python3-pil, i2c-tools

### Python Packages (requirements.txt)
- numpy - DSP/math
- sounddevice - Audio capture
- RPi.GPIO, spidev, gpiozero - Hardware control
- luma.oled, luma.core, pillow - OLED display
- pyserial - UART DMX output
- cairosvg - SVG splash screen support

---

## License

MIT © 2025 Ben Glasser
