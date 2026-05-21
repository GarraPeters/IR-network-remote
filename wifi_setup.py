#!/usr/bin/env python3
"""Update WiFi credentials on the ESP32 over serial without reflashing."""

import argparse
import getpass
import sys
import time

from transport import SerialTransport, find_esp32_port, add_transport_arg


def set_wifi(port, ssid, password):
    print(f"  Connecting to ESP32 on {port}...")
    t = SerialTransport(port)

    t.ser.reset_input_buffer()
    cmd = f"WIFI {ssid} {password}"
    t.ser.write((cmd + "\n").encode())
    resp = t.ser.readline().decode("utf-8", errors="ignore").strip()

    if resp == "OK reconnecting":
        print(f"  Saved. ESP32 is reconnecting to '{ssid}'.")
        print("  Watch the display — it will show the new IP once connected.")
    else:
        print(f"  Unexpected response: {resp!r}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Update ESP32 WiFi credentials over serial.")
    add_transport_arg(parser)
    parser.add_argument("ssid",     nargs="?", help="WiFi network name")
    parser.add_argument("password", nargs="?", help="WiFi password (prompted if omitted)")
    args = parser.parse_args()

    port = args.serial or "auto"
    if port == "auto":
        try:
            port = find_esp32_port()
            print(f"  Auto-detected ESP32 on {port}")
        except ConnectionError as e:
            print(f"  {e}", file=sys.stderr)
            sys.exit(1)

    ssid = args.ssid or input("SSID: ")
    password = args.password or getpass.getpass("Password: ")

    if not ssid:
        print("  SSID cannot be empty.", file=sys.stderr)
        sys.exit(1)

    set_wifi(port, ssid, password)


if __name__ == "__main__":
    main()
