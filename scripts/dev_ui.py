#!/usr/bin/env python3
"""
Start dmx_audio_react.py for local UI / development (TUI + OLED + USB input).

- Clears AUDIO_DEVICE so a stale index from your shell cannot break startup.
- Sets the same defaults as systemd/pi-dmx.service in USB mode.

Run (recommended — uses project venv if present):

  /home/pi/pi-dmx-controller-v2/.venv/bin/python \\
    /home/pi/pi-dmx-controller-v2/scripts/dev_ui.py

Or from the repo directory:

  ./.venv/bin/python scripts/dev_ui.py

Stop the service first if it is already running:

  sudo systemctl stop pi-dmx.service
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    target = root / "dmx_audio_react.py"
    if not target.is_file():
        print(f"dev_ui: missing {target}", file=sys.stderr)
        sys.exit(1)

    venv_py = root / ".venv" / "bin" / "python"
    exe = str(venv_py) if venv_py.is_file() else sys.executable

    env = os.environ.copy()
    env.pop("AUDIO_DEVICE", None)

    # Match systemd/pi-dmx.service (USB default)
    env["AUDIO_INPUT_CHANNEL"] = "right"
    env["DISABLE_I2S_ENCODERS"] = "0"
    env["AUDIO_DEVICE_NAME"] = "USB"

    env.setdefault("NOISE_GATE_ON", "0")
    env.setdefault("ENABLE_TUI", "1")
    env.setdefault("DMX_BACKEND", "uart")

    # Do not set DEV_NO_HW — real GPIO + OLED for on-device UI work.

    os.chdir(root)
    os.execvpe(exe, [exe, str(target)], env)


if __name__ == "__main__":
    main()
