# Pi DMX Controller v3

Audio-reactive DMX lighting controller running on Raspberry Pi. Analyzes audio in real time via FFT and drives DMX fixtures over RS485/UART. Controlled via 5 rotary encoders and an OLED display.

---

## Hardware

| Component | Details |
|-----------|---------|
| **Raspberry Pi** | 4 or 5 (64-bit OS required) |
| **OLED display** | EastRising 3.2" SSD1322 256×64, SPI CE0 |
| **Encoders** | 5× rotary encoders via MCP23017 I2C expander (addr 0x20) + direct GPIO buttons |
| **DMX output** | GPIO UART → RS485 transceiver → XLR (`/dev/serial0`) |
| **Audio input** | USB audio interface (default) **or** HiFiBerry DAC+ ADC HAT |

Full pinout and wiring details: **[docs/WIRING.md](docs/WIRING.md)**

---

## Getting started

**[docs/QUICKSTART.md](docs/QUICKSTART.md)** — the complete guide: flash, clone, configure, bootstrap, reboot.

One-line summary of what you run after SSH-ing into a fresh Pi:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/benjaminglasser/pi-dmx-controller-v3.git ~/pi-dmx-controller-v2
cd ~/pi-dmx-controller-v2
sudo cp config/boot/config.txt /boot/firmware/config.txt
sudo sed -i 's/console=serial0,[0-9]\+ //g' /boot/firmware/cmdline.txt
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo cp config/udev/99-dmx-ttyAMA0-dialout.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
./scripts/bootstrap_pi.sh
sudo scripts/install_oled_splash.sh
sudo reboot
```

---

## Project layout

```
pi-dmx-controller-v2/
├── dmx_audio_react.py          # Main app — audio FFT → DMX, OLED UI, encoder input
├── oled_boot.py                # Boot splash (CSW logo, CRT reveal animation)
├── dmx_uart_test.py            # Quick DMX chase test (stop pi-dmx first)
├── run_dmx.sh                  # Manual launch with same env as systemd unit
├── requirements.txt
│
├── config/
│   ├── boot/config.txt         # Pi firmware — SPI, UART, BT-off, no HiFiBerry overlay
│   ├── alsa/asound.conf        # HiFiBerry default ALSA device (HiFiBerry installs only)
│   ├── udev/                   # ttyAMA0 → dialout group (required for DMX without root)
│   └── initramfs/              # Early-boot OLED hook (optional)
│
├── scripts/
│   ├── bootstrap_pi.sh         # Full install: apt, venv, OLA, systemd units
│   ├── install_oled_splash.sh  # Install / refresh oled_splash.service
│   ├── install_oled_initramfs.sh # Build + install early-boot OLED (optional)
│   ├── build_oled_initramfs.sh # Rebuild utils/oled_early C binary only
│   ├── apply_repo_boot_firmware_config.sh # Re-copy config.txt with backup
│   ├── audio-source.sh         # Switch pi-dmx.service between USB / HiFiBerry
│   ├── audio_test.py           # Live PortAudio meter — verify capture device
│   ├── dev_ui.py               # Launch app with clean env + TUI (USB defaults)
│   ├── dmx-dev                 # Toggle pi-dmx.service autostart (disable/enable/status)
│   ├── dmx_probe.py            # Sweep frame length + break style for picky dimmers
│   ├── stop.sh                 # Stop pi-dmx and blank the OLED
│   ├── verify_universe.sh      # Print OLA universe config, repatch if missing
│   └── wm8960-rebind.sh        # SB Components WM8960 codec workaround (run by systemd)
│
├── systemd/
│   ├── pi-dmx.service          # Main app unit (USB defaults; HiFiBerry block commented)
│   ├── oled_splash.service     # Runs oled_boot.py before main app
│   └── wm8960-rebind.service   # WM8960 codec HAT rebind workaround (optional)
│
├── utils/
│   └── oled_initramfs.c        # C source for early-boot OLED display
│
└── docs/
    ├── QUICKSTART.md           # Step-by-step install guide (start here)
    └── WIRING.md               # Full pinout and wiring diagrams
