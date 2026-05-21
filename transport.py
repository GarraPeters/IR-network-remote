#!/usr/bin/env python3
# transport.py — HTTP or USB-serial transport for IR Remote ESP32
import time

POLL_HZ  = 0.2   # seconds between recv polls
BASE_URL = "http://192.168.1.141"


def _parse_recv(text):
    signal = {}
    for part in text.strip().split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            signal[k] = v
    return signal


class HttpTransport:
    def __init__(self, base_url=BASE_URL):
        import requests
        self._req = requests
        self.base = base_url

    def ping(self):
        self._req.get(f"{self.base}/ping", timeout=3)

    def recv(self):
        """Returns parsed signal dict or None."""
        r = self._req.get(f"{self.base}/recv", timeout=5)
        if r.status_code == 200 and r.text.strip():
            return _parse_recv(r.text)
        return None

    def recv_raw(self):
        """Returns list of int timings or None."""
        r = self._req.get(f"{self.base}/recv/raw", timeout=5)
        if r.status_code == 200 and r.text.strip():
            return [int(x) for x in r.text.strip().split(",") if x.strip()]
        return None

    def send(self, signal):
        params = {
            "protocol": signal["protocol"],
            "address":  signal["address"],
            "command":  signal["command"],
            "repeats":  signal.get("repeats", 0),
        }
        if signal.get("value"):
            params["value"] = signal["value"]
        try:
            r = self._req.post(f"{self.base}/send", params=params, timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def send_raw(self, timings, repeats=0):
        raw_str = ",".join(str(t) for t in timings)
        try:
            r = self._req.post(f"{self.base}/send/raw",
                               data={"raw": raw_str, "repeats": repeats}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def send_sequence(self, signals, db, delay_ms=300):
        """POST a full signal sequence to the ESP32 for local execution.
        Returns the number of signals queued, or 0 on error."""
        parts = []
        for name in signals:
            if name.startswith('delay:'):
                ms = int(name.split(':')[1])
                parts.append(f"DELAY,{hex(ms)},0,0")
                continue
            if name not in db:
                print(f"  unknown signal: {name!r}")
                return 0
            sig = db[name]
            if "raw" in sig:
                print(f"  warning: raw signal {name!r} not supported in sequences, skipping")
                continue
            entry = f"{sig['protocol']},{sig['address']},{sig['command']},{sig.get('repeats', 0)}"
            if sig.get("value"):
                entry += f",{sig['value']}"
            parts.append(entry)
        if not parts:
            return 0
        try:
            r = self._req.post(
                f"{self.base}/sequence",
                data={"delay": delay_ms, "seq": "|".join(parts)},
                timeout=10,
            )
            if r.status_code == 200:
                words = r.text.strip().split()
                return int(words[1]) if len(words) > 1 else len(parts)
            print(f"  ERROR: /sequence returned {r.status_code}: {r.text.strip()}")
            return 0
        except Exception as e:
            print(f"  ERROR: {e}")
            return 0


class SerialTransport:
    """Communicates with the ESP32 over USB serial.

    Protocol (newline-terminated commands, single-line responses):
      PING                                    → PONG
      RECV                                    → protocol=...&... | NONE
      RECV_RAW                                → 9000,4500,...   | NONE
      SEND <proto> <addr_hex> <cmd_hex> <rep> → OK | ERROR ...
      SEND_RAW <repeats> <timings_csv>        → OK | ERROR ...
    """

    def __init__(self, port, baud=115200):
        import serial
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.5  # readline timeout per attempt
        self.ser.dtr = False    # EN line — keep high (not in reset)
        self.ser.rts = False    # GPIO0 line — keep high (normal boot, not bootloader)
        self.ser.open()

        start = time.time()
        showed_waiting = False
        for _ in range(50):  # up to ~25s
            self.ser.write(b"PING\n")
            if self.ser.readline().decode("utf-8", errors="ignore").strip() == "PONG":
                if showed_waiting:
                    print(" ready")
                # drain leftover PONGs queued while the ESP32 was booting
                self.ser.timeout = 0.1
                while self.ser.readline():
                    pass
                self.ser.timeout = 5
                return
            if not showed_waiting and time.time() - start > 1.0:
                print("  Waiting for ESP32...", end="", flush=True)
                showed_waiting = True
            elif showed_waiting:
                print(".", end="", flush=True)

        if showed_waiting:
            print()
        raise ConnectionError(f"ESP32 on {port} did not respond — is the firmware flashed?")

    def _cmd(self, line):
        self.ser.reset_input_buffer()
        self.ser.write((line + "\n").encode())
        resp = self.ser.readline().decode("utf-8", errors="ignore").strip()
        if resp != "PONG" and resp != "NONE" and resp != "OK":
            print(f"  [serial] cmd={line[:40]!r}... resp={resp!r}")
        return resp

    def ping(self):
        resp = self._cmd("PING")
        if resp != "PONG":
            raise ConnectionError(f"Expected PONG, got: {resp!r}")

    def recv(self):
        resp = self._cmd("RECV")
        if not resp or resp == "NONE":
            return None
        return _parse_recv(resp)

    def recv_raw(self):
        resp = self._cmd("RECV_RAW")
        if not resp or resp == "NONE":
            return None
        return [int(x) for x in resp.split(",") if x.strip()]

    def send(self, signal):
        line = (f"SEND {signal['protocol']} {signal['address']} "
                f"{signal['command']} {signal.get('repeats', 0)}")
        if signal.get("value"):
            line += f" {signal['value']}"
        resp = self._cmd(line)
        return resp == "OK"

    def send_raw(self, timings, repeats=0):
        raw_str = ",".join(str(t) for t in timings)
        resp = self._cmd(f"SEND_RAW {repeats} {raw_str}")
        return resp == "OK"

    def send_sequence(self, signals, db, delay_ms=300):
        """Send signals one-by-one over serial (no batch support on firmware side)."""
        for name in signals:
            if name.startswith('delay:'):
                time.sleep(int(name.split(':')[1]) / 1000)
                continue
            if name not in db:
                print(f"  unknown signal: {name!r}")
                continue
            sig = db[name]
            time.sleep(delay_ms / 1000)
            if "raw" in sig:
                self.send_raw(sig["raw"], sig.get("repeats", 0))
            else:
                self.send(sig)
        return len(signals)


def find_esp32_port():
    """Return the first USB serial port that looks like an ESP32, or raise."""
    import glob
    import serial

    candidates = (
        glob.glob("/dev/cu.wchusbserial*") +
        glob.glob("/dev/cu.usbserial*") +
        glob.glob("/dev/cu.SLAB_USBtoUART*") +
        glob.glob("/dev/ttyUSB*") +
        glob.glob("/dev/ttyACM*")
    )
    if not candidates:
        raise ConnectionError("No USB serial ports found — is the ESP32 plugged in?")
    if len(candidates) == 1:
        return candidates[0]

    # Multiple ports: probe each without pulsing DTR (avoids resetting the ESP32)
    for port in candidates:
        try:
            s = serial.Serial()
            s.port = port
            s.baudrate = 115200
            s.timeout = 0.5
            s.dtr = False
            s.open()
            s.reset_input_buffer()
            s.write(b"PING\n")
            resp = s.readline().decode().strip()
            s.close()
            if resp == "PONG":
                return port
        except Exception:
            pass

    return candidates[0]


def make_transport(serial_port=None):
    """Return the appropriate transport based on CLI args."""
    if serial_port == "auto":
        serial_port = find_esp32_port()
        print(f"  Auto-detected ESP32 on {serial_port}")
    if serial_port:
        return SerialTransport(serial_port)
    return HttpTransport()


def add_transport_arg(parser):
    """Add --serial / --port flags to an argparse parser."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--serial", dest="serial", action="store_const", const="auto",
        help="Use USB serial, auto-detect port"
    )
    group.add_argument(
        "--port", dest="serial", metavar="PORT",
        help="Use USB serial on explicit PORT (e.g. /dev/cu.wchusbserial1410)"
    )
