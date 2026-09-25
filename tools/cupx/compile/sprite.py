from __future__ import annotations

from pathlib import Path
import struct

from PIL import Image

from ..pack.format import TYPE_SPRITE
from ..pack.volumes import AssetSpec
from .texture import (
    TEXFMT_A8R8G8B8,
    TEXFLAG_HAS_ALPHA,
    TEXFLAG_TOP_LEFT,
    TEXFLAG_LINEAR_SOURCE,
    TEXFLAG_POT_PADDED,
    TEXFLAG_EDGE_REPLICATED,
    _edge_replicated_pot_rgba,
    _safe_name,
)

SPRITE_MAGIC = b"CUPR"

# ---------------------------------------------------------------------------
# CUPR v1 - M1 standalone cropped sprite image.
# Retained so the diagnostic/media path stays backwards compatible.
# ---------------------------------------------------------------------------
SPRITE_VERSION_V1 = 1
SPRITE_HEADER_V1 = struct.Struct("<4sHHIIIIIIIIIfffI")

# ---------------------------------------------------------------------------
# CUPR v2 - production atlas/reference sprite descriptor.
#
# No duplicated pixel body is stored.  The descriptor references an already
# packaged CUPT texture and preserves the Unity 5.x SpriteRenderData geometry
# needed by the runtime to derive UVs / packing transforms.
#
# Header layout (<4sHHIIII20fI), 108 bytes:
#   magic/version/header
#   flags, texture_asset_id, alpha_texture_asset_id, settings_raw
#   source_rect x/y/w/h
#   texture_rect x/y/w/h
#   texture_rect_offset x/y
#   atlas_rect_offset x/y
#   pivot x/y
#   pixels_per_unit
#   downscale_multiplier
#   uv_transform x/y/z/w
#   reserved
# ---------------------------------------------------------------------------
SPRITE_VERSION = 2
SPRITE_HEADER_V2 = struct.Struct("<4sHHIIII20fI")

SPRITE_FLAG_ATLAS_RENDER_DATA = 0x00000001
SPRITE_FLAG_DIRECT_RENDER_DATA = 0x00000002
SPRITE_FLAG_HAS_ALPHA_TEXTURE = 0x00000004
SPRITE_FLAG_PACKED = 0x00000008
SPRITE_FLAG_TIGHT_MESH = 0x00000010
SPRITE_FLAG_HAS_UV_TRANSFORM = 0x00000020
SPRITE_FLAG_RECT_UNITY_BOTTOM_LEFT = 0x00000040
# CUPR v2 keeps its payload size/version.  When CUPX downsamples a backing
# atlas for the strict-480p profile, bit 8 marks the existing reserved DWORD as
# a Q16.16 *CUPX mastering scale*.  This is deliberately separate from
# Unity's serialized downscaleMultiplier so authored Sprite geometry remains
# lossless and the Xbox can distinguish logical pixels from mastered texels.
SPRITE_FLAG_CUPX_MASTER_SCALE = 0x00000100


def _v2(value, default=(0.0, 0.0)):
    if value is None:
        return default
    try:
        return float(getattr(value, "x")), float(getattr(value, "y"))
    except Exception:
        pass
    if isinstance(value, dict):
        return float(value.get("x", default[0])), float(value.get("y", default[1]))
    try:
        return float(value[0]), float(value[1])
    except Exception:
        return default


def _v4(value, default=(0.0, 0.0, 0.0, 0.0)):
    if value is None:
        return default
    try:
        return (
            float(getattr(value, "x")), float(getattr(value, "y")),
            float(getattr(value, "z")), float(getattr(value, "w")),
        )
    except Exception:
        pass
    if isinstance(value, dict):
        return tuple(float(value.get(k, default[i])) for i, k in enumerate(("x", "y", "z", "w")))
    try:
        return tuple(float(value[i]) for i in range(4))
    except Exception:
        return default


def _rect(value):
    if value is None:
        return None
    try:
        return {
            "x": float(getattr(value, "x")),
            "y": float(getattr(value, "y")),
            "width": float(getattr(value, "width")),
            "height": float(getattr(value, "height")),
        }
    except Exception:
        pass
    if isinstance(value, dict):
        return {
            "x": float(value.get("x", 0.0)),
            "y": float(value.get("y", 0.0)),
            "width": float(value.get("width", value.get("w", 0.0))),
            "height": float(value.get("height", value.get("h", 0.0))),
        }
    return None


def _rect4(value):
    r = _rect(value) or {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}
    return r["x"], r["y"], r["width"], r["height"]


def _ptr_ids(ptr):
    if ptr is None:
        return 0, 0
    fid = getattr(ptr, "m_FileID", getattr(ptr, "file_id", 0))
    pid = getattr(ptr, "m_PathID", getattr(ptr, "path_id", 0))
    try:
        return int(fid or 0), int(pid or 0)
    except Exception:
        return 0, 0


