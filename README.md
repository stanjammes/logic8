# logic8

A command-line tool for the Saleae Logic 8 that talks to the hardware directly, without Saleae's software. Every command prints JSON, so AI agents and scripts can capture signals, summarise them and decode protocols.

Written almost entirely by **Claude Opus 5.5** (Anthropic), working in Claude Code. See [Credits](#credits).

> **Unofficial.** This project is not affiliated with or endorsed by Saleae. "Saleae" and "Logic 8" are trademarks of Saleae, Inc. No Saleae firmware or FPGA bitstreams are included: you extract them yourself from your own copy of Logic 2 (see below). Use at your own risk.

## Status

- **Hardware:** second-generation Logic 8, USB ID `21a9:1004`. Tested on a board with a Lattice LFE5U-12F FPGA. Bitstreams for the other three ECP5 variants are tried automatically, but they are untested.
- **Works:**
  - 8-channel digital capture at 200 MHz / k (100, 50, 25, 10 or 1 MS/s, …). Lower rates (for example 10 kS/s) use host-side decimation.
  - Software trigger with a pre-trigger buffer.
  - Built-in UART, I²C and SPI decoders. Any sigrok decoder is reachable through `sigrok-cli`.
  - Capture files in `.sr` (opens in PulseView), `.vcd`, `.npz` and `.csv`.
- **Not supported (yet):**
  - Analog capture.
  - Threshold voltage selection.
  - Logic Pro 8/16 and first-generation Logic devices.

## Install

You do this once per machine. After that, `logic8` works from any folder or project.

1. **Install the system packages:** Python ≥ 3.10, libusb-1.0, pipx and 7z (7z is only used in step 4). On Fedora: `sudo dnf install pipx p7zip p7zip-plugins libusb1`. On Debian/Ubuntu: `sudo apt install pipx 7zip libusb-1.0-0` (on older releases, `p7zip-full` instead of `7zip`).

2. **Give your user access to the device** (no root needed afterwards):

   ```sh
   echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="21a9", ATTR{idProduct}=="1004", MODE="0666"' \
     | sudo tee /etc/udev/rules.d/99-logic8.rules
   sudo udevadm control --reload && sudo udevadm trigger
   ```

3. **Install the `logic8` command:**

   ```sh
   pipx install git+https://github.com/stanjammes/logic8
   ```

4. **Extract the firmware.** The Logic 8 needs Saleae's FX2 firmware and FPGA bitstream every time it is plugged in, and this project can't ship them. Download the Logic 2 AppImage for Linux from saleae.com (version 2.4.48 is tested), then run:

   ```sh
   logic8 setup Logic-2.4.48-linux-x64.AppImage
   ```

   The AppImage is unpacked with 7z and **never executed**. The firmware goes to `~/.local/share/logic8/`, and you can delete the AppImage afterwards.

5. **Check it works:** plug in the Logic 8 and run `logic8 init`. It should report `"fpga": "configured"`. You don't need to run `init` yourself after this: any capture command loads the firmware on its own after a replug, which takes about 5 s.

To update later, run `pipx upgrade logic8`. To use logic8 inside one project's virtual environment instead, install it there with `pip install git+https://github.com/stanjammes/logic8`.

## Usage

```sh
logic8 devices                               # list devices and their firmware/FPGA state
logic8 init                                  # load firmware + bitstream (capture does this too)
logic8 watch -d 200ms                        # "what's on the pins?": capture and summarise
logic8 capture -r 25M -d 100ms -o cap.sr     # capture to a file (.sr/.vcd/.npz/.csv)
logic8 capture -r 25M -d 5ms -t ch0:falling --pre 0.1 -o cap.sr   # with a trigger
logic8 summary cap.sr                        # per channel: periodic/irregular, frequency, duty, pulse widths
logic8 edges cap.sr -c 3 --limit 50          # paginated transitions
logic8 decode cap.sr uart:rx=0               # baud is guessed if omitted
logic8 decode cap.sr i2c:scl=2,sda=3
logic8 decode cap.sr spi:clk=4,mosi=5,cs=6
logic8 decode cap.sr can:can_rx=D0:bitrate=500000 --sigrok   # any sigrok-cli -P decoder
logic8 convert cap.sr cap.vcd
```

- **Output:** results are printed as JSON on stdout. Errors go to stderr as `{"error": ..., "code": n}`.
- **Exit codes:** 2 means bad arguments, 3 a device or USB error, and 4 a trigger timeout.
- **Sample rates:** 200 MHz / k, up to 100 MS/s. USB bandwidth limits rate × channels to 200 M, so use `-c` to capture fewer channels at higher rates:

| Channels | Max rate |
|---|---|
| 8 | 25 MS/s |
| 4 | 50 MS/s |
| 2 | 100 MS/s |

[docs/AGENTS.md](docs/AGENTS.md) has a short recipe sheet to give an agent.

## How it was built

No open-source driver exists for this model, so the protocol was reverse-engineered:

1. **Firmware.** The FX2 firmware and the four ECP5 bitstreams were found by static analysis of Logic 2's native library.
2. **Command channel.** The FX2's command endpoint is XOR-encrypted with a rolling key. The cipher, along with the commands and how the FPGA is loaded, came from disassembling the 8051 firmware.
3. **Capture settings.** The FPGA register setup and the sample stream format came from a one-time USB recording (usbmon) of Logic 2 making a few captures.
4. **Verification.** Everything was checked against a known signal source (below).

Logic 2 also talks to an authentication chip on the board. That step isn't needed for capture, and logic8 doesn't perform it.

## Test-signal generator

`firmware/teensy-siggen` is a PlatformIO project for a Teensy 4.1 that outputs known signals to check the analyser against:

| Logic 8 | Teensy pin | Signal |
|---|---|---|
| D0 | 1 (TX1) | UART 115200 baud, `hello\n` |
| D1 | 2 | PWM 1 kHz, 25 % duty |
| D2 / D3 | 19 (SCL) / 18 (SDA) | I²C 100 kHz write to 0x42 (NACKed, no slave) |
| D4 / D5 / D6 | 13 (SCK) / 11 (MOSI) / 10 (CS) | SPI mode 0, 1 MHz, `A5 5A 01 80` |
| D7 | 6 | 100 kHz square wave |
| GND | GND | |

The protocol bursts repeat every 10 ms. Flash it with `pio run -d firmware/teensy-siggen -t upload`, then run `logic8 watch -d 200ms`.

## Development

To work on logic8 itself, clone the repo and install it in editable mode:

```sh
git clone https://github.com/stanjammes/logic8 && cd logic8
python -m venv venv
venv/bin/pip install -e '.[test]'
venv/bin/python -m pytest -q
```

## Credits

**Claude Opus 5.5** (Anthropic), working in Claude Code, did nearly all of the work. That covers the firmware extraction, the reverse-engineering of the USB protocol, the driver, the CLI, the decoders, the tests and the Teensy firmware.

I just came up with the idea and tested the tool.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). The license covers this project's code only. It does not cover Saleae's firmware or bitstreams, which are not distributed here.
