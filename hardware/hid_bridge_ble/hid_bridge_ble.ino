// HID bridge over Bluetooth LE: an ESP32-C3 (or classic ESP32) that pairs with the iPhone as a
// BLE mouse and keyboard and takes line commands over TCP (Wi-Fi). Same protocol as hid_bridge:
//   ping | home | move <dx> <dy> | press | release | click
//   scroll <v> <h> | type <ascii text> | key <combo, e.g. cmd+h, enter>
// The board answers "ok" or "err <why>"; "err ble not connected" when the phone is not paired/connected.

#include <WiFi.h>
#include <esp_task_wdt.h>
#include "ble_hid.h"
#include "secrets.h"  // WIFI_SSID, WIFI_PASS, SERVER_HOST, SERVER_PORT

#ifndef BLE_NAME
#define BLE_NAME "iPhone Agent HID"
#endif
const int STEP = 10;      // max pointer delta per report while moving
const int STEP_MS = 8;    // report interval; constant pace keeps iOS acceleration predictable (BLE conn interval 7.5-15 ms)
const int HOME_REPORTS = 50;
#ifndef DEBUG_SERIAL
#define DEBUG_SERIAL 0
#endif
const int WATCHDOG_MS = 8000;

BleHid hid;
WiFiClient server;

void movePaced(long dx, long dy) {
  while (dx || dy) {
    int sx = constrain(dx, -STEP, STEP), sy = constrain(dy, -STEP, STEP);
    hid.mouseMove(sx, sy);
    dx -= sx;
    dy -= sy;
    delay(STEP_MS);
  }
}

// HID usage for a key name; 0 if unknown. Single chars go through the ASCII table.
uint8_t keyCode(const String &k, bool &shift) {
  shift = false;
  if (k == "enter") return 0x28;
  if (k == "esc") return 0x29;
  if (k == "tab") return 0x2B;
  if (k == "backspace") return 0x2A;
  if (k == "space") return 0x2C;
  if (k == "up") return 0x52;
  if (k == "down") return 0x51;
  if (k == "left") return 0x50;
  if (k == "right") return 0x4F;
  if (k == "cmd") return HID_LGUI;
  if (k == "ctrl") return HID_LCTRL;
  if (k == "alt") return HID_LALT;
  if (k == "shift") return HID_LSHIFT;
  if (k.length() == 1 && k[0] >= 0x20 && k[0] <= 0x7E) {
    uint8_t v = ASCII_HID[k[0] - 0x20];
    shift = v & 0x80;
    return v & 0x7F;
  }
  return 0;
}

bool pressCombo(String combo) {
  combo += '+';
  for (int i; (i = combo.indexOf('+')) >= 0; combo = combo.substring(i + 1)) {
    bool shift;
    uint8_t c = keyCode(combo.substring(0, i), shift);
    if (!c) { hid.releaseAll(); return false; }
    if (shift) hid.press(HID_LSHIFT);
    hid.press(c);
  }
  delay(30);
  hid.releaseAll();
  return true;
}

String run(const String &line) {
  int sp = line.indexOf(' ');
  String cmd = sp < 0 ? line : line.substring(0, sp);
  String arg = sp < 0 ? "" : line.substring(sp + 1);
  long a = 0, b = 0;
  sscanf(arg.c_str(), "%ld %ld", &a, &b);

  if (cmd == "ping") return "ok";
  if (!hid.ready()) return "err ble not connected";
  if (cmd == "home") {
    for (int i = 0; i < HOME_REPORTS; i++) { hid.mouseMove(-127, -127); delay(STEP_MS); }
    return "ok";
  }
  if (cmd == "move") { movePaced(a, b); return "ok"; }
  if (cmd == "press") { hid.mousePress(MOUSE_LEFT); return "ok"; }
  if (cmd == "release") { hid.mouseRelease(MOUSE_LEFT); return "ok"; }
  if (cmd == "click") { hid.mousePress(MOUSE_LEFT); delay(60); hid.mouseRelease(MOUSE_LEFT); return "ok"; }
  if (cmd == "scroll") { hid.mouseMove(0, 0, constrain(a, -127, 127), constrain(b, -127, 127)); return "ok"; }
  if (cmd == "type") { hid.print(arg); return "ok"; }
  if (cmd == "key") return pressCombo(arg) ? "ok" : "err unknown key";
  return "err unknown command";
}

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_TASK_WDT: case ESP_RST_INT_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_SW: return "software";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}

void setup() {
#if DEBUG_SERIAL
  Serial.begin(115200);
#endif
  hid.begin(BLE_NAME);
  esp_task_wdt_config_t wdt = {.timeout_ms = WATCHDOG_MS, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
  esp_task_wdt_add(NULL);
  WiFi.setHostname("hid-bridge");
  WiFi.setSleep(false);  // note: with BLE active, the ESP32 core may force modem sleep for coexistence
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void loop() {
  esp_task_wdt_reset();
  if (WiFi.status() != WL_CONNECTED) { hid.mouseRelease(MOUSE_LEFT); delay(50); return; }
  if (!server.connected()) {
    hid.releaseEverything();  // a gesture cut off mid-way must not leave the button held
    if (!server.connect(SERVER_HOST, SERVER_PORT, 1000)) { delay(200); return; }
    server.setNoDelay(true);
    server.printf("hello %s %lu\n", resetReason(), millis());
#if DEBUG_SERIAL
    Serial.printf("connected to %s:%d as %s after %lu ms, ble %s\n", SERVER_HOST, SERVER_PORT,
                  WiFi.localIP().toString().c_str(), millis(), hid.ready() ? "ready" : "not connected");
#endif
  }
  if (server.available()) {
    String line = server.readStringUntil('\n');
    line.trim();
    if (!line.length()) return;
    unsigned long started = millis();
    String reply = run(line);
    server.print(reply + "\n");
#if DEBUG_SERIAL
    Serial.printf("%s -> %s (%lu ms)\n", line.c_str(), reply.c_str(), millis() - started);
#endif
  }
}