def _deref_ptr(ptr):
    if ptr is None or _ptr_ids(ptr)[1] == 0:
        return None
    for method in ("deref_parse_as_object", "read"):
        fn = getattr(ptr, method, None)
        if fn:
            try:
                return fn()
            except Exception:
                pass
    fn = getattr(ptr, "deref", None)
    if fn:
        try:
            reader = fn()
            parse = getattr(reader, "parse_as_object", None) or getattr(reader, "read", None)
            return parse() if parse else reader
        except Exception:
            pass
    return None


def _read_object(obj):
    for method in ("parse_as_object", "read"):
        fn = getattr(obj, method, None)
        if fn:
            try:
                return fn()
            except Exception:
                pass
    raise ValueError("Unable to parse Sprite")


def _type_name(obj):
    t = getattr(obj, "type", None)
    return getattr(t, "name", str(t or "Unknown"))


def _object_name(obj):
    try:
        peek = getattr(obj, "peek_name", None)
        if peek:
            value = peek()
            if value:
                return str(value)
    except Exception:
        pass
    try:
        data = _read_object(obj)
        return str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "")
    except Exception:
        return ""


def _render_key_equal(a, b):
    if a == b:
        return True
    # Compare the structural form before repr.  Objects read from the Sprite
    # container and the external SpriteAtlas bundle are different Python
    # instances even when they represent the same pair<GUID,long>.
    try:
        if _render_key_canonical(a) == _render_key_canonical(b):
            return True
    except Exception:
        pass
    # Last compatibility fallback for UnityPy versions with stable reprs.
    try:
        return repr(a) == repr(b)
    except Exception:
        return False


def _render_key_canonical(value, depth=0):
    """Return a stable cross-process token for Unity SpriteRenderData keys.

    UnityPy has exposed these keys as tuples, dicts and generated classes across
    releases.  The optimized full-game compiler pre-indexes SpriteAtlas render
    maps in separate worker processes, so object identity/equality cannot be
    relied on later during the Sprite pass.
    """
    if depth > 8:
        return f"<{type(value).__name__}>"
    if value is None:
        return "n:null"
    if isinstance(value, bool):
        return "b:1" if value else "b:0"
    if isinstance(value, int):
        return f"i:{value}"
    if isinstance(value, float):
        return f"f:{value:.17g}"
    if isinstance(value, str):
        return "s:" + value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "x:" + bytes(value).hex()
    if isinstance(value, dict):
        parts = []
        for k in sorted(value.keys(), key=lambda x: str(x)):
            parts.append(
                _render_key_canonical(k, depth + 1) + "=" +
                _render_key_canonical(value[k], depth + 1)
            )
        return "d:{" + ";".join(parts) + "}"
    if isinstance(value, (tuple, list)):
        return ("t:[" if isinstance(value, tuple) else "l:[") + ";".join(
            _render_key_canonical(x, depth + 1) for x in value
        ) + "]"

    fields = {}
    d = getattr(value, "__dict__", None)
    if isinstance(d, dict):
        for k, v in d.items():
            ks = str(k)
            if ks.startswith("_") or ks in {
                "object_reader", "assets_file", "environment", "reader",
            } or callable(v):
                continue
            fields[ks] = v

    # Generated UnityPy structs frequently use __slots__ rather than
    # __dict__.  Inspect the complete slot set first; SpriteRenderDataKey is a
    # pair<GUID,long> and GUID field names have changed between UnityPy builds.
    # Missing those fields can collapse multiple render keys to the same token,
    # which is catastrophic for a SpriteAtlas because the first record then
    # wins for unrelated sprites.
    try:
        for cls in type(value).__mro__:
            slots = getattr(cls, "__slots__", ()) or ()
            if isinstance(slots, str):
                slots = (slots,)
            for k in slots:
                k = str(k)
                if k.startswith("_") or k in fields or not hasattr(value, k):
                    continue
                try:
                    v = getattr(value, k)
                except Exception:
                    continue
                if not callable(v):
                    fields[k] = v
    except Exception:
        pass

    # Compatibility names used by older/newer generated UnityPy classes.
    for k in (
        "first", "second", "guid", "m_Guid", "value", "m_Value",
        "m_FileID", "m_PathID", "x", "y", "z", "w",
        "data_0", "data_1", "data_2", "data_3",
        "m_Data0", "m_Data1", "m_Data2", "m_Data3",
    ):
        if k not in fields and hasattr(value, k):
            try:
                fields[k] = getattr(value, k)
            except Exception:
                pass

    if fields:
        parts = [
            str(k) + "=" + _render_key_canonical(fields[k], depth + 1)
            for k in sorted(fields)
        ]
        return f"o:{type(value).__name__}:{{" + ";".join(parts) + "}"

    # String form is preferable to repr here because repr for arbitrary Python
    # objects can contain a process-specific memory address.
    try:
        return f"o:{type(value).__name__}:" + str(value)
    except Exception:
        return f"o:{type(value).__name__}"


