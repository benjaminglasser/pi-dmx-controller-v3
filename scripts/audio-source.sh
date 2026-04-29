#!/bin/bash
# Switch audio source between HiFiBerry (I2S) and USB.
# Default: USB (no argument applies USB — same as ./audio-source.sh usb).
# Usage: ./audio-source.sh [usb|hifiberry|status]

SERVICE_FILE="/etc/systemd/system/pi-dmx.service"
TEMPLATE="/home/pi/pi-dmx-controller-v2/systemd/pi-dmx.service"

apply_usb() {
  echo "Switching to USB audio..."
  sudo sed -i \
    -e 's/^Environment=AUDIO_INPUT_CHANNEL=left/# Environment=AUDIO_INPUT_CHANNEL=left/' \
    -e 's/^Environment=DISABLE_I2S_ENCODERS=1/# Environment=DISABLE_I2S_ENCODERS=1/' \
    -e 's/^# *Environment=AUDIO_INPUT_CHANNEL=right/Environment=AUDIO_INPUT_CHANNEL=right/' \
    -e 's/^# *Environment=DISABLE_I2S_ENCODERS=0/Environment=DISABLE_I2S_ENCODERS=0/' \
    -e 's/^# *Environment=AUDIO_DEVICE_NAME=USB/Environment=AUDIO_DEVICE_NAME=USB/' \
    "$SERVICE_FILE"
  sudo systemctl daemon-reload
  echo "Restarting service..."
  sudo systemctl restart pi-dmx.service
  echo ""
  echo "Now using: USB Audio"
  echo "  - Audio: From USB interface (device name contains 'USB')"
  echo "  - Encoders: All 5 fully functional"
}

case "$1" in
  hifiberry|hat|i2s)
    echo "Switching to HiFiBerry (I2S) audio..."
    sudo sed -i \
      -e 's/^# *Environment=AUDIO_INPUT_CHANNEL=left/Environment=AUDIO_INPUT_CHANNEL=left/' \
      -e 's/^# *Environment=DISABLE_I2S_ENCODERS=1/Environment=DISABLE_I2S_ENCODERS=1/' \
      -e 's/^Environment=AUDIO_INPUT_CHANNEL=right/# Environment=AUDIO_INPUT_CHANNEL=right/' \
      -e 's/^Environment=DISABLE_I2S_ENCODERS=0/# Environment=DISABLE_I2S_ENCODERS=0/' \
      -e 's/^Environment=AUDIO_DEVICE_NAME=USB/# Environment=AUDIO_DEVICE_NAME=USB/' \
      "$SERVICE_FILE"
    sudo systemctl daemon-reload
    echo "Restarting service..."
    sudo systemctl restart pi-dmx.service
    echo ""
    echo "Now using: HiFiBerry (I2S)"
    echo "  - Audio: Left channel from HiFiBerry"
    echo "  - Encoders: E1/E2 rotation, E3/E5 buttons only"
    echo "  - E3/E4/E5 rotation disabled (I2S pin conflict)"
    ;;

  usb|""|default)
    apply_usb
    ;;

  status)
    echo "=== Current Audio Source Configuration ==="
    echo ""
    grep -E "^Environment=(AUDIO|DISABLE)|^# *Environment=(AUDIO|DISABLE)" "$SERVICE_FILE" | sed 's/^/  /'
    echo ""
    echo "=== Available Input Devices ==="
    sudo /home/pi/pi-dmx-controller-v2/.venv/bin/python -c "
import sounddevice as sd
for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels'] > 0:
        print(f'  [{i}] {d[\"name\"]}  (in={d[\"max_input_channels\"]}ch)')
" 2>/dev/null
    echo ""
    echo "Usage: $0 [usb|hifiberry|status]"
    echo "  (no argument)  — same as usb (default)"
    ;;

  *)
    echo "Usage: $0 [usb|hifiberry|status]"
    echo ""
    echo "  (no argument)  — USB audio (default)"
    echo "  usb            — USB audio interface, all encoders work"
    echo "  hifiberry      — HiFiBerry DAC+ ADC (I2S), E3/E4/E5 rotation disabled"
    echo "  status         — Show current configuration and available devices"
    exit 1
    ;;
esac
