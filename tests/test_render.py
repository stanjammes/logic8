import json

import pytest

from logic8 import analysis, cli, decoders, formats, render
from logic8.capture import Capture

import synth
from test_analysis import pwm_capture


@pytest.mark.parametrize("x, unit, want", [
    (0, "s", "0 s"), (1000, "Hz", "1 kHz"), (8.68e-6, "s", "8.68 us"), (2.5e6, "", "2.5M"),
    (25e6, "S/s", "25 MS/s"), (999999, "Hz", "1 MHz"), (1.5e-10, "s", "0.15 ns"), (1500, "s", "1500 s"),
    (None, "s", "-"),
])
def test_si(x, unit, want):
    assert render.si(x, unit) == want


def test_si_roundtrips_through_parse_duration():
    for t in (20e-6, 1.25e-3, 0.5, 3.3e-9):
        assert cli.parse_duration(render.si(t, "s", 9, sep="")) == pytest.approx(t)


def test_summary_text():
    out = render.summary(analysis.summary(pwm_capture())).splitlines()
    assert out[0] == "24 MS/s  240k samples  10 ms"
    assert out[1].split() == ["ch", "kind", "freq", "duty", "period", "edges", "notes"]
    assert out[2].split()[:7] == ["D0", "periodic", "1", "kHz", "25.0%", "1", "ms"]


def test_edges_text_has_continuation():
    d = analysis.edges(pwm_capture(), 0, limit=3)
    out = render.edges(d).splitlines()
    assert out[0] == "D0: 19 edges, showing 0-2"
    assert out[-1].startswith("more: --from ")
    nxt = cli.parse_duration(out[-1].split()[-1])
    assert analysis.edges(pwm_capture(), 0, start_s=nxt, limit=3)["offset"] == 3


def test_decode_text():
    w = synth.Wave(10e6)
    w.hold(1e-4)
    synth.uart(w, 0, b"ok\n", 115200)
    w.hold(1e-4)
    frames = decoders.decode(Capture.from_samples(w.samples(), 10e6), "uart:rx=0,baud=115200")
    d = {"decoder": "uart:rx=0", "count": len(frames), "frames": frames, "truncated": False,
         "text": decoders.uart_text(frames)}
    out = render.decode(d).splitlines()
    assert out[0] == "text: 'ok\\n'"
    assert out[2].split()[2:] == ["0x6f", "o", "-"]
    assert out[-1] == "3 frames"


def test_cli_text_and_json(tmp_path, capsys):
    f = tmp_path / "pwm.npz"
    formats.save(pwm_capture(), f)
    assert cli.main(["summary", str(f)]) == 0
    assert capsys.readouterr().out.startswith("24 MS/s")
    for argv in (["summary", str(f), "--json"], ["--json", "summary", str(f)]):
        assert cli.main(argv) == 0
        assert json.loads(capsys.readouterr().out)["channels"][0]["kind"] == "periodic"


def test_cli_errors(tmp_path, capsys):
    missing = str(tmp_path / "nope.npz")
    assert cli.main(["summary", missing]) == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert cli.main(["summary", missing, "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["code"] == 2
    with pytest.raises(SystemExit) as e:
        cli.main(["--json", "edges", missing])  # missing -c
    assert e.value.code == 2
    assert "required" in json.loads(capsys.readouterr().err)["error"]


def test_edges_past_end():
    d = analysis.edges(pwm_capture(), 0, start_s=1.0)
    assert render.edges(d) == "D0: 19 edges, none from here"


def test_uart_text_is_ascii():
    d = {"decoder": "uart:rx=0", "count": 1, "truncated": False, "text": "\xf9",
         "frames": [{"t_s": 0.0, "value": 0xf9, "hex": "0xf9", "char": None}]}
    assert render.decode(d).splitlines()[0] == "text: '\\xf9'"


def test_decode_missing_channel_message(tmp_path, capsys):
    f = tmp_path / "pwm.npz"
    formats.save(pwm_capture(), f)
    assert cli.main(["decode", str(f), "uart:rx=5"]) == 2
    assert capsys.readouterr().err.startswith("error: channel 5 not in capture")
