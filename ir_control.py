#!/usr/bin/env python3
# ir_control.py — low-level send/listen/ping over HTTP or serial
import sys, time, argparse
from transport import make_transport, add_transport_arg, POLL_HZ


def main():
    parser = argparse.ArgumentParser(description="Low-level IR Remote control")
    add_transport_arg(parser)
    sub = parser.add_subparsers(dest="action")

    p = sub.add_parser("send", help="Send a decoded signal")
    p.add_argument("protocol")
    p.add_argument("address", type=lambda x: hex(int(x, 16)))
    p.add_argument("command", type=lambda x: hex(int(x, 16)))
    p.add_argument("--repeats", type=int, default=0)

    sub.add_parser("listen", help="Poll for received signals until Ctrl+C")
    sub.add_parser("ping",   help="Check ESP32 connectivity")

    args      = parser.parse_args()
    transport = make_transport(args.serial)

    if args.action == "send":
        signal = {
            "protocol": args.protocol,
            "address":  args.address,
            "command":  args.command,
            "repeats":  args.repeats,
        }
        print("OK" if transport.send(signal) else "ERROR")

    elif args.action == "listen":
        mode = f"serial ({args.serial})" if args.serial else "WiFi HTTP"
        print(f"Listening via {mode} (Ctrl+C to stop)...")
        while True:
            try:
                sig = transport.recv()
                if sig:
                    print(
                        f"protocol={sig.get('protocol')}  "
                        f"address={sig.get('address')}  "
                        f"command={sig.get('command')}  "
                        f"value={sig.get('value', '')}"
                    )
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"  error: {e}")
                time.sleep(2)
            time.sleep(POLL_HZ)

    elif args.action == "ping":
        try:
            transport.ping()
            print("PONG")
        except Exception as e:
            print(f"FAIL: {e}")
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
