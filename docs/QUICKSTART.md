# Quick Start: Fresh SD Card to Running DMX Controller

Use this guide when flashing a new SD card and setting up a Pi from scratch.

## Prerequisites

- Raspberry Pi 4 or 5
- Raspberry Pi OS (Bookworm or Bullseye, 64-bit recommended)
- **Audio input:** USB microphone / interface **or** HiFiBerry DAC+ ADC (or Pro)
- EastRising 3.2" SSD1322 OLED
- DMX interface (e.g. UART + RS485 to DMXKing, or USB DMX widget used with OLA)
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

`scripts/bootstrap_pi.sh` expects the project at **`~/pi-dmx-controller-v2`** (i.e. `/home/pi/pi-dmx-controller-v2` if your user is `pi`).

```bash
cd ~
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2
```

---

## Step 3: Systemd units (do this before bootstrap)

Bootstrap copies unit files from the repo into `/etc/systemd/system/`. Edit them **now** so the first install matches your user and path.

| File | What to set |
|------|-------------|
| `systemd/pi-dmx.service` | `User`, `WorkingDirectory`, `ExecStart` → your home and `.../pi-dmx-controller-v2/.venv/bin/python .../dmx_audio_react.py` |
| `systemd/oled_splash.service` | Same `User` / paths for `oled_boot.py` |

The repo defaults target user **`pi`** and **`/home/pi/pi-dmx-controller-v2`**. If your login is different, change **both** files.

---

## Step 4: Firmware and ALSA

### 4a — Copy firmware (`config.txt`)

This sets SPI, UART, `disable-bt` for DMX serial, OLED-related SPI, etc.:

```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
```

**USB audio (typical in this repo’s `config/boot/config.txt`):** no HiFiBerry `dtoverlay`. `dtparam=audio=off` only turns off the **onboard** analog jack; USB capture still shows up in `arecord -l` as its own card.

**HiFiBerry HAT:** edit `config/boot/config.txt` *before* copying and add the correct overlay, for example:

- `dtoverlay=hifiberry-dacplusadc` or  
- `dtoverlay=hifiberry-dacplusadcpro`  

Remove or replace conflicting audio lines as needed (see comments in that file). Then copy as above.

### 4b — ALSA default (HiFiBerry only)

Skip this if you use **USB** capture only.

```bash
sudo cp config/alsa/asound.conf /etc/asound.conf
```

---

## Step 5: Run bootstrap

```bash
./scripts/bootstrap_pi.sh
```

This script:

- Runs **`apt-get`** with **`DEBIAN_FRONTEND=noninteractive`** and **`--force-confold`** so upgrades (e.g. Chromium conffiles) do not stop and wait for keyboard input on SSH/headless installs.
- Installs packages (Python, OLA, PortAudio, PIL, etc.)
- Enables SPI and I2C via `raspi-config`
- **Does not** append HiFiBerry lines to `/boot/firmware/config.txt` — your Step 4 copy is the source of truth for overlays and audio.
- Creates `.venv` (with `--system-site-packages`) and installs `requirements.txt`
- Enables and starts **OLA**, then runs **`ola_patch`** for universe 0 (see below)
- Installs **`pi-dmx.service`** and **`oled_splash.service`** if present under `systemd/`

**OLA patch:** the script looks for a device whose name matches **DMXking** in `ola_dev_info`. If none exists (no USB DMX widget yet), the fallback device id may be wrong — after boot, run `ola_dev_info` and patch manually, e.g.:

```bash
sudo ola_patch --patch --device <id> --port 0 --universe 0
```

For a quick sanity check with no hardware, OLA’s **Dummy** output is often device `1`; only use that for testing.

---

## Step 6: OLED splash service (recommended)

Ensures ordering with `pi-dmx` and removes legacy `oled_wake` if present:

```bash
sudo scripts/install_oled_splash.sh
```

(Re-run this after you change `systemd/oled_splash.service` in the repo.)

---

## Step 7: Optional — early OLED display (initramfs)

Shows a gray bar during the first ~5 seconds of boot (before root mounts):

```bash
# Edit config/initramfs/hook-oled-boot: set BINARY to YOUR path, e.g.
# BINARY="/home/pi/pi-dmx-controller-v2/utils/oled_early"
sudo scripts/install_oled_initramfs.sh
```

---

## Step 8: Reboot

```bash
sudo reboot
```

---

## DMX on GPIO UART (`/dev/serial0`) — required for RS485 / Chauvet etc.

**`dmx_audio_react.py` does not drive your fixtures through OLA.** It outputs DMX frames on **`/dev/serial0`** (Pi primary UART → your RS485 board → XLR). OLA is separate.

Raspberry Pi OS often enables a **serial login console** on that same UART (`console=serial0,115200` in **`/boot/firmware/cmdline.txt`**). That causes:

- **`agetty`** holding the port (see **`serial-getty@ttyAMA0.service`**)
- Garbled DMX / fixtures not responding, even though the app logs **`[DMX] Backend: uart`**

**Fix (do once after imaging, then reboot):**

1. Edit **`/boot/firmware/cmdline.txt`** and remove **`console=serial0,115200`** (keep **`console=tty1`** so you still get a console on HDMI/local tty).
2. Stop the serial login prompt:
   ```bash
   sudo systemctl disable --now serial-getty@ttyAMA0.service
   ```
