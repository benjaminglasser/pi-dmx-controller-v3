# Quickstart — Fresh Pi to Running DMX Controller

Use this guide when setting up a new Raspberry Pi from scratch. Everything below assumes the defaults: username **`pi`**, USB audio input, DMX out via GPIO UART + RS485.

---

## The short version (copy-paste, then reboot)

```bash
# SSH into your freshly flashed Pi, then run these in order:

# 1. Install git and clone the repo
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/benjaminglasser/pi-dmx-controller-v3.git ~/pi-dmx-controller-v2
cd ~/pi-dmx-controller-v2

# 2. Copy firmware config (enables SPI, UART, disables BT)
sudo cp config/boot/config.txt /boot/firmware/config.txt

# 3. Free the GPIO UART for DMX (removes serial console that blocks DMX output)
sudo sed -i 's/console=serial0,[0-9]\+ //g' /boot/firmware/cmdline.txt
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/ttyAMA0 2>/dev/null || true

# 4. Run bootstrap (installs packages, Python venv, OLA, systemd services)
./scripts/bootstrap_pi.sh

# 5. Install the OLED boot splash
sudo scripts/install_oled_splash.sh

# 6. Reboot (applies firmware + cmdline changes)
sudo reboot
```

After reboot, the OLED splash plays and the DMX controller starts automatically.

---

## Step 1: Flash the SD card

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Select **Raspberry Pi OS (64-bit)**.
3. In **OS Customisation** (gear icon), set:
   - **Username: `pi`** — matches all defaults in this repo; anything else means editing service files before bootstrap.
   - Enable **SSH** (password or key).
   - Set hostname (e.g. `pi-dmx`), Wi-Fi, timezone.
4. Flash and boot the Pi, then SSH in.

---

## Step 2: Clone the repo

The bootstrap script expects the repo at exactly `~/pi-dmx-controller-v2`.

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/benjaminglasser/pi-dmx-controller-v3.git ~/pi-dmx-controller-v2
cd ~/pi-dmx-controller-v2
```

---

## Step 3: Copy firmware config

```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
```

This enables SPI (for the OLED), I2C (for the MCP23017 encoder expander), UART (for DMX), and disables Bluetooth so the primary UART is free. The file ships ready-to-use for USB audio.

**HiFiBerry DAC+ ADC?** Edit `config/boot/config.txt` *before* copying — uncomment the appropriate `dtoverlay=hifiberry-dacplusadc` line. Also copy the ALSA config:

```bash
sudo cp config/alsa/asound.conf /etc/asound.conf
```

---

## Step 4: Free the GPIO UART for DMX

Stock Raspberry Pi OS puts a serial console on the same UART the app uses for DMX. You must remove it or fixtures will see garbage / nothing.

```bash
# Remove 'console=serial0,...' from the kernel command line
sudo sed -i 's/console=serial0,[0-9]\+ //g' /boot/firmware/cmdline.txt

# Stop the UART login service
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true

