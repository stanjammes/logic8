#!/usr/bin/env python3
"""Extract the Logic 8 FX2 firmware and ECP5 bitstreams from Saleae's Logic 2 AppImage.

Nothing from the AppImage is executed: the squashfs is unpacked with 7z and the
native library is scanned for the embedded Intel-hex firmware and Lattice
bitstreams. Output goes to ~/.local/share/logic8 (override with --out).

    venv/bin/python tools/extract_blobs.py Logic-2.x.y-linux-x64.AppImage
    venv/bin/python tools/extract_blobs.py --lib path/to/libgraph_server_shared.so
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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
        sys.exit("7z not found (needed to unpack the AppImage squashfs without running it)")
    subprocess.run(["7z", "x", "-y", f"-o{workdir}", str(appimage), LIB_REL],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lib = Path(workdir) / LIB_REL
    if not lib.exists():
        sys.exit(f"{LIB_REL} not found in {appimage}")
    return lib


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("appimage", nargs="?", type=Path)
    ap.add_argument("--lib", type=Path, help="use an already-unpacked libgraph_server_shared.so")
    ap.add_argument("--out", type=Path, default=Path.home() / ".local/share/logic8")
    args = ap.parse_args()
    if not (args.appimage or args.lib):
        ap.error("give an AppImage or --lib")

    with tempfile.TemporaryDirectory() as tmp:
        lib_path = args.lib or unpack_appimage(args.appimage, tmp)
        lib = lib_path.read_bytes()

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {"source": str(args.appimage or args.lib), "source_sha256": None, "firmware": None, "bitstreams": []}
    if args.appimage:
        manifest["source_sha256"] = hashlib.sha256(args.appimage.read_bytes()).hexdigest()

    for text, vid, pid in find_fx2_firmware(lib):
        if (vid, pid) == TARGET_VID_PID:
            p = args.out / "fx2-logic8.ihx"
            p.write_text(text)
            manifest["firmware"] = {"file": p.name, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    for design, part, date, blob in find_bitstreams(lib):
        if not design.startswith("Logic8_"):
            continue
        p = args.out / f"fpga-logic8-{part}.bit"
        p.write_bytes(blob)
        manifest["bitstreams"].append({"file": p.name, "part": part, "design": design, "date": date,
                                       "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})

    if not manifest["firmware"] or not manifest["bitstreams"]:
        sys.exit("Logic 8 firmware or bitstreams not found; Saleae may have changed the packaging")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