def render_key_tokens(value):
    """Return canonical + compatibility tokens for a Sprite render-data key."""
    out = [_render_key_canonical(value)]
    try:
        rep = "repr:" + repr(value)
        if rep not in out:
            out.append(rep)
    except Exception:
        pass
    return tuple(out)




def _sprite_rect_is_suspicious(source_rect, texture_rect, settings_raw=0):
    """Reject atlas rectangles that cannot plausibly describe one Sprite.

    textureRect is the packed/trimmed pixel region.  It may be *smaller* than
    m_Rect, but it should never be several times larger than the authored Sprite
    itself.  A bad cross-process RenderDataKey match commonly returns a whole
    2048/4096 atlas page; without this guard the Xbox faithfully displays every
    pose packed into that page.
    """
    try:
        _sx, _sy, sw, sh = [float(x) for x in source_rect]
        _tx, _ty, tw, th = [float(x) for x in texture_rect]
    except Exception:
        return True
    if sw <= 0.0 or sh <= 0.0 or tw <= 0.0 or th <= 0.0:
        return True

    rotation = (int(settings_raw or 0) >> 2) & 0x0F
    # Unity's 90-degree packed form swaps the stored rectangle axes.
    expected_w, expected_h = (sh, sw) if rotation == 4 else (sw, sh)

    # Allow normal atlas padding and a generous 20% serialization variance.
    max_w = expected_w * 1.20 + 16.0
    max_h = expected_h * 1.20 + 16.0
    return tw > max_w or th > max_h

def _indexed_atlas_record(sprite, atlas_render_index):
    if not atlas_render_index:
        return None
    tags = getattr(sprite, "m_AtlasTags", None) or []
    if not tags:
        return None
    tokens = render_key_tokens(getattr(sprite, "m_RenderDataKey", None))
    for tag in tags:
        records = atlas_render_index.get(str(tag).strip().lower())
        if not records:
            continue
        for token in tokens:
            record = records.get(token)
            if record is not None:
                return record
    return None


def _unwrap_atlas_lookup_value(value, fallback_owner=None):
    """Return (atlas_object, owner_rel) from lookup values.

    Normal/legacy callers store the atlas object directly.  The full-game
    compiler may store ``(atlas, owner_rel)`` so a Sprite resolved from a
    dynamically loaded AssetBundle can resolve the atlas texture against the
    bundle that actually owns it instead of the scene container.
    """
    if isinstance(value, tuple) and len(value) == 2:
        return value[0], value[1]
    return value, fallback_owner


def build_sprite_atlas_lookup(env, owner_rel=None):
    """Parse SpriteAtlas objects once per Unity environment, keyed by name.

    ``owner_rel`` is optional and preserves the outer CUPX source container for
    atlas render data.  It is needed for Cuphead's extensionless
    ``StreamingAssets/AssetBundles/atlas_*`` files because scene Sprites have
    null direct texture PPtrs and their real render data lives in a separately
    loaded SpriteAtlas bundle.
    """
    lookup = {}
    if env is None:
        return lookup
    objects = list(getattr(env, "objects", []) or [])
    for cab in getattr(env, "cabs", {}).values():
        for obj in getattr(cab, "objects", {}).values():
            if obj not in objects:
                objects.append(obj)
    for obj in objects:
        if _type_name(obj) != "SpriteAtlas":
            continue
        name = _object_name(obj)
        if not name or name in lookup:
            continue
        try:
            atlas = _read_object(obj)
            lookup[name] = (atlas, owner_rel) if owner_rel else atlas
        except Exception:
            pass
    return lookup


def _lookup_atlas_by_name(atlas_lookup, wanted, fallback_owner=None):
    if atlas_lookup is None:
        return None, None
    value = None
    try:
        value = atlas_lookup.get(wanted)
    except Exception:
        value = None
    if value is None:
        # Atlas tags are case-sensitive in Unity API calls, but package/file
        # naming and our runtime registry are intentionally case-insensitive.
        # Be forgiving across UnityPy/object-name variants.
        try:
            wanted_low = str(wanted).lower()
            for key in atlas_lookup:
                if str(key).lower() == wanted_low:
                    value = atlas_lookup.get(key)
                    break
        except Exception:
            value = None
    if value is None:
        return None, None
    return _unwrap_atlas_lookup_value(value, fallback_owner)