```

---

## Running the app

The app starts automatically via `pi-dmx.service` after a successful install. To run manually:

```bash
sudo systemctl stop pi-dmx.service
sudo .venv/bin/python dmx_audio_react.py
```

Optional `~/.bashrc` alias:

```bash
alias dmx='sudo /home/pi/pi-dmx-controller-v2/.venv/bin/python /home/pi/pi-dmx-controller-v2/dmx_audio_react.py'
```

Toggle autostart during development:

```bash
scripts/dmx-dev disable    # stop and prevent restart
scripts/dmx-dev enable     # re-enable and start
```

---

## Configuration

**`.dmx_config`** — auto-created on first run in the repo root. Stores presets, gain, detect mode, and channel count as `key=value`. Delete to reset to defaults. Git-ignored (per-Pi state stays local).

**`/etc/systemd/system/pi-dmx.service`** — environment variables that control runtime behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_DEVICE_NAME` | `USB` | Substring match against `sounddevice` device list |
| `AUDIO_INPUT_CHANNEL` | `right` | `left`, `right`, or `mix` — which stereo channel feeds the FFT |
| `DISABLE_I2S_ENCODERS` | `0` | Set `1` for HiFiBerry (I2S pins conflict with E3/E4/E5) |
| `DMX_UART_DEVICE` | `/dev/serial0` | Override if symlink points to wrong UART |
| `DMX_UART_MIN_SLOTS` | `256` | Pad frames to this length; increase if picky dimmers miss frames |
| `DMX_BREAK_STYLE` | `ioctl` | `ioctl` or `baud` — use `baud` if only `dmx_probe.py`'s baud mode works |
| `DMX_BACKEND` | `uart` | `uart` or `null` (null disables DMX output entirely) |

After editing the service file: `sudo systemctl daemon-reload && sudo systemctl restart pi-dmx.service`

---

## DMX output: common failure modes

The app sends DMX on `/dev/serial0` (GPIO UART → RS485 → XLR) directly — **not** through OLA.

| Problem | Symptom | Fix |
|---------|---------|-----|
| Serial console on UART | Fixtures see garbage or nothing | Remove `console=serial0,...` from `cmdline.txt`, disable `serial-getty@ttyAMA0` (QUICKSTART Step 4) |
| Wrong UART permissions | `Permission denied` on `/dev/serial0` | Install `config/udev/99-dmx-ttyAMA0-dialout.rules`, run `udevadm trigger` |
| Frame too short | Chauvet / picky packs never react | Increase `DMX_UART_MIN_SLOTS` (default 256); run `scripts/dmx_probe.py` to find working value |
| Wrong break style | Only probe's baud mode worked | Set `DMX_BREAK_STYLE=baud` in `pi-dmx.service` |
| A/B polarity | RS485 wired backwards | Swap pins 2 and 3 on the XLR connector |

Diagnose without the main app:

```bash
sudo systemctl stop pi-dmx.service
python3 dmx_uart_test.py       # simple chase on channels 1–4
python3 scripts/dmx_probe.py   # sweep all frame/break combos
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| OLED blank | Confirm SPI enabled (`ls /dev/spidev0.0`); test with `python3 oled_boot.py` |
| No USB audio | `arecord -l`; set `AUDIO_DEVICE_NAME=` in service to match |
| No HiFiBerry audio | Verify `dtoverlay=hifiberry-...` in `config.txt` and `/etc/asound.conf` present |
| Wrong audio channel | Set `AUDIO_INPUT_CHANNEL=left` (many USB interfaces: Input 1 = left channel) |
| App crashes on start | `journalctl -u pi-dmx -n 100` — check traceback |
| Service has wrong path/user | Edit `systemd/*.service`, re-run `sudo scripts/install_oled_splash.sh` and `sudo cp systemd/pi-dmx.service /etc/systemd/system/; sudo systemctl daemon-reload` |
| OLA not outputting | `ola_dev_info`; `sudo ola_patch -d <id> -p 0 -u 0` — note RS485/XLR fixtures bypass OLA entirely |
