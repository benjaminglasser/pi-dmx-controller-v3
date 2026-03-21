# Pi DMX Controller v2

Audio-reactive DMX lighting controller for Raspberry Pi with OLED UI, rotary encoders, and FFT-based beat detection.

## Hardware

| Component | Description |
|-----------|-------------|
| **Raspberry Pi** | 4 or 5 recommended |
| **Audio input** | **USB** mic/interface, **or** HiFiBerry DAC+ ADC / DAC+ ADC Pro |
| **OLED** | EastRising 3.2" SSD1322 SPI (256×64), CE1, RST=GPIO12, DC=GPIO24 |
| **DMX** | UART RS485 (e.g. DMXKing on `/dev/serial0`); optional USB DMX via OLA |
| **Encoders** | 5 rotary encoders on GPIO |

## Quick Start (Fresh SD Card)

See **[docs/QUICKSTART.md](docs/QUICKSTART.md)** for the full path from a blank SD card to a running system (order of steps, USB vs HiFiBerry, OLA patching, headless `apt`).

Short version:

1. Clone to **`~/pi-dmx-controller-v2`** (required by `bootstrap_pi.sh`).
2. Edit **`systemd/pi-dmx.service`** and **`systemd/oled_splash.service`** if your user is not **`pi`** or your path is not **`/home/pi/pi-dmx-controller-v2`**.
3. **`sudo cp config/boot/config.txt /boot/firmware/config.txt`** — add HiFiBerry `dtoverlay=...` in that file first if you use the HAT.
4. **`sudo cp config/alsa/asound.conf /etc/asound.conf`** — **HiFiBerry only**; skip for USB-only capture.
5. **`./scripts/bootstrap_pi.sh`** then **`sudo scripts/install_oled_splash.sh`**, then **`sudo reboot`**.

---

## Installation (reference)

### 1. Clone

```bash
git clone https://github.com/benjaminglasser/pi-dmx-controller-v2.git
cd pi-dmx-controller-v2
```

### 2. Systemd templates

Adjust **`systemd/pi-dmx.service`** and **`systemd/oled_splash.service`** before bootstrap so the copied units match your account (defaults in-repo assume **`pi`**).

### 3. Firmware

```bash
sudo cp config/boot/config.txt /boot/firmware/config.txt
```

The shipped **`config/boot/config.txt`** is tuned for **SPI + UART DMX + USB-style audio** (no HiFiBerry overlay). For a HiFiBerry HAT, edit the overlay lines *before* copying (see **Configuration**).

### 4. ALSA (HiFiBerry only)

```bash
sudo cp config/alsa/asound.conf /etc/asound.conf
```

Not needed for USB-only input.

### 5. Bootstrap

```bash
./scripts/bootstrap_pi.sh
```

Installs packages, venv + **`requirements.txt`**, enables SPI/I2C, configures OLA, and installs systemd units. Uses non-interactive **`apt-get`** with **`--force-confold`** for headless safety. Does **not** rewrite HiFiBerry settings in **`/boot/firmware/config.txt`** after you copy it — keep overlays and audio options in **`config/boot/config.txt`**.

### 6. OLED splash (recommended)

```bash
sudo scripts/install_oled_splash.sh
```

### 7. Early OLED (optional)

```bash
# Edit config/initramfs/hook-oled-boot: BINARY must point to your utils/oled_early
sudo scripts/install_oled_initramfs.sh
```

### 8. Reboot

```bash
sudo reboot
```

---

## Manual run

Stop the service first, then run with sudo (required for GPIO access):

```bash
sudo systemctl stop pi-dmx.service
sudo .venv/bin/python dmx_audio_react.py
```

### Optional: Add a `dmx` alias

Add this to your **`~/.bashrc`** for a quick command:

```bash
alias dmx='sudo /home/pi/pi-dmx-controller-v2/.venv/bin/python /home/pi/pi-dmx-controller-v2/dmx_audio_react.py'
```

Then reload: **`source ~/.bashrc`**

Now you can just run **`dmx`** from anywhere (after stopping the service).

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
│   ├── boot/config.txt     # Pi firmware (SPI / UART / audio overlays)
│   ├── alsa/asound.conf    # HiFiBerry default device (optional)
│   ├── udev/               # e.g. ttyAMA0 → dialout when UART is for DMX
│   └── initramfs/          # Early OLED display
├── scripts/
│   ├── bootstrap_pi.sh     # Full system setup
│   ├── dmx_probe.py        # Sweep frame length + break style (Chauvet / picky dimmers)
│   ├── install_oled_splash.sh
│   ├── install_oled_initramfs.sh
│   └── dmx-dev             # systemctl disable/enable pi-dmx (autostart)
├── dmx_uart_test.py        # Quick UART DMX chase test (stop pi-dmx first)
├── systemd/
│   ├── pi-dmx.service
│   └── oled_splash.service
├── deploy/
│   └── pi-dmx.service      # Alternate template
└── utils/
    └── oled_initramfs.c    # C source for early display
