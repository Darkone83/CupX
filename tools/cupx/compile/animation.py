from __future__ import annotations

from pathlib import Path
import re
import struct

from ..pack.format import TYPE_ANIMATION
from ..pack.io import asset_id
from ..pack.volumes import AssetSpec
from .texture import _load_env, _safe_name, compile_image_as_texture

# Native sprite-animation payload v1.
#
# Cuphead's decompiled AnimatorExtensions derives exact frame position from
# AnimationClip.frameRate/length/normalizedTime. CUPA pre-bakes the equivalent
# sprite key times so the Xbox runtime never needs Unity's Animator or curve
# representation for this diagnostic path.
ANIMATION_MAGIC = b"CUPA"
ANIMATION_VERSION = 1
ANIMATION_HEADER = struct.Struct("<4sHHIIIIII")
ANIMATION_FRAME = struct.Struct("<II")  # start_ms, texture asset id (0 = blank)

ANIMFLAG_LOOP = 0x00000001
ANIMFLAG_HAS_BLANKS = 0x00000002
ANIMFLAG_SPRITE_SEQUENCE = 0x00000004

DEFAULT_MIN_FRAMES = 3
DEFAULT_MAX_FRAMES = 24
DEFAULT_MAX_FRAME_DIMENSION = 1024
DEFAULT_MAX_TOTAL_TEXTURE_BYTES = 24 * 1024 * 1024


def _type_name(obj):
    t = getattr(obj, "type", None)
    return getattr(t, "name", str(t or "Unknown"))


def _obj_name(data):
    return str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "")


def _read_object(obj):
    """Parse a Unity object across UnityPy 1.x/2-style APIs."""
    for method in ("parse_as_object", "read"):
        fn = getattr(obj, method, None)
        if fn:
            try:
                return fn()
            except Exception:
                pass
    raise ValueError("Unable to parse Unity object")


def _curve_keyframes(curve):
    return (
        getattr(curve, "curve", None)
        or getattr(curve, "m_Curve", None)
        or []
    )


def _key_time(k):
    return float(getattr(k, "time", getattr(k, "m_Time", 0.0)) or 0.0)


def _key_value(k):
    return getattr(k, "value", getattr(k, "m_Value", None))


def _ptr_ids(ptr):
    if ptr is None:
        return 0, 0
    return (
        int(getattr(ptr, "m_FileID", getattr(ptr, "file_id", 0)) or 0),
        int(getattr(ptr, "m_PathID", getattr(ptr, "path_id", 0)) or 0),
    )


def _deref_ptr(ptr):
    """Return a parsed Unity object from a PPtr across UnityPy variants."""
    if ptr is None:
        return None
    _fid, pid = _ptr_ids(ptr)
    if pid == 0:
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
        reader = fn()
        for method in ("parse_as_object", "read"):
            rf = getattr(reader, method, None)
            if rf:
                try:
                    return rf()
                except Exception:
                    pass
    raise ValueError("Unable to dereference Unity PPtr")


def _sprite_from_ptr(ptr):
    data = _deref_ptr(ptr)
    if data is None:
        return None

    # Parsed Sprite classes expose .image in UnityPy's Sprite handler.
    image = getattr(data, "image", None)
    if image is None:
        raise TypeError("PPtr did not resolve to a previewable Sprite")
    return data


def build_animation_payload(frames, sample_rate: float, loop=True):
    """Build CUPA v1 from [(start_ms, texture_asset_id), ...]."""
    if len(frames) < 1:
        raise ValueError("Animation requires at least one frame")

    clean = []
    last_ms = -1
    has_blanks = False
    for start_ms, tex_id in frames:
        start_ms = max(0, int(start_ms))
        tex_id = int(tex_id) & 0xFFFFFFFF
        if start_ms < last_ms:
            raise ValueError("Animation frame times must be monotonic")
        last_ms = start_ms
        has_blanks = has_blanks or tex_id == 0
        clean.append((start_ms, tex_id))

    frame_period_ms = max(1, int(round(1000.0 / max(float(sample_rate), 1.0))))
    duration_ms = max(clean[-1][0] + frame_period_ms, frame_period_ms)
    flags = ANIMFLAG_SPRITE_SEQUENCE
    if loop:
        flags |= ANIMFLAG_LOOP
    if has_blanks:
        flags |= ANIMFLAG_HAS_BLANKS

    header = ANIMATION_HEADER.pack(
        ANIMATION_MAGIC,
        ANIMATION_VERSION,
        ANIMATION_HEADER.size,
        flags,
        len(clean),
        duration_ms,
        max(1, int(round(float(sample_rate) * 1000.0))),
        ANIMATION_FRAME.size,
        0,
    )
    body = b"".join(ANIMATION_FRAME.pack(ms, aid) for ms, aid in clean)
    meta = {
        "payload_version": ANIMATION_VERSION,
        "payload_header_bytes": ANIMATION_HEADER.size,
        "frame_record_bytes": ANIMATION_FRAME.size,
        "frame_count": len(clean),
        "duration_ms": duration_ms,
        "sample_rate": float(sample_rate),
        "loop": bool(loop),
        "has_blank_frames": bool(has_blanks),
        "layout": "sprite-texture-asset-ids",
    }
    return header + body, meta


