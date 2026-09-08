#!/usr/bin/env python3
"""Point Discord.app at a local Vencord build, the way Vencord's installer
does, without the installer: rename Resources/app.asar to _app.asar and
write a two-file app.asar whose index.js requires <build>/dist/patcher.js.
Discord's own updater replaces the bundle on update, which undoes this;
run it again afterwards.

    patch_discord.py --build ~/.local/discord-local-log/Vencord
    patch_discord.py --unpatch
    patch_discord.py --status

Refuses to touch anything while Discord is running.
"""

import argparse
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

RESOURCES = Path("/Applications/Discord.app/Contents/Resources")
PACKAGE_JSON = '{\n\t"name": "discord",\n\t"main": "index.js"\n}'


def asar_bytes(files):
    """Minimal asar archive of {name: bytes}. Same layout the Vencord
    installer writes: a pickle-style header (four little-endian uint32)
    followed by the JSON header and the concatenated file contents."""
    header = {"files": {}}
    body = b""
    for name, data in files.items():
        header["files"][name] = {"size": len(data), "offset": str(len(body))}
        body += data
    hs = json.dumps(header, separators=(",", ":")).encode()
    hlen = len(hs)
    aligned = (hlen + 3) & ~3
    hs += b"0" * (aligned - hlen)
    prefix = struct.pack("<IIII", 4, aligned + 8, aligned + 4, hlen)
    return prefix + hs + body


def parse_asar(blob):
    """The inverse, for --status and the tests."""
    _, _, _, hlen = struct.unpack("<IIII", blob[:16])
    header = json.loads(blob[16 : 16 + hlen])
    aligned = (hlen + 3) & ~3
    base = 16 + aligned
    out = {}
    for name, e in header["files"].items():
        off = base + int(e["offset"])
        out[name] = blob[off : off + e["size"]]
    return out


def discord_running():
    return subprocess.run(["pgrep", "-x", "Discord"], capture_output=True).returncode == 0


def status(resources=RESOURCES):
    app, orig = resources / "app.asar", resources / "_app.asar"
    if not orig.exists():
        return {"patched": False}
    try:
        index = parse_asar(app.read_bytes())["index.js"].decode()
    except Exception as e:  # noqa: BLE001
        return {"patched": True, "error": f"app.asar unreadable: {e}"}
    return {"patched": True, "index_js": index}


def patch(build, resources=RESOURCES):
    patcher = Path(build).expanduser().resolve() / "dist" / "patcher.js"
    if not patcher.is_file():
        return {"error": f"no built patcher at {patcher}; run pnpm build first"}
    app, orig = resources / "app.asar", resources / "_app.asar"
    if not app.exists():
        return {"error": f"{app} not found"}
    if not orig.exists():
        os.rename(app, orig)  # first time: keep Discord's real bundle aside
    index_js = f"require({json.dumps(str(patcher))})"
    app.write_bytes(
        asar_bytes({"index.js": index_js.encode(), "package.json": PACKAGE_JSON.encode()})
    )
    return {"patched": True, "index_js": index_js}


def unpatch(resources=RESOURCES):
    app, orig = resources / "app.asar", resources / "_app.asar"
    if not orig.exists():
        return {"patched": False, "note": "nothing to undo"}
    os.replace(orig, app)
    return {"patched": False}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build", help="Vencord checkout containing dist/patcher.js")
    ap.add_argument("--unpatch", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.status:
        print(json.dumps(status()))
        return 0
    if discord_running():
        print(json.dumps({"error": "Discord is running; quit it first"}))
        return 1
    if a.unpatch:
        print(json.dumps(unpatch()))
        return 0
    if not a.build:
        ap.error("--build <vencord dir>, --unpatch, or --status")
    out = patch(a.build)
    print(json.dumps(out))
    return 1 if "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())

