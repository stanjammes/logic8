"""Extract the Logic 8 FX2 firmware and ECP5 bitstreams from Saleae's Logic 2 AppImage.

Nothing from the AppImage is executed: the squashfs is unpacked with 7z and the
native library is scanned for the embedded Intel-hex firmware and Lattice
bitstreams. Output goes to ~/.local/share/logic8 (or $LOGIC8_BLOBS).

    logic8 setup Logic-2.x.y-linux-x64.AppImage
    logic8 setup --lib path/to/libgraph_server_shared.so
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .device import BLOB_DIR

LIB_REL = "usr/lib/logic/resources/linux-x64/libgraph_server_shared.so"
TARGET_VID_PID = ("21a9", "1004")
LATTICE_HDR = b"\xff\x00Lattice"


def parse_ihex(text):
    """Return (image bytes, highest address) for an Intel-hex blob; raises on bad checksum."""
    img = bytearray(0x4000)
    top = 0
    for rec in re.findall(r":([0-9A-Fa-f]+)", text):
        b = bytes.fromhex(rec)
        if sum(b) & 0xFF:
            raise ValueError("bad ihex checksum")
        n, addr, rtype = b[0], b[1] << 8 | b[2], b[3]
        if rtype == 0:
            img[addr:addr + n] = b[4:4 + n]
            top = max(top, addr + n)
    return bytes(img[:top]), top


def find_fx2_firmware(lib):
    """Yield (ihex_text, vid, pid) for each embedded FX2 firmware image."""
    for m in re.finditer(rb"(?::[0-9A-Fa-f]{10,}\r?\n?){10,}", lib):
        text = m.group(0).decode()
        try:
            img, _ = parse_ihex(text)
        except ValueError:
            continue
        d = re.search(rb"\x12\x01\x00\x02.{4}(..)(..)", img, re.S)
        if d:
            yield text, d.group(1)[::-1].hex(), d.group(2)[::-1].hex()


def find_bitstreams(lib):
    """Yield (design, part, date, bytes) for each embedded Lattice bitstream."""
    starts = [m.start() for m in re.finditer(re.escape(LATTICE_HDR), lib)]
    for i, s in enumerate(starts):
        hdr = lib[s:s + 0x200]
        design = re.search(rb"Design name: ([^\0]+)", hdr)
        part = re.search(rb"Part: ([^\0]+)", hdr)
        date = re.search(rb"Date: ([^\0]+)", hdr)
        if not (design and part):
            continue
        end = starts[i + 1] if i + 1 < len(starts) else s + 0x100000
        blob = lib[s:end].rstrip(b"\0")
        yield design.group(1).decode(), part.group(1).decode(), date.group(1).decode() if date else "", blob


def unpack_appimage(appimage, workdir):
    if not shutil.which("7z"):
        raise ValueError("7z not found (install p7zip; it unpacks the AppImage without running it)")
    subprocess.run(["7z", "x", "-y", f"-o{workdir}", str(appimage), LIB_REL],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lib = Path(workdir) / LIB_REL
    if not lib.exists():
        raise ValueError(f"{LIB_REL} not found in {appimage}; is this a Logic 2 AppImage?")
    return lib


def extract(appimage=None, lib=None, out=None):
    """Write the Logic 8 firmware, bitstreams and manifest.json to ``out``; return the manifest."""
    if not (appimage or lib):
        raise ValueError("give a Logic 2 AppImage (or --lib)")
    out = Path(out or BLOB_DIR)
    src = Path(appimage or lib)
    if not src.exists():
        raise FileNotFoundError(f"{src} not found")

    with tempfile.TemporaryDirectory() as tmp:
        lib_path = Path(lib) if lib else unpack_appimage(src, tmp)
        data = lib_path.read_bytes()

    manifest = {"source": str(src), "source_sha256": None, "firmware": None, "bitstreams": []}
    if appimage:
        manifest["source_sha256"] = hashlib.sha256(src.read_bytes()).hexdigest()

    files = {}
    for text, vid, pid in find_fx2_firmware(data):
        if (vid, pid) == TARGET_VID_PID:
            files["fx2-logic8.ihx"] = text.encode()
            manifest["firmware"] = {"file": "fx2-logic8.ihx", "sha256": hashlib.sha256(text.encode()).hexdigest()}

    for design, part, date, blob in find_bitstreams(data):
        if not design.startswith("Logic8_"):
            continue
        name = f"fpga-logic8-{part}.bit"
        files[name] = blob
        manifest["bitstreams"].append({"file": name, "part": part, "design": design, "date": date,
                                       "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})

    if not manifest["firmware"] or not manifest["bitstreams"]:
        raise ValueError("Logic 8 firmware or bitstreams not found; Saleae may have changed the packaging")
    out.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out / name).write_bytes(content)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest | {"output": str(out)}
