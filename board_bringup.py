#!/usr/bin/env python3
"""
board_bringup.py - Hardware bring-up test script for Pi DMX Controller v2

Tests:
  - 5 rotary encoders with quadrature decoding
  - SPI OLED display (SSD1309)
  - DMX output via UART
  - Reset button

PIN MAP (BCM):
  ENC1 (Page):    CLK=5,  DT=6,  SW=13
  ENC2 (Param A): CLK=17, DT=27, SW=22
  ENC3 (Param B): CLK=19, DT=26, SW=23
  ENC4 (Param C): CLK=16, DT=20, SW=21
  ENC5 (Bright):  CLK=4,  DT=18, SW=8 (SW disabled - SPI CE0 conflict)
  
  OLED (SSD1309 SPI):
    MOSI=GPIO10, SCLK=GPIO11, CS=GPIO7 (CE1), DC=GPIO24, RST=GPIO12
  
  DMX: UART TX=GPIO14 (/dev/serial0), 250000 baud 8N2
  
  Reset Button: GPIO25

Usage:
  python3 board_bringup.py --enc all       # Test all encoders
  python3 board_bringup.py --enc 1         # Test encoder 1 only
  python3 board_bringup.py --enc1          # Test encoder 1 isolated
  python3 board_bringup.py --enc5          # Test encoder 5 isolated (safe mode)
  python3 board_bringup.py --enc-each      # Test each encoder individually
  python3 board_bringup.py --oled          # Test OLED display
  python3 board_bringup.py --reset         # Test reset button
  sudo python3 board_bringup.py --dmx --channel 1 --value 255
  sudo python3 board_bringup.py --dmx --chase
  python3 board_bringup.py --all           # Run all tests

Dependencies:
  pip3 install --break-system-packages RPi.GPIO luma.oled pyserial

Troubleshooting:
  - Enable SPI: sudo raspi-config -> Interface Options -> SPI
  - Enable Serial: sudo raspi-config -> Interface Options -> Serial
  - Disable serial console: Remove console=serial0,115200 from /boot/cmdline.txt
  - GPIO busy: sudo killall python3 && sudo reboot
"""

import argparse
import signal
import sys
import time

# Global shutdown flag
_shutdown = False

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    global _shutdown
    _shutdown = True
    print("\n\nShutting down...")

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Encoder pin definitions
ENCODERS = {
    1: {"name": "Page",    "clk": 5,  "dt": 6,  "sw": 13},
    2: {"name": "Param A", "clk": 17, "dt": 27, "sw": 22},
    3: {"name": "Param B", "clk": 19, "dt": 26, "sw": 23},
    4: {"name": "Param C", "clk": 16, "dt": 20, "sw": 21},
    5: {"name": "Bright",  "clk": 4,  "dt": 18, "sw": 8},  # SW disabled
}

RESET_PIN = 25

# OLED settings
OLED_SPI_DEV = 1   # CE1 (GPIO 7)
OLED_DC_PIN = 24
OLED_RST_PIN = 12

# DMX settings
DMX_DEVICE = "/dev/serial0"
DMX_BAUD = 250000


