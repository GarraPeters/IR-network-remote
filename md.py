#!/usr/bin/env python3
# md.py — interactive Minidisc player control
import json, os, sys, time, argparse, tty, termios
from collections import deque
from transport import make_transport, add_transport_arg

DB_FILE = "ir_signals.json"
DELAY   = 0.5   # seconds between button presses

CHAR_SETS = {
    'lower': {
        '2': 'abc', '3': 'def', '4': 'ghi', '5': 'jkl',
        '6': 'mno', '7': 'prs', '8': 'tuv', '9': 'wxy',
        '0': 'qz',
    },
    'upper': {
        '2': 'ABC', '3': 'DEF', '4': 'GHI', '5': 'JKL',
        '6': 'MNO', '7': 'PRS', '8': 'TUV', '9': 'WXY',
        '0': 'QZ',
    },
    'number': {str(i): str(i) for i in range(10)},
    'special': {
        '1': '.,!?-\'()',
    },
}
MODE_CYCLE = ['number', 'upper', 'lower']

_R   = "\033[0m"
_B   = "\033[1m"
_DIM = "\033[2m"
_CYN = "\033[96m"
_YEL = "\033[93m"
_GRN = "\033[92m"
_RED = "\033[91m"

_MODE_CLR = {'lower': _GRN, 'upper': _YEL, 'number': _CYN}


def _status_color(s):
    sl = s.lower()
    if any(w in sl for w in ('error', 'fail', 'not found', 'unknown', 'empty')):
        return _RED
    if s in ('ready', 'cancelled', 'nothing typed'):
        return _DIM
    return _GRN


def find_char(ch):
    for mode in MODE_CYCLE:
        for key, chars in CHAR_SETS[mode].items():
            idx = chars.find(ch)
            if idx != -1:
                return (mode, key, idx + 1)
    return None


def mode_presses(current, target):
    ci = MODE_CYCLE.index(current)
    ti = MODE_CYCLE.index(target)
    return (ti - ci) % len(MODE_CYCLE)


def build_sequence(text, start_mode):
    mode = start_mode
    signals = []
    skipped = []
    for ch in text:
        if ch == ' ':
            signals.append('md-ff')
            continue
        mapping = find_char(ch)
        if mapping is None:
            skipped.append(ch)
            continue
        target_mode, key, count = mapping
        for _ in range(mode_presses(mode, target_mode)):
            signals.append('md-char')
        mode = target_mode
        for _ in range(count):
            signals.append(f'md-{key}')
        if target_mode != 'number':
            signals.append('md-ff')
    for _ in range(mode_presses(mode, 'lower')):
        signals.append('md-char')
    if skipped:
        print(f"  skipped: {skipped}")
    return signals, 'lower'


def type_string(text, start_mode, transport, db):
    signals, final_mode = build_sequence(text, start_mode)
    if not signals:
        return final_mode
    delay_ms = int(DELAY * 1000)
    count = transport.send_sequence(signals, db, delay_ms)
    if count:
        wait = count * DELAY + 1.0
        print(f"  {count} signals queued, waiting ~{wait:.0f}s...")
        time.sleep(wait)
    else:
        print("  ERROR: sequence failed to send")
    return final_mode


def load_names(path):
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def name_tracks(names, transport, db):
    full_seq = []
    for i, name in enumerate(names):
        print(f"  [{i + 1}/{len(names)}] {name!r}")
        typing_signals, _ = build_sequence(name, 'lower')
        full_seq += ['md-name', 'delay:2000'] + typing_signals + ['md-name', 'delay:3000']
        if i < len(names) - 1:
            full_seq.append('md-next')

    delay_count = sum(1 for s in full_seq if s.startswith('delay:'))
    print(f"  Sending {len(names)} track(s) as one sequence ({len(full_seq)} signals)...")
    count = transport.send_sequence(full_seq, db, int(DELAY * 1000))
    if count:
        pause_total = len(names) * 5.0  # 2s + 3s per track
        wait = (count - delay_count) * DELAY + pause_total + 1.0
        print(f"  {count} signals queued, waiting ~{wait:.0f}s...")
        time.sleep(wait)
    else:
        print("  ERROR: sequence failed to send")
    print(f"  Done. Named {len(names)} track(s).")


def load_db():
    if not os.path.exists(DB_FILE):
        print(f"ERROR: {DB_FILE} not found. Run record.py first.")
        sys.exit(1)
    with open(DB_FILE) as f:
        return json.load(f)


def send(name, transport, db, retries=2):
    if name not in db:
        print(f"  unknown signal: {name!r}")
        return False
    sig = db[name]
    time.sleep(DELAY)
    for attempt in range(1 + retries):
        if "raw" in sig:
            ok = transport.send_raw(sig["raw"], sig.get("repeats", 0))
        else:
            ok = transport.send(sig)
        if ok:
            return True
        if attempt < retries:
            print(f"  retry {attempt + 1}/{retries} for {name!r}...")
            time.sleep(0.3)
    print(f"  ERROR sending {name!r} after {1 + retries} attempts")
    return False