def parse_animation_payload(data: bytes):
    if len(data) < ANIMATION_HEADER.size:
        raise ValueError("Truncated CUPA header")
    (
        magic, version, header_size, flags, frame_count,
        duration_ms, sample_rate_milli, record_size, _reserved
    ) = ANIMATION_HEADER.unpack_from(data, 0)
    if magic != ANIMATION_MAGIC:
        raise ValueError("Bad CUPA magic")
    if version not in (1, 2):
        raise ValueError(f"Unsupported CUPA version {version}")
    if header_size != ANIMATION_HEADER.size or record_size != ANIMATION_FRAME.size:
        raise ValueError("Unexpected CUPA structure size")
    expected = header_size + frame_count * record_size
    if expected != len(data):
        raise ValueError("CUPA payload size mismatch")

    frames = []
    off = header_size
    for _ in range(frame_count):
        frames.append(ANIMATION_FRAME.unpack_from(data, off))
        off += record_size

    return {
        "version": version,
        "flags": flags,
        "frame_count": frame_count,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate_milli / 1000.0,
        "loop": bool(flags & ANIMFLAG_LOOP),
        "has_blank_frames": bool(flags & ANIMFLAG_HAS_BLANKS),
        "layout": "sprite-asset-ids" if version == 2 else "sprite-texture-asset-ids",
        "frames": [
            {"start_ms": ms, ("sprite_asset_id" if version == 2 else "texture_asset_id"): f"{aid:08X}"}
            for ms, aid in frames
        ],
    }


def _candidate_unity_rows(rows):
    for row in sorted(rows, key=lambda r: r["path"].lower()):
        if row.get("kind") != "unity":
            continue
        p = row["path"].lower()
        if p.endswith(".resource") or p.endswith(".ress"):
            continue
        yield row


def _animation_curve_candidates(clip):
    """Return explicit serialized m_PPtrCurves, preferring m_Sprite."""
    curves = list(getattr(clip, "m_PPtrCurves", None) or [])
    if not curves:
        return []

    preferred = []
    fallback = []
    for c in curves:
        keys = list(_curve_keyframes(c))
        if not keys:
            continue
        attribute = str(getattr(c, "attribute", "") or getattr(c, "m_Attribute", "") or "")
        rec = {
            "source": "m_PPtrCurves",
            "curve": c,
            "path": str(getattr(c, "path", "") or getattr(c, "m_Path", "") or ""),
            "attribute": attribute,
            "frames": [(_key_time(k), _key_value(k)) for k in keys],
        }
        if "sprite" in attribute.lower():
            preferred.append(rec)
        else:
            fallback.append(rec)
    return preferred + fallback


def _binding_mapping_candidates(clip):
    """Recover sprite frame references from optimized ClipBindingConstant data.

    Cuphead's decompiled .anim assets use a Sprite PPtr binding (classID 212,
    customType 23) plus pptrCurveMapping. Some retail/UnityPy combinations do
    not expose populated m_PPtrCurves even though this mapping survives. For the
    common single-Sprite-binding case, the mapping order is the frame order and
    frame time is derived from AnimationClip.m_SampleRate.
    """
    bc = getattr(clip, "m_ClipBindingConstant", None)
    if bc is None:
        return []

    mapping = list(getattr(bc, "pptrCurveMapping", None) or
                   getattr(bc, "m_PPtrCurveMapping", None) or [])
    bindings = list(getattr(bc, "genericBindings", None) or
                    getattr(bc, "m_GenericBindings", None) or [])
    if not mapping or not bindings:
        return []

    sprite_bindings = []
    for b in bindings:
        is_pptr = int(getattr(b, "isPPtrCurve", getattr(b, "m_IsPPtrCurve", 0)) or 0)
        class_id = int(getattr(b, "classID", getattr(b, "m_ClassID", 0)) or 0)
        custom_type = int(getattr(b, "customType", getattr(b, "m_CustomType", 0)) or 0)
        if is_pptr and (class_id == 212 or custom_type == 23):
            sprite_bindings.append(b)

    # Without per-binding key counts, a flattened mapping with several PPtr
    # bindings cannot be safely partitioned. The diagnostic only needs one
    # unambiguous Sprite sequence, so accept the common single-binding case.
    if len(sprite_bindings) != 1:
        return []

    sample_rate = float(getattr(clip, "m_SampleRate", 0.0) or 0.0)
    if sample_rate <= 0.0 or sample_rate > 240.0:
        sample_rate = 24.0

    frames = [(i / sample_rate, ptr) for i, ptr in enumerate(mapping)]
    return [{
        "source": "ClipBindingConstant.pptrCurveMapping",
        "curve": None,
        "path": "",
        "attribute": "m_Sprite",
        "frames": frames,
    }]


