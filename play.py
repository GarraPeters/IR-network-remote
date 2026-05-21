#!/usr/bin/env python3
# play.py — transmit recorded IR signals through ESP32
import json, os, sys, time, argparse
from transport import make_transport, add_transport_arg, BASE_URL

DB_FILE = "ir_signals.json"


def load_db():
    if not os.path.exists(DB_FILE):
        print(f"ERROR: {DB_FILE} not found. Run record.py first.")
        sys.exit(1)
    with open(DB_FILE) as f:
        return json.load(f)


def send_signal(name, signal, transport, delay_after=0.5):
    if "raw" in signal:
        ok = transport.send_raw(signal["raw"], signal.get("repeats", 0))
        status = "OK" if ok else "ERROR"
        print(f"  {name:20s} RAW ({len(signal['raw'])} timings)  [{status}]")
    else:
        ok = transport.send(signal)
        status = "OK" if ok else "ERROR"
        print(f"  {name:20s} {signal['protocol']} addr={signal['address']} cmd={signal['command']}  [{status}]")
    time.sleep(delay_after)
    return ok


def main():
    parser = argparse.ArgumentParser(description="Play back recorded IR signals")
    add_transport_arg(parser)
    sub = parser.add_subparsers(dest="action")

    p_send = sub.add_parser("send", help="Send a single named signal")
    p_send.add_argument("name")

    p_seq = sub.add_parser("sequence", help="Send a sequence of signals")
    p_seq.add_argument("names", nargs="+")
    p_seq.add_argument("--delay", type=float, default=0.5,
                       help="Seconds between signals (default 0.5)")

    sub.add_parser("list", help="List all recorded signals")

    args = parser.parse_args()
    db   = load_db()

    if args.action == "list":
        print(f"Signals in {DB_FILE}:")
        for name, sig in db.items():
            if "raw" in sig:
                raw_str = ",".join(str(t) for t in sig["raw"])
                curl = f'curl -X POST "{BASE_URL}/send/raw" -d "repeats={sig.get("repeats", 0)}&raw={raw_str}"'
                print(f"  {name:20s}  RAW ({len(sig['raw'])} timings)")
                print(f"  {'':20s}  {curl}")
            else:
                params = f"protocol={sig['protocol']}&address={sig['address']}&command={sig['command']}&repeats={sig.get('repeats', 0)}"
                if sig.get("value"):
                    params += f"&value={sig['value']}"
                curl = f'curl -X POST "{BASE_URL}/send?{params}"'
                print(f"  {name:20s}  {sig['protocol']} addr={sig['address']} cmd={sig['command']}")
                print(f"  {'':20s}  {curl}")

    elif args.action == "send":
        if args.name not in db:
            print(f"ERROR: '{args.name}' not found. Run 'play.py list' to see available signals.")
            sys.exit(1)
        send_signal(args.name, db[args.name], make_transport(args.serial))

    elif args.action == "sequence":
        missing = [n for n in args.names if n not in db]
        if missing:
            print(f"ERROR: unknown signals: {', '.join(missing)}")
            sys.exit(1)
        transport = make_transport(args.serial)
        print(f"Sending sequence of {len(args.names)} signals...")
        for name in args.names:
            send_signal(name, db[name], transport, delay_after=args.delay)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
