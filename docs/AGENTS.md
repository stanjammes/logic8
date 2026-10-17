# Using `logic8` from an agent

Output is compact plain text: one record per line, fixed columns, and `-` for a missing value.
Add `--json` anywhere on the line to get one JSON document instead, for scripts or exact values.
Errors go to stderr as `error: ...` (or as `{"error": ..., "code": n}` with `--json`).
Exit codes: 2 bad arguments, 3 device/USB, 4 trigger timeout, 1 other.

```sh
logic8 watch -d 200ms                        # "what's on the pins?": capture + per-channel summary
logic8 capture -r 25M -d 100ms -o cap.sr     # save; .sr opens in PulseView, also .vcd/.npz/.csv
logic8 capture -r 25M -d 5ms -t ch0:falling --pre 0.1 -o cap.sr   # software trigger; header shows "trigger @ t"
logic8 summary cap.sr                        # kind (constant-high/-low, periodic, irregular), frequency, duty, pulse widths
logic8 decode cap.sr uart:rx=0               # baud is auto-guessed if omitted; first line is the text
logic8 decode cap.sr i2c:scl=2,sda=3
logic8 decode cap.sr spi:clk=4,mosi=5,cs=6[,miso=7,mode=0]
logic8 edges cap.sr -c 3 --limit 50 [--from 1.5ms]   # last line "more: --from X" fetches the next page
```

## Reading the output

- **Units:** times are in `ns`/`us`/`ms`/`s` and rates in `Hz`/`kHz`/`MHz` or `MS/s`. Times are written the way `--from` and `-d` accept them, so you can paste them back.
- **`summary` / `watch`:** a header line `<rate>  <samples>  <duration>[  trigger @ t]`, then one row per channel: `ch kind freq duty period edges notes`. The notes are `key=value`:
  - `jitter=`: spread of the period (periodic channels only).
  - `uart~115200`: likely a UART at that baud. This is a hint, not a verdict.
  - `high=`: fraction of time spent high.
  - `min_pulse=`, `high_pulse=min..max`, `low_pulse=min..max`: complete pulse widths.
  - `edges@first..last`: when the first and last transitions happen.
  - `level=a->b`: the level at the start and end of the capture.
- **`decode`:** one row per frame or transaction, then a footer `N frames[, showing first k (raise --limit)]`.
  - UART rows are `t hex char error`. Non-printable characters show `-` in `char`, and the `text:` line escapes them.
  - I²C rows are `t addr rw flags data`. The flags are `addr-nack`, `nack@i` (data byte i was NACKed), `rstart`, `no-stop` and `incomplete`.
  - SPI rows are `t end flags mosi miso`, with the bytes in hex. `partial_bits=n` means the transfer didn't end on a word boundary.
- **`edges`:** `Dn: total edges, showing a-b`, then `t sample edge` rows.

## Notes

- **Sample rates:** 200 MHz / n, up to 100 MS/s: 100M, 50M, 25M, 10M, 1M and so on. Lower rates such as 10k are reached by decimation.
- **Bandwidth:** rate × number of channels must be ≤ 200M. 8 channels can run at up to 25 MS/s, and 2 channels at up to 100 MS/s. Use `-c` to capture fewer channels at higher rates.
- **First use:** the first command after the device is plugged in takes about 5 s, because it loads the firmware.
- **CSV:** `.csv` is export-only. Keep `.sr`, `.vcd` or `.npz` for `summary`, `edges` and `decode`.

Typical loop: run `watch` first to see which channels are active and what they look like,
then `capture` at a rate of at least 10× the fastest signal, then `decode`.
