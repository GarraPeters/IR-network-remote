#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <ESPAsyncWebServer.h>
#include "esp_wifi.h"
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRrecv.h>
#include <IRutils.h>
#include <atomic>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Preferences.h>
#include "remote_html.h"

#define OLED_SDA  23
#define OLED_SCL  22
#define OLED_ADDR 0x3C

Adafruit_SSD1306 display(128, 64, &Wire, -1);

// Compile-time defaults — overridden by NVS after first WIFI command
#define WIFI_SSID_DEFAULT "<WIFI_SSID>"
#define WIFI_PASS_DEFAULT "<WIFI_PASS>"
const char* HOSTNAME = "ir-remote";

static char gWifiSsid[64];
static char gWifiPass[64];
Preferences prefs;

#define IR_RECEIVE_PIN      4
#define IR_SEND_PIN         2
#define CAPTURE_BUFFER_SIZE 1024

IRsend         irsend(IR_SEND_PIN);
IRrecv         irrecv(IR_RECEIVE_PIN, CAPTURE_BUFFER_SIZE, 15, true);
AsyncWebServer server(80);

decode_results results;

static bool          gWifiConnected  = false;
static unsigned long gLastWifiCheck  = 0;
static String        gLocalIP        = "";

// Single-shot job (from /send)
struct IrJob {
    bool     isRaw;
    char     protocol[16];
    uint32_t address, command, rawValue;
    int      repeats;
    uint16_t rawTimings[512];
    uint16_t rawCount;
};
static IrJob             gJob;
static std::atomic<bool> gJobPending(false);
static std::atomic<bool> gWifiReconnectPending(false);

// Wake on LAN job (from /wol)
#define WOL_PORT 9
static WiFiUDP           gUdp;
static uint8_t           gWolMac[6];
static std::atomic<bool> gWolPending(false);

// Sequence job (from /sequence) — executed non-blocking in loop()
#define SEQ_MAX 1024
struct SeqEntry {
    char     protocol[16];
    uint32_t address, command, rawValue;
    int      repeats;
};
static SeqEntry          gSeqEntries[SEQ_MAX];
static uint16_t          gSeqCount   = 0;
static uint16_t          gSeqIndex   = 0;
static uint32_t          gSeqDelay   = 300;
static unsigned long     gSeqNextAt  = 0;
static std::atomic<bool> gSeqRunning(false);

String resultToString(decode_results* r) {
    String out = "protocol=" + String(typeToString(r->decode_type).c_str());
    out += "&address=0x" + String(r->address, HEX);
    out += "&command=0x" + String(r->command, HEX);
    out += "&value=0x"   + String(r->value, HEX);
    return out;
}

bool irSend(const String& protocol, uint32_t address, uint32_t command, int repeats, uint32_t rawValue = 0) {
    auto necPack = [](uint32_t a, uint32_t c) -> uint32_t {
        return (a & 0xFF) | ((~a & 0xFF) << 8) | ((c & 0xFF) << 16) | ((~c & 0xFF) << 24);
    };
    auto samPack = [](uint32_t a, uint32_t c) -> uint32_t {
        return (a & 0xFF) | ((a & 0xFF) << 8) | ((c & 0xFF) << 16) | ((~c & 0xFF) << 24);
    };

    irrecv.pause();
    bool ok = true;
    if (protocol == "NEC") {
        // Extended NEC (NECx): 16-bit address — both bytes are the actual address,
        // no complement. Standard NEC uses 8-bit address + its complement.
        uint32_t data = rawValue ? rawValue
                      : (address > 0xFF)
                          ? (address & 0xFF) | (((address >> 8) & 0xFF) << 8) | ((command & 0xFF) << 16) | ((~command & 0xFF) << 24)
                          : necPack(address, command);
        irsend.sendNEC(data, 32, repeats);
    } else if (protocol == "SAMSUNG") {
        irsend.sendSAMSUNG(samPack(address, command), 32, repeats);
    } else if (protocol == "SONY") {
        // IRremoteESP8266 stores Sony value with bit-reversed byte order, so address/command
        // cannot be reliably reconstructed — use the captured rawValue directly when available.
        uint32_t d = rawValue ? rawValue : ((address << 7) | (command & 0x7F));
        uint16_t bits = (d > 0x7FFF) ? 20 : (d > 0xFFF) ? 15 : 12;
        irsend.sendSony(d, bits, repeats);
    } else if (protocol == "RC5") {
        irsend.sendRC5(address << 6 | command, 12, repeats);
    } else if (protocol == "RC6") {
        irsend.sendRC6(address << 8 | command, 20, repeats);
    } else if (protocol == "LG") {
        irsend.sendLG(necPack(address, command), 28, repeats);
    } else if (protocol == "PANASONIC") {
        irsend.sendPanasonic(address, command, repeats);
    } else {
        ok = false;
    }
    irrecv.resume();
    return ok;
}

