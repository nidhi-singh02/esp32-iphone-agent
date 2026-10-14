// Minimal BLE HID combo (keyboard report 1, mouse report 2) on NimBLE-Arduino 2.x.
// Works on ESP32-C3, ESP32-S3 and classic ESP32 (any chip with BLE).
#pragma once
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>

#define MOUSE_LEFT 1
#define MOUSE_RIGHT 2
// Modifier pseudo-codes (>= 0xE0 are HID modifier usages)
#define HID_LCTRL 0xE0
#define HID_LSHIFT 0xE1
#define HID_LALT 0xE2
#define HID_LGUI 0xE3

static const uint8_t REPORT_MAP[] = {
  // Keyboard, report id 1
  0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
  0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
  0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
  0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
  0xC0,
  // Mouse, report id 2: buttons(1) x(1) y(1) wheel(1) pan(1)
  0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02, 0x09, 0x01, 0xA1, 0x00,
  0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x95, 0x05, 0x75, 0x01, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x03, 0x81, 0x03,
  0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
  0x05, 0x0C, 0x0A, 0x38, 0x02, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
  0xC0, 0xC0,
};

// ASCII 0x20..0x7E -> HID usage, bit 7 = needs shift
static const uint8_t ASCII_HID[95] = {
  0x2C, 0x9E, 0xB4, 0xA0, 0xA1, 0xA2, 0xA4, 0x34, 0xA6, 0xA7, 0xA5, 0xAE, 0x36, 0x2D, 0x37, 0x38,  //  !"#$%&'()*+,-./
  0x27, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0xB3, 0x33, 0xB6, 0x2E, 0xB7, 0xB8,  // 0-9 :;<=>?
  0x9F, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x90, 0x91, 0x92,  // @A-O
  0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x2F, 0x31, 0x30, 0xA3, 0xAD,  // P-Z [\]^_
  0x35, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12,  // `a-o
  0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0xAF, 0xB1, 0xB0, 0xB5,        // p-z {|}~
};

class BleHid : public NimBLEServerCallbacks {
 public:
  void begin(const char *name) {
    NimBLEDevice::init(name);
    NimBLEDevice::setSecurityAuth(true, false, true);  // bonding, no MITM (no screen), secure connections
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    server_ = NimBLEDevice::createServer();
    server_->setCallbacks(this);
    hid_ = new NimBLEHIDDevice(server_);
    kb_ = hid_->getInputReport(1);
    ms_ = hid_->getInputReport(2);
    hid_->setManufacturer("iPhone Agent");
    hid_->setPnp(0x02, 0xe502, 0xa111, 0x0210);
    hid_->setHidInfo(0x00, 0x01);
    hid_->setReportMap((uint8_t *)REPORT_MAP, sizeof(REPORT_MAP));
    hid_->setBatteryLevel(100);
    server_->start();
    NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
    adv->setName(name);
    adv->setAppearance(0x03C0);  // generic HID
    adv->addServiceUUID(hid_->getHidService()->getUUID());
    adv->enableScanResponse(true);
    adv->start();
  }
  bool ready() const { return connected_ && secured_; }

  void mouseMove(int8_t x, int8_t y, int8_t wheel = 0, int8_t pan = 0) {
    uint8_t r[5] = {buttons_, (uint8_t)x, (uint8_t)y, (uint8_t)wheel, (uint8_t)pan};
    if (ready()) { ms_->setValue(r, sizeof r); ms_->notify(); }
  }
  void mousePress(uint8_t b) { buttons_ |= b; mouseMove(0, 0); }
  void mouseRelease(uint8_t b) { buttons_ &= ~b; mouseMove(0, 0); }

  // usage < 0xE0: key, >= 0xE0: modifier
  void press(uint8_t usage) {
    if (usage >= 0xE0) mods_ |= 1 << (usage - 0xE0);
    else for (int i = 0; i < 6; i++) { if (keys_[i] == usage) break; if (!keys_[i]) { keys_[i] = usage; break; } }
    sendKeys();
  }
  void releaseAll() { mods_ = 0; memset(keys_, 0, 6); sendKeys(); }
  void tapAscii(char c) {
    if (c < 0x20 || c > 0x7E) return;
    uint8_t v = ASCII_HID[c - 0x20];
    if (v & 0x80) press(HID_LSHIFT);
    press(v & 0x7F);
    delay(8);
    releaseAll();
    delay(8);
  }
  void print(const String &s) { for (size_t i = 0; i < s.length(); i++) tapAscii(s[i]); }
  void releaseEverything() { buttons_ = 0; mouseMove(0, 0); releaseAll(); }

  void onConnect(NimBLEServer *s, NimBLEConnInfo &info) override {
    connected_ = true;
    s->updateConnParams(info.getConnHandle(), 6, 12, 0, 200);  // 7.5-15 ms interval for smooth pointer
  }
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &, int) override {
    connected_ = secured_ = false;
    buttons_ = 0;
    NimBLEDevice::startAdvertising();
  }
  void onAuthenticationComplete(NimBLEConnInfo &info) override { secured_ = info.isEncrypted(); }

 private:
  void sendKeys() {
    uint8_t r[8] = {mods_, 0, keys_[0], keys_[1], keys_[2], keys_[3], keys_[4], keys_[5]};
    if (ready()) { kb_->setValue(r, sizeof r); kb_->notify(); }
  }
  NimBLEServer *server_ = nullptr;
  NimBLEHIDDevice *hid_ = nullptr;
  NimBLECharacteristic *kb_ = nullptr, *ms_ = nullptr;
  volatile bool connected_ = false, secured_ = false;
  uint8_t buttons_ = 0, mods_ = 0, keys_[6] = {0};
};