def _find_sprite_atlas_info(sprite, env=None, atlas_lookup=None, default_owner_rel=None):
    atlas_ptr = getattr(sprite, "m_SpriteAtlas", None)
    if _ptr_ids(atlas_ptr)[1] != 0:
        atlas = _deref_ptr(atlas_ptr)
        if atlas is not None:
            return atlas, default_owner_rel

    tags = getattr(sprite, "m_AtlasTags", None) or []
    if not tags:
        return None, None
    wanted = str(tags[0])
    if atlas_lookup is not None:
        # An explicit lookup, including an empty one, means the caller already
        # performed atlas discovery.  Do not rescan the entire Unity container
        # once per Sprite when a tag misses.
        return _lookup_atlas_by_name(atlas_lookup, wanted, default_owner_rel)
    local = build_sprite_atlas_lookup(env, owner_rel=default_owner_rel)
    return _lookup_atlas_by_name(local, wanted, default_owner_rel)


def _find_sprite_atlas(sprite, env=None, atlas_lookup=None):
    # Backwards-compatible helper used by tests/diagnostic code.
    atlas, _owner = _find_sprite_atlas_info(sprite, env, atlas_lookup, None)
    return atlas


def iter_render_data_map(value):
    """Yield (key, value) pairs from UnityPy's list- or mapping-style map."""
    if value is None:
        return
    items = getattr(value, "items", None)
    if callable(items):
        try:
            for key, item in items():
                yield key, item
            return
        except Exception:
            pass
    for pair in value or []:
        try:
            key, item = pair
        except Exception:
            key = getattr(pair, "key", None)
            item = getattr(pair, "value", None)
        yield key, item


def _sprite_render_data_info(sprite, env=None, atlas_lookup=None, default_owner_rel=None):
    atlas, atlas_owner = _find_sprite_atlas_info(
        sprite, env, atlas_lookup, default_owner_rel)
    if atlas is not None:
        wanted = getattr(sprite, "m_RenderDataKey", None)
        for key, value in iter_render_data_map(getattr(atlas, "m_RenderDataMap", None)):
            if _render_key_equal(key, wanted):
                return value, "atlas", atlas_owner
        raise ValueError("SpriteAtlas found but RenderDataKey is missing")

    rd = getattr(sprite, "m_RD", None)
    if rd is None:
        raise ValueError("Sprite has no render data")
    return rd, "direct", default_owner_rel


def _sprite_render_data(sprite, env=None, atlas_lookup=None):
    # Backwards-compatible two-value form.
    rd, source, _owner = _sprite_render_data_info(sprite, env, atlas_lookup, None)
    return rd, source


# ---------------------------------------------------------------------------
# v1 standalone image helpers (diagnostic compatibility)
# ---------------------------------------------------------------------------

def build_sprite_payload(image: Image.Image, pivot=(0.5, 0.5), pixels_per_unit=100.0):
    storage, width, height, storage_width, storage_height = _edge_replicated_pot_rgba(image)
    bgra = storage.tobytes("raw", "BGRA")
    alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
    alpha = alpha_extrema != (255, 255)

    flags = TEXFLAG_TOP_LEFT | TEXFLAG_LINEAR_SOURCE | TEXFLAG_EDGE_REPLICATED
    if alpha:
        flags |= TEXFLAG_HAS_ALPHA
    if storage_width != width or storage_height != height:
        flags |= TEXFLAG_POT_PADDED

    row_pitch = storage_width * 4
    data_bytes = len(bgra)
    px, py = pivot
    header = SPRITE_HEADER_V1.pack(
        SPRITE_MAGIC,
        SPRITE_VERSION_V1,
        SPRITE_HEADER_V1.size,
        width,
        height,
        storage_width,
        storage_height,
        TEXFMT_A8R8G8B8,
        1,
        flags,
        row_pitch,
        data_bytes,
        float(px),
        float(py),
        float(pixels_per_unit),
        0,
    )
    meta = {
        "payload_version": SPRITE_VERSION_V1,
        "payload_header_bytes": SPRITE_HEADER_V1.size,
        "width": width,
        "height": height,
        "storage_width": storage_width,
        "storage_height": storage_height,
        "u_max": width / float(storage_width),
        "v_max": height / float(storage_height),
        "native_format": "A8R8G8B8",
        "native_format_id": TEXFMT_A8R8G8B8,
        "row_pitch": row_pitch,
        "data_bytes": data_bytes,
        "pivot_x": float(px),
        "pivot_y": float(py),
        "pixels_per_unit": float(pixels_per_unit),
        "has_alpha": alpha,
        "layout": "sprite-bgra8-pot",
    }
    return header + bgra, meta