// ---- Send feedback screen ----

// Shown briefly after a send, then loop() restores the idle screen
#define FEEDBACK_MS 1500
static bool          gFeedbackActive = false;
static unsigned long gFeedbackAt     = 0;

void showFeedback(const char* title, const String& line1, const String& line2 = "") {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(2);
    display.setCursor(0, 0);
    display.println(title);
    display.setTextSize(1);
    display.setCursor(0, 28);
    display.println(line1);
    display.setCursor(0, 44);
    display.println(line2);
    display.display();
    gFeedbackActive = true;
    gFeedbackAt     = millis();
}

void showIrFeedback(bool ok, const String& protocol, uint32_t address, uint32_t command) {
    if (ok) showFeedback("IR sent", protocol, "0x" + String(address, HEX) + " 0x" + String(command, HEX));
    else    showFeedback("IR error", "Unknown protocol", protocol);
}

// ---- Wake on LAN ----

// Accepts 12 hex digits with optional ':' or '-' separators
bool parseMac(const String& s, uint8_t mac[6]) {
    int nibbles = 0;
    for (int i = 0; i < (int)s.length(); i++) {
        char c = s[i];
        if (c == ':' || c == '-') continue;
        if (!isxdigit((unsigned char)c) || nibbles >= 12) return false;
        uint8_t v = isdigit((unsigned char)c) ? c - '0' : (tolower(c) - 'a' + 10);
        if (nibbles % 2 == 0) mac[nibbles / 2] = v << 4;
        else                  mac[nibbles / 2] |= v;
        nibbles++;
    }
    return nibbles == 12;
}

// Magic packet: 6 x 0xFF followed by the MAC repeated 16 times
void sendWol(const uint8_t mac[6]) {
    uint8_t packet[102];
    memset(packet, 0xFF, 6);
    for (int i = 0; i < 16; i++) memcpy(packet + 6 + i * 6, mac, 6);

    for (int rep = 0; rep < 3; rep++) {
        gUdp.beginPacket(WiFi.broadcastIP(), WOL_PORT);
        gUdp.write(packet, sizeof(packet));
        gUdp.endPacket();
        if (rep < 2) delay(50);
    }
}

// ---- Serial command handler (runs in loop() — safe to call irSend directly) ----

