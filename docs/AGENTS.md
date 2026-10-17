# Using `logic8` from an agent

Every command prints one JSON document on stdout. Errors go to stderr as
`{"error": ..., "code": n}`. Exit codes: 2 bad arguments, 3 device/USB, 4 trigger timeout, 1 other.

```sh
logic8 watch -d 200ms                        # "what's on the pins?": capture + per-channel summary
logic8 capture -r 25M -d 100ms -o cap.sr     # save; .sr opens in PulseView, also .vcd/.npz/.csv
logic8 capture -r 25M -d 5ms -t ch0:falling --pre 0.1 -o cap.sr   # software trigger; output has trigger_s
logic8 summary cap.sr                        # kind (constant/periodic/irregular), frequency, duty,
                                             # pulse widths, uart_baud_guess (a hint, not a verdict)
logic8 decode cap.sr uart:rx=0               # baud is auto-guessed if omitted; "text" field has ASCII
logic8 decode cap.sr i2c:scl=2,sda=3
logic8 decode cap.sr spi:clk=4,mosi=5,cs=6[,miso=7,mode=0]
logic8 edges cap.sr -c 3 --limit 50 [--from 1.5ms]   # paginate with next_from_s
```

- **Sample rates:** 200 MHz / n, up to 100 MS/s: 100M, 50M, 25M, 10M, 1M and so on. Lower rates such as 10k are reached by decimation.
- **Bandwidth:** rate × number of channels must be ≤ 200M. 8 channels can run at up to 25 MS/s, and 2 channels at up to 100 MS/s. Use `-c` to capture fewer channels at higher rates.
- **First use:** the first command after the device is plugged in takes about 5 s, because it loads the firmware.
- **CSV:** `.csv` is export-only. Keep `.sr`, `.vcd` or `.npz` for `summary`, `edges` and `decode`.

Typical loop: run `watch` first to see which channels are active and what they look like,
then `capture` at a rate of at least 10× the fastest signal, then `decode`.