def test_encoder(enc_nums, duration=None, isolate=False):
    """Test one or more encoders with quadrature decoding.
    
    Args:
        enc_nums: List of encoder numbers to test (1-5) or 'all'
        duration: Test duration in seconds (None = until Ctrl+C)
        isolate: If True, use isolated mode with slower polling
    """
    global _shutdown
    
    try:
        import RPi.GPIO as GPIO
    except ImportError:
        print("ERROR: RPi.GPIO not installed")
        print("  pip3 install --break-system-packages RPi.GPIO")
        return
    
    if enc_nums == 'all':
        enc_nums = [1, 2, 3, 4, 5]
    elif isinstance(enc_nums, int):
        enc_nums = [enc_nums]
    
    print("=" * 60)
    print("ENCODER TEST")
    if isolate and len(enc_nums) == 1:
        enc = ENCODERS[enc_nums[0]]
        print(f"*** ENCODER {enc_nums[0]} ISOLATED TEST MODE ***")
        if enc_nums[0] == 5:
            print("Using slower polling and extra debouncing for GPIO4/18")
    print("=" * 60)
    
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    
    # Setup pins
    print("\nPin Setup Status:")
    print("-" * 40)
    
    active_encoders = []
    pin_status = {}
    
    for num in enc_nums:
        enc = ENCODERS[num]
        print(f"Enc{num} ({enc['name']}):")
        
        # CLK pin
        try:
            GPIO.setup(enc['clk'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
            note = " (GPCLK0)" if enc['clk'] == 4 else ""
            print(f"  CLK (GPIO{enc['clk']:2d}): OK{note}")
            pin_status[(num, 'clk')] = True
        except Exception as e:
            print(f"  CLK (GPIO{enc['clk']:2d}): BUSY")
            pin_status[(num, 'clk')] = False
        
        # DT pin
        try:
            GPIO.setup(enc['dt'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
            note = " (PCM_CLK)" if enc['dt'] == 18 else ""
            print(f"  DT  (GPIO{enc['dt']:2d}): OK{note}")
            pin_status[(num, 'dt')] = True
        except Exception as e:
            print(f"  DT  (GPIO{enc['dt']:2d}): BUSY")
            pin_status[(num, 'dt')] = False
        
        # SW pin (skip GPIO8 for encoder 5)
        if num == 5:
            print(f"  SW  (GPIO {enc['sw']}): SKIP (SPI CE0)")
            pin_status[(num, 'sw')] = None
        else:
            try:
                GPIO.setup(enc['sw'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
                print(f"  SW  (GPIO{enc['sw']:2d}): OK")
                pin_status[(num, 'sw')] = True
            except Exception as e:
                print(f"  SW  (GPIO{enc['sw']:2d}): BUSY (skip)")
                pin_status[(num, 'sw')] = False
        
        # Only add if CLK and DT are working
        if pin_status[(num, 'clk')] and pin_status[(num, 'dt')]:
            active_encoders.append(num)
    
    if not active_encoders:
        print("\nNo encoders available to test!")
        GPIO.cleanup()
        return
    
    # Polling interval
    poll_interval = 0.015 if (5 in enc_nums) else 0.005
    if isolate:
        poll_interval = 0.015
    print(f"\nUsing polling interval: {int(poll_interval * 1000)}ms")
    
    # Quadrature state machine
    # Transition table: [old_state][new_state] -> direction
    transition = [
        [  0, -1,  1,  0],  # old = 0
        [  1,  0,  0, -1],  # old = 1
        [ -1,  0,  0,  1],  # old = 2
        [  0,  1, -1,  0],  # old = 3
    ]
    
    # Initialize state
    enc_state = {}
    enc_count = {}
    enc_position = {}
    enc_rotation_detected = {}
    enc_button_detected = {}
    last_sw = {}
    
    # Debounce buffers (3 samples)
    DEBOUNCE_SAMPLES = 3
    clk_buffer = {n: [1] * DEBOUNCE_SAMPLES for n in active_encoders}
    dt_buffer = {n: [1] * DEBOUNCE_SAMPLES for n in active_encoders}
    
    for num in active_encoders:
        enc = ENCODERS[num]
        clk = GPIO.input(enc['clk'])
        dt = GPIO.input(enc['dt'])
        enc_state[num] = (clk << 1) | dt
        enc_count[num] = 0
        enc_position[num] = 0
        enc_rotation_detected[num] = False
        enc_button_detected[num] = False
        if pin_status[(num, 'sw')]:
            last_sw[num] = GPIO.input(enc['sw'])
        else:
            last_sw[num] = 1
    
    print(f"\nMonitoring {len(active_encoders)} encoder(s). Rotate and press buttons to test.")
    if duration:
        print(f"Running for {duration} seconds...")
    else:
        print("Press Ctrl+C to exit.")
    print("-" * 60)
    
    start_time = time.time()
    
    try:
        while not _shutdown:
            if duration and (time.time() - start_time) >= duration:
                break
            
            for num in active_encoders:
                enc = ENCODERS[num]
                
                # Read with debouncing
                clk_buffer[num].pop(0)
                clk_buffer[num].append(GPIO.input(enc['clk']))
                dt_buffer[num].pop(0)
                dt_buffer[num].append(GPIO.input(enc['dt']))
                
                # Use majority vote for debouncing
                clk = 1 if sum(clk_buffer[num]) > DEBOUNCE_SAMPLES // 2 else 0
                dt = 1 if sum(dt_buffer[num]) > DEBOUNCE_SAMPLES // 2 else 0
                
                new_state = (clk << 1) | dt
                old_state = enc_state[num]
                
                if new_state != old_state:
                    enc_state[num] = new_state
                    direction = transition[old_state][new_state]
                    
                    if direction != 0:
                        enc_count[num] += direction
                        
                        # Threshold of 2 for responsive feel
                        if enc_count[num] >= 2:
                            enc_count[num] = 0
                            enc_position[num] += 1
                            enc_rotation_detected[num] = True
                            print(f"Enc{num} ({enc['name']}): Position = {enc_position[num]}")
                        elif enc_count[num] <= -2:
                            enc_count[num] = 0
                            enc_position[num] -= 1
                            enc_rotation_detected[num] = True
                            print(f"Enc{num} ({enc['name']}): Position = {enc_position[num]}")
                
                # Check button (skip encoder 5)
                if pin_status[(num, 'sw')]:
                    sw = GPIO.input(enc['sw'])
                    if sw == 0 and last_sw[num] == 1:
                        time.sleep(0.02)  # Debounce
                        if GPIO.input(enc['sw']) == 0:
                            enc_button_detected[num] = True
                            print(f"Enc{num} ({enc['name']}): BUTTON PRESSED")
                    last_sw[num] = sw
            
            time.sleep(poll_interval)
    
    finally:
        GPIO.cleanup()
    
    # Summary
    print("-" * 60)
    print("SUMMARY:")
    for num in enc_nums:
        enc = ENCODERS[num]
        rotation = "detected" if enc_rotation_detected.get(num) else "none"
        if num == 5:
            button = "n/a (GPIO8=SPI)"
        elif enc_button_detected.get(num):
            button = "detected"
        elif pin_status.get((num, 'sw')) is False:
            button = "GPIO error"
        else:
            button = "none"
        
        status = "PASS" if enc_rotation_detected.get(num) else "no rotation"
        print(f"  Enc{num} ({enc['name']}): rotation={rotation}, button={button} -> {status}")


def test_encoders_individually(duration=10):
    """Test each encoder one at a time."""
    global _shutdown
    
    print("=" * 60)
    print("INDIVIDUAL ENCODER TESTS")
    print("Testing each encoder separately for", duration, "seconds each")
    print("=" * 60)
    
    for num in range(1, 6):
        if _shutdown:
            break
        print(f"\n>>> Testing Encoder {num} <<<")
        test_encoder([num], duration=duration, isolate=True)
        if not _shutdown and num < 5:
            print("\nMoving to next encoder in 2 seconds...")
            time.sleep(2)


def test_oled(duration=10):
    """Test OLED display with visual pattern."""
    global _shutdown
    
    print("=" * 60)
    print("OLED TEST")
    print("=" * 60)
    
    try:
        from PIL import Image, ImageDraw, ImageFont
        from luma.core.interface.serial import spi as luma_spi
        from luma.oled.device import ssd1309
    except ImportError:
        print("ERROR: luma.oled not installed")
        print("  pip3 install --break-system-packages luma.oled")
        return
    
    try:
        serial = luma_spi(
            device=OLED_SPI_DEV,
            port=0,
            bus_speed_hz=2000000,
            gpio_DC=OLED_DC_PIN,
            gpio_RST=OLED_RST_PIN,
        )
        device = ssd1309(serial, width=128, height=64)
        print("[OK] OLED initialized")
    except Exception as e:
        print(f"ERROR: Failed to initialize OLED: {e}")
        return
    
    try:
        font = ImageFont.load_default()
    except:
        font = None
    
    print(f"Running visual test for {duration} seconds...")
    print("You should see 'OLED OK' text and a moving bar.")
    
    start_time = time.time()
    bar_pos = 0
    bar_dir = 1
    
    while not _shutdown and (time.time() - start_time) < duration:
        img = Image.new("1", (128, 64), 0)
        draw = ImageDraw.Draw(img)
        
        # Draw text
        draw.text((30, 10), "OLED OK", fill=1, font=font)
        draw.text((20, 30), "Pi DMX v2", fill=1, font=font)
        
        # Draw moving bar
        draw.rectangle([bar_pos, 55, bar_pos + 20, 63], fill=1)
        
        device.display(img)
        
        # Animate bar
        bar_pos += bar_dir * 3
        if bar_pos >= 108 or bar_pos <= 0:
            bar_dir *= -1
        
        time.sleep(0.05)
    
    # Clear display
    device.clear()
    print("[OK] OLED test complete")


def test_reset(duration=None):
    """Test reset button."""
    global _shutdown
    
    try:
        import RPi.GPIO as GPIO
    except ImportError:
        print("ERROR: RPi.GPIO not installed")
        return
    
    print("=" * 60)
    print("RESET BUTTON TEST")
    print("=" * 60)
    
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    
    try:
        GPIO.setup(RESET_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        print(f"[OK] Reset button (GPIO{RESET_PIN}) configured")
    except Exception as e:
        print(f"ERROR: Failed to setup GPIO{RESET_PIN}: {e}")
        return
    
    print("\nPress the reset button to test. Press Ctrl+C to exit.")
    if duration:
        print(f"Running for {duration} seconds...")
    print("-" * 60)
    
    last_state = GPIO.input(RESET_PIN)
    press_count = 0
    start_time = time.time()
    
    try:
        while not _shutdown:
            if duration and (time.time() - start_time) >= duration:
                break
            
            state = GPIO.input(RESET_PIN)
            if state == 0 and last_state == 1:
                time.sleep(0.02)  # Debounce
                if GPIO.input(RESET_PIN) == 0:
                    press_count += 1
                    print(f"Reset button PRESSED (count: {press_count})")
            last_state = state
            time.sleep(0.01)
    finally:
        GPIO.cleanup()
    
    print("-" * 60)
    print(f"SUMMARY: {press_count} button press(es) detected")


def test_dmx(channel=1, value=255, chase=False, duration=10):
    """Test DMX output."""
    global _shutdown
    
    print("=" * 60)
    print("DMX TEST")
    print("=" * 60)
    
    try:
        import serial
    except ImportError:
        print("ERROR: pyserial not installed")
        print("  pip3 install --break-system-packages pyserial")
        return
    
    try:
        ser = serial.Serial(
            port=DMX_DEVICE,
            baudrate=DMX_BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0
        )
        print(f"[OK] Serial port {DMX_DEVICE} opened at {DMX_BAUD} baud")
    except Exception as e:
        print(f"ERROR: Failed to open serial port: {e}")
        print("\nTroubleshooting:")
        print("  1. Enable serial: sudo raspi-config -> Interface Options -> Serial")
        print("  2. Disable serial console in /boot/cmdline.txt")
        print("  3. Run with sudo: sudo python3 board_bringup.py --dmx")
        return
    
    def send_break():
        """Send DMX break signal."""
        try:
            # Try using break_condition (preferred)
            ser.break_condition = True
            time.sleep(0.000092)  # 92us break
            ser.break_condition = False
            time.sleep(0.000012)  # 12us MAB
            return True
        except:
            # Fallback: switch to low baud rate
            ser.baudrate = 50000
            ser.write(b'\x00')
            ser.flush()
            ser.baudrate = DMX_BAUD
            return False
    
    def send_dmx_frame(data):
        """Send a complete DMX frame."""
        send_break()
        # Start code (0) + up to 512 channels
        frame = bytes([0] + list(data[:512]))
        ser.write(frame)
        ser.flush()
    
    # Create DMX data buffer
    dmx_data = [0] * 512
    
    if chase:
        print(f"Running chase pattern on channel {channel} for {duration} seconds...")
        print("DMX value will ramp up and down.")
    else:
        print(f"Setting channel {channel} to {value} for {duration} seconds...")
    
    start_time = time.time()
    chase_value = 0
    chase_dir = 5
    
    try:
        while not _shutdown and (time.time() - start_time) < duration:
            if chase:
                dmx_data[channel - 1] = chase_value
                chase_value += chase_dir
                if chase_value >= 255:
                    chase_value = 255
                    chase_dir = -5
                elif chase_value <= 0:
                    chase_value = 0
                    chase_dir = 5
            else:
                dmx_data[channel - 1] = value
            
            send_dmx_frame(dmx_data)
            time.sleep(0.023)  # ~44 frames/sec
    finally:
        # Blackout
        dmx_data = [0] * 512
        send_dmx_frame(dmx_data)
        ser.close()
    
    print("[OK] DMX test complete")


def main():
    parser = argparse.ArgumentParser(
        description="Hardware bring-up test for Pi DMX Controller v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument("--enc", metavar="N", 
                       help="Test encoder(s): 1-5 or 'all'")
    parser.add_argument("--enc1", action="store_true",
                       help="Test encoder 1 in isolated mode")
    parser.add_argument("--enc2", action="store_true",
                       help="Test encoder 2 in isolated mode")
    parser.add_argument("--enc3", action="store_true",
                       help="Test encoder 3 in isolated mode")
    parser.add_argument("--enc4", action="store_true",
                       help="Test encoder 4 in isolated mode")
    parser.add_argument("--enc5", action="store_true",
                       help="Test encoder 5 in isolated mode (safe mode)")
    parser.add_argument("--enc-each", action="store_true",
                       help="Test each encoder individually")
    parser.add_argument("--oled", action="store_true",
                       help="Test OLED display")
    parser.add_argument("--reset", action="store_true",
                       help="Test reset button")
    parser.add_argument("--dmx", action="store_true",
                       help="Test DMX output")
    parser.add_argument("--channel", type=int, default=1,
                       help="DMX channel (1-512, default: 1)")
    parser.add_argument("--value", type=int, default=255,
                       help="DMX value (0-255, default: 255)")
    parser.add_argument("--chase", action="store_true",
                       help="Run DMX chase pattern")
    parser.add_argument("--duration", type=int, default=10,
                       help="Test duration in seconds (default: 10)")
    parser.add_argument("--all", action="store_true",
                       help="Run all tests")
    
    args = parser.parse_args()
    
    # Check if any test was requested
    any_test = (args.enc or args.enc1 or args.enc2 or args.enc3 or 
                args.enc4 or args.enc5 or args.enc_each or
                args.oled or args.reset or args.dmx or args.all)
    
    if not any_test:
        parser.print_help()
        return
    
    # Run tests
    if args.all:
        print("\n=== RUNNING ALL TESTS ===\n")
        test_encoder('all', duration=args.duration)
        if not _shutdown:
            test_oled(duration=args.duration)
        if not _shutdown:
            test_reset(duration=args.duration)
        if not _shutdown:
            test_dmx(channel=args.channel, value=args.value, 
                    chase=args.chase, duration=args.duration)
    else:
        if args.enc:
            if args.enc.lower() == 'all':
                test_encoder('all', duration=args.duration)
            else:
                try:
                    enc_num = int(args.enc)
                    if 1 <= enc_num <= 5:
                        test_encoder([enc_num], duration=args.duration)
                    else:
                        print("ERROR: Encoder number must be 1-5")
                except ValueError:
                    print("ERROR: Invalid encoder number")
        
        if args.enc1:
            test_encoder([1], duration=args.duration, isolate=True)
        if args.enc2:
            test_encoder([2], duration=args.duration, isolate=True)
        if args.enc3:
            test_encoder([3], duration=args.duration, isolate=True)
        if args.enc4:
            test_encoder([4], duration=args.duration, isolate=True)
        if args.enc5:
            test_encoder([5], duration=args.duration, isolate=True)
        
        if args.enc_each:
            test_encoders_individually(duration=args.duration)
        
        if args.oled:
            test_oled(duration=args.duration)
        
        if args.reset:
            test_reset(duration=args.duration)
        
        if args.dmx:
            test_dmx(channel=args.channel, value=args.value,
                    chase=args.chase, duration=args.duration)
    
    print("\nDone!")


if __name__ == "__main__":
    main()
