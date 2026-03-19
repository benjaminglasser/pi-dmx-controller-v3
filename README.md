# Pi DMX Controller v2

Audio-reactive DMX lighting controller for Raspberry Pi with OLED UI, rotary encoders, and FFT-based beat detection.

## Hardware

| Component | Description |
|-----------|-------------|
| **Raspberry Pi** | 4 or 5 recommended |
| **HiFiBerry DAC+ ADC** | Stereo ADC for microphone/line input (DAC+ ADC or DAC+ ADC Pro) |
| **OLED** | EastRising 3.2" SSD1322 SPI (256×64), CE1, RST=GPIO12, DC=GPIO24 |
| **DMX** | UART RS485 (DMXKing or similar) |
| **Encoders** | 5 rotary encoders on GPIO |

## Quick Start (Fresh SD Card)

See **[docs/QUICKSTART.md](docs/QUICKSTART.md)** for the step-by-step guide to get from a blank SD card to a running system.

---

## Installation

### 1. Clone & bootstrap

```bash
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2
./scripts/bootstrap_pi.sh
```

This installs system packages, creates a Python venv, configures OLA, enables SPI/I2C, and sets up systemd services (if present).

### 2. Firmware config

Copy the Pi firmware config (enables HiFiBerry, SPI, UART, disables BT for DMX):

```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
```

### 3. ALSA config

Set HiFiBerry as the default audio device:

```bash
sudo cp config/alsa/asound.conf /etc/asound.conf
```

### 4. OLED boot splash & early display (optional)

**Splash (CSW logo, 3s):**

```bash
# Adjust User/WorkingDirectory in systemd/oled_splash.service if your path differs
sudo scripts/install_oled_splash.sh
```

**Early initramfs display (gray bar ~5s into boot):**

```bash
# Edit config/initramfs/hook-oled-boot: set BINARY to your project path/utils/oled_early
sudo scripts/install_oled_initramfs.sh
```

### 5. DMX service

Create a systemd service for the DMX app. Copy and adapt from `deploy/pi-dmx.service`:

```bash
# Example: create systemd/pi-dmx.service with your user and paths
# After=network-online.target olad.service oled_splash.service
# ExecStart=/home/YOUR_USER/pi-dmx-controller-v2/.venv/bin/python .../dmx_audio_react.py
sudo cp systemd/pi-dmx.service /etc/systemd/system/
sudo systemctl enable pi-dmx.service
```

Bootstrap will install `systemd/pi-dmx.service` and `systemd/oled_splash.service` if they exist.

### 6. Reboot

```bash
sudo reboot
```

---

## Manual run

```bash
cd ~/pi-dmx-controller-v2
source .venv/bin/activate
python dmx_audio_react.py
```

Or use `./run_dmx.sh` (uses DEV_NO_HW=1 and DMX_BACKEND=uart by default).

---

## Development mode

Disable DMX autostart for development:

```bash
./scripts/dmx-dev disable   # Stop service, prevent autostart
./scripts/dmx-dev enable    # Re-enable autostart
./scripts/dmx-dev status    # Show status
```

---

## Project layout

```
pi-dmx-controller-v2/
├── dmx_audio_react.py      # Main app
├── oled_boot.py            # Boot splash (CSW logo, CRT reveal)
├── requirements.txt
├── config/
│   ├── boot/config.txt     # Pi firmware
│   ├── alsa/asound.conf    # HiFiBerry default device
│   └── initramfs/          # Early OLED display
├── scripts/
│   ├── bootstrap_pi.sh     # Full system setup
│   ├── install_oled_splash.sh
│   ├── install_oled_initramfs.sh
│   └── dmx-dev             # Toggle autostart
├── systemd/
│   └── oled_splash.service
├── deploy/
│   └── pi-dmx.service      # Template for DMX service
└── utils/
    └── oled_initramfs.c    # C source for early display
```

---

## Configuration

- **`.dmx_config`** – JSON runtime config (auto-created).
- **`config/boot/config.txt`** – Overlay choices: `hifiberry-dacplusadc` or `hifiberry-dacplusadcpro` depending on your HAT.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No audio input | `sudo cp config/alsa/asound.conf /etc/asound.conf` and reboot |
| OLED blank | Check SPI enabled, `spidev0.1` exists. Run `python oled_boot.py` to test |
| DMX no output | Patch OLA universe: `ola_patch -d <device_id> -p 0 -u 0` |
| Splash uses wrong user | Edit `systemd/oled_splash.service` User and paths |
| Initramfs hook fails | Edit `config/initramfs/hook-oled-boot` BINARY path, run install again |
