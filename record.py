#!/usr/bin/env python3
# record.py — capture IR signals from ESP32 and save to file
import json, os, sys, time, argparse
from transport import make_transport, add_transport_arg, POLL_HZ

DB_FILE = "ir_signals.json"


def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {}


def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)
    print(f"  Saved to {DB_FILE}")


def wait_for_decoded_signal(transport):
    while True:
        try:
            sig = transport.recv()
            if sig is not None:
                return sig
        except Exception as e:
            print(f"  Connection error: {e}")
            time.sleep(2)
        time.sleep(POLL_HZ)


def wait_for_raw_signal(transport):
    while True:
        try:
            timings = transport.recv_raw()
            if timings:
                return timings
        except Exception as e:
            print(f"  Connection error: {e}")
            time.sleep(2)
        time.sleep(POLL_HZ)


def main():
    parser = argparse.ArgumentParser(description="Record IR signals from ESP32")
    add_transport_arg(parser)
    parser.add_argument("--raw", action="store_true",
                        help="Always capture raw timings, skip protocol decode")
    args = parser.parse_args()

    transport = make_transport(args.serial)
    mode = f"serial ({args.serial})" if args.serial else "WiFi HTTP"
    print(f"IR Signal Recorder — saving to {DB_FILE}")
    print(f"Transport: {mode}\n")

    try:
        transport.ping()
    except Exception as e:
        print(f"ERROR: Can't reach ESP32 — {e}")
        sys.exit(1)

    db = load_db()
    print(f"Loaded {len(db)} existing signals.\n")

    while True:
        name = input("Enter signal name (or 'list', 'quit'): ").strip()

        if name == "quit":
            break
        elif name == "list":
            if db:
                for k, v in db.items():
                    if "raw" in v:
                        print(f"  {k:20s} RAW ({len(v['raw'])} timings)")
                    else:
                        print(f"  {k:20s} {v['protocol']} addr={v['address']} cmd={v['command']}")
            else:
                print("  No signals recorded yet.")
            print()
            continue
        elif not name:
            continue

        if name in db:
            overwrite = input(f"  '{name}' already exists. Overwrite? (y/n): ")
            if overwrite.lower() != "y":
                continue

        print(f"  Point remote at ESP32 and press the button for '{name}'...")

        if args.raw:
            raw_timings = wait_for_raw_signal(transport)
            entry = {"raw": raw_timings, "repeats": 0}
            print(f"  Captured raw: {len(raw_timings)} timing values")
        else:
            signal = wait_for_decoded_signal(transport)
            proto = signal.get("protocol", "").upper()
            if proto in ("UNKNOWN", ""):
                print("  Protocol unrecognised — press the button again for raw capture...")
                raw_timings = wait_for_raw_signal(transport)
                entry = {"raw": raw_timings, "repeats": 0}
                print(f"  Captured raw: {len(raw_timings)} timing values")
            else:
                entry = {
                    "protocol": proto,
                    "address":  signal.get("address", "0x0"),
                    "command":  signal.get("command", "0x0"),
                    "value":    signal.get("value", ""),
                    "repeats":  2 if proto == "SONY" else 0,
                }
                print(f"  Captured: {entry['protocol']} addr={entry['address']} cmd={entry['command']}")

        db[name] = entry
        save_db(db)
        print()


if __name__ == "__main__":
    main()