def compile_sprite_object(obj, rel_container: str, group="common", asset_name: str | None = None):
    """Compile legacy standalone CUPR v1.  Used by the M1 diagnostic only."""
    sprite = _read_object(obj)
    image = getattr(sprite, "image", None)
    if image is None:
        raise ValueError("UnityPy returned no Sprite image")
    image = image.convert("RGBA")
    path_id = int(getattr(obj, "path_id", 0) or 0)
    name = str(getattr(sprite, "m_Name", "") or getattr(sprite, "name", "") or f"sprite_{path_id}")
    pivot = _v2(getattr(sprite, "m_Pivot", None), (0.5, 0.5))
    ppu = float(getattr(sprite, "m_PixelsToUnits", 100.0) or 100.0)

    payload, meta = build_sprite_payload(image, pivot=pivot, pixels_per_unit=ppu)
    meta.update({
        "source_type": "Sprite",
        "source_container": rel_container.replace("\\", "/"),
        "source_path_id": path_id,
        "source_name": name,
        "source_rect": _rect(getattr(sprite, "m_Rect", None)),
    })
    logical = asset_name or (
        f"unity/{rel_container.replace('\\', '/')}/sprite/{path_id}/{_safe_name(name)}"
    )
    return AssetSpec(
        name=logical,
        source=None,
        data=payload,
        kind="sprite",
        group=group,
        type=TYPE_SPRITE,
        metadata=meta,
    )


# ---------------------------------------------------------------------------
# v2 production atlas/reference descriptor
# ---------------------------------------------------------------------------

def build_sprite_reference_payload(
    texture_asset_id: int,
    alpha_texture_asset_id: int = 0,
    source_rect=(0.0, 0.0, 0.0, 0.0),
    texture_rect=(0.0, 0.0, 0.0, 0.0),
    texture_rect_offset=(0.0, 0.0),
    atlas_rect_offset=(0.0, 0.0),
    pivot=(0.5, 0.5),
    pixels_per_unit=100.0,
    downscale_multiplier=1.0,
    uv_transform=(0.0, 0.0, 0.0, 0.0),
    settings_raw=0,
    render_source="direct",
    cupx_master_scale=1.0,
):
    texture_asset_id = int(texture_asset_id or 0) & 0xFFFFFFFF
    alpha_texture_asset_id = int(alpha_texture_asset_id or 0) & 0xFFFFFFFF
    settings_raw = int(settings_raw or 0) & 0xFFFFFFFF
    if texture_asset_id == 0:
        raise ValueError("CUPR v2 requires a backing texture asset")

    # SpriteRenderData.textureRect is preserved in Unity's serialized
    # coordinate convention. CUPT pixels are top-left after offline decode, so
    # the runtime must invert rect Y against the backing texture height.
    flags = SPRITE_FLAG_RECT_UNITY_BOTTOM_LEFT
    if render_source == "atlas":
        flags |= SPRITE_FLAG_ATLAS_RENDER_DATA
    else:
        flags |= SPRITE_FLAG_DIRECT_RENDER_DATA
    if alpha_texture_asset_id:
        flags |= SPRITE_FLAG_HAS_ALPHA_TEXTURE
    if settings_raw & 0x1:
        flags |= SPRITE_FLAG_PACKED
    if (settings_raw >> 6) & 0x1:
        flags |= SPRITE_FLAG_TIGHT_MESH
    if any(abs(float(x)) > 1e-12 for x in uv_transform):
        flags |= SPRITE_FLAG_HAS_UV_TRANSFORM

    cupx_master_scale = float(cupx_master_scale or 1.0)
    if not (0.0 < cupx_master_scale <= 1.0):
        raise ValueError("invalid CUPR master scale")
    reserved = 0
    if abs(cupx_master_scale - 1.0) > 1e-9:
        flags |= SPRITE_FLAG_CUPX_MASTER_SCALE
        reserved = max(1, min(0xFFFFFFFF, int(round(cupx_master_scale * 65536.0))))

    sx, sy, sw, sh = [float(x) for x in source_rect]
    tx, ty, tw, th = [float(x) for x in texture_rect]
    tox, toy = [float(x) for x in texture_rect_offset]
    aox, aoy = [float(x) for x in atlas_rect_offset]
    px, py = [float(x) for x in pivot]
    ux, uy, uz, uw = [float(x) for x in uv_transform]

    payload = SPRITE_HEADER_V2.pack(
        SPRITE_MAGIC,
        SPRITE_VERSION,
        SPRITE_HEADER_V2.size,
        flags,
        texture_asset_id,
        alpha_texture_asset_id,
        settings_raw,
        sx, sy, sw, sh,
        tx, ty, tw, th,
        tox, toy,
        aox, aoy,
        px, py,
        float(pixels_per_unit),
        float(downscale_multiplier),
        ux, uy, uz, uw,
        reserved,
    )
    meta = {
        "payload_version": SPRITE_VERSION,
        "payload_header_bytes": SPRITE_HEADER_V2.size,
        "layout": "atlas-reference",
        "texture_asset_id": f"{texture_asset_id:08X}",
        "alpha_texture_asset_id": f"{alpha_texture_asset_id:08X}" if alpha_texture_asset_id else None,
        "settings_raw": settings_raw,
        "flags": flags,
        "source_rect": {"x": sx, "y": sy, "width": sw, "height": sh},
        "texture_rect": {"x": tx, "y": ty, "width": tw, "height": th},
        "texture_rect_offset": {"x": tox, "y": toy},
        "atlas_rect_offset": {"x": aox, "y": aoy},
        "pivot_x": px,
        "pivot_y": py,
        "pixels_per_unit": float(pixels_per_unit),
        "downscale_multiplier": float(downscale_multiplier),
        "cupx_master_scale": cupx_master_scale,
        "uv_transform": {"x": ux, "y": uy, "z": uz, "w": uw},
        "render_source": render_source,
        "packed": bool(settings_raw & 0x1),
        "packing_mode": (settings_raw >> 1) & 0x1,
        "packing_rotation": (settings_raw >> 2) & 0xF,
        "mesh_type": (settings_raw >> 6) & 0x1,
    }
    return payload, meta


