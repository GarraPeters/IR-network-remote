import keybow
import requests
import signal
import subprocess

# --- keypad layout ---
#  11  8   5   2
#  10  7   4   1
#  9   6   3   0


IR_BLASTER_URL = "http://192.168.1.200/send"
IR_BLASTER_RAW_URL = "http://192.168.1.200/send/raw"
WOL_MAC = "D8:43:AE:C9:AC:BA"

KEY_COMMANDS = {
    # 0: {
    #     "url": IR_BLASTER_URL,
    #     "params": {
    #         "protocol": "SONY",
    #         "address": "0x1",
    #         "command": "0x15",
    #         "repeats": 2,
    #         "value": "0xa90"
    #     }
    # },

    # "amp-on"
    1: {
        "url": IR_BLASTER_RAW_URL,
        "params": {
            "repeats": 2,
            "raw": "9060,4474,600,1660,602,552,580,552,578,1662,600,554,576,554,576,554,580,1684,578,1662,600,552,578,556,576,1660,602,554,580,552,576,532,600,1662,600,556,576,554,578,554,576,554,578,552,578,554,578,1686,576,554,578,1662,598,1664,598,1662,600,1686,576,1664,598,1684,580,552,578,1662,602"
        }
    },
    # "tv-on"
    2: {
        "url": IR_BLASTER_URL,
        "params": {
            "protocol": "SONY",
            "address": "0x1",
            "command": "0x15",
            "repeats": 2,
            "value": "0xa90"
        }
    },

    # "amp-optic"
    3: {
        "url": IR_BLASTER_RAW_URL,
        "params": {
            "repeats": 2,
            "raw": "9034,4492,584,1680,582,548,584,546,584,1678,584,548,582,552,580,550,580,1678,584,1680,584,548,584,548,582,1678,586,546,584,548,580,550,582,1680,582,548,584,546,582,550,582,550,582,1680,584,548,582,550,582,548,582,1680,584,1678,582,1678,582,1678,584,550,584,1676,584,1678,584,1680,582"
        }
    },

    # "amp-linein"
    6: {
        "url": IR_BLASTER_RAW_URL,
        "params": {
            "repeats": 2,
            "raw": "9054,4474,602,1680,582,550,580,552,580,1662,602,550,580,550,582,548,582,1682,582,1680,580,550,582,550,580,1680,582,552,578,552,582,550,580,1660,600,1682,580,550,582,548,582,550,580,1662,602,550,580,550,582,548,582,550,582,1660,602,1680,582,1680,580,550,582,1660,602,1680,580,1682,580"
        }
    },

    # "amp-voldown"
    10: {
        "url": IR_BLASTER_RAW_URL,
        "params": {
            "repeats": 2,
            "raw": "9056,4494,582,1660,602,550,582,548,580,1682,582,550,582,548,582,550,582,1678,584,1660,602,548,580,550,584,1678,582,550,580,550,584,550,582,1680,582,548,584,1680,586,546,580,1682,580,1682,582,548,580,552,580,550,582,1680,584,546,582,1662,602,550,582,548,582,1662,600,1680,582,1680,584"
        }
    },

    # "amp-volup"
    11: {
        "url": IR_BLASTER_RAW_URL,
        "params": {
            "repeats": 2,
            "raw": "9038,4492,584,1678,584,548,584,550,580,1680,584,550,580,550,582,548,582,1678,586,1676,584,548,582,548,582,1678,584,550,582,548,584,548,584,1676,586,546,584,1678,584,1678,584,550,582,1676,586,548,582,548,586,546,584,1678,584,548,584,548,582,1680,584,548,584,1678,584,1680,584,1678,584"
        }
    },

}

def set_key_colours():
    keybow.clear()
    keybow.set_all(0, 32, 64)
    keybow.set_led(1,  128, 0,   0)    # red
    keybow.set_led(2,  128, 0,   0)    # red
    keybow.set_led(3,  128, 82, 0)    # orange
    keybow.set_led(5,  128, 128, 128)  # WOL white 
    keybow.set_led(6,  128, 82, 0)    # orange
    keybow.set_led(10, 128, 0,   0)    # red
    keybow.set_led(11, 128, 0,   0)    # red
    keybow.show()

keybow.setup(keybow.FULL)
set_key_colours()

@keybow.on()
def handle_key(index, state):
    if state:
        if index in KEY_COMMANDS:
            cmd = KEY_COMMANDS[index]
            print(f"Key {index} pressed, sending to {cmd['url']}")
            try:
                requests.post(cmd["url"], params=cmd["params"], timeout=3)
            except requests.exceptions.RequestException as e:
                print(f"Request failed: {e}")
            set_key_colours()

        elif index == 5:  # Wake on LAN
            print(f"Sending Wake on LAN to {WOL_MAC}...")
            subprocess.run(["wakeonlan", WOL_MAC])
            set_key_colours()


keybow.show()

signal.pause()  # keep the script running