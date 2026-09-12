"""
Sticker catalog for the [STICKER: 标签] protocol.

Local sticker knowledge base: tag -> image file mapping stored in
data/stickers/catalog.json, image files under data/stickers/.
The AI sees a tag digest in its context and emits [STICKER: tag];
the bridge resolves the tag and sends the image as its own bubble.

Management is via QQ commands:
  /sticker <tag> [备注]   — add sticker from an image in the replied message
  /sticker del <tag>      — remove
  /stickers               — list catalog
"""

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Sticker:
    tag: str
    file: str        # filename under the sticker dir (not full path)
    note: str = ""
    use_count: int = 0
    last_used: float = 0.0


_catalog: dict[str, Sticker] = {}   # tag(lower) -> Sticker
_catalog_file: str = ""
_sticker_dir: str = ""


def _paths() -> tuple[str, str]:
    base = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "data", "stickers"
    )
    return base, os.path.join(base, "catalog.json")


def normalize_tag(tag: str) -> str:
    """Normalize a tag for storage/matching."""
    return re.sub(r"\s+", "", tag or "").strip().lower()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load() -> None:
    """Load the sticker catalog from disk (called at startup)."""
    global _catalog, _catalog_file, _sticker_dir
    _sticker_dir, _catalog_file = _paths()
    _catalog = {}
    try:
        if os.path.exists(_catalog_file):
            with open(_catalog_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for item in raw if isinstance(raw, list) else []:
                tag = normalize_tag(item.get("tag", ""))
                if not tag or not item.get("file"):
                    continue
                _catalog[tag] = Sticker(
                    tag=tag,
                    file=os.path.basename(str(item["file"])),
                    note=str(item.get("note", "")),
                    use_count=int(item.get("use_count", 0)),
                    last_used=float(item.get("last_used", 0.0)),
                )
        logging.info(f"Stickers: loaded {len(_catalog)} catalog entr(ies)")
    except Exception as e:
        logging.warning(f"Stickers: failed to load catalog: {e}")
        _catalog = {}


def _ensure_paths() -> None:
    """Lazily resolve paths so CRUD works even without an explicit load()."""
    global _sticker_dir, _catalog_file
    if not _sticker_dir or not _catalog_file:
        _sticker_dir, _catalog_file = _paths()


def save() -> None:
    """Persist the sticker catalog to disk."""
    _ensure_paths()
    try:
        os.makedirs(_sticker_dir, exist_ok=True)
        data = [
            {
                "tag": s.tag,
                "file": s.file,
                "note": s.note,
                "use_count": s.use_count,
                "last_used": s.last_used,
            }
            for s in _catalog.values()
        ]
        with open(_catalog_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Stickers: failed to save catalog: {e}")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add(tag: str, src_file: str, note: str = "") -> str:
    """Add/replace a sticker by copying the image into the sticker dir.

    Returns the normalized tag. Raises ValueError on bad input.
    """
    tag = normalize_tag(tag)
    if not tag:
        raise ValueError("标签不能为空")
    if not src_file or not os.path.exists(src_file):
        raise ValueError("图片文件不存在")
    _ensure_paths()
    os.makedirs(_sticker_dir, exist_ok=True)
    ext = os.path.splitext(src_file)[1].lower() or ".gif"
    dest_name = f"stk_{int(time.time() * 1000)}{ext}"
    dest = os.path.join(_sticker_dir, dest_name)
    shutil.copyfile(src_file, dest)
    old = _catalog.get(tag)
    if old is not None:
        try:
            old_path = os.path.join(_sticker_dir, old.file)
            if os.path.exists(old_path):
                os.remove(old_path)
        except OSError:
            pass
    _catalog[tag] = Sticker(
        tag=tag, file=dest_name, note=note.strip(),
        use_count=old.use_count if old else 0,
        last_used=old.last_used if old else 0.0,
    )
    save()
    logging.info(f"Stickers: added '{tag}' ({dest_name})")
    return tag


def remove(tag: str) -> bool:
    """Remove a sticker by tag. Returns True if it existed."""
    tag = normalize_tag(tag)
    sticker = _catalog.pop(tag, None)
    if sticker is None:
        return False
    try:
        path = os.path.join(_sticker_dir, sticker.file)
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    save()
    return True


def find(ref: str) -> Optional[Sticker]:
    """Resolve a [STICKER: ref] emitted by the AI to a catalog entry.

    Matching order: exact tag -> tag substring -> note substring.
    """
    ref = normalize_tag(ref)
    if not ref:
        return None
    if ref in _catalog:
        return _catalog[ref]
    for sticker in _catalog.values():
        if ref in sticker.tag or sticker.tag in ref:
            return sticker
    for sticker in _catalog.values():
        if sticker.note and ref in normalize_tag(sticker.note):
            return sticker
    return None


def mark_used(sticker: Sticker) -> None:
    """Record one usage of a sticker."""
    sticker.use_count += 1
    sticker.last_used = time.time()
    save()


def file_uri(sticker: Sticker) -> str:
    """file:// URI for sending via OneBot image segment."""
    return "file:///" + os.path.join(
        os.path.abspath(_sticker_dir), sticker.file
    ).replace("\\", "/")


def all_tags() -> list[Sticker]:
    """All catalog entries, most-used first."""
    return sorted(
        _catalog.values(),
        key=lambda s: (s.use_count, s.last_used),
        reverse=True,
    )


def catalog_summary(limit: int = 16) -> str:
    """Render the catalog digest for prompt injection (empty if no stickers)."""
    entries = all_tags()[: max(1, limit)]
    if not entries:
        return ""
    lines = []
    for s in entries:
        extra = f"（{s.note}）" if s.note else ""
        lines.append(f"- {s.tag}{extra}")
    return "\n".join(lines)
