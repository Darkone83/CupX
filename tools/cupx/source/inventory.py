from __future__ import annotations
from pathlib import Path
import hashlib, json, re

UNITY_EXTS = {".assets", ".resource", ".ress", ".bundle", ".unity3d"}
AUDIO_EXTS = {".wav", ".mp3", ".ogg"}
VIDEO_EXTS = {".mp4", ".avi", ".wmv", ".xmv", ".webm", ".mov"}

def sha256_file(path: Path, chunk=1024*1024):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b: break
            h.update(b)
    return h.hexdigest()

def classify(p: Path):
    n, s = p.name.lower(), p.suffix.lower()
    # Unity player scene data is commonly stored as extensionless level0,
    # level1, ... files. Cuphead uses the classic *_Data layout, so these must
    # participate in object discovery just like .assets/sharedassets files.
    is_level_file = re.fullmatch(r"level\d+", n) is not None

    # Cuphead also stores runtime-loaded SpriteAtlas/music/texture content in
    # extensionless Unity AssetBundles beneath StreamingAssets/AssetBundles
    # (and the DLC-equivalent AssetBundles directory).  Earlier CUPX scans
    # treated these files as ``other``, which meant the scene Sprites could see
    # their atlas tag but the actual SpriteAtlas render data/Texture2D payloads
    # were never indexed or compiled.  Manifest sidecars are plain text and
    # must not be handed to UnityPy as bundles.
    parts = {part.lower() for part in p.parts}
    in_assetbundle_dir = "assetbundles" in parts
    is_bundle_payload = in_assetbundle_dir and s.lower() != ".manifest"

    if (n in {"globalgamemanagers", "globalgamemanagers.assets"}
            or s in UNITY_EXTS or "sharedassets" in n or is_level_file
            or is_bundle_payload):
        return "unity"
    if s.lower() in AUDIO_EXTS: return "audio"
    if s.lower() in VIDEO_EXTS: return "video"
    if s.lower() in {".dll", ".exe"}: return "binary"
    return "other"

def inventory(root: Path, hash_files=False, progress_callback=None):
    """Inventory a Cuphead install.

    ``progress_callback`` is optional and receives ``(current, total, path)``.
    The demo builder uses it to keep the scan responsive without exposing the
    diagnostic inventory browser from the development tool.
    """
    root = root.resolve()
    paths = []
    for candidate in root.rglob("*"):
        try:
            if candidate.is_file():
                paths.append(candidate)
        except OSError:
            pass

    rows = []
    total = len(paths)
    for index, p in enumerate(paths, 1):
        if progress_callback:
            try:
                progress_callback(index, total, p)
            except Exception:
                pass
        try:
            st = p.stat()
            row = {
                "path": p.relative_to(root).as_posix(),
                "size": st.st_size,
                "kind": classify(p),
            }
            if hash_files:
                row["sha256"] = sha256_file(p)
            rows.append(row)
        except (OSError, ValueError):
            pass
    rows.sort(key=lambda x: x["path"].lower())
    return rows

def summarize(rows):
    out = {"files": len(rows), "bytes": sum(x["size"] for x in rows), "kinds": {}}
    for x in rows:
        out["kinds"][x["kind"]] = out["kinds"].get(x["kind"], 0) + 1
    return out
