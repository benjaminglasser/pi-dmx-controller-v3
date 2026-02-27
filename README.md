# DMX Audio-Reactive Light Controller v2

A fully standalone, audio-reactive DMX lighting controller for Raspberry Pi with OLED display, rotary encoders, and UART-based DMX output.

---

## Disaster Recovery (SD Card Failure)

If your SD card fails and you need to restore the system from scratch, follow these steps:

### Quick Restore (For Agents)

```bash
# 1. Flash Raspberry Pi OS Bookworm (32-bit) to SD card
#    - Enable SSH in Raspberry Pi Imager settings
#    - Set username: benglasser
#    - Configure WiFi if needed

# 2. After first boot, SSH in and clone the repository
ssh benglasser@raspberrypi.local
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2

# 3. Run the full restore script
sudo scripts/full_restore.sh

# 4. Reboot
sudo reboot

# 5. Verify after reboot
arecord -l                                              # Should show HiFiBerry
sudo systemctl status dmx_audio_react.service           # Should be running
```

### Manual Restore Steps

If the automated script fails, restore manually:

```bash
# Install system packages
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip \
    alsa-utils libportaudio2 portaudio19-dev libsndfile1 \
    python3-pil i2c-tools libcairo2-dev

# Enable interfaces
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

# Install config files
sudo cp config/boot/config.txt /boot/firmware/config.txt
sudo cp config/alsa/asound.conf /etc/asound.conf

# Create Python environment
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt

# Install services
sudo scripts/install_services.sh

# Reboot
sudo reboot
```

---

## Features

### Audio Processing
- Real-time FFT spectrum analysis with 32 frequency bands
- TouchDesigner-style onset detection (Lag -> Slope -> Gain -> Trigger)
- Configurable center frequency (80Hz - 12kHz)
- Adjustable Q factor (bandwidth) with visual feedback
- Multiple threshold modes: Fixed and Adaptive
- Multiple release modes: Fixed, Reactive, Brightness-reactive, Both, Random

### 3-Band Onset Detection
- **LOW band**: 20-200 Hz (kick drums, bass)
- **MID band**: 200-2000 Hz (snare, vocals)
- **HIGH band**: 2000-20000 Hz (hi-hats, cymbals)
- Independent trigger thresholds per band
- TouchDesigner-style signal chain for clean triggers

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
- 4-second splash screen on OLED at boot
- Auto-start on power-up via systemd
- Development mode toggle (`dmx-dev` command)
- Fast boot optimizations

---

## Current System Configuration

This section documents the exact configuration of the working system.

### Hardware Setup
| Component | Model/Type | Connection |
|-----------|------------|------------|
| Raspberry Pi | Pi 4 or 5 | - |
| Audio HAT | HiFiBerry DAC+ ADC Pro | GPIO header (stacked) |
| OLED Display | SSD1309 128x64 SPI | SPI0 CE1 (GPIO 7) |
| DMX Output | MAX485 RS485 | UART TX (GPIO 14) |
| Encoders | 5x KY-040 rotary | Various GPIO pins |

### Boot Configuration (`/boot/firmware/config.txt`)

Key settings that MUST be present:

```ini
# Audio - HiFiBerry DAC+ ADC Pro (NOT "dacplusadc")
dtoverlay=hifiberry-dacplusadcpro
dtparam=audio=off

# UART for DMX (Bluetooth disabled to free UART)
enable_uart=1
dtoverlay=disable-bt

# SPI for OLED
dtparam=spi=on

# I2C for HiFiBerry codec
dtparam=i2c_arm=on

# Single SPI CS to free GPIO8 for encoder 5
dtoverlay=spi0-2cs,cs0_pin=0
```

### ALSA Configuration (`/etc/asound.conf`)

Sets HiFiBerry as default audio device:

```
pcm.!default {
    type hw
    card sndrpihifiberry
}
ctl.!default {
    type hw
    card sndrpihifiberry
}
```

### Systemd Services

| Service | Purpose | Status |
|---------|---------|--------|
| `oled_splash.service` | Boot splash on OLED | Enabled |
| `dmx_audio_react.service` | Main DMX controller | Enabled |

---

## Quick Start Guide

**For a comprehensive step-by-step guide, see [docs/QUICKSTART.md](docs/QUICKSTART.md)**

### What You'll Need

