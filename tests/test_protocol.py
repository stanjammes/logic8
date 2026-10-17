from logic8 import protocol


class FakeFx2:
    """Mimics the firmware's EP1 handling (flags checked on raw byte 0, then decrypt)."""

    def __init__(self):
        self.key = protocol.BASE_KEY
        self.sent, self.pending = [], b""

    def write(self, ep, data, timeout=None):
        raw = bytes(data)
        self.sent.append(raw)
        if raw[0] & 0x20:
            plain, _ = protocol.crypt(raw[:5], protocol.BASE_KEY)
            self.key = int.from_bytes(plain[1:5], "little")
            return
        if raw[0] & 0x08:
            assert raw[1] == 0x7F, "flag bit would be misread as FPGA chunk"
            return
        plain, self.key = protocol.crypt(raw, self.key)
        if plain[1] == protocol.CMD_GET_REVID:
            self.pending, self.key = protocol.crypt(b"\x01\x03", self.key)

    def read(self, ep, n, timeout=None):
        out, self.pending = self.pending, b""
        return out


def test_seed_and_command_roundtrip():
    dev = FakeFx2()
    ch = protocol.Channel(dev)
    ch.seed(0xDEADBEEF)
    assert dev.sent[0][0] == 0x20 and dev.key == 0xDEADBEEF
    for _ in range(5):
        assert ch.command(protocol.CMD_GET_REVID, reply_len=2) == b"\x01\x03"
        assert dev.sent[-1][0] == 0x00
    assert ch.key == dev.key