void handleSerialCommand(const String& line) {
    int sp = line.indexOf(' ');
    String cmd = (sp < 0) ? line : line.substring(0, sp);

    if (cmd == "PING") {
        Serial.println("PONG");

    } else if (cmd == "RECV") {
        if (irrecv.decode(&results)) {
            Serial.println(resultToString(&results));
            irrecv.resume();
        } else {
            Serial.println("NONE");
        }

    } else if (cmd == "RECV_RAW") {
        if (irrecv.decode(&results)) {
            String raw = "";
            for (uint16_t i = 1; i < results.rawlen; i++) {
                raw += String(results.rawbuf[i] * kRawTick);
                if (i < results.rawlen - 1) raw += ",";
            }
            irrecv.resume();
            Serial.println(raw);
        } else {
            Serial.println("NONE");
        }

    } else if (cmd == "SEND") {
        String rest = line.substring(sp + 1) + " ";
        String tokens[5];
        int tc = 0, s = 0;
        for (int i = 0; i < (int)rest.length() && tc < 5; i++) {
            if (rest[i] == ' ') {
                if (i > s) tokens[tc++] = rest.substring(s, i);
                s = i + 1;
            }
        }
        if (tc < 4) { Serial.println("ERROR missing args"); return; }

        String protocol = tokens[0];
        protocol.toUpperCase();
        uint32_t address  = strtol(tokens[1].c_str(), nullptr, 16);
        uint32_t command  = strtol(tokens[2].c_str(), nullptr, 16);
        int      repeats  = tokens[3].toInt();
        uint32_t rawValue = (tc > 4) ? strtol(tokens[4].c_str(), nullptr, 16) : 0;

        bool ok = irSend(protocol, address, command, repeats, rawValue);
        Serial.println(ok ? "OK" : "ERROR unknown protocol");
        showIrFeedback(ok, protocol, address, command);

    } else if (cmd == "SEND_RAW") {
        int sp2 = line.indexOf(' ', sp + 1);
        if (sp2 < 0) { Serial.println("ERROR missing timings"); return; }

        int    repeats    = line.substring(sp + 1, sp2).toInt();
        String timingsStr = line.substring(sp2 + 1);

        static uint16_t timings[512];
        uint16_t count = 0, s = 0;
        for (int i = 0; i <= (int)timingsStr.length() && count < 512; i++) {
            if (i == (int)timingsStr.length() || timingsStr[i] == ',') {
                if (i > s) timings[count++] = (uint16_t)timingsStr.substring(s, i).toInt();
                s = i + 1;
            }
        }
        if (count == 0) { Serial.println("ERROR no timing data"); return; }

        irrecv.pause();
        for (int rep = 0; rep <= repeats; rep++) {
            irsend.sendRaw(timings, count, 38);
            if (rep < repeats) delay(100);
        }
        irrecv.resume();
        Serial.println("OK");
        showFeedback("IR sent", "RAW", String(count) + " timings");

    } else if (cmd == "WIFI") {
        // WIFI <ssid> <pass>  — pass may contain spaces
        int sp2 = line.indexOf(' ', sp + 1);
        if (sp < 0 || sp2 < 0) { Serial.println("ERROR usage: WIFI <ssid> <pass>"); return; }
        String ssid = line.substring(sp + 1, sp2);
        String pass = line.substring(sp2 + 1);
        prefs.begin("wifi", false);
        prefs.putString("ssid", ssid);
        prefs.putString("pass", pass);
        prefs.end();
        ssid.toCharArray(gWifiSsid, sizeof(gWifiSsid));
        pass.toCharArray(gWifiPass, sizeof(gWifiPass));
        gWifiReconnectPending.store(true);
        Serial.println("OK reconnecting");

    } else {
        Serial.println("ERROR unknown command");
    }
}

// ---- Display helpers ----

void drawSignalBars(int x, int y, int rssi) {
    int bars = 0;
    if      (rssi >= -60) bars = 4;
    else if (rssi >= -70) bars = 3;
    else if (rssi >= -80) bars = 2;
    else if (rssi >= -90) bars = 1;

    const int barW = 4, gap = 2, maxH = 10;
    const int heights[] = {4, 6, 8, maxH};
    for (int i = 0; i < 4; i++) {
        int bx = x + i * (barW + gap);
        int h  = heights[i];
        int by = y + maxH - h;
        if (i < bars) display.fillRect(bx, by, barW, h, SSD1306_WHITE);
        else          display.drawRect(bx, by, barW, h, SSD1306_WHITE);
    }
}

void showConnectedScreen(const String& ip) {
    int rssi = WiFi.RSSI();
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("IR Remote ready");
    display.setCursor(0, 20);
    display.println(ip);
    display.setCursor(0, 44);
    display.print(String(rssi) + "dBm");
    drawSignalBars(50, 43, rssi);
    display.display();
}

// ---- HTTP handlers ----

