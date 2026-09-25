from __future__ import annotations

import struct
from collections import defaultdict

from ..pack.format import TYPE_SCENE
from ..pack.volumes import AssetSpec

SCENE_MAGIC = b"CUPN"
SCENE_VERSION = 2

# Header:
# magic, version, header_size, flags,
# node_count, camera_count, string_bytes,
# node_record_size, camera_record_size,
# scene_name_offset, reserved
SCENE_HEADER = struct.Struct("<4sHHIIIIIIIIIIII")

# CUPN v2 adds explicit Canvas and CanvasGroup sections. The node record stays
# compact; UI Images reuse the existing Sprite/renderer fields and are marked
# with NODE_HAS_UI_IMAGE / RENDERER_UI_IMAGE. Animator controller references are
# still retained, while the formerly-reserved DWORD carries the resolved native
# CUPA animation asset ID when a default/idle clip can be identified.
SCENE_CANVAS = struct.Struct("<qIIII4f")
SCENE_CANVAS_GROUP = struct.Struct("<qIfIII")

# One record per GameObject. Components used by the first native scene pass are
# flattened into the node so the Xbox runtime can walk/render without recreating
# Unity's Component/PPtr object model.
#
# path_id, parent_gameobject_path_id,
# name_offset, flags, layer, reserved,
# local position xyz, local rotation xyzw, local scale xyz,
# RectTransform anchorMin xy, anchorMax xy, anchoredPosition xy,
# sizeDelta xy, pivot xy,
# sprite asset id, renderer flags, renderer color rgba,
# sorting layer id, sorting order,
# Animator controller file id/path id, Animator flags
SCENE_NODE = struct.Struct("<qqIIII3f4f3f10fII4fiiIqI")

# Camera GameObject path id, flags, orthographic size, FOV, depth, aspect,
# normalized viewport xywh, culling mask.
SCENE_CAMERA = struct.Struct("<qIffff4fI")

SCENE_FLAG_HAS_CAMERA = 0x00000001

NODE_ACTIVE = 0x00000001
NODE_HAS_TRANSFORM = 0x00000002
NODE_RECT_TRANSFORM = 0x00000004
NODE_HAS_SPRITE_RENDERER = 0x00000008
NODE_HAS_ANIMATOR = 0x00000010
NODE_HAS_SPRITE = 0x00000020
NODE_HAS_UI_IMAGE = 0x00000040
NODE_HAS_RUNTIME_ANIMATION = 0x00000080
NODE_HAS_CANVAS = 0x00000100

RENDERER_ENABLED = 0x00000001
RENDERER_FLIP_X = 0x00000002
RENDERER_FLIP_Y = 0x00000004
RENDERER_UI_IMAGE = 0x00000008
RENDERER_UI_TEXT = 0x00000010
RENDERER_MENU_ITEM = 0x00000020

ANIMATOR_ENABLED = 0x00000001

CAMERA_ENABLED = 0x00000001
CAMERA_ORTHOGRAPHIC = 0x00000002

CANVAS_ENABLED = 0x00000001
CANVAS_OVERRIDE_SORTING = 0x00000002

CANVAS_SCALE_CONSTANT_PIXEL = 0
CANVAS_SCALE_WITH_SCREEN = 1
CANVAS_SCALE_CONSTANT_PHYSICAL = 2

# UnityEngine.UI script local identifiers used by the retail Cuphead project.
# These are verified against the decompiled scene_slot_select.unity rather than
# inferred from object names.  They are only identity fallbacks for cases where
# UnityPy cannot dereference the external MonoScript; field validation still
# guards each component type.
UNITY_UI_TEXT_SCRIPT_PATH_ID = 708705254
UNITY_UI_IMAGE_SCRIPT_PATH_ID = -765806418
UNITY_UI_VERTICAL_LAYOUT_SCRIPT_PATH_ID = 1297475563
UNITY_UI_LAYOUT_ELEMENT_SCRIPT_PATH_ID = 1679637790

# Verified retail scene_slot_select schema fallback.
#
# Cuphead's retail level2 contains legacy UnityEngine.UI components whose
# managed typetrees are not embedded in the player data.  UnityPy therefore
# sees the objects, but on this title it can fail after the 32-byte
# MonoBehaviour base header before exposing Text/LayoutGroup fields.  These
# identifiers and scalar values are taken from the decompiled retail
# scene_slot_select.unity and are used only after the live SerializedFile is
# checked to have the same GameObject/component/transform structure.
# Positions still come from the retail RectTransforms and are laid out by the
# normal offline layout pass; this table is not a hand-authored screen layout.
_SLOT_SELECT_ITEMS_GO = 25
_SLOT_SELECT_ITEMS_TRANSFORM = 472
_SLOT_SELECT_LAYOUT_COMPONENT = 738
_SLOT_SELECT_ITEMS_CHILD_TRANSFORMS = (520, 563, 511, 602, 564)
_SLOT_SELECT_UI_TEXT_SCHEMA = (
    # GameObject, Text MonoBehaviour, authored identity, initial alpha
    (76, 804, "START", 1.0),
    (119, 858, "ACHIEVEMENTS", 0.3921569),
    (67, 788, "OPTIONS", 0.3921569),
    (158, 904, "DLC", 0.3921569),
    (120, 861, "EXIT", 0.3921569),
)
_SLOT_SELECT_TEXT_FONT_NAME = "CupheadVogue-ExtraBold"
_SLOT_SELECT_TEXT_FONT_SIZE = 32
_SLOT_SELECT_TEXT_ALIGNMENT = 4



def _read_object(obj):
    # UnityPy can materialize older Unity scene components differently by
    # type/version.  Native Unity classes are best kept as generated objects
    # because their PPtrs retain SerializedFile/external-table context.
    errors = []
    for method in ("parse_as_object", "read", "parse_as_dict", "read_typetree"):
        fn = getattr(obj, method, None)
        if fn:
            try:
                value = fn()
                if value is not None:
                    return value
            except Exception as e:
                errors.append(f"{method}: {e}")
    raise ValueError("Unable to parse Unity scene object" +
                     ((" (" + "; ".join(errors) + ")") if errors else ""))


def _read_monobehaviour_tree(obj):
    """Read the complete serialized MonoBehaviour payload.

    This is intentionally separate from _read_object().  UnityPy's normal
    read()/parse_as_object() path can successfully return the generated
    MonoBehaviour base class while exposing only m_GameObject/m_Script/m_Name.
    For UnityEngine.UI.Text, Image, LayoutGroup, and Cuphead behaviours, the
    authored fields live in the MonoBehaviour typetree and therefore must be
    requested explicitly.

    UnityPy 1.x calls this read_typetree(); newer releases call it
    parse_as_dict().  Prefer the dictionary form, then fall back to the base
    object only when the asset genuinely has no available typetree.
    """
    errors = []
    for method in ("parse_as_dict", "read_typetree"):
        fn = getattr(obj, method, None)
        if fn:
            try:
                value = fn()
                if value is not None:
                    return value
            except Exception as e:
                errors.append(f"{method}: {e}")
    if errors:
        raise ValueError("MonoBehaviour typetree unavailable (" + "; ".join(errors) + ")")
    raise ValueError("MonoBehaviour typetree API unavailable")


def _field(data, name, default=None):
    if data is None:
        return default
    if isinstance(data, dict):
        return data.get(name, default)
    return getattr(data, name, default)


def _field_any(data, names, default=None):
    """Return the first available serialized/generated field.

    UnityPy's generated Camera class intentionally exposes a few native fields
    without the historical m_ prefix (orthographic, orthographic_size,
    field_of_view). Older typetrees can still expose the m_ names. Keep the
    compiler compatible with both forms instead of silently using defaults.
    """
    for name in names:
        marker = object()
        value = _field(data, name, marker)
        if value is not marker:
            return value
    return default


