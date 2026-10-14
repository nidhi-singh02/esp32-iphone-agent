# Builds the BLE HID bridge firmware without installing anything on the host.
# Build the image (from the repo root):
#   docker build --platform linux/amd64 --progress=plain -t hid-bridge-ble hardware/hid_bridge_ble
# Compile (C3 by default; set FQBN for a classic ESP32), merged .bin files land in ./build_ble:
#   docker run --rm --platform linux/amd64 -v "$PWD/hardware/hid_bridge_ble:/src/hid_bridge_ble" -v "$PWD/build_ble:/out" hid-bridge-ble
FROM debian:bookworm-slim

ARG ARDUINO_CLI_VERSION=1.5.1
ARG ESP32_CORE_VERSION=3.3.12
ARG NIMBLE_VERSION=2.5.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL --retry 5 https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
    | BINDIR=/usr/local/bin sh -s ${ARDUINO_CLI_VERSION}

# Longer network timeout for large toolchain downloads.
ENV ARDUINO_NETWORK_CONNECTION_TIMEOUT=600s

RUN arduino-cli config init \
 && arduino-cli config set network.connection_timeout 600s \
 && arduino-cli config add board_manager.additional_urls https://espressif.github.io/arduino-esp32/package_esp32_index.json \
 && arduino-cli core update-index

# Retry the big core download; partial downloads are kept in the staging cache between attempts.
RUN for i in 1 2 3 4 5; do \
      arduino-cli core install esp32:esp32@${ESP32_CORE_VERSION} && exit 0; \
      echo "core install failed (attempt $i), retrying in 15s"; sleep 15; \
    done; exit 1

RUN for i in 1 2 3; do \
      arduino-cli lib install NimBLE-Arduino@${NIMBLE_VERSION} && exit 0; \
      sleep 10; \
    done; exit 1

ENV FQBN="esp32:esp32:esp32c3:CDCOnBoot=cdc,PartitionScheme=huge_app"
WORKDIR /src
# Mount the sketch folder at /src/hid_bridge_ble and an output folder at /out.
CMD ["sh", "-c", "arduino-cli compile -b \"$FQBN\" --output-dir /out hid_bridge_ble && ls -l /out/*.merged.bin"]