# Fix permissions so the app can open the port without root
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/ttyAMA0 2>/dev/null || true
```

> **This is the #1 reason DMX doesn't work on a fresh install.** Don't skip it.

**Pi 5 note:** verify that `/dev/serial0` actually points to the UART on GPIO 14/15 after reboot:

```bash
ls -la /dev/serial0   # should point to ttyAMA0 or ttyAMA10
```

If it points elsewhere, force the device in `/etc/systemd/system/pi-dmx.service`:
```ini
Environment=DMX_UART_DEVICE=/dev/ttyAMA0
```

---

## Step 5: Bootstrap

```bash
cd ~/pi-dmx-controller-v2
./scripts/bootstrap_pi.sh
```

This script is **idempotent** — safe to re-run. It:

- Runs `apt-get` non-interactively (no stuck prompts over SSH).
- Installs: `python3-venv`, `libportaudio2`, `ola`, `ola-python`, `build-essential`, `i2c-tools`, and other deps.
- Creates `.venv` with `--system-site-packages` so OLA Python bindings are visible.
- Runs `pip install -r requirements.txt`.
- Loads `snd-dummy` (keeps PortAudio happy even when no USB audio is plugged in).
- Enables `olad` and patches DMXKing → universe 0.
- Installs and enables `pi-dmx.service` and `oled_splash.service`.

---

## Step 6: OLED splash

```bash
sudo scripts/install_oled_splash.sh
```

Installs the boot splash service and ensures it runs before the main app opens SPI.

### Optional: early-boot OLED (before OS loads)

Shows a sign-of-life bar on the OLED during the first ~5 s of boot, before the root filesystem mounts. Requires `gcc` (installed by bootstrap).

```bash
sudo scripts/install_oled_initramfs.sh
```

---

## Step 7: Reboot

```bash
sudo reboot
```

The firmware config (`config.txt`) and kernel command line (`cmdline.txt`) changes only take effect after a full reboot.

---

## After reboot: verify everything works

| Check | Command | Expected |
|-------|---------|---------|
| OLED splash | — | CSW logo → live UI within ~15 s |
| Services running | `systemctl status pi-dmx oled_splash olad` | All active |
| UART free | `systemctl status serial-getty@ttyAMA0` | `inactive (dead)` |
| UART permissions | `ls -la /dev/ttyAMA0` | `crw-rw---- root dialout` |
| DMX backend | `journalctl -u pi-dmx -n 80 \| grep DMX` | `[DMX] Backend: uart` |
| Audio | `arecord -l` | USB card or HiFiBerry listed |

---

## Switching between USB and HiFiBerry audio

After install, use the helper script to switch audio source without editing files by hand:

```bash
scripts/audio-source.sh           # → USB (default, all 5 encoders work)
scripts/audio-source.sh hifiberry # → HiFiBerry I2S (E3/E4/E5 rotation disabled, pin conflict)
scripts/audio-source.sh status    # show active config + available sound devices
```

---

## Manual run (for debugging)

Stop the service first, then run directly:

```bash
sudo systemctl stop pi-dmx.service
sudo .venv/bin/python dmx_audio_react.py
```

Or use the alias — add to `~/.bashrc`:

```bash
alias dmx='sudo /home/pi/pi-dmx-controller-v2/.venv/bin/python /home/pi/pi-dmx-controller-v2/dmx_audio_react.py'
```

Toggle autostart while developing:

```bash
scripts/dmx-dev disable   # stop service, prevent autostart
scripts/dmx-dev enable    # re-enable and start
scripts/dmx-dev status
```

---

## DMX not working? (fixture troubleshooting)

**Quick smoke test** — plug in a fixture addressed to channel 1 and run:

```bash
sudo systemctl stop pi-dmx.service
python3 dmx_uart_test.py        # runs a chase; Ctrl+C to stop
sudo systemctl start pi-dmx.service
```

**Sweep frame length and break style** — some dimmers (Chauvet, etc.) need specific timing:

```bash
sudo systemctl stop pi-dmx.service
python3 scripts/dmx_probe.py    # sweeps min_slots × break style
```

When the dimmer reacts, note the parameters and set them in `/etc/systemd/system/pi-dmx.service`:

```ini
Environment=DMX_UART_MIN_SLOTS=256
Environment=DMX_BREAK_STYLE=baud   # only if probe's baud mode worked and ioctl didn't
```

Then: `sudo systemctl daemon-reload && sudo systemctl restart pi-dmx.service`

**If nothing ever responds to dmx_probe.py** — the problem is hardware, not software:
- RS485 transceiver DE/RE pins not tied high (must be in transmit mode)
- A/B wires swapped on XLR (try swapping pins 2 and 3)
- Missing ground between Pi and fixture
- Fixture start address not set to 1

---

## Troubleshooting quick reference

| Symptom | Fix |
|---------|-----|
| OLED blank after boot | Check SPI enabled (`ls /dev/spidev0.0`); test with `python3 oled_boot.py` |
| No audio input (USB) | `arecord -l` to find card; set `AUDIO_DEVICE_NAME=` in `pi-dmx.service` |
| No audio input (HiFiBerry) | Verify `dtoverlay=hifiberry-...` in `config.txt` and `/etc/asound.conf` exists |
| Wrong audio channel | Set `AUDIO_INPUT_CHANNEL=left` or `right` in `pi-dmx.service` (many USB interfaces: Input 1 = left) |
| Fixtures ignore DMX | See DMX section above; run `dmx_probe.py` |
| `pi-dmx` crashes on start | `journalctl -u pi-dmx -n 100` for traceback; check venv and audio device |
| Service file has wrong user/path | Edit `systemd/pi-dmx.service` and `systemd/oled_splash.service`, re-run `sudo scripts/install_oled_splash.sh` and `sudo cp systemd/pi-dmx.service /etc/systemd/system/; sudo systemctl daemon-reload` |
| OLA not sending to fixture | `ola_dev_info` to find device ID; `sudo ola_patch -d <id> -p 0 -u 0` (note: RS485/XLR fixtures bypass OLA entirely) |