def _mask32(value, default=0xFFFFFFFF):
    if value is None:
        return int(default) & 0xFFFFFFFF
    if isinstance(value, dict):
        value = value.get("m_Bits", value.get("bits", default))
    else:
        value = getattr(value, "m_Bits", getattr(value, "bits", value))
    try:
        return int(value) & 0xFFFFFFFF
    except Exception:
        return int(default) & 0xFFFFFFFF


def _type_name(obj):
    t = getattr(obj, "type", None)
    return getattr(t, "name", str(t or "Unknown"))


def _pid(obj):
    try:
        return int(getattr(obj, "path_id", 0) or 0)
    except Exception:
        return 0


def _ptr_ids(ptr):
    if ptr is None:
        return 0, 0
    try:
        if isinstance(ptr, dict):
            fid = int(ptr.get("m_FileID", ptr.get("file_id", 0)) or 0)
            pid = int(ptr.get("m_PathID", ptr.get("path_id", 0)) or 0)
        else:
            fid = int(getattr(ptr, "m_FileID", getattr(ptr, "file_id", 0)) or 0)
            pid = int(getattr(ptr, "m_PathID", getattr(ptr, "path_id", 0)) or 0)
        return fid, pid
    except Exception:
        return 0, 0


def _v2(value, default=(0.0, 0.0)):
    if value is None:
        return tuple(default)
    if isinstance(value, dict):
        return float(value.get("x", default[0])), float(value.get("y", default[1]))
    try:
        return float(value.x), float(value.y)
    except Exception:
        pass
    try:
        return float(value[0]), float(value[1])
    except Exception:
        return tuple(default)


def _v3(value, default=(0.0, 0.0, 0.0)):
    if value is None:
        return tuple(default)
    if isinstance(value, dict):
        return tuple(float(value.get(k, default[i])) for i, k in enumerate(("x", "y", "z")))
    try:
        return float(value.x), float(value.y), float(value.z)
    except Exception:
        pass
    try:
        return float(value[0]), float(value[1]), float(value[2])
    except Exception:
        return tuple(default)


def _v4(value, default=(0.0, 0.0, 0.0, 1.0)):
    if value is None:
        return tuple(default)
    if isinstance(value, dict):
        return tuple(float(value.get(k, default[i])) for i, k in enumerate(("x", "y", "z", "w")))
    try:
        return float(value.x), float(value.y), float(value.z), float(value.w)
    except Exception:
        pass
    try:
        return tuple(float(value[i]) for i in range(4))
    except Exception:
        return tuple(default)


def _color(value):
    if value is None:
        return 1.0, 1.0, 1.0, 1.0
    if isinstance(value, dict):
        out = tuple(float(value.get(k, 1.0)) for k in ("r", "g", "b", "a"))
    else:
        try:
            out = (float(value.r), float(value.g), float(value.b), float(value.a))
        except Exception:
            try:
                out = tuple(float(value[i]) for i in range(4))
            except Exception:
                return 1.0, 1.0, 1.0, 1.0
    # SpriteRenderer uses ColorRGBA in older Unity versions, which UnityPy can
    # expose as 0..255 bytes. Native runtime colors are normalized floats.
    if max(abs(x) for x in out) > 1.5:
        out = tuple(x / 255.0 for x in out)
    return out


def _rect4(value):
    if value is None:
        return 0.0, 0.0, 1.0, 1.0
    if isinstance(value, dict):
        return (
            float(value.get("x", 0.0)), float(value.get("y", 0.0)),
            float(value.get("width", value.get("w", 1.0))),
            float(value.get("height", value.get("h", 1.0))),
        )
    try:
        return float(value.x), float(value.y), float(value.width), float(value.height)
    except Exception:
        return 0.0, 0.0, 1.0, 1.0


def _name(data, fallback):
    return str(_field(data, "m_Name", "") or _field(data, "name", "") or fallback)


class _Strings:
    def __init__(self):
        self._data = bytearray(b"\0")
        self._off = {"": 0}

    def add(self, text):
        text = str(text or "")
        if text in self._off:
            return self._off[text]
        encoded = text.encode("utf-8", "replace") + b"\0"
        off = len(self._data)
        self._data.extend(encoded)
        self._off[text] = off
        return off

    def bytes(self):
        return bytes(self._data)


def _read_ptr(ptr):
    if ptr is None:
        return None
    # UnityPy PPtr normally resolves through read(); dependency-loaded scene
    # environments make this work for MonoScript / AnimatorController objects.
    for name in ("read", "parse_as_object"):
        fn = getattr(ptr, name, None)
        if fn:
            try:
                value = fn()
                if value is not None:
                    return value
            except Exception:
                pass
    return None


def _script_identity(ptr):
    d = _read_ptr(ptr)
    if d is None:
        return "", "", ""
    return (
        _name(d, ""),
        str(_field(d, "m_Namespace", "") or _field(d, "namespace", "") or ""),
        str(_field(d, "m_AssemblyName", "") or _field(d, "assembly_name", "") or ""),
    )


def _ptr_name(ptr):
    d = _read_ptr(ptr)
    return _name(d, "") if d is not None else ""


def _looks_like_ui_image(d, script_name, script_path_id=0):
    sn = (script_name or "").lower()
    if (sn == "image" or sn.endswith(".image") or
            int(script_path_id or 0) == UNITY_UI_IMAGE_SCRIPT_PATH_ID):
        return True
    # Typetree fallback when the external MonoScript cannot be resolved. Keep
    # this deliberately narrow so arbitrary Cuphead behaviours are not mistaken
    # for UI graphics.
    has_sprite = _field(d, "m_Sprite", None) is not None
    has_type = _field(d, "m_Type", None) is not None
    has_fill = (_field(d, "m_FillMethod", None) is not None or
                _field(d, "m_FillAmount", None) is not None)
    return bool(has_sprite and has_type and has_fill)


def _looks_like_canvas_scaler(d, script_name):
    sn = (script_name or "").lower()
    return sn == "canvasscaler" or (
        _field(d, "m_UiScaleMode", None) is not None and
        _field(d, "m_ReferenceResolution", None) is not None
    )


def _looks_like_ui_text(d, script_name, script_path_id=0):
    sn = (script_name or "").lower()
    if (sn == "text" or sn.endswith(".text") or
            int(script_path_id or 0) == UNITY_UI_TEXT_SCRIPT_PATH_ID):
        return True

    # Legacy UnityEngine.UI.Text serializes its font settings inside the nested
    # m_FontData structure (m_Font, m_FontSize, m_FontStyle, m_Alignment, ...).
    # Some UnityPy versions expose equivalent fields directly, so accept both
    # layouts.  TMP uses a different serialized field set/casing and is not
    # intentionally matched here.
    fd = _field(d, "m_FontData", None)
    nested_font_size = _field(fd, "m_FontSize", None) if fd is not None else None
    top_font_size = _field(d, "m_FontSize", None)
    return (
        _field(d, "m_Text", None) is not None and
        (nested_font_size is not None or top_font_size is not None) and
        _field(d, "m_Color", None) is not None
    )


def _looks_like_vertical_layout_group(d, script_name, script_path_id=0):
    sn = (script_name or "").lower()
    if (sn == "verticallayoutgroup" or sn.endswith(".verticallayoutgroup") or
            int(script_path_id or 0) == UNITY_UI_VERTICAL_LAYOUT_SCRIPT_PATH_ID):
        return True
    # Exact structural fallback from UnityEngine.UI.VerticalLayoutGroup.  This
    # remains narrow enough not to confuse Cuphead-specific behaviours.
    return (
        _field(d, "m_Padding", None) is not None and
        _field(d, "m_ChildAlignment", None) is not None and
        _field(d, "m_Spacing", None) is not None and
        _field(d, "m_ChildControlHeight", None) is not None
    )