void setup() {
    Serial.setRxBufferSize(4096);
    Serial.begin(115200);
    irsend.begin();
    irrecv.enableIRIn();

    Wire.begin(OLED_SDA, OLED_SCL);
    if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
        display.clearDisplay();
        display.setTextSize(1);
        display.setTextColor(SSD1306_WHITE);
        display.setCursor(0, 0);
        display.println("Connecting...");
        display.display();
    }

    prefs.begin("wifi", true);
    String storedSsid = prefs.getString("ssid", WIFI_SSID_DEFAULT);
    String storedPass = prefs.getString("pass", WIFI_PASS_DEFAULT);
    prefs.end();
    storedSsid.toCharArray(gWifiSsid, sizeof(gWifiSsid));
    storedPass.toCharArray(gWifiPass, sizeof(gWifiPass));

    WiFi.begin(gWifiSsid, gWifiPass);
    Serial.print("Connecting to WiFi");
    unsigned long wifiStart = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 15000) {
        delay(500);
        Serial.print(".");
    }

    if (WiFi.status() == WL_CONNECTED) {
        gWifiConnected = true;
        esp_wifi_set_ps(WIFI_PS_NONE);
        gLocalIP = WiFi.localIP().toString();
        Serial.println("\nIP: " + gLocalIP);
        MDNS.begin(HOSTNAME);
        server.begin();
        Serial.println("HTTP server started");
        showConnectedScreen(gLocalIP);
    } else {
        Serial.println("\nWiFi failed — serial-only mode");

        display.clearDisplay();
        display.setTextSize(1);
        display.setTextColor(SSD1306_WHITE);
        display.setCursor(0, 0);
        display.println("WiFi failed");
        display.println("Serial-only mode");
        display.display();
    }

    // Control page — baked in from ../remote.html at build time
    server.on("/", HTTP_GET, [](AsyncWebServerRequest* req) {
        req->send(200, "text/html", REMOTE_HTML);
    });

    server.on("/ping", HTTP_GET, [](AsyncWebServerRequest* req) {
        req->send(200, "text/plain", "PONG");
    });

    server.on("/send/raw", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!req->hasArg("raw")) {
            req->send(400, "text/plain", "ERROR missing raw arg");
            return;
        }
        String body  = req->arg("raw");
        gJob.isRaw   = true;
        gJob.repeats = req->hasArg("repeats") ? req->arg("repeats").toInt() : 0;
        gJob.rawCount = 0;
        uint16_t s = 0;
        for (int i = 0; i <= (int)body.length() && gJob.rawCount < 512; i++) {
            if (i == (int)body.length() || body[i] == ',') {
                if (i > s) gJob.rawTimings[gJob.rawCount++] = (uint16_t)body.substring(s, i).toInt();
                s = i + 1;
            }
        }
        if (gJob.rawCount == 0) { req->send(400, "text/plain", "ERROR no timing data"); return; }
        gJobPending.store(true);
        req->send(200, "text/plain", "OK");
    });

    // Single-shot send — queued to loop() to avoid blocking the WiFi task
    server.on("/send", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!req->hasArg("protocol") || !req->hasArg("address") || !req->hasArg("command")) {
            req->send(400, "text/plain", "ERROR missing args");
            return;
        }
        String protocol = req->arg("protocol");
        protocol.toUpperCase();
        gJob.isRaw    = false;
        strncpy(gJob.protocol, protocol.c_str(), 15);
        gJob.protocol[15] = '\0';
        gJob.address  = strtol(req->arg("address").c_str(), nullptr, 16);
        gJob.command  = strtol(req->arg("command").c_str(), nullptr, 16);
        gJob.repeats  = req->hasArg("repeats") ? req->arg("repeats").toInt() : 0;
        gJob.rawValue = req->hasArg("value") ? strtol(req->arg("value").c_str(), nullptr, 16) : 0;
        gJobPending.store(true);
        req->send(200, "text/plain", "OK");
    });

    server.on("/sequence/status", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (gSeqRunning.load()) {
            req->send(200, "text/plain",
                      "BUSY " + String(gSeqIndex) + "/" + String(gSeqCount));
        } else {
            req->send(200, "text/plain", "IDLE");
        }
    });

    // Sequence send — ESP32 runs the full list locally, no per-signal round-trips.
    // Body fields: delay=<ms>  seq=<proto,addr,cmd,rep,val|proto,...>
    server.on("/sequence", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!req->hasArg("seq")) {
            req->send(400, "text/plain", "ERROR missing seq");
            return;
        }
        if (gSeqRunning.load()) {
            req->send(503, "text/plain", "ERROR busy");
            return;
        }

        gSeqDelay = req->hasArg("delay") ? (uint32_t)req->arg("delay").toInt() : 300;
        String seq = req->arg("seq");
        gSeqCount  = 0;

        int start = 0;
        while (start <= (int)seq.length() && gSeqCount < SEQ_MAX) {
            int end = seq.indexOf('|', start);
            if (end < 0) end = seq.length();
            if (end == start) { start = end + 1; continue; }

            String entry = seq.substring(start, end);
            start = end + 1;

            // Parse: protocol,address,command,repeats[,value]
            String fields[5];
            int fc = 0, fs = 0;
            for (int i = 0; i <= (int)entry.length() && fc < 5; i++) {
                if (i == (int)entry.length() || entry[i] == ',') {
                    if (i > fs) fields[fc++] = entry.substring(fs, i);
                    fs = i + 1;
                }
            }
            if (fc < 4) continue;

            SeqEntry& e = gSeqEntries[gSeqCount];
            String proto = fields[0];
            proto.toUpperCase();
            strncpy(e.protocol, proto.c_str(), 15);
            e.protocol[15] = '\0';
            e.address  = strtol(fields[1].c_str(), nullptr, 16);
            e.command  = strtol(fields[2].c_str(), nullptr, 16);
            e.repeats  = fields[3].toInt();
            e.rawValue = (fc > 4) ? strtol(fields[4].c_str(), nullptr, 16) : 0;
            gSeqCount++;
        }

        if (gSeqCount == 0) {
            req->send(400, "text/plain", "ERROR no valid signals");
            return;
        }

        gSeqIndex  = 0;
        gSeqNextAt = millis();
        gSeqRunning.store(true);
        req->send(200, "text/plain", "OK " + String(gSeqCount));
    });

    server.on("/recv/raw", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (irrecv.decode(&results)) {
            String raw = "";
            for (uint16_t i = 1; i < results.rawlen; i++) {
                raw += String(results.rawbuf[i] * kRawTick);
                if (i < results.rawlen - 1) raw += ",";
            }
            irrecv.resume();
            req->send(200, "text/plain", raw);
        } else {
            req->send(204, "text/plain", "NONE");
        }
    });

    server.on("/recv", HTTP_GET, [](AsyncWebServerRequest* req) {
        if (irrecv.decode(&results)) {
            String response = resultToString(&results);
            irrecv.resume();
            req->send(200, "text/plain", response);
        } else {
            req->send(204, "text/plain", "NONE");
        }
    });

    // Wake on LAN — queued to loop() like /send
    server.on("/wol", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!req->hasArg("mac")) {
            req->send(400, "text/plain", "ERROR missing mac arg");
            return;
        }
        uint8_t mac[6];
        if (!parseMac(req->arg("mac"), mac)) {
            req->send(400, "text/plain", "ERROR invalid mac");
            return;
        }
        memcpy(gWolMac, mac, 6);
        gWolPending.store(true);
        req->send(200, "text/plain", "OK");
    });

    server.on("/wifi", HTTP_POST, [](AsyncWebServerRequest* req) {
        if (!req->hasArg("ssid") || !req->hasArg("pass")) {
            req->send(400, "text/plain", "ERROR missing ssid or pass");
            return;
        }
        String ssid = req->arg("ssid");
        String pass = req->arg("pass");
        prefs.begin("wifi", false);
        prefs.putString("ssid", ssid);
        prefs.putString("pass", pass);
        prefs.end();
        ssid.toCharArray(gWifiSsid, sizeof(gWifiSsid));
        pass.toCharArray(gWifiPass, sizeof(gWifiPass));
        req->send(200, "text/plain", "OK reconnecting");
        gWifiReconnectPending.store(true);
    });
}

