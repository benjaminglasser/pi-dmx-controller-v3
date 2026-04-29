#!/usr/bin/env bash
# Workaround for SB Components Audio Codec HAT (WM8960) where the codec's
# first I2C reset write at boot occasionally fails with EIO (-5), so the
# kernel never instantiates the soundcard. A driver unbind/bind retry
# almost always succeeds because by then the I2C bus + HAT power are settled.
#
# This script is meant to be run from systemd at boot.
set -u

LOG_TAG="wm8960-rebind"

log() { logger -t "$LOG_TAG" -- "$*"; echo "[$LOG_TAG] $*"; }

# If the soundcard is already present, nothing to do.
if grep -q "^ *[0-9]\+ \[wm8960soundcard" /proc/asound/cards 2>/dev/null; then
  log "wm8960soundcard already registered, no rebind needed"
  exit 0
fi

# If the codec isn't bound at all, there's nothing to unbind/bind.
if [ ! -e /sys/bus/i2c/drivers/wm8960 ]; then
  log "wm8960 driver not loaded, aborting"
  exit 1
fi

# Try up to 5 rebind attempts.
for attempt in 1 2 3 4 5; do
  log "rebind attempt $attempt"
  echo 1-001a > /sys/bus/i2c/drivers/wm8960/unbind 2>/dev/null || true
  sleep 0.3
  echo 1-001a > /sys/bus/i2c/drivers/wm8960/bind   2>/dev/null || true
  sleep 1
  if grep -q "^ *[0-9]\+ \[wm8960soundcard" /proc/asound/cards 2>/dev/null; then
    log "wm8960soundcard registered after $attempt attempt(s)"
    # Re-apply stored mixer state in case rebind reverted it
    /usr/sbin/alsactl restore wm8960soundcard 2>/dev/null || true
    exit 0
  fi
done

log "FAILED to bring up wm8960soundcard after 5 attempts"
exit 1