3. After disabling the console, **`/dev/ttyAMA0`** may be **`root:tty`** instead of **`root:dialout`**, which breaks the app (Permission denied). Install the udev rule from this repo:
   ```bash
   sudo cp ~/pi-dmx-controller-v2/config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger /dev/ttyAMA0
   ```
4. **`sudo reboot`**

**Also check:** Chauvet DMX-4 **start address** matches the channels the app drives (default **4 channels from 1** — set the pack to address **1**, or change channel count / addressing in the UI and **`.dmx_config`**). RS485 **A/B** wiring to DMX **+ / −** must match your transceiver (swap if needed).

**USB RS485 adapter** instead of GPIO: set in **`pi-dmx.service`**  
`Environment=DMX_UART_DEVICE=/dev/ttyUSB0` (device name from **`ls /dev/ttyUSB*`**).

**Hardware test (no audio):** with the dimmer wired and addressed to **1**, run:

```bash
sudo systemctl stop pi-dmx.service
cd ~/pi-dmx-controller-v2 && python3 dmx_uart_test.py
```

You should see outputs **1–4** chase. If not, the problem is wiring, RS485 **A/B** vs DMX **±**, or dimmer address — not the trigger logic. (`Ctrl+C` stops the script; then **`sudo systemctl start pi-dmx.service`**.)

### Picky dimmer packs (e.g. Chauvet DMX-4): frame length + break style

Some fixtures **ignore** DMX packets that only contain **start code + a few channel bytes**. The main app therefore **pads** each frame with trailing **zero slots** (default **`DMX_UART_MIN_SLOTS=256`**, set in **`systemd/pi-dmx.service`**).

If **`dmx_uart_test.py`** still shows **no** output but wiring looks right, run a full sweep (tries **24 → 512** slots and **two** break methods):

```bash
sudo systemctl stop pi-dmx.service
cd ~/pi-dmx-controller-v2 && python3 scripts/dmx_probe.py
```

When you see the dimmer respond, match the main app:

- Copy/install the repo **`systemd/pi-dmx.service`** (it sets **`DMX_UART_MIN_SLOTS=256`**) and run **`sudo systemctl daemon-reload && sudo systemctl restart pi-dmx.service`**.
- If the dimmer only reacted during the probe’s **`baud9600`** phases, uncomment **`Environment=DMX_BREAK_STYLE=baud`** in **`pi-dmx.service`** (or add it), then **`daemon-reload`** + **`restart`**.
- If it only reacted at **`min_slots=512`**, set **`Environment=DMX_UART_MIN_SLOTS=512`** the same way.

See **`journalctl -u pi-dmx.service`** for a line like:  
`[DMX] Backend: uart (... min_slots=256 break=ioctl)`.

### Stopping `pi-dmx` for tests

- **`sudo systemctl stop pi-dmx.service`** freezes the OLED on the last image until you **`start`** the service again — expected.
- To **prevent autostart on boot** while you debug, use **`./scripts/dmx-dev disable`** (runs **`systemctl disable`**). **`./scripts/dmx-dev enable`** turns autostart back on.

### Manual run (with `dmx` alias)

For quick manual testing, add this alias to **`~/.bashrc`**:

```bash
alias dmx='sudo /home/pi/pi-dmx-controller-v2/.venv/bin/python /home/pi/pi-dmx-controller-v2/dmx_audio_react.py'
```

Then **`source ~/.bashrc`** and run:

```bash
sudo systemctl stop pi-dmx.service
dmx
```

Press **`q`** to quit. Restart the service with **`sudo systemctl start pi-dmx.service`**.

---

## Verify

After reboot:

- **OLED:** CSW logo (~3.5 s), then DMX UI (splash keeps last frame during handoff to reduce blank gap)
- **Audio:** `arecord -l` — USB card or HiFiBerry as expected. If triggers use the wrong input (e.g. Input 1 = left on your interface), set **`AUDIO_INPUT_CHANNEL=left`** in **`pi-dmx.service`** `Environment=`.
- **DMX / OLA:** `ola_dev_info`; fix patching if universe has no output (fixtures on **UART** follow **`pi-dmx`**, not OLA)
- **DMX UART:** In logs, confirm **`min_slots=`** / **`break=`** match your **`pi-dmx.service`** `Environment=`
- **Services:** `systemctl status oled_splash.service pi-dmx.service olad.service`
- **App logs:** `journalctl -u pi-dmx.service -f`

---

## One-command restore (after clone)

**USB audio (no HiFiBerry `asound.conf`):**

```bash
cd ~/pi-dmx-controller-v2
# Edit systemd/* if user/path is not pi
sudo cp config/boot/config.txt /boot/firmware/config.txt
./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
# DMX on GPIO UART: free the serial port from console + dialout on ttyAMA0 (see section "DMX on GPIO UART" above)
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/ 2>/dev/null || true
sudo reboot
```

**HiFiBerry (include ALSA):**

```bash
cd ~/pi-dmx-controller-v2
sudo cp config/boot/config.txt /boot/firmware/config.txt   # ensure HAT overlay is in this file first
sudo cp config/alsa/asound.conf /etc/asound.conf
./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
sudo reboot
```