void loop() {
    // WiFi reconnection watchdog — checks every 5 s, non-blocking
    unsigned long now = millis();
    if (now - gLastWifiCheck >= 5000) {
        gLastWifiCheck = now;
        bool connected = (WiFi.status() == WL_CONNECTED);
        if (!connected && gWifiConnected) {
            gWifiConnected = false;
            Serial.println("WiFi lost — reconnecting...");
            display.clearDisplay();
            display.setTextSize(1);
            display.setTextColor(SSD1306_WHITE);
            display.setCursor(0, 0);
            display.println("WiFi lost");
            display.println("Reconnecting...");
            display.display();
            WiFi.reconnect();
        } else if (connected && !gWifiConnected) {
            gWifiConnected = true;
            esp_wifi_set_ps(WIFI_PS_NONE);
            gLocalIP = WiFi.localIP().toString();
            Serial.println("WiFi reconnected: " + gLocalIP);
            MDNS.begin(HOSTNAME);
            showConnectedScreen(gLocalIP);
        } else if (connected && gWifiConnected && !gFeedbackActive) {
            showConnectedScreen(gLocalIP);  // refresh RSSI every 5 s
        }
    }

    // Restore the idle screen once send feedback has been up long enough
    if (gFeedbackActive && millis() - gFeedbackAt >= FEEDBACK_MS) {
        gFeedbackActive = false;
        if (gWifiConnected) {
            showConnectedScreen(gLocalIP);
        } else {
            display.clearDisplay();
            display.setTextSize(1);
            display.setTextColor(SSD1306_WHITE);
            display.setCursor(0, 0);
            display.println("WiFi offline");
            display.println("Serial-only mode");
            display.display();
        }
    }

    if (gWifiReconnectPending.exchange(false)) {
        gWifiConnected = false;
        WiFi.disconnect();
        WiFi.begin(gWifiSsid, gWifiPass);
    }

    if (gWolPending.exchange(false)) {
        sendWol(gWolMac);
        char mac[18];
        snprintf(mac, sizeof(mac), "%02X:%02X:%02X:%02X:%02X:%02X",
                 gWolMac[0], gWolMac[1], gWolMac[2], gWolMac[3], gWolMac[4], gWolMac[5]);
        showFeedback("WOL sent", mac);
    }

    // Execute single-shot jobs
    if (gJobPending.exchange(false)) {
        if (gJob.isRaw) {
            irrecv.pause();
            for (int rep = 0; rep <= gJob.repeats; rep++) {
                irsend.sendRaw(gJob.rawTimings, gJob.rawCount, 38);
                if (rep < gJob.repeats) delay(100);
            }
            irrecv.resume();
            showFeedback("IR sent", "RAW", String(gJob.rawCount) + " timings");
        } else {
            bool ok = irSend(String(gJob.protocol), gJob.address, gJob.command, gJob.repeats, gJob.rawValue);
            showIrFeedback(ok, String(gJob.protocol), gJob.address, gJob.command);
        }
    }

    // Execute sequence entries one at a time, non-blocking
    if (gSeqRunning.load()) {
        unsigned long now = millis();
        if (now >= gSeqNextAt && gSeqIndex < gSeqCount) {
            SeqEntry& e = gSeqEntries[gSeqIndex];
            String proto = String(e.protocol);
            if (proto == "DELAY") {
                gSeqNextAt = millis() + (uint32_t)e.address;
            } else {
                irSend(proto, e.address, e.command, e.repeats, e.rawValue);
                gSeqNextAt = millis() + gSeqDelay;
                showFeedback("Sequence", String(gSeqIndex + 1) + "/" + String(gSeqCount), proto);
            }
            gSeqIndex++;
            if (gSeqIndex >= gSeqCount) {
                gSeqRunning.store(false);
            }
        }
    }

    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();
        if (line.length() > 0) handleSerialCommand(line);
    }
}