| Component | Purpose |
|-----------|---------|
| Raspberry Pi 4 or 5 | Main computer |
| HiFiBerry DAC+ ADC Pro | Audio input |
| MAX485 RS485 transceiver | DMX output |
| SPI OLED 128x64 (SSD1309) | Display |
| 5x Rotary encoders with push buttons | Controls |
| 3-pin or 5-pin XLR connector | DMX output |
| DMX fixtures | Lights! |

### Installation

```bash
# Clone repository
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2

# Run bootstrap (installs packages, creates venv, configures system)
bash scripts/bootstrap_pi.sh
sudo reboot

# After reboot, install services
cd ~/pi-dmx-controller-v2
sudo scripts/install_services.sh
```

---

## Hardware Wiring

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

---

## Configuration Files

All system configuration files are stored in the `config/` directory for easy restoration:

```
config/
├── boot/
│   └── config.txt      # /boot/firmware/config.txt
├── alsa/
│   └── asound.conf     # /etc/asound.conf
└── README-config.md    # Documentation
```

To restore config files manually:

```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
sudo cp config/alsa/asound.conf /etc/asound.conf
sudo reboot
```

---

## Signal Processing

### TouchDesigner-Style Onset Detection

The 3-band onset detector uses a signal chain modeled after TouchDesigner's proven approach:

```
Audio Input
    │
    ▼
┌─────────────────┐
│  FFT Analysis   │  Extract energy per frequency band
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Lag Stage     │  Asymmetric smoothing (fast attack, slow decay)
│                 │  Creates sawtooth envelope
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Slope Stage    │  Calculate derivative (rate of change)
│                 │  Only spikes on rising edges
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Gain + Limit   │  Normalize to 0-1 range
│                 │  Clean, consistent amplitude
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    Trigger      │  Threshold crossing detection
│                 │  Fires DMX output
└─────────────────┘
```

This produces clean triggers that only fire on actual transients (kick attacks, snare hits) rather than sustained bass or gradual energy changes.

---

## Usage

### Boot Sequence

1. Power on the Pi
2. OLED shows splash logo for 4 seconds
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
├── dmx_audio_react.py          # Main application
├── requirements.txt            # Python dependencies
├── .dmx_config                 # Persistent settings (auto-created)
├── assets/
│   ├── csw.svg                 # Boot splash logo (optional)
│   ├── logo.jpg                # Fallback boot logo
│   └── logo.BMP                # Alternative logo format
├── config/
│   ├── boot/
│   │   └── config.txt          # /boot/firmware/config.txt backup
│   ├── alsa/
│   │   └── asound.conf         # /etc/asound.conf backup
│   └── README-config.md        # Config documentation
├── docs/
│   ├── QUICKSTART.md           # Step-by-step setup guide
│   └── WIRING.md               # Detailed wiring guide
├── scripts/
│   ├── bootstrap_pi.sh         # Initial setup script
│   ├── full_restore.sh         # Complete system restore
│   ├── install_services.sh     # Install systemd services
│   ├── dmx-dev                 # Development mode toggle
│   └── verify_universe.sh      # OLA/DMX verification
├── systemd/
│   ├── dmx_audio_react.service # Main service definition
│   └── oled_splash.service     # Boot splash service
├── utils/
│   └── oled_boot.py            # Boot splash screen script
└── tests/
    ├── hardware_test.py        # Hardware verification
    ├── mcp3008_test.py         # ADC test (legacy)
    └── vu_meter_test.py        # Audio level test
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

### Common Issues

**HiFiBerry not detected:**
- Ensure overlay is `hifiberry-dacplusadcpro` (with "pro")
- Check `/etc/asound.conf` exists and references `sndrpihifiberry`
- Verify HAT is properly seated on GPIO header

**UART/DMX not working:**
- Bluetooth must be disabled (`dtoverlay=disable-bt`)
- Check `/dev/serial0` exists
- Verify RS485 DE/RE pins are tied HIGH

**Encoder 5 button not working:**
- Requires `dtoverlay=spi0-2cs,cs0_pin=0` to free GPIO8

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

## Dependencies

### System Packages
- python3, python3-venv, python3-pip
- alsa-utils, libportaudio2, portaudio19-dev
- python3-pil, i2c-tools
- libcairo2-dev (for SVG splash support)

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
