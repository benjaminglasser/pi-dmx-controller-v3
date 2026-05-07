# Quick Start: Fresh SD Card to Running DMX Controller

**This file is the canonical onboarding guide.** Use it when:

- Flashing a brand-new SD card / setting up a new Raspberry Pi
- Restoring after an SD card failure
- Pointing an AI agent at one document to bring a fresh Pi up to a working, identical-to-production deployment (hardware + firmware + systemd + repo + Python deps + DMX UART)

Companion docs: **[WIRING.md](WIRING.md)** (pinout / wiring); **[README.md](../README.md)** (architecture, runtime, troubleshooting).

> **Defaults assumed in this guide:** Linux user **`pi`**, repo at **`/home/pi/pi-dmx-controller-v2`**, audio in via **USB**, DMX out on the **Pi GPIO UART** (`/dev/serial0`) through an RS485 transceiver. Anything that changes if you deviate is called out below.

---

## TL;DR — full path on a fresh Pi (USB audio, default user `pi`)

Run this top-to-bottom on the freshly imaged Pi (works headless over SSH). Each block has its own section below for context / troubleshooting.

```bash
# 0. (one-time) make sure git is present so we can clone
sudo apt-get update && sudo apt-get install -y git

# 1. Clone repo to the EXACT path bootstrap_pi.sh expects
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd ~/pi-dmx-controller-v2
git checkout main      # canonical branch — onboarding/docs live here

# 2. Firmware (SPI + UART + USB-audio-friendly defaults; no HiFiBerry overlay)
sudo cp config/boot/config.txt /boot/firmware/config.txt

# 3. Free /dev/serial0 for DMX (remove serial console + agetty + fix permissions)
sudo sed -i 's/console=serial0,[0-9]\+ //g' /boot/firmware/cmdline.txt
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/ttyAMA0 2>/dev/null || true

# 4. System packages, Python venv, OLA, systemd units
./scripts/bootstrap_pi.sh

# 5. OLED boot splash service
sudo scripts/install_oled_splash.sh

# 6. (optional) early-boot OLED via initramfs (needs gcc, installed by bootstrap)
sudo scripts/install_oled_initramfs.sh

# 7. Reboot — required for cmdline.txt + config.txt + initramfs to take effect
sudo reboot
```

