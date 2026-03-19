# Quick Start: Fresh SD Card to Running DMX Controller

Use this guide when flashing a new SD card and setting up a Pi from scratch.

## Prerequisites

- Raspberry Pi 4 or 5
- Raspberry Pi OS (Bookworm or Bullseye, 64-bit recommended)
- HiFiBerry DAC+ ADC (or DAC+ ADC Pro)
- EastRising 3.2" SSD1322 OLED
- DMX interface (e.g. DMXKing)
- 5 rotary encoders (optional)

---

## Step 1: Flash SD card

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Choose **Raspberry Pi OS** (64-bit)
3. Click the gear to set:
   - Hostname: `pi-dmx` (or similar)
   - Enable SSH, set username/password
   - Configure Wi‑Fi if headless
4. Flash to SD card and boot the Pi

---

## Step 2: Clone the repo

```bash
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2
```

---

## Step 3: Copy firmware & ALSA config

**Firmware (before first boot or before reboot):**

```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
```

**ALSA (HiFiBerry as default audio):**

```bash
sudo cp config/alsa/asound.conf /etc/asound.conf
```

---

## Step 4: Run bootstrap

```bash
./scripts/bootstrap_pi.sh
```

This:

- Updates system and installs packages (Python, OLA, PortAudio, PIL, etc.)
- Enables SPI and I2C
- Appends HiFiBerry overlay to config.txt (if needed)
- Creates `.venv` and installs `requirements.txt`
- Enables and starts OLA, patches Universe 0 to DMX device
- Installs `oled_splash.service` and `pi-dmx.service` if present

---

## Step 5: DMX systemd service

The repo includes `systemd/pi-dmx.service`. Edit it if your username or project path differs from `pi` / `/home/pi/pi-dmx-controller-v2`:

```bash
nano systemd/pi-dmx.service
# Set User, WorkingDirectory, ExecStart paths
```

Bootstrap installs it automatically. The `After=oled_splash.service` ensures the splash runs before the DMX app.

---

## Step 6: Adjust OLED splash service (if needed)

If your username or project path is not `benglasser` / `/home/benglasser/pi-dmx-controller-v2`, edit `systemd/oled_splash.service`:

```ini
User=pi
WorkingDirectory=/home/pi/pi-dmx-controller-v2
ExecStart=/home/pi/pi-dmx-controller-v2/.venv/bin/python /home/pi/pi-dmx-controller-v2/oled_boot.py
```

Then install the splash:

```bash
sudo scripts/install_oled_splash.sh
```

---

## Step 7: Optional – early OLED display (initramfs)

Shows a gray bar during the first ~5 seconds of boot (before root mounts):

```bash
# Edit config/initramfs/hook-oled-boot: set BINARY="/home/YOUR_USER/pi-dmx-controller-v2/utils/oled_early"
sudo scripts/install_oled_initramfs.sh
```

---

## Step 8: Reboot

```bash
sudo reboot
```

---

## Verify

After reboot:

- **OLED**: CSW logo appears (3 s), then DMX UI
- **Audio**: Check `arecord -l` for HiFiBerry
- **DMX**: Run `ola_dev_info` and patch universe if needed
- **Service**: `systemctl status oled_splash.service pi-dmx.service`

---

## One-command restore (after clone)

If config files are already in place and you only need to re-apply them:

```bash
cd ~/pi-dmx-controller-v2
sudo cp config/boot/config.txt /boot/firmware/config.txt
sudo cp config/alsa/asound.conf /etc/asound.conf
./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
sudo reboot
```
