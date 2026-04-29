#!/usr/bin/env bash
# Manual start: same env as systemd/pi-dmx.service (USB input, full encoders, TUI + OLED).
# Python equivalent (clears AUDIO_DEVICE, same defaults):  ./.venv/bin/python scripts/dev_ui.py
# Stop the service first if it is running:  sudo systemctl stop pi-dmx.service
set -euo pipefail

cd /home/pi/pi-dmx-controller-v2

# Avoid stale index from an old shell (e.g. AUDIO_DEVICE=2) breaking startup.
unset AUDIO_DEVICE

export AUDIO_INPUT_CHANNEL=right
export DISABLE_I2S_ENCODERS=0
export AUDIO_DEVICE_NAME=USB

export NOISE_GATE_ON=0
export ENABLE_TUI=1
export DMX_BACKEND=uart

exec /home/pi/pi-dmx-controller-v2/.venv/bin/python dmx_audio_react.py