def compile_sprite_reference_object(
    obj,
    rel_container: str,
    texture_resolver,
    env=None,
    atlas_lookup=None,
    atlas_render_index=None,
    strict_indexed_atlas=False,
    group="common",
    asset_name: str | None = None,
):
    """Compile a production CUPR v2 descriptor without duplicating pixels.

    ``texture_resolver(ptr, current_rel)`` must return the final runtime CUPT
    asset ID (after any FNV collision resolution), or ``None`` when unresolved.

    ``atlas_render_index`` is the optimized production path: a precomputed,
    process-safe map of atlas tag -> render key -> primitive render metadata.
    It avoids reparsing external ``atlas_*`` AssetBundles for every scene.
    """
    sprite = _read_object(obj)
    path_id = int(getattr(obj, "path_id", 0) or 0)
    name = str(getattr(sprite, "m_Name", "") or getattr(sprite, "name", "") or f"sprite_{path_id}")
    source_rect = _rect4(getattr(sprite, "m_Rect", None))
    atlas_tags = [str(x).strip() for x in (getattr(sprite, "m_AtlasTags", None) or []) if str(x).strip()]
    render_key_token = render_key_tokens(getattr(sprite, "m_RenderDataKey", None))[0]
    rect_resolution = "direct"

    indexed = _indexed_atlas_record(sprite, atlas_render_index)
    if indexed is None and strict_indexed_atlas:
        if atlas_tags:
            wanted = ",".join(atlas_tags[:3])
            raise ValueError(f"SpriteAtlas preindex miss ({wanted})")
    if indexed is not None:
        texture_id = int(indexed.get("texture_asset_id", 0) or 0)
        alpha_id = int(indexed.get("alpha_texture_asset_id", 0) or 0)
        if not texture_id:
            raise ValueError("indexed SpriteAtlas record has no backing Texture2D")
        render_source = "atlas"
        render_owner_rel = str(indexed.get("owner_rel", "") or rel_container)
        texture_rect = tuple(indexed.get("texture_rect", (0.0, 0.0, 0.0, 0.0)))
        texture_rect_offset = tuple(indexed.get("texture_rect_offset", (0.0, 0.0)))
        atlas_rect_offset = tuple(indexed.get("atlas_rect_offset", (0.0, 0.0)))
        settings_raw = int(indexed.get("settings_raw", 0) or 0)
        downscale = float(indexed.get("downscale_multiplier", 1.0) or 1.0)
        uv_transform = tuple(indexed.get("uv_transform", (0.0, 0.0, 0.0, 0.0)))
        rect_resolution = "preindex"
        if _sprite_rect_is_suspicious(source_rect, texture_rect, settings_raw):
            raise ValueError(
                "SpriteAtlas indexed rect inconsistent with source "
                f"(sprite={name}, source={source_rect}, atlas={texture_rect}, "
                f"tags={','.join(atlas_tags[:3]) or '<none>'})"
            )
    else:
        rd, render_source, render_owner_rel = _sprite_render_data_info(
            sprite, env, atlas_lookup, rel_container)

        texture_ptr = getattr(rd, "texture", None) or getattr(rd, "m_Texture", None)
        alpha_ptr = getattr(rd, "alphaTexture", None) or getattr(rd, "m_AlphaTexture", None)
        texture_lookup_rel = render_owner_rel or rel_container
        texture_id = texture_resolver(texture_ptr, texture_lookup_rel)
        if not texture_id:
            _fid, pid = _ptr_ids(texture_ptr)
            raise ValueError(f"backing Texture2D unresolved (PathID {pid})")

        alpha_id = 0
        if _ptr_ids(alpha_ptr)[1] != 0:
            alpha_id = texture_resolver(alpha_ptr, texture_lookup_rel) or 0
            if not alpha_id:
                _fid, pid = _ptr_ids(alpha_ptr)
                raise ValueError(f"alpha Texture2D unresolved (PathID {pid})")

        texture_rect = _rect4(getattr(rd, "textureRect", None) or getattr(rd, "m_TextureRect", None))
        texture_rect_offset = _v2(getattr(rd, "textureRectOffset", None) or getattr(rd, "m_TextureRectOffset", None))
        atlas_rect_offset = _v2(getattr(rd, "atlasRectOffset", None) or getattr(rd, "m_AtlasRectOffset", None))
        settings_raw = int(getattr(rd, "settingsRaw", 0) or getattr(rd, "m_SettingsRaw", 0) or 0)
        downscale = float(getattr(rd, "downscaleMultiplier", 1.0) or getattr(rd, "m_DownscaleMultiplier", 1.0) or 1.0)
        uv_transform = _v4(getattr(rd, "uvTransform", None) or getattr(rd, "m_UVTransform", None))
        rect_resolution = "direct-atlas" if render_source == "atlas" else "direct"
        if render_source == "atlas" and _sprite_rect_is_suspicious(source_rect, texture_rect, settings_raw):
            raise ValueError(
                "SpriteAtlas direct rect inconsistent with source "
                f"(sprite={name}, source={source_rect}, atlas={texture_rect}, "
                f"tags={','.join(atlas_tags[:3]) or '<none>'})"
            )

    pivot = _v2(getattr(sprite, "m_Pivot", None), (0.5, 0.5))
    ppu = float(getattr(sprite, "m_PixelsToUnits", 100.0) or 100.0)

    payload, meta = build_sprite_reference_payload(
        texture_id,
        alpha_id,
        source_rect=source_rect,
        texture_rect=texture_rect,
        texture_rect_offset=texture_rect_offset,
        atlas_rect_offset=atlas_rect_offset,
        pivot=pivot,
        pixels_per_unit=ppu,
        downscale_multiplier=downscale,
        uv_transform=uv_transform,
        settings_raw=settings_raw,
        render_source=render_source,
    )
    meta.update({
        "source_type": "Sprite",
        "source_container": rel_container.replace("\\", "/"),
        "source_path_id": path_id,
        "source_name": name,
        "render_owner_container": (render_owner_rel or rel_container).replace("\\", "/"),
        "atlas_indexed": bool(indexed is not None),
        "atlas_rect_resolution": rect_resolution,
        "atlas_tags": atlas_tags,
        "render_key_token": render_key_token,
    })
    logical = asset_name or (
        f"unity/{rel_container.replace('\\', '/')}/sprite/{path_id}/{_safe_name(name)}"
    )
    deps = [texture_id]
    if alpha_id and alpha_id != texture_id:
        deps.append(alpha_id)
    return AssetSpec(
        name=logical,
        source=None,
        data=payload,
        kind="sprite",
        group=group,
        type=TYPE_SPRITE,
        dependencies=deps,
        metadata=meta,
    )