def _animation_source_candidates(clip):
    explicit = _animation_curve_candidates(clip)
    if explicit:
        return explicit
    return _binding_mapping_candidates(clip)


def _trim_frames(frames, min_frames, max_frames):
    frames = list(frames)
    if len(frames) < min_frames:
        raise ValueError(f"frame count {len(frames)} below diagnostic minimum {min_frames}")
    if len(frames) > max_frames:
        # M1 is a hardware-path test, not a full clip export. Keep a deterministic
        # prefix instead of rejecting otherwise valid Cuphead animations.
        frames = frames[:max_frames]
    return frames


def _compile_animation_frames(
    rel_container: str,
    clip_obj,
    clip,
    source,
    min_frames: int,
    max_frames: int,
    max_frame_dimension: int,
    max_total_texture_bytes: int,
):
    frames = _trim_frames(source["frames"], min_frames, max_frames)

    sample_rate = float(getattr(clip, "m_SampleRate", 0.0) or 0.0)
    if sample_rate <= 0.0 or sample_rate > 240.0:
        sample_rate = 24.0

    clip_name = _obj_name(clip) or f"clip_{getattr(clip_obj, 'path_id', 0)}"
    clip_safe = _safe_name(clip_name)
    curve_path = source.get("path", "")
    curve_attribute = source.get("attribute", "")

    texture_specs = []
    texture_by_ptr = {}
    frame_records = []
    total_texture_bytes = 0
    non_blank = 0

    for start_time, ptr in frames:
        fid, pid = _ptr_ids(ptr)
        start_ms = max(0, int(round(float(start_time) * 1000.0)))

        if pid == 0:
            frame_records.append((start_ms, 0))
            continue

        ptr_key = (fid, pid)
        tex_spec = texture_by_ptr.get(ptr_key)
        if tex_spec is None:
            sprite = _sprite_from_ptr(ptr)
            image = sprite.image.convert("RGBA")
            w, h = image.size
            if w <= 0 or h <= 0 or w > max_frame_dimension or h > max_frame_dimension:
                raise ValueError(f"sprite frame {w}x{h} exceeds diagnostic limit")

            sprite_name = _obj_name(sprite) or f"sprite_{pid}"
            tex_name = (
                f"diagnostic/animation/{clip_safe}/frame_texture/"
                f"{fid}_{pid}_{_safe_name(sprite_name)}"
            )
            tex_spec = compile_image_as_texture(
                image,
                tex_name,
                {
                    "source_type": "Sprite",
                    "source_container": rel_container.replace("\\", "/"),
                    "source_file_id": fid,
                    "source_path_id": pid,
                    "source_name": sprite_name,
                    "animation_clip": clip_name,
                    "animation_curve_path": curve_path,
                },
            )
            total_texture_bytes += len(tex_spec.data or b"")
            if total_texture_bytes > max_total_texture_bytes:
                raise ValueError("animation frame textures exceed diagnostic memory budget")
            texture_by_ptr[ptr_key] = tex_spec
            texture_specs.append(tex_spec)

        non_blank += 1
        frame_records.append((start_ms, asset_id(tex_spec.name)))

    if non_blank < min_frames:
        raise ValueError("animation does not contain enough non-blank sprite frames")
    if len(texture_specs) < 2:
        raise ValueError("animation does not contain enough unique sprite images")

    payload, meta = build_animation_payload(frame_records, sample_rate, loop=True)
    meta.update({
        "source_type": "AnimationClip",
        "source_container": rel_container.replace("\\", "/"),
        "source_path_id": int(getattr(clip_obj, "path_id", 0) or 0),
        "source_name": clip_name,
        "source_curve_path": curve_path,
        "source_curve_attribute": curve_attribute,
        "source_frame_table": source.get("source", "unknown"),
        "unique_frame_textures": len(texture_specs),
        "total_frame_texture_bytes": total_texture_bytes,
    })

    dependencies = []
    seen = set()
    for _ms, aid in frame_records:
        if aid and aid not in seen:
            dependencies.append(aid)
            seen.add(aid)

    anim_spec = AssetSpec(
        name=f"diagnostic/animation/{clip_safe}/clip",
        source=None,
        data=payload,
        kind="animation",
        group="diagnostic",
        type=TYPE_ANIMATION,
        dependencies=dependencies,
        metadata=meta,
    )
    return texture_specs + [anim_spec], anim_spec


