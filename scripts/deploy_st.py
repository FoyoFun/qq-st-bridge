"""Deploy ST-side files from the repo into the local SillyTavern install.

Usage:
    python scripts/deploy_st.py [ST_ROOT]

ST_ROOT defaults to D:/TempFiles/SillyTavern.

Copies:
  st/plugins/nb-qq-bot/*.js   -> <ST_ROOT>/plugins/nb-qq-bot/
  st/preset/*.json            -> <ST_ROOT>/data/default-user/OpenAI Settings/
  st/char/*.json              -> re-embedded into matching character PNG
                                 (updates the 'chara' v2 and 'ccv3' tEXt
                                 chunks in place, pixels untouched)

Requires SillyTavern to be restarted (plugin code) — presets/characters
are re-read per request by the nb-qq-bot plugin, so they hot-apply.
"""

import base64
import json
import os
import shutil
import struct
import sys
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ST = r"D:\TempFiles\SillyTavern"


def read_png_chunks(data: bytes) -> list[tuple[str, bytes]]:
    chunks = []
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8].decode("latin1")
        payload = data[pos + 8:pos + 8 + length]
        chunks.append((ctype, payload))
        pos += 12 + length
    return chunks


def build_chunk(ctype: str, payload: bytes) -> bytes:
    crc = zlib.crc32(ctype.encode("latin1") + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + ctype.encode("latin1")
        + payload
        + struct.pack(">I", crc)
    )


def write_card(png_path: str, card: dict) -> None:
    """Replace the chara/ccv3 tEXt chunks of a character PNG."""
    with open(png_path, "rb") as f:
        data = f.read()
    chunks = read_png_chunks(data)

    v2 = {k: v for k, v in card.items() if k != "data"}
    v2["data"] = card.get("data", {})
    chara_b64 = base64.b64encode(json.dumps(v2, ensure_ascii=False).encode("utf-8"))
    ccv3_b64 = base64.b64encode(json.dumps(card, ensure_ascii=False).encode("utf-8"))

    out = [data[:8]]
    seen_chara = False
    for ctype, payload in chunks:
        if ctype == "tEXt" and payload.split(b"\x00", 1)[0] == b"chara":
            out.append(build_chunk("tEXt", b"chara\x00" + chara_b64))
            seen_chara = True
        elif ctype == "tEXt" and payload.split(b"\x00", 1)[0] == b"ccv3":
            out.append(build_chunk("tEXt", b"ccv3\x00" + ccv3_b64))
        else:
            out.append(build_chunk(ctype, payload))
    if not seen_chara:  # v2 chunk missing: insert before IEND
        out.insert(-1, build_chunk("tEXt", b"chara\x00" + chara_b64))

    tmp = png_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"".join(out))
    shutil.move(tmp, png_path)
    print(f"  card re-embedded: {os.path.basename(png_path)}")


def main() -> None:
    st_root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ST
    errors = 0

    # Plugin code
    src_plugin = os.path.join(REPO_ROOT, "st", "plugins", "nb-qq-bot")
    dst_plugin = os.path.join(st_root, "plugins", "nb-qq-bot")
    for name in os.listdir(src_plugin):
        if name.endswith(".js") or name == "README.md":
            shutil.copy2(os.path.join(src_plugin, name), os.path.join(dst_plugin, name))
    print(f"plugin -> {dst_plugin}")

    # Presets
    src_presets = os.path.join(REPO_ROOT, "st", "preset")
    dst_presets = os.path.join(st_root, "data", "default-user", "OpenAI Settings")
    for name in os.listdir(src_presets):
        if name.endswith(".json"):
            shutil.copy2(
                os.path.join(src_presets, name), os.path.join(dst_presets, name)
            )
    print(f"presets -> {dst_presets}")

    # Character cards (repo JSON is the source of truth; PNG is the container)
    src_chars = os.path.join(REPO_ROOT, "st", "char")
    dst_chars = os.path.join(st_root, "data", "default-user", "characters")
    for name in os.listdir(src_chars):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(src_chars, name), encoding="utf-8") as f:
            card = json.load(f)
        png_path = os.path.join(dst_chars, os.path.splitext(name)[0] + ".png")
        if os.path.exists(png_path):
            try:
                write_card(png_path, card)
            except Exception as e:
                errors += 1
                print(f"  FAILED {name}: {e}")
        else:
            print(f"  no PNG container for {name}, skipped")
    print(f"characters -> {dst_chars}")

    if errors:
        sys.exit(1)
    print("Deploy complete. Restart SillyTavern to load plugin changes.")


if __name__ == "__main__":
    main()