def scale_sprite_texture_coordinates(payload: bytes, scale: float):
    """Scale only atlas-space coordinates in a CUPR v2 descriptor.

    Source sprite geometry, pivot and pixels-per-unit intentionally remain
    unchanged, so a lower-resolution 480p atlas renders at the exact same
    world/UI size.
    """
    scale = float(scale)
    if not (0.0 < scale <= 1.0):
        raise ValueError("invalid CUPR texture scale")
    if len(payload) != SPRITE_HEADER_V2.size:
        raise ValueError("CUPR v2 payload size mismatch")
    vals = list(SPRITE_HEADER_V2.unpack_from(payload, 0))
    if vals[0] != SPRITE_MAGIC or vals[1] != SPRITE_VERSION or vals[2] != SPRITE_HEADER_V2.size:
        raise ValueError("CUPR v2 header mismatch")
    # <4sHHIIII20fI>: textureRect starts at tuple index 11.
    vals[11] *= scale; vals[12] *= scale; vals[13] *= scale; vals[14] *= scale
    # atlasRectOffset is also atlas-space. textureRectOffset is source-sprite
    # space and must NOT change or the rendered geometry would shift.
    vals[17] *= scale; vals[18] *= scale

    # Preserve the cumulative CUPX mastering scale without overloading
    # SpriteRenderData.downscaleMultiplier (tuple index 22), which belongs to
    # Unity.  The final DWORD was reserved in CUPR v2 specifically so we can
    # extend the contract without changing header size/version.
    current_master = 1.0
    if vals[3] & SPRITE_FLAG_CUPX_MASTER_SCALE:
        q16 = int(vals[27]) & 0xFFFFFFFF
        if q16:
            current_master = q16 / 65536.0
    master = current_master * scale
    if not (0.0 < master <= 1.0):
        raise ValueError("invalid cumulative CUPR master scale")
    vals[3] |= SPRITE_FLAG_CUPX_MASTER_SCALE
    vals[27] = max(1, min(0xFFFFFFFF, int(round(master * 65536.0))))
    return SPRITE_HEADER_V2.pack(*vals)


