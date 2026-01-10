#!/usr/bin/env bash
set -euo pipefail

cd ~/pi-dmx-controller-v2

# activate venv
source .venv/bin/activate

# defaults
export NOISE_GATE_ON=0
export ENABLE_TUI=1
export DEV_NO_HW=1
export DMX_BACKEND=uart

python dmx_audio_react.py