def _getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _draw_menu(current_mode, history):
    print("\033[2J\033[H", end="")
    mc = _MODE_CLR.get(current_mode, _R)
    print("  ┌─────────────────────────────┐")
    print(f"  │      {_B}{_CYN}MINIDISC CONTROL{_R}       │")
    print("  ├─────────────────────────────┤")
    print(f"  │  delay: {DELAY:.1f}s   mode: {mc}{_B}{current_mode:<9}{_R}│")
    print("  ├─────────────────────────────┤")
    print("  │  n  name track              │")
    print("  │  b  batch name from file    │")
    print("  │  t  type text               │")
    print("  │  o  power on/off            │")
    print("  │  ]  next track              │")
    print("  │  [  previous track          │")
    print("  │  f  ff  (next char)         │")
    print("  │  r  rw  (prev char)         │")
    print("  │  c  cycle char mode         │")
    print("  │  s  send signal             │")
    print("  │  +  delay up                │")
    print("  │  -  delay down              │")
    print("  │  q  quit                    │")
    print("  ├─────────────────────────────┤")
    hist = list(history)
    while len(hist) < 4:
        hist.insert(0, "")
    for i, entry in enumerate(hist):
        newest = (i == 3)
        clr = (_status_color(entry) if entry else "") if newest else (_DIM if entry else "")
        rst = _R if clr else ""
        print(f"  │  {clr}{entry[:27]:<27}{rst}│")
    print("  └─────────────────────────────┘")
    print("  key: ", end="", flush=True)


def repl(transport, db):
    global DELAY
    current_mode = 'lower'
    history = deque(["ready"], maxlen=4)

    while True:
        _draw_menu(current_mode, history)
        key = _getch()

        if key in ('q', 'Q', '\x03', '\x04'):
            print("\033[2J\033[H", end="")
            break

        elif key in ('n', 'N'):
            send("md-name", transport, db)
            current_mode = 'lower'
            history.append("naming mode active")

        elif key in ('b', 'B'):
            print("\n  names file: ", end="", flush=True)
            try:
                path = input().strip()
            except (EOFError, KeyboardInterrupt):
                history.append("cancelled")
                continue
            if not path:
                history.append("cancelled")
            elif not os.path.exists(path):
                history.append(f"not found: {path!r}")
            else:
                names = load_names(path)
                if names:
                    name_tracks(names, transport, db)
                    history.append(f"named {len(names)} track(s)")
                else:
                    history.append("file empty")

        elif key in ('t', 'T'):
            print("\n  text: ", end="", flush=True)
            try:
                text = input()
            except (EOFError, KeyboardInterrupt):
                history.append("cancelled")
                continue
            if text:
                current_mode = type_string(text, current_mode, transport, db)
                history.append(f"typed: {text!r}")
            else:
                history.append("nothing typed")

        elif key in ('o', 'O'):
            send("md-on", transport, db)
            history.append("power toggled")

        elif key == ']':
            send("md-next", transport, db)
            history.append("next track")

        elif key == '[':
            send("md-prev", transport, db)
            history.append("previous track")

        elif key in ('f', 'F'):
            send("md-ff", transport, db)
            history.append("ff")

        elif key in ('r', 'R'):
            send("md-rw", transport, db)
            history.append("rw")

        elif key in ('c', 'C'):
            send("md-char", transport, db)
            idx = MODE_CYCLE.index(current_mode)
            current_mode = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
            history.append(f"mode → {current_mode}")

        elif key in ('s', 'S'):
            print("\n  signal: ", end="", flush=True)
            try:
                name = input().strip()
            except (EOFError, KeyboardInterrupt):
                history.append("cancelled")
                continue
            if name:
                ok = send(name, transport, db)
                history.append(f"sent {name!r}" if ok else f"failed: {name!r}")
            else:
                history.append("cancelled")

        elif key == '+':
            DELAY = round(DELAY + 0.1, 1)
            history.append(f"delay: {DELAY}s")

        elif key == '-':
            DELAY = max(0.1, round(DELAY - 0.1, 1))
            history.append(f"delay: {DELAY}s")

        else:
            history.append(f"unknown key: {key!r}")


def main():
    parser = argparse.ArgumentParser(description="Interactive Minidisc control")
    add_transport_arg(parser)
    parser.add_argument("--names-file", metavar="FILE",
                        help="Name tracks from file (one name per line) and exit")
    args = parser.parse_args()

    transport = make_transport(args.serial)
    db = load_db()

    try:
        transport.ping()
    except Exception as e:
        print(f"ERROR: can't reach ESP32 — {e}")
        sys.exit(1)

    if args.names_file:
        names = load_names(args.names_file)
        if not names:
            print("ERROR: names file is empty")
            sys.exit(1)
        print(f"Naming {len(names)} track(s) from {args.names_file!r}...")
        name_tracks(names, transport, db)
    else:
        repl(transport, db)


if __name__ == "__main__":
    main()
