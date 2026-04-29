#!/bin/bash
# Stop the DMX controller service and turn off the OLED display

echo "Stopping pi-dmx service..."
sudo systemctl stop pi-dmx.service

echo "Turning off OLED..."
/home/pi/pi-dmx-controller-v2/.venv/bin/python - <<'EOF'
try:
    from luma.core.interface.serial import spi
    from luma.oled.device import ssd1322
    serial = spi(device=0, port=1, gpio_DC=24, gpio_RST=12)
    device = ssd1322(serial, width=256, height=64)
    device.hide()
    device.cleanup()
    print("OLED off")
except Exception as e:
    print(f"Could not turn off OLED: {e}")
EOF

echo "Done."