def parse_sprite_payload(data: bytes):
    if len(data) < 8:
        raise ValueError("Truncated CUPR header")
    magic, version, header_size = struct.unpack_from("<4sHH", data, 0)
    if magic != SPRITE_MAGIC:
        raise ValueError("Bad CUPR magic")

    if version == SPRITE_VERSION_V1:
        if len(data) < SPRITE_HEADER_V1.size:
            raise ValueError("Truncated CUPR v1 header")
        values = SPRITE_HEADER_V1.unpack_from(data, 0)
        (
            _magic, _version, hs, width, height, storage_width, storage_height,
            fmt, mips, flags, pitch, data_bytes, pivot_x, pivot_y, pixels_per_unit,
            _reserved,
        ) = values
        if hs != SPRITE_HEADER_V1.size or header_size != hs:
            raise ValueError("Unexpected CUPR v1 header size")
        if fmt != TEXFMT_A8R8G8B8 or mips != 1:
            raise ValueError("Unsupported CUPR v1 texture format")
        if width <= 0 or height <= 0 or storage_width < width or storage_height < height:
            raise ValueError("Invalid CUPR v1 dimensions")
        if pitch != storage_width * 4 or data_bytes != storage_width * storage_height * 4:
            raise ValueError("Invalid CUPR v1 pixel geometry")
        if header_size + data_bytes != len(data):
            raise ValueError("CUPR v1 payload size mismatch")
        return {
            "version": version,
            "layout": "sprite-bgra8-pot",
            "width": width,
            "height": height,
            "storage_width": storage_width,
            "storage_height": storage_height,
            "u_max": width / float(storage_width),
            "v_max": height / float(storage_height),
            "format": fmt,
            "flags": flags,
            "row_pitch": pitch,
            "data_bytes": data_bytes,
            "pivot_x": pivot_x,
            "pivot_y": pivot_y,
            "pixels_per_unit": pixels_per_unit,
        }

    if version != SPRITE_VERSION:
        raise ValueError(f"Unsupported CUPR version {version}")
    if len(data) != SPRITE_HEADER_V2.size or header_size != SPRITE_HEADER_V2.size:
        raise ValueError("Unexpected CUPR v2 payload size")

    values = SPRITE_HEADER_V2.unpack_from(data, 0)
    (
        _magic, _version, _hs,
        flags, texture_asset_id, alpha_texture_asset_id, settings_raw,
        sx, sy, sw, sh,
        tx, ty, tw, th,
        tox, toy,
        aox, aoy,
        pivot_x, pivot_y,
        pixels_per_unit, downscale_multiplier,
        uvx, uvy, uvz, uvw,
        _reserved,
    ) = values
    if texture_asset_id == 0:
        raise ValueError("CUPR v2 has no backing texture")
    return {
        "version": version,
        "layout": "atlas-reference",
        "flags": flags,
        "texture_asset_id": texture_asset_id,
        "alpha_texture_asset_id": alpha_texture_asset_id,
        "settings_raw": settings_raw,
        "source_rect": {"x": sx, "y": sy, "width": sw, "height": sh},
        "texture_rect": {"x": tx, "y": ty, "width": tw, "height": th},
        "texture_rect_offset": {"x": tox, "y": toy},
        "atlas_rect_offset": {"x": aox, "y": aoy},
        "pivot_x": pivot_x,
        "pivot_y": pivot_y,
        "pixels_per_unit": pixels_per_unit,
        "downscale_multiplier": downscale_multiplier,
        "uv_transform": {"x": uvx, "y": uvy, "z": uvz, "w": uvw},
        "cupx_master_scale": (
            ((int(_reserved) & 0xFFFFFFFF) / 65536.0)
            if (flags & SPRITE_FLAG_CUPX_MASTER_SCALE) and int(_reserved)
            else 1.0
        ),
        "render_source": "atlas" if (flags & SPRITE_FLAG_ATLAS_RENDER_DATA) else "direct",
        "packed": bool(flags & SPRITE_FLAG_PACKED),
        "tight_mesh": bool(flags & SPRITE_FLAG_TIGHT_MESH),
    }