"""Copies the minimal GStreamer runtime TritonScreenShare needs into dist, so other devices
don't have to install GStreamer.

Takes the plugins the app uses, then follows every DLL import (normal + delay-load)
recursively and copies whichever of those DLLs live in GStreamer's bin folder. System DLLs
(kernel32, d3d11, ...) are left alone because every Windows machine has them.

GStreamer's DLLs also need the Visual C++ runtime (vcruntime140.dll etc.), which a fresh
Windows install may lack. Given Visual Studio's redist folder, the needed ones are copied in
too (app-local deployment of these files is permitted by Microsoft).

Usage: python bundle_gstreamer.py <gstreamer root> <output dir> <TritonScreenShare.exe> [<VC CRT redist dir>]
"""
import shutil
import struct
import sys
from pathlib import Path

# Plugin files (lib/gstreamer-1.0/gst<name>.dll) providing every element the app may use.
PLUGINS = [
    "coreelements",       # queue, capsfilter
    "d3d11", "d3d12",     # screen capture, convert, GPU decoders, video sink
    "qsv", "nvcodec", "amfcodec", "mediafoundation",  # hardware encoders (Intel/NVIDIA/AMD/any)
    "x264", "x265",       # software encoder fallback
    "libav",              # avdec_h264: the lowest-latency decoder
    "videoconvertscale", "videoparsersbad",
    "rtp", "rtpmanager", "udp",
    "wasapi2", "audioconvert", "audioresample", "opus",
    "volume",             # receiver mute (in the stream, so it survives the sound device resetting)
    "wasapi",             # wasapisrc: exclusive-mode mic for webcams that refuse shared mode
]
HELPERS = ["libexec/gstreamer-1.0/gst-plugin-scanner.exe"]


def pe_imports(path: Path) -> list[str]:
    """DLL names imported by a PE32+ file (import table + delay-load table)."""
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    num_sections = struct.unpack_from("<H", data, pe + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    if struct.unpack_from("<H", data, opt)[0] != 0x20B:  # PE32+ only (64-bit)
        return []
    data_dirs = opt + 112
    sections = []
    for i in range(num_sections):
        s = opt + opt_size + i * 40
        vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, s + 8)
        sections.append((vaddr, max(vsize, raw_size), raw_ptr))

    def to_offset(rva: int) -> int | None:
        for vaddr, size, raw in sections:
            if vaddr <= rva < vaddr + size:
                return rva - vaddr + raw
        return None

    def read_name(rva: int) -> str:
        off = to_offset(rva)
        return data[off:data.index(b"\0", off)].decode("ascii", "replace") if off is not None else ""

    names = []
    for index, entry_size, name_field in ((1, 20, 12), (13, 32, 4)):  # imports, delay imports
        rva, size = struct.unpack_from("<II", data, data_dirs + index * 8)
        off = to_offset(rva) if rva else None
        while off is not None and off + entry_size <= len(data):
            entry = data[off:off + entry_size]
            if not any(entry):
                break
            name_rva = struct.unpack_from("<I", entry, name_field)[0]
            if name_rva:
                names.append(read_name(name_rva))
            off += entry_size
    return names


def main() -> int:
    root, out, exe = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    gst_bin = root / "bin"
    available = {p.name.lower(): p for p in gst_bin.glob("*.dll")}

    if out.exists():
        shutil.rmtree(out)
    (out / "bin").mkdir(parents=True)
    (out / "lib" / "gstreamer-1.0").mkdir(parents=True)
    (out / "libexec" / "gstreamer-1.0").mkdir(parents=True)

    roots = [exe]
    for name in PLUGINS:
        src = root / "lib" / "gstreamer-1.0" / f"gst{name}.dll"
        if not src.exists():
            print(f"  (skipping missing plugin {src.name})")
            continue
        shutil.copy2(src, out / "lib" / "gstreamer-1.0" / src.name)
        roots.append(src)
    for rel in HELPERS:
        src = root / rel
        if src.exists():
            shutil.copy2(src, out / rel)
            roots.append(src)

    # Breadth-first walk of the import graph, staying inside GStreamer's bin folder.
    seen: set[str] = set()
    all_imports: set[str] = set()
    queue = list(roots)
    while queue:
        for dep in pe_imports(queue.pop()):
            key = dep.lower()
            all_imports.add(key)
            if key in available and key not in seen:
                seen.add(key)
                shutil.copy2(available[key], out / "bin" / available[key].name)
                queue.append(available[key])

    crt_note = ""
    if len(sys.argv) > 4:
        crt_dir = Path(sys.argv[4])
        crt = {p.name.lower(): p for p in crt_dir.glob("*.dll")}
        needed = sorted(n for n in all_imports if n in crt)
        for n in needed:
            shutil.copy2(crt[n], out / "bin" / crt[n].name)
        crt_note = f" + VC++ runtime ({', '.join(needed)})"
    else:
        print("  (no VC++ redist folder given: target PCs need the VC++ 2015-2022 x64 redistributable)")

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Bundled {len(PLUGINS)} plugins + {len(seen)} DLLs{crt_note} ({total / 1e6:.0f} MB) into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