def _sprite_sequence_key(name: str):
    """Return (base, frame_number) for Cuphead-style numbered sprite names."""
    if not name:
        return None
    stem = name.rsplit(".", 1)[0]
    m = re.match(r"^(.*?)(?:[_\-.]?)(\d{2,})$", stem)
    if not m:
        return None
    base = m.group(1).rstrip("_-. ").lower()
    if not base:
        return None
    return base, int(m.group(2))


def _compile_sprite_sequence_fallback(
    rel_container: str,
    objects,
    min_frames: int,
    max_frames: int,
    max_frame_dimension: int,
    max_total_texture_bytes: int,
):
    """Compile a real retail Sprite sequence when AnimationClip PPtr data is hidden.

    Cuphead's decompiled AnimationChartParser explicitly derives frame numbers
    from numbered sprite/image names. This fallback uses that same observable
    convention only for the M1 playback test. It is marked in metadata and does
    not pretend to be the final Animator reconstruction path.
    """
    groups = {}
    for obj in objects:
        if _type_name(obj) != "Sprite":
            continue
        try:
            sprite = _read_object(obj)
            name = _obj_name(sprite)
            key = _sprite_sequence_key(name)
            if key is None:
                continue
            base, frame_no = key
            groups.setdefault(base, []).append((frame_no, obj, sprite, name))
        except Exception:
            continue

    candidates = []
    for base, items in groups.items():
        by_num = {}
        for item in items:
            by_num.setdefault(item[0], item)
        ordered = [by_num[k] for k in sorted(by_num)]
        if len(ordered) >= min_frames:
            candidates.append((base, ordered))

    # Prefer larger sequences, then deterministic lexical order.
    candidates.sort(key=lambda x: (-len(x[1]), x[0]))

    for base, ordered in candidates:
        ordered = ordered[:max_frames]
        texture_specs = []
        frame_records = []
        total_texture_bytes = 0
        sample_rate = 24.0
        seq_safe = _safe_name(base)
        try:
            for i, (frame_no, obj, sprite, sprite_name) in enumerate(ordered):
                image = sprite.image.convert("RGBA")
                w, h = image.size
                if w <= 0 or h <= 0 or w > max_frame_dimension or h > max_frame_dimension:
                    raise ValueError(f"sprite frame {w}x{h} exceeds diagnostic limit")

                pid = int(getattr(obj, "path_id", 0) or 0)
                tex_name = (
                    f"diagnostic/animation/{seq_safe}/frame_texture/"
                    f"{pid}_{_safe_name(sprite_name)}"
                )
                tex_spec = compile_image_as_texture(
                    image,
                    tex_name,
                    {
                        "source_type": "Sprite",
                        "source_container": rel_container.replace("\\", "/"),
                        "source_path_id": pid,
                        "source_name": sprite_name,
                        "sequence_base": base,
                        "sequence_frame_number": frame_no,
                    },
                )
                total_texture_bytes += len(tex_spec.data or b"")
                if total_texture_bytes > max_total_texture_bytes:
                    raise ValueError("animation frame textures exceed diagnostic memory budget")
                texture_specs.append(tex_spec)
                frame_records.append((int(round(i * 1000.0 / sample_rate)), asset_id(tex_spec.name)))

            if len(texture_specs) < min_frames:
                continue

            payload, meta = build_animation_payload(frame_records, sample_rate, loop=True)
            meta.update({
                "source_type": "SpriteSequenceFallback",
                "source_container": rel_container.replace("\\", "/"),
                "source_name": base,
                "source_frame_table": "numbered-sprite-sequence",
                "unique_frame_textures": len(texture_specs),
                "total_frame_texture_bytes": total_texture_bytes,
            })
            deps = [asset_id(x.name) for x in texture_specs]
            anim_spec = AssetSpec(
                name=f"diagnostic/animation/{seq_safe}/clip",
                source=None,
                data=payload,
                kind="animation",
                group="diagnostic",
                type=TYPE_ANIMATION,
                dependencies=deps,
                metadata=meta,
            )
            return texture_specs + [anim_spec], anim_spec
        except Exception:
            continue

    raise ValueError("no numbered Sprite sequence could be compiled")


