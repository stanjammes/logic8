"""EP1 command channel of the Logic 8 FX2 firmware (reverse-engineered from the 8051 code).

Packets on EP1 OUT/IN are XORed with a rolling 32-bit key: byte ``i`` uses key byte
``i % 4`` (little-endian), and after every packet in either direction the key
advances through an LFSR (taps 0, 1, 21, 31; ``(key & 0x1f) + 0x23`` steps).

Byte 0 of a raw packet carries flags that the firmware checks *before* decrypting:
  0x20  seed packet: bytes 0..4 decrypted with BASE_KEY, bytes 1..4 become the key
  0x08  with byte 1 == 0x7f: raw FPGA bitstream chunk (not encrypted)
otherwise the packet is decrypted and byte 1 is the command.
"""
BASE_KEY = 0x354B248E

CMD_START_ACQUISITION = 0x01
CMD_ABORT_ACQUISITION = 0x02
CMD_READ_EEPROM = 0x07
CMD_FPGA_UPLOAD_INIT = 0x7E
CMD_FPGA_UPLOAD_DATA = 0x7F
CMD_FPGA_WRITE_REGISTER = 0x80
CMD_FPGA_READ_REGISTER = 0x81
CMD_GET_REVID = 0x82


def lfsr(key):
    for _ in range((key & 0x1F) + 0x23):
        fb = (key ^ key >> 1 ^ key >> 21 ^ key >> 31) & 1
        key = key >> 1 | fb << 31
    return key


def crypt(data, key):
    """XOR ``data`` with the keystream for ``key``; returns (bytes, next key)."""
    out = bytes(b ^ (key >> 8 * (i % 4)) & 0xFF for i, b in enumerate(data))
    return out, lfsr(key)


class Channel:
    """Encrypted EP1 command/response channel. ``dev`` is a pyusb device."""

    def __init__(self, dev, timeout=1000):
        self.dev = dev
        self.timeout = timeout
        self.key = None

    def seed(self, new_key=0x12345678):
        plain = bytes([0]) + new_key.to_bytes(4, "little")
        k0 = BASE_KEY & 0xFF
        plain = bytes([0x20 ^ k0]) + plain[1:]  # ciphertext byte 0 == 0x20 (seed flag)
        raw, _ = crypt(plain, BASE_KEY)
        self.dev.write(0x01, raw, self.timeout)
        self.key = new_key

    def command(self, cmd, payload=b"", reply_len=0):
        if self.key is None:
            self.seed()
        plain = bytes([self.key & 0xFF, cmd]) + bytes(payload)  # ciphertext byte 0 == 0 (no flags)
        raw, self.key = crypt(plain, self.key)
        self.dev.write(0x01, raw, self.timeout)
        if not reply_len:
            return b""
        data = bytes(self.dev.read(0x81, 64, self.timeout))
        reply, self.key = crypt(data, self.key)
        return reply[:reply_len]

    def fpga_chunk(self, chunk):
        """Raw bitstream chunk: [0x08, 0x7f, len, data...]."""
        self.dev.write(0x01, bytes([0x08, 0x7F, len(chunk)]) + bytes(chunk), self.timeout)
