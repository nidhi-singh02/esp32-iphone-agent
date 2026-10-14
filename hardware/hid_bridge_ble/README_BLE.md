# HID bridge over Bluetooth LE (ESP32-C3 or classic ESP32)

This is a drop-in replacement for `hid_bridge` (which needs an ESP32-S3 on USB). The board pairs with the iPhone as a Bluetooth mouse and keyboard. It still talks to the server over Wi-Fi with the same TCP line protocol, so the server does not change. When the phone is not paired or connected, commands get the reply `err ble not connected` (`ping` still answers `ok`).

Library: NimBLE-Arduino 2.5.1 (h2zero), on the esp32 Arduino core 3.3.x. The mouse and keyboard HID descriptor is in `ble_hid.h`.

## Setup and flashing
1. Install arduino-cli, then the esp32 core and the library:
   ```
   arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
   arduino-cli core update-index && arduino-cli core install esp32:esp32
   arduino-cli lib install NimBLE-Arduino
   ```
2. Copy `secrets.example.h` to `secrets.h` and fill in your Wi-Fi details and the server IP and port.
3. Plug in the board and flash it:
   - ESP32-C3: `hardware/flash_ble.sh` (add `--debug` to log commands over USB serial)
   - Classic ESP32 (WROOM-32E): `hardware/flash_ble.sh --classic`
   - You can pass a port too, for example `hardware/flash_ble.sh /dev/ttyACM0`. If a C3 won't upload, hold BOOT while you plug it in.

## Pairing with the iPhone
1. On the iPhone, open Settings > Bluetooth and tap **iPhone Agent HID**. Accept the pairing request.
2. Turn on Settings > Accessibility > Touch > AssistiveTouch. A pointer should appear.
3. Run the server (`python -m hidagent`). The board connects and sends `hello`.
4. If you want to pair again, use Forget This Device on the phone and reset the board.

## Mac screen without UxPlay

On a Mac, the server can read the iPhone from the built-in AirPlay Receiver instead of UxPlay. `SCREEN_SOURCE` defaults to `uxplay`, so an existing setup does not change. This source does not launch UxPlay.

1. On the Mac, open System Settings, General, AirDrop and Handoff (on current macOS the pane is AirDrop & Continuity). Turn on AirPlay Receiver. Set "Allow AirPlay for" to Current User.
2. On the iPhone, open Control Center, tap Screen Mirroring, and choose this Mac. Leave the mirroring window visible and not minimized. Leave the Mac unlocked, and do not cover the window with another window.
3. Allow screen capture for the app that starts the server: System Settings, Privacy and Security, Screen Recording, turn on Terminal. Quit Terminal and open it again so the grant applies.
4. From a clone of this branch:

```bash
git clone https://github.com/nidhi-singh02/esp32-iphone-agent
cd esp32-iphone-agent
git checkout ble-hid-esp32c3
cd server
python3 -m venv .venv
.venv/bin/pip install -e ".[mac]"
cp .env.example .env
```

Already cloned: `git checkout ble-hid-esp32c3 && git pull origin ble-hid-esp32c3`, then from `server/` run the `python3 -m venv` line through `cp .env.example .env`.

5. Edit `.env` in `server/`. Set `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` (a commented Gemini example is in `.env.example`), and set:

```bash
SCREEN_SOURCE=macos_airplay
```

If the mirroring window is not owned by AirPlayUIAgent, set `AIRPLAY_WINDOW_OWNER` to a substring of its owner name, or `AIRPLAY_WINDOW_TITLE` to a substring of its title.

6. From `server/`, start the server:

```bash
set -a && . ./.env && set +a && .venv/bin/python -m hidagent
```

Open `http://127.0.0.1:8890`. The live screen is the AirPlay window. Calibrate from Manual controls, the same way as with UxPlay.

The capture follows that window only. A minimized window, a window on another Space, a locked Mac, or a missing Screen Recording grant stops the frames. Keep the window large enough to read: the JPEG is the window's pixels, with black letterbox bars removed.

## Known caveats
- **Pointer speed:** Bluetooth reports aren't spaced as evenly as USB ones. If the agent overshoots or undershoots taps, change `STEP` and `STEP_MS` in `hid_bridge_ble.ino`, or the AssistiveTouch Tracking Speed slider. Keep Tracking Speed fixed after the server is calibrated.
- **iOS pointer acceleration:** iOS speeds the pointer up based on how fast it moves. Constant pace moves keep that predictable, but a move can still land a few pixels off. `home` slams into the top left corner to reset the position.
- **Wi-Fi and BLE share one radio:** commands can take a few ms longer than on USB.
- **Lock screen:** when the phone is locked, the BLE link may drop, and you'll see `err ble not connected` until the phone reconnects.
- **Flash size:** the build uses 93% of the default partition on the C3, so the flash script uses the huge_app partition.
- **Boot loop with `partition 0 invalid magic number` (C3):** the default C3 bootloader is built for QIO flash and switches the flash chip into quad mode on its own, even if you flash with `--flash-mode dio`. Many C3 boards can't read flash that way. The build now uses `FlashMode=dio,FlashFreq=40`, which selects the DIO bootloader. Rebuild, run `esptool --chip esp32c3 --port PORT erase-flash`, then flash `build_ble/hid_bridge_ble.ino.merged.bin` at `0x0`.
- **Classic ESP32:** the code is the same; just add `--classic`. It has no native USB, so serial goes through its USB to UART chip.
