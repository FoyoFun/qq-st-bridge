"""Deploy ST-side files from the repo into the local SillyTavern install.

Usage:
    python scripts/deploy_st.py [ST_ROOT]

ST_ROOT defaults to C:/TempProgram/SillyTavern.

Copies:
  st/plugins/nb-qq-bot/*.js   -> <ST_ROOT>/plugins/nb-qq-bot/
  st/preset/*.json            -> <ST_ROOT>/data/default-user/OpenAI Settings/
  st/char/*.json              -> re-embedded into matching character PNG
                                 (updates the 'chara' v2 and 'ccv3' tEXt
                                 chunks in place, pixels untouched)

Requires SillyTavern to be restarted (plugin code) — presets/characters
are re-read per request by the nb-qq-bot plugin, so they hot-apply.

No model configuration lives in this repo: the plugin resolves the model
per request from ST's live connection settings. If ST is running, a
post-deploy connection check prints which source/model the bot will use.
"""

import base64
import json
import os
import re
import shutil
import struct
import sys
import urllib.error
import urllib.request
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ST = r"C:\TempProgram\SillyTavern"


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
    seen_ccv3 = False
    for ctype, payload in chunks:
        if ctype == "tEXt" and payload.split(b"\x00", 1)[0] == b"chara":
            out.append(build_chunk("tEXt", b"chara\x00" + chara_b64))
            seen_chara = True
        elif ctype == "tEXt" and payload.split(b"\x00", 1)[0] == b"ccv3":
            out.append(build_chunk("tEXt", b"ccv3\x00" + ccv3_b64))
            seen_ccv3 = True
        else:
            out.append(build_chunk(ctype, payload))
    if not seen_chara or not seen_ccv3:
        # Fresh container (or legacy file missing one format): insert the
        # missing chunks before IEND. Order after IHDR/IDAT is fine — ST
        # scans all tEXt chunks regardless of position.
        if not seen_chara:
            out.insert(-1, build_chunk("tEXt", b"chara\x00" + chara_b64))
        if not seen_ccv3:
            out.insert(-1, build_chunk("tEXt", b"ccv3\x00" + ccv3_b64))

    tmp = png_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"".join(out))
    shutil.move(tmp, png_path)
    print(f"  card re-embedded: {os.path.basename(png_path)}")


def _merge_cookies(existing: str, set_cookies: list[str]) -> str:
    """Fold Set-Cookie header values into a Cookie header string.

    ST sends both a session cookie and its .sig companion; both must be
    echoed back or the API answers 403.
    """
    jar: dict[str, str] = {}
    if existing:
        for pair in existing.split("; "):
            if "=" in pair:
                name, value = pair.split("=", 1)
                jar[name] = value
    for header in set_cookies:
        pair = header.split(";", 1)[0]
        if "=" in pair:
            name, value = pair.split("=", 1)
            jar[name] = value
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _st_http(
    base: str,
    path: str,
    body: dict | None,
    token: str | None,
    cookie: str,
) -> tuple[dict, str]:
    """HTTP call to ST's API; returns (parsed_json, cookie_header)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-CSRF-Token"] = token
    if cookie:
        headers["Cookie"] = cookie
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        merged = _merge_cookies(cookie, resp.headers.get_all("Set-Cookie") or [])
        return json.loads(resp.read().decode("utf-8")), merged


def report_connection(st_root: str) -> None:
    """Print which source/model the bot will use, resolved from live ST.

    Read-only. The plugin resolves the model per request from ST's live
    connection settings, so nothing is baked in at deploy time — this just
    surfaces what a fresh deployment would pick up, so misconfiguration
    (wrong source, stale model name) is visible immediately.
    """
    port = None
    try:
        with open(os.path.join(st_root, "config.yaml"), encoding="utf-8") as f:
            m = re.search(r"^port\s*:\s*(\d+)", f.read(), re.M)
        port = int(m.group(1)) if m else None
    except OSError:
        pass
    if not port:
        print("connection check: skipped (no port in config.yaml)")
        return

    base = f"http://127.0.0.1:{port}"
    try:
        body, cookie = _st_http(base, "/csrf-token", None, None, "")
        token = body["token"]
        payload, cookie = _st_http(base, "/api/settings/get", {}, token, cookie)
        raw = payload.get("settings")
        # /api/settings/get wraps the full settings.json as a string under
        # `settings`; tolerate a direct oai_settings object as well
        parsed = json.loads(raw) if isinstance(raw, str) else payload
        oai = parsed.get("oai_settings") or {}
        source = oai.get("chat_completion_source")
        model = oai.get(f"{source}_model") if source else None
        if not source or not model:
            print(
                "connection check: ST UI has no connection configured yet — "
                "open the ST web UI, connect an API once, and the bot will "
                "follow it automatically."
            )
            return
        print(f"connection check: ST UI source={source!r} model={model!r}")
        status, _ = _st_http(
            base,
            "/api/backends/chat-completions/status",
            {"chat_completion_source": source, "reverse_proxy": ""},
            token,
            cookie,
        )
        ids = sorted(
            m["id"]
            for m in (status.get("data") or [])
            if isinstance(m, dict) and "id" in m
        )
        if ids and model not in ids:
            print(
                f"  WARNING: {model!r} is no longer offered. "
                f"Available: {', '.join(ids)}"
            )
            print(
                "  The bot falls back to the closest model automatically; "
                "pick one in the ST UI to pin it."
            )
        elif ids:
            print(f"  OK: {model!r} is offered by the source ({len(ids)} models).")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        print(f"connection check: failed (HTTP {e.code}: {detail})")
    except Exception as e:
        print(f"connection check: skipped (ST not reachable: {e})")


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
        base = os.path.splitext(name)[0]
        png_path = os.path.join(dst_chars, base + ".png")
        if not os.path.exists(png_path):
            # New character: seed the PNG container from a repo-provided
            # base image (st/char/<name>.png) if the user supplied one.
            repo_png = os.path.join(src_chars, base + ".png")
            if os.path.exists(repo_png):
                shutil.copyfile(repo_png, png_path)
                print(f"  seeded container from repo image: {base}.png")
        if os.path.exists(png_path):
            try:
                write_card(png_path, card)
            except Exception as e:
                errors += 1
                print(f"  FAILED {name}: {e}")
        else:
            errors += 1
            print(
                f"  FAILED {name}: no PNG container. Drop any square PNG as "
                f"st/char/{base}.png (the avatar image) and re-run deploy."
            )
    print(f"characters -> {dst_chars}")

    if errors:
        sys.exit(1)
    report_connection(st_root)
    print("Deploy complete. Restart SillyTavern to load plugin changes.")


if __name__ == "__main__":
    main()
