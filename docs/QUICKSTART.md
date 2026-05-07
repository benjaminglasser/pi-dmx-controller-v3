# Quick Start: Fresh SD Card to Running DMX Controller

**This file is the canonical onboarding guide** — use it when flashing a new SD card, restoring after failure, or when an AI agent needs a single checklist to replicate a working Pi deployment (hardware + firmware + systemd + repo + deps). Companion docs: **[WIRING.md](WIRING.md)** (pinout / wiring); **[README.md](../README.md)** (architecture and troubleshooting).

---

## Master checklist (do in order)

1. Flash **Raspberry Pi OS (64-bit)**; enable SSH and network in Raspberry Pi Imager.
2. First boot → update packages and clone this repo **exactly** to **`~/pi-dmx-controller-v2`** (required by **`scripts/bootstrap_pi.sh`**).
3. If your Linux user is not **`pi`** or your home directory is not **`/home/pi`**, edit **`systemd/pi-dmx.service`**, **`systemd/oled_splash.service`**, **`scripts/audio-source.sh`** (`TEMPLATE=` path), **`config/initramfs/hook-oled-boot`** (`BINARY=` path, if you use initramfs), and any **`~/.bashrc`** alias paths **before** running bootstrap.
4. Copy **`config/boot/config.txt`** → **`/boot/firmware/config.txt`** (adjust HiFiBerry overlays *before* copying if you use a HAT). HiFiBerry: also **`config/alsa/asound.conf`** → **`/etc/asound.conf`**; USB-only skips ALSA copy.
5. **Free `/dev/serial0` for DMX**: edit **`cmdline.txt`**, stop **`serial-getty@ttyAMA0`**, install udev **`99-dmx-ttyAMA0-dialout.rules`** (see **[Step 5](#step-5-free-gpio-dmx-uart-before-reboot)**). **Pi 5 note:** canonical UART mapping differs from Pi 4 — see that step.
6. Run **`./scripts/bootstrap_pi.sh`**, then **`sudo scripts/install_oled_splash.sh`**.
7. Optional: **`sudo scripts/install_oled_initramfs.sh`** (needs **`BINARY=`** edited in-repo if not **`/home/pi/...`**; build runs automatically).
8. **`sudo reboot`**, then run the **[Verify](#verify)** checklist.

Skipping step 5 is the usual reason “everything is installed” but fixtures still see no UART DMX.

---

## Prerequisites

- Raspberry Pi 4 or **5**
- Raspberry Pi OS (**Bookworm** or Bullseye, **64‑bit recommended** — matches **`numpy==1.26.4`** and tested wheels)
- **Audio input:** USB microphone / interface **or** HiFiBerry DAC+ ADC (or Pro)
- EastRising 3.2" SSD1322 OLED (SPI CE1 wiring as in **`docs/WIRING.md`** / header comments)
- DMX RS485 adapter on **GPIO UART** (e.g. DMXKing) **and/or** a USB OLA-compatible DMX device (OLA is separate from the Python app’s UART stream)
- 5 rotary encoders (optional; HiFiBerry I2S mode limits some encoder pins — see **`systemd/pi-dmx.service`** comments)

---

## Step 1: Flash SD card

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Choose **Raspberry Pi OS (64-bit)**
3. Click the gear to set hostname, SSH, locale, timezone, wireless (if needed)
4. Flash to SD card and boot

Use a full Raspberry Pi OS image (not “bare” lite) unless you intend to **`apt install`** `git`, `gcc`, **`raspi-config`**, **`build-essential`** (needed for **`install_oled_initramfs.sh`**) manually.

---

## Step 2: Clone the repo

`scripts/bootstrap_pi.sh` hardcodes **`cd ~/pi-dmx-controller-v2`**. Your clone **must** end up at **`$HOME/pi-dmx-controller-v2`**.

HTTPS (simplest):

```bash
sudo apt-get update && sudo apt-get install -y git
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git pi-dmx-controller-v2
cd pi-dmx-controller-v2
```

SSH (deploy key available):

```bash
git clone git@github.com:benjaminglasser/pi-dmx-controller-v2.git ~/pi-dmx-controller-v2
cd ~/pi-dmx-controller-v2
```

---

## Step 3: Systemd units (before bootstrap)

Bootstrap copies unit files into **`/etc/systemd/system/`**. Edit them **before** `./scripts/bootstrap_pi.sh`:

| File | What to verify |
|------|----------------|
| **`systemd/pi-dmx.service`** | **`User=`**, **`WorkingDirectory=`**, **`ExecStart=`** → **`.../pi-dmx-controller-v2`** and **`.../.venv/bin/python .../dmx_audio_react.py`**. Uncomment **`AUDIO_INPUT_CHANNEL=left`** / **`DISABLE_I2S_ENCODERS=1`** / **`AUDIO_DEVICE_NAME=hifiberry`** for HiFiBerry (see **`scripts/audio-source.sh`**). |
| **`systemd/oled_splash.service`** | Same **`User=`** / **`WorkingDirectory`** / **`ExecStart`** for **`oled_boot.py`** |

Repo defaults assume **`pi`** + **`/home/pi/pi-dmx-controller-v2`**.

---

## Step 4: Firmware (`config.txt`) and ALSA

### Copy firmware

```bash
sudo cp ~/pi-dmx-controller-v2/config/boot/config.txt /boot/firmware/config.txt
```

**USB audio:** the shipped **`config/boot/config.txt`** has **no** HiFiBerry overlay; **`dtparam=audio=off`** only disables **on‑board analog** jack — USB capture still appears under **`arecord -l`**.

**HiFiBerry:** edit **`config/boot/config.txt` *before* copying** — add **`dtoverlay=hifiberry-dacplusadc`** or **`hifiberry-dacplusadcpro`**, reconcile with comments in-file; then **`sudo cp ...`**.

After changing **`config.txt`** or **`cmdline.txt`**, a **`reboot`** is required for overlays and kernel **`cmdline`** to fully apply — you can batch this with bootstrap + splash (final reboot).

### ALSA defaults (HiFiBerry only)

```bash
sudo cp ~/pi-dmx-controller-v2/config/alsa/asound.conf /etc/asound.conf
```

Skip for USB‑only installs.

---

## Step 5: Free GPIO DMX UART (before reboot)

**`dmx_audio_react.py`** sends DMX on **`/dev/serial0`** through RS485; it **does not** push through OLA. Stock Raspberry Pi OS often attaches a **kernel console** and **`serial-getty@ttyAMA0`** to that UART, blocking DMX and yielding logs like **`[DMX] Backend: uart`** with silence on wired devices.

Do all of:

1. Edit **`/boot/firmware/cmdline.txt`**: remove **`console=serial0,115200`** (keep **`console=tty1`** for local display).
   ```bash
   sudo nano /boot/firmware/cmdline.txt
   ```

2. Stop the UART login banner:
   ```bash
   sudo systemctl disable --now serial-getty@ttyAMA0.service
   ```

3. **`dialout` group ownership** on **`ttyAMA0`** — install repo udev rule and reload:
   ```bash
   sudo cp ~/pi-dmx-controller-v2/config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger /dev/ttyAMA0   # harmless if absent until next boot
   ```

4. **`sudo reboot`** (or continue to bootstrap below and reboot once at the end.)

### Raspberry Pi 5

Pi 5 **UART numbering differs from Pi 4**: **`enable_uart`** and **`disable-bt`** behavior is not identical. After reboot, inspect:

```bash
ls -la /dev/serial0
readlink -f /dev/serial0 || true
```

If **`/dev/serial0`** is **not** the PL011 wired to UART TX on **GPIO14** per your schematic, force the device explicitly in **`/etc/systemd/system/pi-dmx.service`** with:

```ini
Environment=DMX_UART_DEVICE=/dev/ttyAMA0
```

See the official Raspberry Pi UART guide: **[UART configuration](https://www.raspberrypi.com/documentation/computers/configuration.html#overview-of-raspberry-pi-uarts)** (bookmark may move — search Raspberry Pi Documentation for “UART”).

### USB RS485 dongle instead of GPIO UART

Match **`ExecStart`/Environment** via:

```ini
Environment=DMX_UART_DEVICE=/dev/ttyUSB0
```

(Device name from **`ls /dev/ttyUSB*`**.)

---

## Step 6: Run bootstrap

```bash
cd ~/pi-dmx-controller-v2
./scripts/bootstrap_pi.sh
```

Bootstrap:

- Runs **`DEBIAN_FRONTEND=noninteractive`** **`apt`** with **`--force-confold`** (no stuck conffile prompts over SSH during upgrades — same pattern as **`README`**).
- Installs **`git`**, Python 3 tooling, **`ola`**, **`ola-python`**, PortAudio libs, Pillow, **`i2c-tools`**, etc.
- Runs **`raspi-config nonint do_spi`** and **`do_i2c`**.
- Drops **`snd-dummy`** (“ALSA placeholder capture”) via **`/etc/modules-load.d/pi-dmx-alsa-placeholder.conf`** plus **`modprobe snd-dummy`**, avoiding **PortAudio** seeing **zero** capture devices before your USB gadget enumerates (`[4/7]` — safe on real hardware).
- Removes and recreates **`.venv`** with **`--system-site-packages`** and **`pip install -r requirements.txt`** (pins **`protobuf==3.20.3`** compatible with distro **`ola-python`**).
- Runs **`ola_dev_info`**; if awk finds a **`DMXking`** line, **`ola_patch -d … -p 0 -u 0`** maps port 0 to universe 0 (`[6/7]`). **Fallback device id 10** applies if absent — rerun **`ola_patch`** manually with **`ola_dev_info`** output if the wrong patch was chosen.
  ```bash
  sudo ola_patch -d <device_id> -p 0 -u 0
  ```

- Copies **`systemd/pi-dmx.service`** + **`oled_splash.service`**, disables legacy **`oled_wake`**, **`daemon-reload`**.

**Important:** **`bootstrap_pi.sh`** does **not** modify **`config.txt`** for overlays (your Step 4 copy remains source of truth; no accidental HiFiBerry append).

---

## Step 7: OLED splash installer (recommended)

```bash
cd ~/pi-dmx-controller-v2
sudo scripts/install_oled_splash.sh
```

Re-run whenever you edit **`systemd/oled_splash.service`** locally.

---

## Step 8: Optional — early OLED (initramfs)

Builds **`utils/oled_early`**, installs hooks and rebuilds initramfs:

```bash
# Ensure config/initramfs/hook-oled-boot BINARY=/home/<you>/pi-dmx-controller-v2/utils/oled_early
cd ~/pi-dmx-controller-v2
sudo scripts/install_oled_initramfs.sh
```

Needs **`gcc`** (present on Raspberry Pi Desktop image; lite → **`sudo apt install build-essential`**).

---

## Step 9: Reboot

```bash
sudo reboot
```

---

## Audio source: HiFiBerry vs USB after install

**`scripts/audio-source.sh`** flips uncommented **`Environment=`** blocks in **`/etc/systemd/system/pi-dmx.service`** and restarts **`pi-dmx`**. Repo helper defaults assume **`TEMPLATE=/home/pi/pi-dmx-controller-v2/systemd/pi-dmx.service`** — adjust inside the script for other users.

Example:

```bash
cd ~/pi-dmx-controller-v2/scripts
./audio-source.sh          # USB (also default — all encoders)
./audio-source.sh hifiberry
./audio-source.sh status
```

**Firmware:** Ensure **`dtoverlay=hifiberry-…`** in **`config.txt`** and **`/etc/asound.conf`** deployed before expecting capture.

---

## DMX tuning: Chauvet-style dimmers / frame length / break style

Defaults match pickier decode hardware:

- **`DMX_UART_MIN_SLOTS=256`** in **`systemd/pi-dmx.service`**
- Optional **`DMX_BREAK_STYLE=baud`** (`ioctl` vs **`baud`**) commented in-repo

Smoke test (wired pack at address **1**):

```bash
sudo systemctl stop pi-dmx.service
cd ~/pi-dmx-controller-v2 && python3 dmx_uart_test.py    # Ctrl+C to stop
sudo systemctl start pi-dmx.service
```

If nothing responds despite wiring, **`python3 scripts/dmx_probe.py`** sweeps **`min_slots`** and **`baud`** break styles — align **`pi-dmx.service`** **`Environment=`** with the phase that reacted.

Inspect runtime:

```bash
journalctl -u pi-dmx.service -n 120 | grep -E 'min_slots|break'
```

Stopping **`pi-dmx`** freezes OLED on last frame until **`start`** — expected.

**Autostart:**

```bash
./scripts/dmx-dev disable      # omit from boot temporarily
./scripts/dmx-dev enable
```

Manual run / alias (venv + sudo): see **[README § Manual run](../README.md#manual-run)**.

Also verify **DMX addressing** (**default:** first **four** logical channels beginning at **fixture address 1** — match pack wheels or **`~/.dmx_config`** presets).

---

## Verify

Post-reboot sanity:

| Check | Expected |
|-------|----------|
| **UART free** | No **`agetty`** on UART; **`journalctl -u pi-dmx.service`** shows **[DMX]** backend **uart**. |
| **OLED splash** | CSW logo (**~4 s**) then live UI (**`journalctl -u oled_splash.service -n 60`** optional). |
| **Audio capture** | **`arecord -l`** lists HiFiBerry or USB; wrong card → **`Environment=AUDIO_DEVICE_NAME=`** etc. (**`AUDIO_INPUT_CHANNEL`** `left`|`right`|`mix`). |
| **OLA (USB widget)** | **`ola_dev_info`**; **`ola_patch`** if universe/output wrong; **fixtures on RS485/XLR ≠ OLA.** |
| **Services** | **`systemctl status oled_splash.service pi-dmx.service olad.service`**. |

---

## One-shot restore snippets (clone already at `~/pi-dmx-controller-v2`)

USB audio (repository **`config/boot/config.txt`** as-is):

```bash
cd ~/pi-dmx-controller-v2

# systemd/* already edited here if username ≠ pi …

sudo cp config/boot/config.txt /boot/firmware/config.txt

# Console + permissions for GPIO UART BEFORE expecting DMX
# Remove console=serial0,115200 from /boot/firmware/cmdline.txt (keep console=tty1) — nano is safer than automated sed here
sudo nano /boot/firmware/cmdline.txt
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger /dev/ttyAMA0 || true

./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
sudo reboot
```

HiFiBerry (add overlay + **`asound`** before **`cp config.txt`** if not already folded into your working copy):

```bash
cd ~/pi-dmx-controller-v2

sudo cp config/alsa/asound.conf /etc/asound.conf        # AFTER editing config/boot/config.txt overlays
sudo cp config/boot/config.txt /boot/firmware/config.txt

# UART: same cmdline edits + disable serial-getty@ttyAMA0 + udev as in USB branch above …

./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
~/pi-dmx-controller-v2/scripts/audio-source.sh hifiberry
sudo reboot
```

---

## Troubleshooting hints

See **[README Troubleshooting](../README.md#troubleshooting)** for OLED (`spidev`), **`apt`** stuck (**`DEBIAN_FRONTEND=noninteractive`**, **`sudo dpkg --configure -a --force-confold`**); **`sounddevice`/PortAudio`** issues after removing USB gear — **`snd-dummy`** keeps non-zero ALSA enumeration.