def _looks_like_layout_element(d, script_name, script_path_id=0):
    sn = (script_name or "").lower()
    if (sn == "layoutelement" or sn.endswith(".layoutelement") or
            int(script_path_id or 0) == UNITY_UI_LAYOUT_ELEMENT_SCRIPT_PATH_ID):
        return True
    return (
        _field(d, "m_IgnoreLayout", None) is not None and
        (_field(d, "m_PreferredWidth", None) is not None or
         _field(d, "m_PreferredHeight", None) is not None)
    )


def _blob_bytes(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    try:
        return bytes(value)
    except Exception:
        return b""


def _font_info(ptr):
    d = _read_ptr(ptr)
    if d is None:
        return {"name": "", "data": b"", "path_id": _ptr_ids(ptr)[1]}
    raw = _field(d, "m_FontData", None)
    if raw is None:
        raw = _field(d, "font_data", None)
    return {
        "name": _name(d, ""),
        "data": _blob_bytes(raw),
        "path_id": _ptr_ids(ptr)[1],
    }


def _padding4(value):
    if value is None:
        return 0, 0, 0, 0
    def g(*names):
        try:
            return int(_field_any(value, names, 0) or 0)
        except Exception:
            return 0
    return (g("m_Left", "left"), g("m_Right", "right"),
            g("m_Top", "top"), g("m_Bottom", "bottom"))


# Controller -> initial/default clip bridges needed by the currently compiled
# retail route.  The old scene compiler only handled controller names beginning
# with ``animator_``; level40's real controllers are named "Elder Kettle",
# "elder_kettle_music_notes_one_0001" and "ek_bg_fireplace_glow", so CUPN v2
# emitted eight Animator components but zero runtime animations.
#
# Keep this mapping data-driven and exact.  It does not invent animation frames:
# every target is an already-converted retail AnimationClip/CUPA.
_CONTROLLER_DEFAULT_CLIPS = {
    "elder kettle": "anim_level_house_elder_kettle_idle",
    "elder_kettle_music_notes_one_0001": "anim_elder_kettle_music_notes_1",
    "ek_bg_fireplace_glow": "anim_level_house_elder_kettle_fire",
    # The house uses the stock player-only-running controller for its authored
    # run-out helpers.  Profile 12 synthesizes anim_player_run from the retail
    # Cuphead run Sprite sequence before CUPN pass 4.
    "animator_player_onlyrunning": "anim_player_run",
}

def _controller_idle_name(controller_name):
    name = (controller_name or "").strip()
    if not name:
        return ""
    low = name.lower()
    mapped = _CONTROLLER_DEFAULT_CLIPS.get(low)
    if mapped:
        return mapped
    if low.startswith("animator_"):
        return "anim_" + name[len("animator_"):] + "_idle"
    return ""


def _object_assets_name(obj):
    try:
        return str(getattr(getattr(obj, "assets_file", None), "name", "") or "")
    except Exception:
        return ""


def _obj_map(env, primary_rel=""):
    """Return objects owned by the scene SerializedFile only.

    CUPN compilation loads dependencies so MonoScript / AnimatorController PPtrs
    can be resolved, but env.objects then contains objects from those dependency
    files as well. Treating that combined environment as the scene graph caused
    scene_title to balloon from the retail level1 graph (34 GameObjects / one
    Camera) to hundreds of dependency objects and multiple cameras.

    UnityPy gives each object an assets_file owner. When that ownership data is
    available, retain only objects whose owner basename matches the requested
    level file. Synthetic smoke-test environments do not always expose owner
    names, so fall back to the old all-object behavior only when no primary-owned
    objects can be identified at all.
    """
    out = defaultdict(dict)
    target = str(primary_rel or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    rows = []
    have_primary = False
    for obj in env.objects:
        owner = _object_assets_name(obj).replace("\\", "/").rsplit("/", 1)[-1].lower()
        rows.append((obj, owner))
        if target and owner == target:
            have_primary = True

    for obj, owner in rows:
        if target and have_primary and owner != target:
            continue
        typ = _type_name(obj)
        pid = _pid(obj)
        if pid:
            out[typ][pid] = obj
    return out


def compile_scene_environment(env, rel: str, scene_name: str, resolve_sprite,
                              resolve_animation=None, resolve_ui_text=None):
    """Compile one Unity level SerializedFile into CUPN v2.

    v2 keeps the proven world SpriteRenderer path and adds the static scene
    assembly pieces required by Cuphead's frontend: Canvas / RectTransform,
    CanvasGroup alpha, UnityEngine.UI.Image Sprite PPtrs, hierarchy draw order,
    offline-baked UnityEngine.UI.Text, static VerticalLayoutGroup positioning,
    and a resolved idle/default CUPA animation when an AnimatorController name
    maps cleanly to an already-compiled AnimationClip.
    """
    objects = _obj_map(env, rel)
    strings = _Strings()
    scene_name_off = strings.add(scene_name)

    gameobjects = {}
    transforms = {}
    transform_to_go = {}
    transform_children = defaultdict(list)
    renderers = {}
    animators = {}
    canvases = {}
    canvas_groups = {}
    canvas_scalers = {}
    ui_texts = {}
    vertical_layouts = {}
    layout_elements = {}
    cameras = []
    warnings = []
    dependencies = set()
    mono_scripts = defaultdict(int)
    ui_image_count = 0
    ui_text_count = 0
    ui_text_baked_count = 0
    ui_text_skipped_count = 0
    vertical_layout_baked_count = 0
    canvas_renderer_count = len(objects.get("CanvasRenderer", {}))

    for pid, obj in objects.get("GameObject", {}).items():
        try:
            d = _read_object(obj)
            gameobjects[pid] = {
                "name": _name(d, f"GameObject_{pid}"),
                "active": bool(_field(d, "m_IsActive", 1)),
                "layer": int(_field(d, "m_Layer", 0) or 0),
            }
        except Exception as e:
            warnings.append(f"GameObject {pid}: {e}")

    for typ in ("Transform", "RectTransform"):
        for pid, obj in objects.get(typ, {}).items():
            try:
                d = _read_object(obj)
                _gf, go_pid = _ptr_ids(_field(d, "m_GameObject", None))
                if not go_pid:
                    warnings.append(f"{typ} {pid}: missing GameObject")
                    continue
                _pf, parent_transform_pid = _ptr_ids(_field(d, "m_Father", None))
                rec = {
                    "transform_pid": pid,
                    "go_pid": go_pid,
                    "parent_transform_pid": parent_transform_pid,
                    "rect": typ == "RectTransform",
                    "pos": _v3(_field(d, "m_LocalPosition", None)),
                    "rot": _v4(_field(d, "m_LocalRotation", None)),
                    "scale": _v3(_field(d, "m_LocalScale", None), (1.0, 1.0, 1.0)),
                    "anchor_min": _v2(_field(d, "m_AnchorMin", None)),
                    "anchor_max": _v2(_field(d, "m_AnchorMax", None), (1.0, 1.0)),
                    "anchored_pos": _v2(_field(d, "m_AnchoredPosition", None)),
                    "size_delta": _v2(_field(d, "m_SizeDelta", None)),
                    "pivot": _v2(_field(d, "m_Pivot", None), (0.5, 0.5)),
                }
                transforms[go_pid] = rec
                transform_to_go[pid] = go_pid
                if parent_transform_pid:
                    transform_children[parent_transform_pid].append(pid)
            except Exception as e:
                warnings.append(f"{typ} {pid}: {e}")

    # Rebuild exact sibling order from m_Children when available. This matters
    # for CanvasRenderer/Image ordering, where hierarchy order is draw order.
    for typ in ("Transform", "RectTransform"):
        for pid, obj in objects.get(typ, {}).items():
            try:
                d = _read_object(obj)
                kids = []
                for ptr in (_field(d, "m_Children", None) or []):
                    _f, kpid = _ptr_ids(ptr)
                    if kpid:
                        kids.append(kpid)
                if kids:
                    transform_children[pid] = kids
            except Exception:
                pass

    for pid, obj in objects.get("SpriteRenderer", {}).items():
        try:
            d = _read_object(obj)
            _gf, go_pid = _ptr_ids(_field(d, "m_GameObject", None))
            if not go_pid:
                warnings.append(f"SpriteRenderer {pid}: missing GameObject")
                continue
            sprite_ptr = _field(d, "m_Sprite", None)
            sid = resolve_sprite(sprite_ptr)
            renderer_flags = 0
            if bool(_field(d, "m_Enabled", 1)):
                renderer_flags |= RENDERER_ENABLED
            if bool(_field(d, "m_FlipX", False)):
                renderer_flags |= RENDERER_FLIP_X
            if bool(_field(d, "m_FlipY", False)):
                renderer_flags |= RENDERER_FLIP_Y
            renderers[go_pid] = {
                "kind": "sprite",
                "sprite_asset_id": int(sid or 0),
                "flags": renderer_flags,
                "color": _color(_field(d, "m_Color", None)),
                "sorting_layer_id": int(_field(d, "m_SortingLayerID", 0) or 0),
                "sorting_order": int(_field(d, "m_SortingOrder", 0) or 0),
            }
            if sid:
                dependencies.add(int(sid))
            else:
                _sf, spid = _ptr_ids(sprite_ptr)
                if spid:
                    warnings.append(f"SpriteRenderer {pid}: unresolved Sprite PathID {spid}")
        except Exception as e:
            warnings.append(f"SpriteRenderer {pid}: {e}")

    for pid, obj in objects.get("Canvas", {}).items():
        try:
            d = _read_object(obj)
            _gf, go_pid = _ptr_ids(_field(d, "m_GameObject", None))
            if not go_pid:
                continue
            flags = CANVAS_ENABLED if bool(_field(d, "m_Enabled", 1)) else 0
            if bool(_field(d, "m_OverrideSorting", False)):
                flags |= CANVAS_OVERRIDE_SORTING
            canvases[go_pid] = {
                "flags": flags,
                "render_mode": int(_field(d, "m_RenderMode", 0) or 0),
                "sorting_order": int(_field(d, "m_SortingOrder", 0) or 0),
                "scale_mode": CANVAS_SCALE_CONSTANT_PIXEL,
                "ref_w": 0.0, "ref_h": 0.0, "match": 0.0,
                "ref_ppu": float(_field(d, "m_ReferencePixelsPerUnit", 100.0) or 100.0),
            }
        except Exception as e:
            warnings.append(f"Canvas {pid}: {e}")

    for pid, obj in objects.get("CanvasGroup", {}).items():
        try:
            d = _read_object(obj)
            _gf, go_pid = _ptr_ids(_field(d, "m_GameObject", None))
            if not go_pid:
                continue
            canvas_groups[go_pid] = {
                "alpha": float(_field(d, "m_Alpha", 1.0) or 0.0),
                "interactable": 1 if bool(_field(d, "m_Interactable", True)) else 0,
                "blocks_raycasts": 1 if bool(_field(d, "m_BlocksRaycasts", True)) else 0,
                "ignore_parent": 1 if bool(_field(d, "m_IgnoreParentGroups", False)) else 0,
            }
        except Exception as e:
            warnings.append(f"CanvasGroup {pid}: {e}")

    # Resolve MonoBehaviour scripts. For this pass we only compile the built-in
    # Unity UI pieces that materially affect the frontend scenes; all Cuphead-specific
    # behaviours are reported in metadata instead of guessed at runtime.
    for pid, obj in objects.get("MonoBehaviour", {}).items():
        try:
            # Keep the generated/base object for context-rich PPtrs (especially
            # m_Script and external asset references), but read the typetree for
            # the actual authored managed fields.  The previous implementation
            # stopped at read()/parse_as_object(), which is why every UI.Text in
            # level2 appeared to have no m_Text/m_FontData at all.
            base_d = _read_object(obj)
            try:
                d = _read_monobehaviour_tree(obj)
            except Exception as tree_error:
                d = base_d
                # Only report the typetree failure when the base object does not
                # itself expose managed fields. This avoids warning spam for
                # benign generated-object cases while preserving useful failure
                # data for the frontend compiler.
                if (_field(base_d, "m_Text", None) is None and
                        _field(base_d, "m_Sprite", None) is None and
                        _field(base_d, "m_Padding", None) is None):
                    warnings.append(f"MonoBehaviour {pid}: {tree_error}")

            script_ptr = _field(base_d, "m_Script", None)
            if script_ptr is None:
                script_ptr = _field(d, "m_Script", None)
            script_name, script_ns, script_asm = _script_identity(script_ptr)
            _sfid, script_path_id = _ptr_ids(script_ptr)
            script_label = script_name or (f"<script:{script_path_id}>" if script_path_id else "<unresolved>")
            mono_scripts[script_label] += 1

            go_ptr = _field(base_d, "m_GameObject", None)
            if go_ptr is None:
                go_ptr = _field(d, "m_GameObject", None)
            _gf, go_pid = _ptr_ids(go_ptr)
            if not go_pid:
                continue

            if _looks_like_canvas_scaler(d, script_name):
                ref = _v2(_field(d, "m_ReferenceResolution", None), (0.0, 0.0))
                canvas_scalers[go_pid] = {
                    "scale_mode": int(_field(d, "m_UiScaleMode", CANVAS_SCALE_CONSTANT_PIXEL) or 0),
                    "ref_w": float(ref[0]), "ref_h": float(ref[1]),
                    "match": float(_field(d, "m_MatchWidthOrHeight", 0.0) or 0.0),
                    "ref_ppu": float(_field(d, "m_ReferencePixelsPerUnit", 100.0) or 100.0),
                }
                continue

            if _looks_like_vertical_layout_group(d, script_name, script_path_id):
                vertical_layouts[go_pid] = {
                    "padding": _padding4(_field(d, "m_Padding", None)),
                    "child_alignment": int(_field(d, "m_ChildAlignment", 0) or 0),
                    "spacing": float(_field(d, "m_Spacing", 0.0) or 0.0),
                    "force_expand_w": bool(_field(d, "m_ChildForceExpandWidth", True)),
                    "force_expand_h": bool(_field(d, "m_ChildForceExpandHeight", True)),
                    "control_w": bool(_field(d, "m_ChildControlWidth", True)),
                    "control_h": bool(_field(d, "m_ChildControlHeight", True)),
                }
                continue

            if _looks_like_layout_element(d, script_name, script_path_id):
                layout_elements[go_pid] = {
                    "ignore": bool(_field(d, "m_IgnoreLayout", False)),
                    "min_w": float(_field(d, "m_MinWidth", -1.0) or -1.0),
                    "min_h": float(_field(d, "m_MinHeight", -1.0) or -1.0),
                    "preferred_w": float(_field(d, "m_PreferredWidth", -1.0) or -1.0),
                    "preferred_h": float(_field(d, "m_PreferredHeight", -1.0) or -1.0),
                    "flex_w": float(_field(d, "m_FlexibleWidth", -1.0) or -1.0),
                    "flex_h": float(_field(d, "m_FlexibleHeight", -1.0) or -1.0),
                }
                continue

            if _looks_like_ui_text(d, script_name, script_path_id):
                # UnityEngine.UI.Text keeps these values in FontData in the
                # retail-era Unity serialization.  Retain top-level fallbacks
                # for UnityPy/generated-object variants.
                fd = _field(d, "m_FontData", None)

                def _text_font_field(name, default=None):
                    if fd is not None:
                        value = _field(fd, name, None)
                        if value is not None:
                            return value
                    return _field(d, name, default)

                font_ptr = _text_font_field("m_Font", None)
                # A generated MonoBehaviour object may retain a context-rich
                # external PPtr even when the typetree dictionary is used for
                # scalar Text fields. Prefer that PPtr when available.
                base_fd = _field(base_d, "m_FontData", None)
                base_font_ptr = _field(base_fd, "m_Font", None) if base_fd is not None else None
                if base_font_ptr is not None:
                    font_ptr = base_font_ptr
                finfo = _font_info(font_ptr)
                ui_texts[go_pid] = {
                    "go_pid": int(go_pid),
                    "component_path_id": int(pid),
                    # Keep GameObject identity alongside the display string.
                    # SlotSelectScreen uses authored object names (START,
                    # OPTIONS, ACHIEVEMENTS, DLC, EXIT) as stable menu-entry
                    # identities even when m_Text is empty/localized.
                    "go_name": str(gameobjects.get(go_pid, {}).get("name", "") or ""),
                    "text": str(_field(d, "m_Text", "") or ""),
                    "font_name": finfo["name"],
                    "font_data": finfo["data"],
                    "font_path_id": int(finfo["path_id"] or 0),
                    "font_size": max(1, int(_text_font_field("m_FontSize", 14) or 14)),
                    "font_style": int(_text_font_field("m_FontStyle", 0) or 0),
                    "alignment": int(_text_font_field("m_Alignment", 0) or 0),
                    "line_spacing": float(_text_font_field("m_LineSpacing", 1.0) or 1.0),
                    "horizontal_overflow": int(_text_font_field("m_HorizontalOverflow", 0) or 0),
                    "vertical_overflow": int(_text_font_field("m_VerticalOverflow", 0) or 0),
                    "enabled": bool(_field(d, "m_Enabled", 1)),
                    "color": _color(_field(d, "m_Color", None)),
                    "script_name": script_label,
                    "script_namespace": script_ns,
                    "script_assembly": script_asm,
                }
                ui_text_count += 1
                continue

            if _looks_like_ui_image(d, script_name, script_path_id):
                sprite_ptr = _field(base_d, "m_Sprite", None)
                if sprite_ptr is None:
                    sprite_ptr = _field(d, "m_Sprite", None)
                sid = resolve_sprite(sprite_ptr)
                rflags = RENDERER_UI_IMAGE
                if bool(_field(d, "m_Enabled", 1)):
                    rflags |= RENDERER_ENABLED
                # Do not replace an actual SpriteRenderer if a GameObject happens
                # to carry both components; that uncommon case is reported.
                if go_pid in renderers and renderers[go_pid].get("kind") == "sprite":
                    warnings.append(f"UI Image {pid}: GameObject {go_pid} also has SpriteRenderer; keeping SpriteRenderer")
                    continue
                renderers[go_pid] = {
                    "kind": "ui_image",
                    "sprite_asset_id": int(sid or 0),
                    "flags": rflags,
                    "color": _color(_field(d, "m_Color", None)),
                    "sorting_layer_id": 0,
                    "sorting_order": 0,
                    "preserve_aspect": bool(_field(d, "m_PreserveAspect", False)),
                    "script_name": script_label,
                    "script_namespace": script_ns,
                    "script_assembly": script_asm,
                }
                ui_image_count += 1
                if sid:
                    dependencies.add(int(sid))
                else:
                    _sf, spid = _ptr_ids(sprite_ptr)
                    if spid:
                        warnings.append(f"UI Image {pid}: unresolved Sprite PathID {spid}")
        except Exception as e:
            warnings.append(f"MonoBehaviour {pid}: {e}")

    # Retail scene_slot_select fallback for legacy UnityEngine.UI.Text.
    #
    # Do this only when UnityPy found *zero* UI.Text components.  Before using
    # the decompiled scalar schema, validate the live retail scene's exact
    # GameObject IDs/names, Text component IDs, Items transform, parentage and
    # child order.  A different game build therefore fails loudly instead of
    # receiving guessed menu data.
    slot_select_verified_ui_fallback = False
    if scene_name == "scene_slot_select" and ui_text_count == 0:
        mb_objects = objects.get("MonoBehaviour", {})
        schema_errors = []

        items_go = gameobjects.get(_SLOT_SELECT_ITEMS_GO)
        items_tr = transforms.get(_SLOT_SELECT_ITEMS_GO)
        if not items_go or str(items_go.get("name") or "") != "Items":
            schema_errors.append("GameObject 25 is not Items")
        if not items_tr or int(items_tr.get("transform_pid", 0) or 0) != _SLOT_SELECT_ITEMS_TRANSFORM:
            schema_errors.append("Items RectTransform is not PathID 472")
        live_children = tuple(transform_children.get(_SLOT_SELECT_ITEMS_TRANSFORM, ()))
        if live_children != _SLOT_SELECT_ITEMS_CHILD_TRANSFORMS:
            schema_errors.append(
                "Items child order differs: " + repr(live_children))
        if _SLOT_SELECT_LAYOUT_COMPONENT not in mb_objects:
            schema_errors.append(
                "Items VerticalLayoutGroup MonoBehaviour PathID 738 is absent")

        for go_pid, component_pid, expected_name, _alpha in _SLOT_SELECT_UI_TEXT_SCHEMA:
            go = gameobjects.get(go_pid)
            tr = transforms.get(go_pid)
            live_name = str((go or {}).get("name") or "")
            if live_name != expected_name:
                schema_errors.append(
                    f"GameObject {go_pid} expected {expected_name!r}, found {live_name!r}")
            if component_pid not in mb_objects:
                schema_errors.append(
                    f"{expected_name} Text MonoBehaviour PathID {component_pid} is absent")
            if not tr or int(tr.get("parent_transform_pid", 0) or 0) != _SLOT_SELECT_ITEMS_TRANSFORM:
                schema_errors.append(
                    f"{expected_name} is not parented to Items RectTransform 472")

        if schema_errors:
            raise ValueError(
                "scene_slot_select UI.Text typetree unavailable and verified retail "
                "fallback signature did not match: " + "; ".join(schema_errors))

        # GUID 17c581de3058caa42b1a116bb320efb5 in the decompiled scene maps
        # to Assets/Font/CupheadVogue-ExtraBold.ttf.  The full-game compiler
        # resolves this name against the embedded resources.assets FontData;
        # no external font file is shipped to the Xbox.
        for go_pid, component_pid, expected_name, initial_alpha in _SLOT_SELECT_UI_TEXT_SCHEMA:
            live_name = str(gameobjects[go_pid].get("name") or expected_name).strip()
            ui_texts[go_pid] = {
                "go_pid": int(go_pid),
                "component_path_id": int(component_pid),
                "go_name": live_name,
                "text": live_name,
                "font_name": _SLOT_SELECT_TEXT_FONT_NAME,
                "font_data": b"",
                "font_path_id": 0,
                "font_size": _SLOT_SELECT_TEXT_FONT_SIZE,
                "font_style": 0,
                "alignment": _SLOT_SELECT_TEXT_ALIGNMENT,
                "line_spacing": 1.0,
                "horizontal_overflow": 1,
                "vertical_overflow": 1,
                "enabled": True,
                "color": (1.0, 1.0, 1.0, float(initial_alpha)),
                "script_name": f"<script:{UNITY_UI_TEXT_SCRIPT_PATH_ID}>",
                "script_namespace": "UnityEngine.UI",
                "script_assembly": "UnityEngine.UI",
                "verified_decomp_fallback": True,
            }
        ui_text_count = len(_SLOT_SELECT_UI_TEXT_SCHEMA)

        # The exact Items layout in scene_slot_select.unity is a
        # VerticalLayoutGroup with zero padding/spacing, MiddleCenter alignment,
        # controlled child width/height, force-expand width only.  Runtime
        # positions are still computed from the live Items RectTransform and
        # the baked glyph preferred sizes below.
        if _SLOT_SELECT_ITEMS_GO not in vertical_layouts:
            vertical_layouts[_SLOT_SELECT_ITEMS_GO] = {
                "padding": (0, 0, 0, 0),
                "child_alignment": 4,
                "spacing": 0.0,
                "force_expand_w": True,
                "force_expand_h": False,
                "control_w": True,
                "control_h": True,
            }

        warnings.append(
            "scene_slot_select: UnityPy exposed no legacy UI.Text typetrees; "
            "used verified retail scene schema after exact live hierarchy/component validation")
        slot_select_verified_ui_fallback = True

    # CanvasScaler usually sits on the same GameObject as Canvas. Fold its
    # runtime-independent settings into the compact Canvas record.
    for go_pid, sc in canvas_scalers.items():
        if go_pid in canvases:
            canvases[go_pid].update(sc)

    # Static UnityEngine.UI.Text is interpreted offline.  The resolver supplied
    # by fullgame.py rasterizes the authored string with the embedded Unity font
    # into a generated CUPT/CUPR pair.  At runtime it is deliberately just another
    # UI Image, keeping the Xbox side small and bounded.
    for go_pid, txt in ui_texts.items():
        if resolve_ui_text is None:
            warnings.append(f"UI Text {txt['component_path_id']}: no text bake resolver")
            txt["skip_layout"] = True
            ui_text_skipped_count += 1
            continue
        try:
            baked = resolve_ui_text(dict(txt))
        except Exception as e:
            warnings.append(f"UI Text {txt['component_path_id']}: {e}")
            txt["skip_layout"] = True
            ui_text_skipped_count += 1
            continue
        if not baked or baked.get("skip"):
            txt["skip_layout"] = True
            ui_text_skipped_count += 1
            continue
        sid = int(baked.get("sprite_asset_id", 0) or 0)
        if not sid:
            warnings.append(f"UI Text {txt['component_path_id']}: bake returned no Sprite asset")
            txt["skip_layout"] = True
            ui_text_skipped_count += 1
            continue
        pw = max(1.0, float(baked.get("width", txt["font_size"]) or txt["font_size"]))
        ph = max(1.0, float(baked.get("height", txt["font_size"]) or txt["font_size"]))
        txt["preferred_w"] = pw
        txt["preferred_h"] = ph
        txt["sprite_asset_id"] = sid
        rflags = RENDERER_UI_IMAGE | RENDERER_UI_TEXT
        if txt["enabled"]:
            rflags |= RENDERER_ENABLED
        if bool(baked.get("menu_item")):
            rflags |= RENDERER_MENU_ITEM
        renderers[go_pid] = {
            "kind": "ui_text",
            "sprite_asset_id": sid,
            "flags": rflags,
            "color": txt["color"],
            "sorting_layer_id": 0,
            "sorting_order": 0,
            "text": txt["text"],
            "font_name": txt["font_name"],
            "preferred_w": pw,
            "preferred_h": ph,
        }
        dependencies.add(sid)
        ui_text_baked_count += 1

    # Bake the static part of VerticalLayoutGroup now that Text preferred sizes
    # are known.  This intentionally covers the layout semantics used by the
    # frontend menu without recreating Unity's layout engine on Xbox.
    for layout_go, lg in vertical_layouts.items():
        parent = transforms.get(layout_go)
        if not parent:
            continue
        child_go = []
        for child_tpid in transform_children.get(parent["transform_pid"], []):
            cgo = transform_to_go.get(child_tpid, 0)
            if not cgo or cgo not in transforms:
                continue
            if not gameobjects.get(cgo, {}).get("active", True):
                continue
            le = layout_elements.get(cgo, {})
            if le.get("ignore"):
                continue
            if ui_texts.get(cgo, {}).get("skip_layout"):
                continue
            child_go.append(cgo)
        if not child_go:
            continue

        left, right, top, bottom = lg["padding"]
        spacing = float(lg["spacing"])

        # RectTransform.sizeDelta is NOT the actual rect size when the anchors
        # are stretched.  scene_slot_select/Items is the important retail case:
        # it stretches 0..1 inside a 100x100 MainMenu rect and adds 414x174, so
        # Unity lays it out as 514x274.  Treating sizeDelta (414x174) as the
        # parent rect makes MiddleCenter start 50 units too high, which becomes
        # ~24 pixels after the authored 0.487037 Canvas scale.
        #
        # Bake Unity's effective rect size recursively so VerticalLayoutGroup
        # receives the same geometry as the retail scene instead of compensating
        # with an Xbox-side screen offset.
        rect_size_cache = {}

        def effective_rect_size(go_pid, depth=0):
            if go_pid in rect_size_cache:
                return rect_size_cache[go_pid]
            tr = transforms.get(go_pid)
            if not tr:
                return (0.0, 0.0)

            sdw = float(tr["size_delta"][0])
            sdh = float(tr["size_delta"][1])
            if not tr.get("rect") or depth >= 64:
                out = (abs(sdw), abs(sdh))
                rect_size_cache[go_pid] = out
                return out

            parent_tpid = int(tr.get("parent_transform_pid", 0) or 0)
            parent_go = transform_to_go.get(parent_tpid, 0) if parent_tpid else 0
            if not parent_go or parent_go not in transforms:
                out = (abs(sdw), abs(sdh))
                rect_size_cache[go_pid] = out
                return out

            pw, ph = effective_rect_size(parent_go, depth + 1)
            amin = tr.get("anchor_min", (0.0, 0.0))
            amax = tr.get("anchor_max", (0.0, 0.0))
            span_x = float(amax[0]) - float(amin[0])
            span_y = float(amax[1]) - float(amin[1])
            w = pw * span_x + sdw
            h = ph * span_y + sdh
            out = (abs(w), abs(h))
            rect_size_cache[go_pid] = out
            return out

        parent_w, parent_h = effective_rect_size(layout_go)

        def pref_size(cgo):
            tr = transforms[cgo]
            le = layout_elements.get(cgo, {})
            txt = ui_texts.get(cgo, {})
            rw = float(le.get("preferred_w", -1.0))
            rh = float(le.get("preferred_h", -1.0))
            if rw < 0.0:
                rw = float(txt.get("preferred_w", abs(tr["size_delta"][0]) or 1.0))
            if rh < 0.0:
                rh = float(txt.get("preferred_h", abs(tr["size_delta"][1]) or 1.0))
            return max(1.0, rw), max(1.0, rh)

        sizes = [pref_size(cgo) for cgo in child_go]
        if parent_w < 1.0:
            parent_w = max(w for w, _h in sizes) + left + right
            parent["size_delta"] = (parent_w, parent["size_delta"][1])
        content_h = top + bottom + sum(h for _w, h in sizes) + spacing * max(0, len(sizes) - 1)
        if parent_h < 1.0:
            parent_h = content_h
            parent["size_delta"] = (parent["size_delta"][0], parent_h)

        align = int(lg["child_alignment"])
        hmode = align % 3
        vmode = max(0, min(2, align // 3))
        extra_h = max(0.0, parent_h - content_h)
        y = float(top) + (0.0 if vmode == 0 else (extra_h * 0.5 if vmode == 1 else extra_h))
        avail_w = max(1.0, parent_w - left - right)

        for cgo, (pw, ph) in zip(child_go, sizes):
            tr = transforms[cgo]
            is_text = cgo in ui_texts and not ui_texts[cgo].get("skip_layout")
            w = pw
            h = ph
            if not is_text and lg["control_w"] and lg["force_expand_w"]:
                w = avail_w
            if hmode == 0:
                x = -parent_w * 0.5 + left + w * 0.5
            elif hmode == 2:
                x = parent_w * 0.5 - right - w * 0.5
            else:
                x = (left - right) * 0.5
            tr["anchor_min"] = (0.5, 1.0)
            tr["anchor_max"] = (0.5, 1.0)
            tr["pivot"] = (0.5, 0.5)
            tr["anchored_pos"] = (x, -(y + h * 0.5))
            tr["size_delta"] = (w, h)
            y += h + spacing
        vertical_layout_baked_count += 1

    # Non-layout Text objects still need a concrete quad size.  For the common
    # non-stretched RectTransform case, use the baked glyph dimensions directly.
    for go_pid, txt in ui_texts.items():
        if txt.get("skip_layout") or "preferred_w" not in txt:
            continue
        tr = transforms.get(go_pid)
        if not tr:
            continue
        ax0, ay0 = tr["anchor_min"]
        ax1, ay1 = tr["anchor_max"]
        if abs(ax1 - ax0) < 0.0001 and abs(ay1 - ay0) < 0.0001:
            tr["size_delta"] = (txt["preferred_w"], txt["preferred_h"])

    for pid, obj in objects.get("Animator", {}).items():
        try:
            d = _read_object(obj)
            _gf, go_pid = _ptr_ids(_field(d, "m_GameObject", None))
            if not go_pid:
                warnings.append(f"Animator {pid}: missing GameObject")
                continue
            controller_ptr = _field(d, "m_Controller", None)
            cfid, cpid = _ptr_ids(controller_ptr)
            controller_name = _ptr_name(controller_ptr)
            clip_name = _controller_idle_name(controller_name)
            anim_id = int(resolve_animation(clip_name) or 0) if (resolve_animation and clip_name) else 0
            animators[go_pid] = {
                "controller_file_id": cfid,
                "controller_path_id": cpid,
                "controller_name": controller_name,
                "animation_asset_id": anim_id,
                "animation_name": clip_name,
                "flags": ANIMATOR_ENABLED if bool(_field(d, "m_Enabled", 1)) else 0,
            }
            if anim_id:
                dependencies.add(anim_id)
        except Exception as e:
            warnings.append(f"Animator {pid}: {e}")

    for pid, obj in objects.get("Camera", {}).items():
        try:
            d = _read_object(obj)
            _gf, go_pid = _ptr_ids(_field(d, "m_GameObject", None))
            flags = 0
            if bool(_field(d, "m_Enabled", 1)):
                flags |= CAMERA_ENABLED
            is_ortho = bool(_field_any(d, ("orthographic", "m_Orthographic"), False))
            if is_ortho:
                flags |= CAMERA_ORTHOGRAPHIC
            cameras.append({
                "go_pid": go_pid,
                "flags": flags,
                "orthographic_size": float(_field_any(d, ("orthographic_size", "m_OrthographicSize"), 5.0) or 5.0),
                "fov": float(_field_any(d, ("field_of_view", "m_FieldOfView"), 60.0) or 60.0),
                "depth": float(_field(d, "m_Depth", 0.0) or 0.0),
                "aspect": float(_field_any(d, ("aspect", "m_Aspect"), 0.0) or 0.0),
                "viewport": _rect4(_field(d, "m_NormalizedViewPortRect", None)),
                "culling_mask": _mask32(_field(d, "m_CullingMask", None)),
            })
        except Exception as e:
            warnings.append(f"Camera {pid}: {e}")

    # Stable hierarchy order. Roots retain GameObject PathID ordering; children
    # follow Transform.m_Children exactly when Unity serialized it.
    hierarchy_order = {}
    order_counter = [0]
    def walk_transform(tpid):
        go_pid = transform_to_go.get(tpid, 0)
        if go_pid and go_pid not in hierarchy_order:
            hierarchy_order[go_pid] = order_counter[0]
            order_counter[0] += 1
        for child in transform_children.get(tpid, []):
            walk_transform(child)
    roots = []
    for go_pid, tr in transforms.items():
        if not tr["parent_transform_pid"]:
            roots.append(tr["transform_pid"])
    for tpid in sorted(roots):
        walk_transform(tpid)
    for go_pid in sorted(gameobjects):
        if go_pid not in hierarchy_order:
            hierarchy_order[go_pid] = order_counter[0]
            order_counter[0] += 1

    # UI Images use Canvas sorting order + exact hierarchy order. Unity UI does
    # not use SpriteRenderer.sortingOrder for sibling composition. Encode a
    # deterministic flat key in the existing sorting_order field so the Xbox
    # runtime can keep its draw list compact.
    def nearest_canvas(go_pid):
        seen = set()
        cur = go_pid
        while cur and cur not in seen:
            seen.add(cur)
            if cur in canvases:
                return cur
            tr = transforms.get(cur)
            if not tr:
                break
            cur = transform_to_go.get(tr["parent_transform_pid"], 0)
        return 0

    for go_pid, rr in renderers.items():
        if rr.get("kind") in ("ui_image", "ui_text"):
            cgo = nearest_canvas(go_pid)
            canvas_order = int(canvases.get(cgo, {}).get("sorting_order", 0) or 0)
            rr["sorting_order"] = canvas_order * 100000 + int(hierarchy_order.get(go_pid, 0))
            rr["canvas_go_pid"] = cgo

    node_blob = bytearray()
    node_meta = []
    for go_pid in sorted(gameobjects):
        go = gameobjects[go_pid]
        tr = transforms.get(go_pid)
        rr = renderers.get(go_pid)
        an = animators.get(go_pid)
        flags = NODE_ACTIVE if go["active"] else 0
        parent_go = 0
        pos = (0.0, 0.0, 0.0)
        rot = (0.0, 0.0, 0.0, 1.0)
        scale = (1.0, 1.0, 1.0)
        anchor_min = (0.0, 0.0)
        anchor_max = (1.0, 1.0)
        anchored_pos = (0.0, 0.0)
        size_delta = (0.0, 0.0)
        pivot = (0.5, 0.5)
        if tr:
            flags |= NODE_HAS_TRANSFORM
            if tr["rect"]:
                flags |= NODE_RECT_TRANSFORM
            parent_go = transform_to_go.get(tr["parent_transform_pid"], 0)
            pos, rot, scale = tr["pos"], tr["rot"], tr["scale"]
            anchor_min, anchor_max = tr["anchor_min"], tr["anchor_max"]
            anchored_pos, size_delta, pivot = tr["anchored_pos"], tr["size_delta"], tr["pivot"]
        if go_pid in canvases:
            flags |= NODE_HAS_CANVAS

        sprite_asset_id = 0
        renderer_flags = 0
        color = (1.0, 1.0, 1.0, 1.0)
        sorting_layer_id = 0
        sorting_order = 0
        renderer_kind = "none"
        if rr:
            renderer_kind = rr.get("kind", "sprite")
            if renderer_kind in ("ui_image", "ui_text"):
                flags |= NODE_HAS_UI_IMAGE
            else:
                flags |= NODE_HAS_SPRITE_RENDERER
            sprite_asset_id = rr["sprite_asset_id"]
            if sprite_asset_id:
                flags |= NODE_HAS_SPRITE
            renderer_flags = rr["flags"]
            color = rr["color"]
            sorting_layer_id = rr["sorting_layer_id"]
            sorting_order = rr["sorting_order"]

        controller_file_id = 0
        controller_path_id = 0
        animator_flags = 0
        animation_asset_id = 0
        controller_name = ""
        animation_name = ""
        if an:
            flags |= NODE_HAS_ANIMATOR
            controller_file_id = an["controller_file_id"]
            controller_path_id = an["controller_path_id"]
            animator_flags = an["flags"]
            animation_asset_id = an["animation_asset_id"]
            controller_name = an["controller_name"]
            animation_name = an["animation_name"]
            if animation_asset_id:
                flags |= NODE_HAS_RUNTIME_ANIMATION

        name_off = strings.add(go["name"])
        node_blob.extend(SCENE_NODE.pack(
            int(go_pid), int(parent_go),
            int(name_off), int(flags), int(go["layer"]), int(animation_asset_id),
            *pos, *rot, *scale,
            *anchor_min, *anchor_max, *anchored_pos, *size_delta, *pivot,
            int(sprite_asset_id), int(renderer_flags), *color,
            int(sorting_layer_id), int(sorting_order),
            int(controller_file_id) & 0xFFFFFFFF, int(controller_path_id), int(animator_flags),
        ))
        node_meta.append({
            "path_id": int(go_pid), "name": go["name"], "parent_path_id": int(parent_go),
            "active": bool(flags & NODE_ACTIVE),
            "rect_transform": bool(flags & NODE_RECT_TRANSFORM),
            "canvas": bool(flags & NODE_HAS_CANVAS),
            "renderer_kind": renderer_kind,
            "ui_text": rr.get("text", "") if rr else "",
            "font_name": rr.get("font_name", "") if rr else "",
            "sprite_asset_id": f"{sprite_asset_id:08X}" if sprite_asset_id else "00000000",
            "sorting_layer_id": sorting_layer_id, "sorting_order": sorting_order,
            "hierarchy_order": int(hierarchy_order.get(go_pid, 0)),
            "animator_controller_file_id": controller_file_id,
            "animator_controller_path_id": controller_path_id,
            "animator_controller_name": controller_name,
            "animation_asset_id": f"{animation_asset_id:08X}" if animation_asset_id else "00000000",
            "animation_name": animation_name,
        })

    camera_blob = bytearray()
    for c in cameras:
        camera_blob.extend(SCENE_CAMERA.pack(
            int(c["go_pid"]), int(c["flags"]),
            c["orthographic_size"], c["fov"], c["depth"], c["aspect"],
            *c["viewport"], int(c["culling_mask"]),
        ))

    canvas_blob = bytearray()
    for go_pid in sorted(canvases):
        c = canvases[go_pid]
        canvas_blob.extend(SCENE_CANVAS.pack(
            int(go_pid), int(c["flags"]), int(c["render_mode"]),
            int(c["sorting_order"]), int(c["scale_mode"]),
            float(c["ref_w"]), float(c["ref_h"]), float(c["match"]), float(c["ref_ppu"]),
        ))

    group_blob = bytearray()
    for go_pid in sorted(canvas_groups):
        g = canvas_groups[go_pid]
        group_blob.extend(SCENE_CANVAS_GROUP.pack(
            int(go_pid), 0, float(g["alpha"]), int(g["interactable"]),
            int(g["blocks_raycasts"]), int(g["ignore_parent"]),
        ))

    string_blob = strings.bytes()
    scene_flags = SCENE_FLAG_HAS_CAMERA if cameras else 0
    header = SCENE_HEADER.pack(
        SCENE_MAGIC, SCENE_VERSION, SCENE_HEADER.size, scene_flags,
        len(node_meta), len(cameras), len(canvases), len(canvas_groups), len(string_blob),
        SCENE_NODE.size, SCENE_CAMERA.size, SCENE_CANVAS.size, SCENE_CANVAS_GROUP.size,
        scene_name_off, 0,
    )
    payload = header + bytes(node_blob) + bytes(camera_blob) + bytes(canvas_blob) + bytes(group_blob) + string_blob
    metadata = {
        "payload_version": SCENE_VERSION,
        "scene_name": scene_name,
        "source_container": rel.replace("\\", "/"),
        "node_count": len(node_meta),
        "camera_count": len(cameras),
        "canvas_count": len(canvases),
        "canvas_renderer_count": canvas_renderer_count,
        "canvas_group_count": len(canvas_groups),
        "ui_image_count": ui_image_count,
        "ui_text_count": ui_text_count,
        "ui_text_baked_count": ui_text_baked_count,
        "ui_text_skipped_count": ui_text_skipped_count,
        "vertical_layout_group_count": len(vertical_layouts),
        "vertical_layout_baked_count": vertical_layout_baked_count,
        "slot_select_verified_ui_fallback": bool(slot_select_verified_ui_fallback),
        "sprite_renderer_count": sum(1 for n in node_meta if n["renderer_kind"] == "sprite" and n["sprite_asset_id"] != "00000000"),
        "animator_count": sum(1 for n in node_meta if n["animator_controller_path_id"]),
        "runtime_animation_count": sum(1 for n in node_meta if n["animation_asset_id"] != "00000000"),
        "unresolved_sprite_renderer_count": sum(
            1 for go_pid in gameobjects
            if go_pid in renderers and renderers[go_pid]["sprite_asset_id"] == 0
        ),
        "mono_script_counts": dict(sorted(mono_scripts.items())),
        "warnings": warnings,
        "cameras": [
            {
                "game_object_path_id": int(c["go_pid"]),
                "enabled": bool(c["flags"] & CAMERA_ENABLED),
                "orthographic": bool(c["flags"] & CAMERA_ORTHOGRAPHIC),
                "orthographic_size": float(c["orthographic_size"]),
                "field_of_view": float(c["fov"]),
                "depth": float(c["depth"]),
                "aspect": float(c["aspect"]),
                "viewport": {"x": float(c["viewport"][0]), "y": float(c["viewport"][1]),
                             "width": float(c["viewport"][2]), "height": float(c["viewport"][3])},
                "culling_mask": f"{int(c['culling_mask']) & 0xFFFFFFFF:08X}",
            }
            for c in cameras
        ],
        "canvases": [
            {"game_object_path_id": int(go_pid), **c}
            for go_pid, c in sorted(canvases.items())
        ],
        "nodes": node_meta,
    }
    return payload, metadata, sorted(dependencies)

def build_scene_spec(env, rel: str, scene_name: str, group: str, resolve_sprite, resolve_animation=None, resolve_ui_text=None):
    payload, metadata, dependencies = compile_scene_environment(
        env, rel, scene_name, resolve_sprite, resolve_animation, resolve_ui_text)
    return AssetSpec(
        name=f"scene/{scene_name}",
        source=None,
        data=payload,
        kind="scene",
        group=group,
        type=TYPE_SCENE,
        dependencies=dependencies,
        metadata=metadata,
    )


def parse_scene_payload(data: bytes):
    if len(data) < SCENE_HEADER.size:
        raise ValueError("Truncated CUPN header")
    (
        magic, version, header_size, flags,
        node_count, camera_count, canvas_count, group_count, string_bytes,
        node_record_size, camera_record_size, canvas_record_size, group_record_size,
        scene_name_offset, _reserved,
    ) = SCENE_HEADER.unpack_from(data, 0)
    if magic != SCENE_MAGIC or version != SCENE_VERSION:
        raise ValueError("Bad CUPN magic/version")
    if header_size != SCENE_HEADER.size:
        raise ValueError("Unexpected CUPN header size")
    if (node_record_size != SCENE_NODE.size or camera_record_size != SCENE_CAMERA.size or
            canvas_record_size != SCENE_CANVAS.size or group_record_size != SCENE_CANVAS_GROUP.size):
        raise ValueError("Unexpected CUPN record size")
    expected = (header_size + node_count * node_record_size + camera_count * camera_record_size +
                canvas_count * canvas_record_size + group_count * group_record_size + string_bytes)
    if expected != len(data):
        raise ValueError("CUPN payload size mismatch")
    string_base = (header_size + node_count * node_record_size + camera_count * camera_record_size +
                   canvas_count * canvas_record_size + group_count * group_record_size)
    strings = data[string_base:string_base + string_bytes]

    def get_string(off):
        if off >= len(strings):
            return ""
        end = strings.find(b"\0", off)
        if end < 0:
            end = len(strings)
        return strings[off:end].decode("utf-8", "replace")

    nodes = []
    off = header_size
    for _ in range(node_count):
        raw = SCENE_NODE.unpack_from(data, off)
        nodes.append({
            "path_id": raw[0], "parent_path_id": raw[1],
            "name": get_string(raw[2]), "flags": raw[3], "layer": raw[4],
            "animation_asset_id": f"{raw[5]:08X}",
            "sprite_asset_id": f"{raw[26]:08X}",
            "renderer_flags": raw[27],
            "sorting_layer_id": raw[32], "sorting_order": raw[33],
            "animator_controller_file_id": raw[34],
            "animator_controller_path_id": raw[35],
            "animator_flags": raw[36],
        })
        off += node_record_size
    return {
        "version": version,
        "flags": flags,
        "scene_name": get_string(scene_name_offset),
        "node_count": node_count,
        "camera_count": camera_count,
        "canvas_count": canvas_count,
        "canvas_group_count": group_count,
        "nodes": nodes,
    }