After reboot run the **[Verify](#verify)** checklist. If anything fails, read the matching numbered section below.

> **HiFiBerry HAT instead of USB:** edit `config/boot/config.txt` *before* step 2 to add the HAT overlay (see [Step 4 – HiFiBerry](#step-4-firmware-configtxt-and-alsa)) and also `sudo cp config/alsa/asound.conf /etc/asound.conf`. After the reboot in step 7, run `~/pi-dmx-controller-v2/scripts/audio-source.sh hifiberry`.

---

## Master checklist (do in order)

1. **Flash** Raspberry Pi OS (64-bit). In Imager, **set username = `pi`** (skip Step 3 user-rewrites), enable SSH and Wi-Fi.
2. **First boot:** install **`git`**, clone to **`~/pi-dmx-controller-v2`** (required by `scripts/bootstrap_pi.sh`), `git checkout main`.
3. **Username/path != `pi` / `/home/pi/...`?** Edit `systemd/pi-dmx.service`, `systemd/oled_splash.service`, `scripts/audio-source.sh` (`TEMPLATE=`), `config/initramfs/hook-oled-boot` (`BINARY=`, only if using initramfs), and any `~/.bashrc` alias paths **before** Step 6.
4. **Firmware:** `config/boot/config.txt` → `/boot/firmware/config.txt` (HiFiBerry: edit overlay first; HiFiBerry only: also copy `config/alsa/asound.conf` → `/etc/asound.conf`).
5. **Free `/dev/serial0` for DMX:** strip `console=serial0,...` from `cmdline.txt`, stop `serial-getty@ttyAMA0`, install `99-dmx-ttyAMA0-dialout.rules`. **Pi 5:** verify the UART symlink target (see [Step 5 – Pi 5](#raspberry-pi-5)).
6. **Bootstrap:** `./scripts/bootstrap_pi.sh`, then `sudo scripts/install_oled_splash.sh`, optional `sudo scripts/install_oled_initramfs.sh`.
7. **Reboot** and run [Verify](#verify).

> Skipping Step 5 is the #1 reason “everything is installed” but fixtures still see no UART DMX.

---

## Prerequisites

- Raspberry Pi 4 or **5**
- Raspberry Pi OS (**Bookworm** or Bullseye, **64-bit recommended** — matches `numpy==1.26.4` and our tested wheels)
- **Audio input:** USB microphone / interface **or** HiFiBerry DAC+ ADC (or Pro) **or** SB Components WM8960 codec HAT (see [WM8960 codec HAT](#optional-sb-components-wm8960-codec-hat))
- EastRising 3.2" SSD1322 OLED (SPI CE1; pinout in **`docs/WIRING.md`** / `dmx_audio_react.py` header comments)
- DMX RS485 adapter on **GPIO UART** (e.g. DMXKing) **and/or** a USB OLA-compatible DMX device (OLA is separate from the Python app’s UART stream — see [README §DMX output](../README.md#dmx-output-what-usually-breaks-and-what-we-fixed))
- 5 rotary encoders (optional; HiFiBerry I2S mode disables some encoder pins — see comments in `systemd/pi-dmx.service`)

Use a full Raspberry Pi OS image (not “lite”) unless you intend to manually `apt install` `git`, `gcc`, `build-essential`, `raspi-config` (`bootstrap_pi.sh` installs `git` and `build-essential`; `raspi-config` ships with the desktop image).

---

## Step 1: Flash the SD card

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. Choose **Raspberry Pi OS (64-bit)**.
3. Click the gear / “OS customisation” and set:
   - **Username: `pi`** (matches every default in this repo — anything else means hand-editing several files later)
   - Enable **SSH** with password or public-key auth
   - Hostname (e.g. `pi-dmx`), locale, timezone, Wi-Fi if headless
4. Flash → boot the Pi → SSH in.

---

## Step 2: Clone the repo

`scripts/bootstrap_pi.sh` hardcodes **`cd ~/pi-dmx-controller-v2`**. Your clone **must** end up at `$HOME/pi-dmx-controller-v2`.

HTTPS (simplest, works without GitHub credentials):

```bash
sudo apt-get update && sudo apt-get install -y git
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd ~/pi-dmx-controller-v2
git checkout main      # canonical branch for docs/configs
```

SSH (only if you've already added a deploy key for this Pi):

```bash
git clone git@github.com:benjaminglasser/pi-dmx-controller-v2.git ~/pi-dmx-controller-v2
cd ~/pi-dmx-controller-v2
git checkout main
```

> **Branches:** Always start from `main` for onboarding. Other branches (`detect-options`, `feature/large-oled-ui`, etc.) may carry experimental tweaks to `dmx_audio_react.py` but the install instructions / configs / systemd units on `main` are the source of truth.

---

## Step 3: Systemd units (before bootstrap)

`scripts/bootstrap_pi.sh` copies the unit files in `systemd/` into `/etc/systemd/system/`. Edit them **before** you run bootstrap if anything below differs from your install:

| File | What to verify |
|------|----------------|
| `systemd/pi-dmx.service` | `User=`, `WorkingDirectory=`, `ExecStart=` → `.../pi-dmx-controller-v2/.venv/bin/python .../dmx_audio_react.py`. For HiFiBerry, uncomment the **Option A** block (`AUDIO_INPUT_CHANNEL=left`, `DISABLE_I2S_ENCODERS=1`, `AUDIO_DEVICE_NAME=hifiberry`) and comment the **Option B** USB block (or just run `scripts/audio-source.sh hifiberry` after install — it patches the same lines). |
| `systemd/oled_splash.service` | Same `User=` / `WorkingDirectory=` / `ExecStart=` for `oled_boot.py`. |
| `systemd/wm8960-rebind.service` | Only if using the SB Components WM8960 HAT — see [WM8960 codec HAT](#optional-sb-components-wm8960-codec-hat). |

Repo defaults assume **`pi`** + `/home/pi/pi-dmx-controller-v2`.

---

## Step 4: Firmware (`config.txt`) and ALSA

### Copy firmware

```bash
sudo cp ~/pi-dmx-controller-v2/config/boot/config.txt /boot/firmware/config.txt
```

The shipped `config/boot/config.txt` enables what this project needs and nothing else:

- `dtparam=spi=on` + `dtoverlay=spi0-2cs,cs0_pin=0` — SPI for the OLED on **CE1**, GPIO8 freed for encoder 5
- `enable_uart=1` + `dtoverlay=disable-bt` — primary UART (`/dev/serial0`) usable for DMX
- `dtparam=i2c_arm=on` — I²C (used by HiFiBerry codec / future peripherals)
- `dtparam=audio=off` — disables on-board analog jack only; **USB capture still appears** in `arecord -l` as its own card

**HiFiBerry:** edit `config/boot/config.txt` *before* copying — add `dtoverlay=hifiberry-dacplusadc` (or `hifiberry-dacplusadcpro`), reconcile with comments in-file; then `sudo cp ...`.

A `reboot` is required after `config.txt` or `cmdline.txt` changes for overlays / kernel cmdline to fully apply — the TL;DR sequence above batches this with the bootstrap reboot.

### ALSA defaults (HiFiBerry only)

```bash
sudo cp ~/pi-dmx-controller-v2/config/alsa/asound.conf /etc/asound.conf
```

Skip for USB-only installs. (USB cards show up regardless; `asound.conf` only matters when you need a default = HiFiBerry.)

---

## Step 5: Free GPIO DMX UART (before reboot)

`dmx_audio_react.py` sends DMX on `/dev/serial0` through your RS485 transceiver — it **does not** push fixtures through OLA. Stock Raspberry Pi OS often attaches a **kernel console** and **`serial-getty@ttyAMA0`** to that UART, blocking DMX output even though logs show `[DMX] Backend: uart`.

Do all of the following (the TL;DR block at the top runs them in one go):

1. **Edit `/boot/firmware/cmdline.txt`** and remove `console=serial0,115200` (keep `console=tty1` so you still get a console on HDMI/local TTY):

   ```bash
   sudo nano /boot/firmware/cmdline.txt
   # …or non-interactive:
   sudo sed -i 's/console=serial0,[0-9]\+ //g' /boot/firmware/cmdline.txt
   ```

2. **Stop the UART login banner**:

   ```bash
   sudo systemctl disable --now serial-getty@ttyAMA0.service
   ```

3. **`dialout` group ownership on `ttyAMA0`** — without this rule the device often comes up as `root:tty` after step 2 and the app gets `Permission denied`:

   ```bash
   sudo cp ~/pi-dmx-controller-v2/config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger /dev/ttyAMA0   # harmless if absent until next boot
   ```

4. **`sudo reboot`** (or continue to bootstrap below and reboot once at the end).

### Raspberry Pi 5

Pi 5’s UART numbering and overlay names differ from Pi 4: `enable_uart` and `disable-bt` behavior is **not** identical, and `/dev/serial0` may not point at the PL011 wired to GPIO14/15. After reboot, inspect:

```bash
ls -la /dev/serial0
readlink -f /dev/serial0
```

If `/dev/serial0` is **not** the UART connected to your RS485 transceiver per your schematic, force the device explicitly in `/etc/systemd/system/pi-dmx.service`:

```ini
Environment=DMX_UART_DEVICE=/dev/ttyAMA0
```

Reference: [Raspberry Pi UART configuration](https://www.raspberrypi.com/documentation/computers/configuration.html#configuring-uarts).

### USB RS485 dongle instead of GPIO UART

If you want the app to talk DMX over a USB RS485 adapter, set the device in `pi-dmx.service`:

```ini
Environment=DMX_UART_DEVICE=/dev/ttyUSB0
```

Find the device name with `ls /dev/ttyUSB*`.

---

## Step 6: Run bootstrap

```bash
cd ~/pi-dmx-controller-v2
./scripts/bootstrap_pi.sh
```

Bootstrap (idempotent — safe to re-run):

- Runs `apt-get` with `DEBIAN_FRONTEND=noninteractive` and `--force-confold` (no stuck conffile prompts on SSH-only / headless installs).
- Installs system packages: `git`, `python3` + `python3-venv` + `python3-pip`, `alsa-utils`, `libportaudio2`, `portaudio19-dev`, `libsndfile1`, `ola`, `ola-python`, `python3-pil`, `i2c-tools`, **`build-essential`** (so the optional `install_oled_initramfs.sh` step’s `gcc -static` build works on Pi OS Lite too).
- Runs `raspi-config nonint do_spi 0` and `do_i2c 0` (also covered by `config.txt`, but belt-and-braces).
- Drops `snd-dummy` via `/etc/modules-load.d/pi-dmx-alsa-placeholder.conf` and `modprobe`s it. This guarantees PortAudio sees ≥1 capture device even when no USB audio is plugged in (otherwise startup of `dmx_audio_react.py` can fail with “no input device available”).
- Removes any existing `.venv` and re-creates it with **`--system-site-packages`** so the OLA Python bindings (`/usr/lib/python3/...`) are visible to the venv. Then `pip install -r requirements.txt` (pins listed in `requirements.txt`, including `protobuf==3.20.3` to stay compatible with the distro `ola-python`).
- Enables and starts `olad`, then runs `ola_dev_info | awk` to find a DMXKing device and patches port 0 → universe 0. **Fallback:** if no DMXKing entry is found, it tries device id **10**. Re-run manually after boot if the wrong device was patched (see [OLA patching](#ola-patching)).
- Copies `systemd/pi-dmx.service` and `systemd/oled_splash.service` into `/etc/systemd/system/`, disables legacy `oled_wake.service` if present, and `systemctl daemon-reload`.

Bootstrap deliberately **does not** modify `/boot/firmware/config.txt` — Step 4 is the source of truth, so HiFiBerry overlays don’t accidentally get re-appended.

### OLA patching

If the bootstrap’s `ola_patch` ran with the wrong device id (e.g. it picked the OLA Dummy device because no widget was attached at the time), repatch after the widget is connected:

```bash
ola_dev_info                                  # note the numeric id of your hardware
sudo ola_patch -d <device_id> -p 0 -u 0
```

Reminder: fixtures wired to **GPIO UART → RS485 → XLR** do **not** go through OLA — they get DMX directly from `dmx_audio_react.py` via `/dev/serial0`. OLA is only relevant for USB DMX widgets / external software output.

---

## Step 7: OLED splash service (recommended)

```bash
cd ~/pi-dmx-controller-v2
sudo scripts/install_oled_splash.sh
```

This script:

- Copies `systemd/oled_splash.service` → `/etc/systemd/system/`.
- Removes any leftover `oled_wake.service` (caused an `After=basic.target` ordering cycle in earlier deployments).
- Adds `After=oled_splash.service` to `pi-dmx.service` if missing, so the splash always finishes before the main app reopens SPI.
- `daemon-reload` + `enable oled_splash.service`.

Re-run whenever you edit `systemd/oled_splash.service` locally.

---

## Step 8: Optional — early OLED display (initramfs)

Shows a “sign-of-life” bar on the OLED during the first ~5 s of boot, before the root filesystem mounts. Builds the static `utils/oled_early` binary and installs initramfs hooks:

```bash
# Default config/initramfs/hook-oled-boot expects /home/pi/pi-dmx-controller-v2/utils/oled_early
# If your username is not `pi`, edit BINARY= in that file FIRST.
cd ~/pi-dmx-controller-v2
sudo scripts/install_oled_initramfs.sh
```

Bootstrap installs `build-essential`, so the `gcc -static` build inside `install_oled_initramfs.sh` will succeed on Pi OS Lite too.

---

## Step 9: Reboot

```bash
sudo reboot
```

---

## Audio source: HiFiBerry vs USB after install

`scripts/audio-source.sh` flips uncommented `Environment=` blocks in `/etc/systemd/system/pi-dmx.service` and restarts `pi-dmx`. Repo helper defaults assume `TEMPLATE=/home/pi/pi-dmx-controller-v2/systemd/pi-dmx.service` — adjust inside the script for other users.

```bash
cd ~/pi-dmx-controller-v2/scripts
./audio-source.sh                # → USB (default — all 5 encoders)
./audio-source.sh usb            # same as above
./audio-source.sh hifiberry      # I2S; E3/E4/E5 rotation disabled (pin conflict)
./audio-source.sh status         # show currently-uncommented Environment= block + sounddevice list
```

For HiFiBerry to actually capture, you must **also** have:

- `dtoverlay=hifiberry-…` line in `/boot/firmware/config.txt` (Step 4)
- `/etc/asound.conf` from `config/alsa/asound.conf` (Step 4 ALSA)

### Optional: SB Components WM8960 codec HAT

Only relevant if you’re using a **WM8960** codec HAT. Its kernel codec sometimes loses its first I²C reset write at boot, leaving `/proc/asound/cards` without `wm8960soundcard`. The repo ships a workaround:

```bash
sudo cp ~/pi-dmx-controller-v2/systemd/wm8960-rebind.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wm8960-rebind.service
```

The unit runs `scripts/wm8960-rebind.sh` early at boot (before `pi-dmx.service`), unbinds/rebinds `1-001a` on `/sys/bus/i2c/drivers/wm8960`, and re-applies stored `alsactl` mixer state once the card registers. It is a no-op on USB / HiFiBerry installs (`ConditionPathExists=/sys/bus/i2c/drivers/wm8960` keeps it from running).

---

## DMX tuning: Chauvet-style dimmers / frame length / break style

Defaults match pickier decode hardware:

- `Environment=DMX_UART_MIN_SLOTS=256` set in `systemd/pi-dmx.service`
- Optional `Environment=DMX_BREAK_STYLE=baud` (vs default `ioctl`) commented in-repo

Smoke test (pack wired and addressed to **1**):

```bash
sudo systemctl stop pi-dmx.service
cd ~/pi-dmx-controller-v2 && python3 dmx_uart_test.py    # Ctrl+C to stop
sudo systemctl start pi-dmx.service
```

If nothing responds despite wiring, sweep frame length + break style:

```bash
sudo systemctl stop pi-dmx.service
python3 scripts/dmx_probe.py
```

When the dimmer reacts, align `pi-dmx.service` `Environment=` with the phase that worked, then `daemon-reload && systemctl restart pi-dmx.service`.

Inspect runtime:

```bash
journalctl -u pi-dmx.service -n 120 | grep -E 'min_slots|break'
```

Stopping `pi-dmx` freezes the OLED on the last frame until you `start` again — that is normal.

### Autostart while debugging

```bash
./scripts/dmx-dev disable     # stop & prevent autostart
./scripts/dmx-dev enable      # re-enable & start
./scripts/dmx-dev status
```

### Manual run / `dmx` alias

See **[README § Manual run](../README.md#manual-run)** for the venv + `sudo` invocation and the optional `dmx` alias for `~/.bashrc`.

Also verify **DMX addressing** — default is the first **four** logical channels starting at fixture address **1**. Match your pack’s wheels or the saved presets in `~/.dmx_config` (see [Runtime config file](#runtime-config-file)).

---

## Helper scripts (worth bookmarking)

All under `scripts/` unless noted.

| Script | Purpose |
|--------|---------|
| `bootstrap_pi.sh` | Full system setup (Step 6). Idempotent. |
| `install_oled_splash.sh` | Install / update the OLED splash service (Step 7). |
| `install_oled_initramfs.sh` | Build `utils/oled_early` + install initramfs hooks (Step 8). |
| `build_oled_initramfs.sh` | Just rebuild the static `utils/oled_early` C binary (called by the installer; run by hand if you edit `utils/oled_initramfs.c`). |
| `apply_repo_boot_firmware_config.sh` | Re-copy `config/boot/config.txt` → `/boot/firmware/config.txt` with a timestamped `.bak`. Use when the firmware drifted. |
| `audio-source.sh` | Switch `pi-dmx.service` between USB and HiFiBerry (I2S) audio profiles in-place. |
| `audio_test.py` | Live PortAudio peak/RMS meter — confirms `sounddevice` sees the same card the main app uses. `sudo .venv/bin/python scripts/audio_test.py`. |
| `dev_ui.py` | Re-launch `dmx_audio_react.py` with a clean env matching `pi-dmx.service` (clears stale `AUDIO_DEVICE`, sets USB defaults, `ENABLE_TUI=1`). Stop the service first. |
| `dmx-dev` | Toggle `pi-dmx.service` autostart (`disable` / `enable` / `status`). |
| `dmx_probe.py` | Sweep `min_slots` × break style for picky dimmers. |
| `stop.sh` | Stop `pi-dmx.service` and blank the OLED via luma. |
| `verify_universe.sh` | Print `ola-universe.conf` and re-patch DMXKing → universe 0 if missing. |
| `wm8960-rebind.sh` | One-shot WM8960 codec rebind (run by `wm8960-rebind.service`; safe to invoke by hand). |
| `../run_dmx.sh` (repo root) | Manual run with the same env as the systemd unit (USB + TUI + UART). |

---

## Runtime config file

`~/.dmx_config` (repo root after first launch, alongside `dmx_audio_react.py`) is auto-created by the app. It stores presets, per-band parameters, input gain, detect mode, DMX output mode, and channel count as `key=value` lines. Wipe it to reset to defaults; it’s in `.gitignore` so per-Pi state stays local.

Example contents from a working install:

```
defaults_mode=LOW
dmx_output_mode=Dimmer
dmx_channel_count=4
input_gain_db=-12
detect_mode_index=3
LOW=50.96675546113668,0.4,542.0,2.0,0,0
MID=2279.8633865981606,0.41,542.0,1.5,0,0
HIGH=13094.798282109798,0.25,542.0,0.82,0,0
```

---

## Verify

After the final reboot:

| Check | Expected |
|-------|----------|
| **UART free** | `systemctl status serial-getty@ttyAMA0` says `inactive (dead)`; `ls -la /dev/ttyAMA0` shows `root:dialout`. |
| **OLED splash** | CSW logo (~3.5 s) → live UI. `journalctl -u oled_splash.service -n 60` shows clean exit. |
| **Audio capture** | `arecord -l` lists your USB card or `sndrpihifiberry`. If the wrong card is chosen by `dmx_audio_react.py`, set `Environment=AUDIO_DEVICE_NAME=...` (substring match) or `Environment=AUDIO_DEVICE=<index>` in `pi-dmx.service`. `AUDIO_INPUT_CHANNEL` ∈ `left` / `right` / `mix` selects which stereo channel feeds the FFT (default `right`; many USB interfaces wire Input 1 = left). |
| **DMX backend log** | `journalctl -u pi-dmx.service -n 120 \| grep DMX` shows `[DMX] Backend: uart (... min_slots=256 break=ioctl)`. |
| **OLA (USB widget)** | `ola_dev_info` lists your widget; `ola_patch -d <id> -p 0 -u 0` if universe 0 has no output. **Reminder:** RS485/XLR fixtures bypass OLA — see [Step 6 §OLA patching](#ola-patching). |
| **Services** | `systemctl status oled_splash.service pi-dmx.service olad.service` all green. |

---

## One-shot restore snippets (clone already at `~/pi-dmx-controller-v2`)

USB audio (repository `config/boot/config.txt` as-is):

```bash
cd ~/pi-dmx-controller-v2

# (Edit systemd/* first if username ≠ pi.)

# 1. Firmware
sudo cp config/boot/config.txt /boot/firmware/config.txt

# 2. Free GPIO UART for DMX
sudo sed -i 's/console=serial0,[0-9]\+ //g' /boot/firmware/cmdline.txt
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger /dev/ttyAMA0 2>/dev/null || true

# 3. System / venv / OLA / systemd
./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh

# 4. Reboot (overlays + cmdline + initramfs)
sudo reboot
```

HiFiBerry (add overlay + `asound.conf` before `cp config.txt`):

```bash
cd ~/pi-dmx-controller-v2

# Edit config/boot/config.txt to add `dtoverlay=hifiberry-dacplusadc` (or pro)
sudo cp config/alsa/asound.conf /etc/asound.conf
sudo cp config/boot/config.txt /boot/firmware/config.txt

# Same UART steps as USB block above …

./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
~/pi-dmx-controller-v2/scripts/audio-source.sh hifiberry
sudo reboot
```

---

## Troubleshooting hints

See **[README § Troubleshooting](../README.md#troubleshooting)** for OLED (`spidev0.1`), `apt` stuck on conffile prompts (`DEBIAN_FRONTEND=noninteractive` / `sudo dpkg --configure -a --force-confold`), and `sounddevice` / PortAudio “no input device” after removing USB gear (the `snd-dummy` placeholder keeps ALSA enumeration non-zero).