def discover_animation_specs(
    root: Path,
    rows,
    min_frames: int = DEFAULT_MIN_FRAMES,
    max_frames: int = DEFAULT_MAX_FRAMES,
    max_frame_dimension: int = DEFAULT_MAX_FRAME_DIMENSION,
    max_total_texture_bytes: int = DEFAULT_MAX_TOTAL_TEXTURE_BYTES,
):
    """Find one real Cuphead animation source and compile it for M1.

    Order of preference:
      1. AnimationClip.m_PPtrCurves (authoring representation),
      2. AnimationClip ClipBindingConstant.pptrCurveMapping (optimized retail),
      3. numbered real Sprite sequence fallback, matching Cuphead's decompiled
         AnimationChartParser frame-number convention for the diagnostic only.
    """
    root = Path(root)
    notes = []
    clip_count = 0
    pptr_curve_clips = 0
    binding_map_clips = 0
    sprite_count = 0
    containers_scanned = 0

    # Save candidate environments so a second pass can try real Sprite sequences
    # without re-opening every Unity file.
    loaded = []

    for row in _candidate_unity_rows(rows):
        rel = row["path"]
        path = root / rel
        try:
            env = _load_env(path, dependency_mode=True)
            containers_scanned += 1
            loaded.append((rel, env))
        except Exception as e:
            if len(notes) < 60:
                notes.append(f"animation skip {rel}: UnityPy.load: {e}")
            continue

        for obj in env.objects:
            tn = _type_name(obj)
            if tn == "Sprite":
                sprite_count += 1
                continue
            if tn != "AnimationClip":
                continue

            clip_count += 1
            try:
                clip = _read_object(obj)
                explicit = _animation_curve_candidates(clip)
                binding = _binding_mapping_candidates(clip) if not explicit else []
                if explicit:
                    pptr_curve_clips += 1
                    candidates = explicit
                elif binding:
                    binding_map_clips += 1
                    candidates = binding
                else:
                    continue

                for source in candidates:
                    try:
                        specs, anim_spec = _compile_animation_frames(
                            rel, obj, clip, source,
                            min_frames, max_frames,
                            max_frame_dimension, max_total_texture_bytes,
                        )
                        notes.append(
                            f"animation selected {anim_spec.metadata.get('source_name','')} "
                            f"via {anim_spec.metadata.get('source_frame_table','unknown')}")
                        return specs, anim_spec, notes
                    except Exception as e:
                        if len(notes) < 60:
                            notes.append(
                                f"animation skip {rel} PathID {getattr(obj,'path_id','?')}: {e}")
            except Exception as e:
                if len(notes) < 60:
                    notes.append(
                        f"animation clip parse skip {rel} PathID {getattr(obj,'path_id','?')}: {e}")

    # Some Cuphead retail builds expose Sprite assets but UnityPy does not expose
    # a usable PPtr curve on AnimationClip. For the M1 hardware path, compile one
    # real numbered Sprite sequence. This is explicitly tagged as a fallback.
    for rel, env in loaded:
        try:
            specs, anim_spec = _compile_sprite_sequence_fallback(
                rel, env.objects,
                min_frames, max_frames,
                max_frame_dimension, max_total_texture_bytes,
            )
            notes.append(
                f"animation fallback selected real Sprite sequence "
                f"{anim_spec.metadata.get('source_name','')} from {rel}")
            return specs, anim_spec, notes
        except Exception:
            pass

    summary = (
        f"Scanned {containers_scanned} Unity containers; found {clip_count} AnimationClip, "
        f"{pptr_curve_clips} with m_PPtrCurves, {binding_map_clips} with usable "
        f"ClipBindingConstant mapping, and {sprite_count} Sprite objects."
    )
    notes.append(summary)
    detail = "\n".join(notes[-12:])
    raise RuntimeError(
        "M1 media build could not compile a real animation test.\n" + detail)

