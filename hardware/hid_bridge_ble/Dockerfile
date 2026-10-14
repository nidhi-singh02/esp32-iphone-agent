# Builds the BLE HID bridge firmware without installing anything on the host.
# Build the image (from the repo root):
#   docker build -t hid-bridge-ble hardware/hid_bridge_ble
# Compile (C3 by default; set FQBN for a classic ESP32), merged .bin files land in ./build_ble:
#   docker run --rm -v "$PWD/hardware/hid_bridge_ble:/src/hid_bridge_ble" -v "$PWD/build_ble:/out" hid-bridge-ble
#   docker run --rm -e FQBN="esp32:esp32:esp32:PartitionScheme=huge_app" -v "$PWD/hardware/hid_bridge_ble:/src/hid_bridge_ble" -v "$PWD/build_ble_classic:/out" hid-bridge-ble
FROM debian:bookworm-slim

ARG ARDUINO_CLI_VERSION=1.5.1
ARG ESP32_CORE_VERSION=3.3.12
ARG NIMBLE_VERSION=2.5.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
    | BINDIR=/usr/local/bin sh -s ${ARDUINO_CLI_VERSION}

RUN arduino-cli config init \
 && arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json \
 && arduino-cli core update-index \
 && arduino-cli core install esp32:esp32@${ESP32_CORE_VERSION} \
 && arduino-cli lib install NimBLE-Arduino@${NIMBLE_VERSION}

ENV FQBN="esp32:esp32:esp32c3:CDCOnBoot=cdc,PartitionScheme=huge_app"
WORKDIR /src
# Mount the sketch folder at /src/hid_bridge_ble and an output folder at /out.
CMD arduino-cli compile -b "$FQBN" --output-dir /out hid_bridge_ble && ls -l /out/*.merged.bin