```

---

## DMX output: what usually breaks (and what we fixed)

The main app sends DMX on **`/dev/serial0`** (GPIO UART → RS485 → XLR), **not** through OLA. Typical failures on a fresh Pi OS image:

| Problem | Symptom | Fix |
|---------|---------|-----|
| **Serial console** on the UART | `console=serial0,115200` in **`cmdline.txt`** + **`serial-getty@ttyAMA0`** | Remove serial console from **`cmdline.txt`**, **`disable --now serial-getty@ttyAMA0`**, reboot. |
| **`ttyAMA0` not `dialout`** | Permission denied opening **`/dev/serial0`** | Install **`config/udev/99-dmx-ttyAMA0-dialout.rules`**, **`udevadm trigger`**. |
| **Short DMX frames** | Wiring OK but Chauvet / some packs never react | Many decoders need **many trailing slot bytes** (not just start + 4 channels). Defaults: **`DMX_UART_MIN_SLOTS=256`** in code and **`pi-dmx.service`**. |
| **Break timing** | Probe works only on **baud9600** half | Set **`Environment=DMX_BREAK_STYLE=baud`** in **`pi-dmx.service`**. |

**Diagnose without the main app:** **`sudo systemctl stop pi-dmx.service`** then **`python3 scripts/dmx_probe.py`**. If the dimmer reacts during the sweep, align **`pi-dmx.service`** with **`DMX_UART_MIN_SLOTS`** / **`DMX_BREAK_STYLE`** as in **`systemd/pi-dmx.service`** comments.

**Note:** Stopping **`pi-dmx`** leaves the OLED on the last frame until you **`start`** the service again — that is normal.

---

## Configuration

- **`.dmx_config`** – Runtime config (key=value, auto-created). Stores presets, input gain, detect mode, DMX output mode, etc.
- **`config/boot/config.txt`** – For HiFiBerry, set **`dtoverlay=hifiberry-dacplusadc`** or **`hifiberry-dacplusadcpro`**. For USB input, omit HiFiBerry overlays; USB cards appear separately in ALSA.
- **Audio device selection** – Env vars in **`dmx_audio_react.py`** or **`pi-dmx.service`**: **`AUDIO_DEVICE`**, **`AUDIO_DEVICE_NAME`**. **`AUDIO_INPUT_CHANNEL`** = `left` \| `right` \| `mix` — which stereo channel feeds the FFT (default `right`; many USB interfaces use Input 1 = left). Use **`arecord -l`** to list hardware.
- **DMX UART (Chauvet / picky dimmers)** – **`DMX_UART_MIN_SLOTS`** (default **256**): longer padded frames like **`scripts/dmx_probe.py`**. **`DMX_BREAK_STYLE=baud`** if only the probe’s **baud9600** half worked; default **`ioctl`**. Set in **`pi-dmx.service`** `Environment=` or shell when testing.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| No audio input (USB) | `arecord -l`; set **`AUDIO_DEVICE`** / **`AUDIO_DEVICE_NAME`** in **`pi-dmx.service`** `Environment=` if the wrong card is chosen |
| No audio input (HiFiBerry) | `sudo cp config/alsa/asound.conf /etc/asound.conf`, correct **`dtoverlay`** in **`config.txt`**, reboot |
| OLED blank | SPI enabled, **`spidev0.1`** present; test with **`python oled_boot.py`** |
| **Fixtures ignore DMX (Chauvet / RS485)** | See **DMX output: what usually breaks** above. Quick sweep: **`sudo systemctl stop pi-dmx.service`** then **`python3 scripts/dmx_probe.py`**. If **nothing** ever flickers: **hardware** (RS485 **not** raw TTL to XLR, **DE/RE**, **A/B swap**, ground, cable, dimmer **start address**). |
| DMX no output (OLA only) | **`ola_dev_info`** then **`ola_patch --patch --device <id> --port 0 --universe 0`** (OLA does not feed the Python app’s stream to the UART) |
| Splash / service wrong user | Edit repo **`systemd/*.service`**, then **`sudo scripts/install_oled_splash.sh`** and **`sudo cp systemd/pi-dmx.service /etc/systemd/system/`**, **`systemctl daemon-reload`** |
| Initramfs hook fails | Edit **`config/initramfs/hook-oled-boot`** **`BINARY`** path, reinstall |
| **`apt`** / **`dpkg`** stuck on conffile prompts over SSH | Bootstrap uses **`DEBIAN_FRONTEND=noninteractive`** and **`--force-confold`**; if you run **`apt`** by hand, use the same or **`sudo dpkg --configure -a --force-confold`** |
| **`pi-dmx`** errors in **`journalctl`** | Check traceback; verify venv path and audio device |
