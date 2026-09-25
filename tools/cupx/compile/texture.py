from __future__ import annotations

from pathlib import Path
import hashlib
import io
import ntpath
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.request

from PIL import Image

from ..pack.format import TYPE_TEXTURE
from ..pack.volumes import AssetSpec

# Native CUPX texture payload v2.
#
# Xbox-facing contract:
#   - logical width/height are the visible image dimensions
#   - storage width/height are power-of-two dimensions suitable for NV2A swizzle
#   - pixels are tightly packed linear BGRA8 in storage dimensions
#   - Xbox creates D3DFMT_A8R8G8B8 and calls XGSwizzleRect on the payload
#
# This matches the proven DarkDash / EOS texture upload model: non-POT images are
# padded on the PC, then the Xbox only has to allocate, lock, swizzle, unlock.
TEXTURE_MAGIC = b"CUPT"
TEXTURE_VERSION = 2
TEXTURE_PRODUCTION_VERSION = 3
TEXTURE_HEADER_V2 = struct.Struct("<4sHHIIIIIIIII")
TEXTURE_HEADER_V3 = TEXTURE_HEADER_V2
TEXTURE_HEADER_V1 = struct.Struct("<4sHHIIIIIII")

TEXFMT_A8R8G8B8 = 1
TEXFMT_DXT1 = 2
TEXFMT_DXT5 = 3

# Unity 5.x TextureFormat enum values used by Cuphead retail data.
UNITY_TEXFMT_ALPHA8 = 1
UNITY_TEXFMT_ARGB4444 = 2
UNITY_TEXFMT_RGB24 = 3
UNITY_TEXFMT_RGBA32 = 4
UNITY_TEXFMT_ARGB32 = 5
UNITY_TEXFMT_RGB565 = 7
UNITY_TEXFMT_BGR24 = 8
UNITY_TEXFMT_DXT1 = 10
UNITY_TEXFMT_DXT5 = 12
UNITY_TEXFMT_RGBA4444 = 13
UNITY_TEXFMT_BGRA32 = 14
UNITY_TEXFMT_BC6H = 24
UNITY_TEXFMT_BC7 = 25
UNITY_TEXFMT_BC4 = 26
UNITY_TEXFMT_BC5 = 27
UNITY_TEXFMT_DXT1_CRUNCHED = 28
UNITY_TEXFMT_DXT5_CRUNCHED = 29
UNITY_TEXFMT_R8 = 63

# DirectXTex is used as the production block-codec backend on Windows.  It is
# pinned to a known Microsoft release and SHA-256 so the user-side mastering
# tool never depends on UnityPy/Pillow native BC7 decode stability.
DIRECTXTEX_TEXCONV_URL = (
    "https://github.com/microsoft/DirectXTex/releases/download/may2026/texconv.exe"
)
DIRECTXTEX_TEXCONV_SHA256 = (
    "dcfdec10244e02cf5037fba089c55fb7e1326b1c8181742d77d15fa5cb5eef06"
)
TEXFLAG_HAS_ALPHA = 0x00000001
TEXFLAG_TOP_LEFT = 0x00000002
TEXFLAG_LINEAR_SOURCE = 0x00000004
TEXFLAG_POT_PADDED = 0x00000008
TEXFLAG_EDGE_REPLICATED = 0x00000010
TEXFLAG_BLOCK_COMPRESSED = 0x00000020
TEXFLAG_SOURCE_PASSTHROUGH = 0x00000040
TEXFLAG_UNITY_BOTTOM_LEFT = 0x00000080

DEFAULT_TEXTURE_COUNT = 1
DEFAULT_MIN_DIMENSION = 32
DEFAULT_MAX_DIMENSION = 2048
DEFAULT_MAX_DECODED_BYTES = 16 * 1024 * 1024
PRODUCTION_KEEP_RGBA_MAX_BYTES = 4 * 1024 * 1024




def _is_power_of_two(v: int) -> bool:
    v = int(v)
    return v > 0 and (v & (v - 1)) == 0


def _texture_format_int(value) -> int:
    try:
        return int(value)
    except Exception:
        pass
    for attr in ("value", "m_Value"):
        try:
            return int(getattr(value, attr))
        except Exception:
            pass
    text = str(value or "")
    upper = text.upper()
    if "DXT1" in upper:
        return UNITY_TEXFMT_DXT1
    if "DXT5" in upper or "BC3" in upper:
        return UNITY_TEXFMT_DXT5
    if "BC7" in upper:
        return UNITY_TEXFMT_BC7
    return 0


def _dxt_base_bytes(width: int, height: int, fmt: int) -> int:
    block_bytes = 8 if int(fmt) == TEXFMT_DXT1 else 16
    return max(1, (int(width) + 3) // 4) * max(1, (int(height) + 3) // 4) * block_bytes


def _build_compressed_texture_payload(
    blocks: bytes,
    width: int,
    height: int,
    fmt: int,
    *,
    has_alpha: bool,
    source_passthrough: bool,
    source_format: str = "",
    top_left: bool = True,
):
    if fmt not in (TEXFMT_DXT1, TEXFMT_DXT5):
        raise ValueError(f"Unsupported production compressed format {fmt}")
    width = int(width); height = int(height)
    if width <= 0 or height <= 0:
        raise ValueError("Invalid compressed texture dimensions")
    expected = _dxt_base_bytes(width, height, fmt)
    if len(blocks) < expected:
        raise ValueError(f"Compressed texture truncated: expected {expected:,}, got {len(blocks):,}")
    blocks = bytes(blocks[:expected])
    block_bytes = 8 if fmt == TEXFMT_DXT1 else 16
    row_pitch = max(1, (width + 3) // 4) * block_bytes
    flags = TEXFLAG_LINEAR_SOURCE | TEXFLAG_BLOCK_COMPRESSED
    if top_left:
        flags |= TEXFLAG_TOP_LEFT
    else:
        flags |= TEXFLAG_UNITY_BOTTOM_LEFT
    if has_alpha:
        flags |= TEXFLAG_HAS_ALPHA
    if source_passthrough:
        flags |= TEXFLAG_SOURCE_PASSTHROUGH

    header = TEXTURE_HEADER_V3.pack(
        TEXTURE_MAGIC, TEXTURE_PRODUCTION_VERSION, TEXTURE_HEADER_V3.size,
        width, height, width, height, fmt, 1, flags, row_pitch, len(blocks),
    )
    name = "DXT1" if fmt == TEXFMT_DXT1 else "DXT5"
    meta = {
        "native_format": name,
        "native_format_id": fmt,
        "width": width,
        "height": height,
        "storage_width": width,
        "storage_height": height,
        "u_max": 1.0,
        "v_max": 1.0,
        "mip_count": 1,
        "row_pitch": row_pitch,
        "data_bytes": len(blocks),
        "has_alpha": bool(has_alpha),
        "origin": ("top-left" if top_left else "unity-bottom-left"),
        "layout": "linear-dxt-blocks",
        "padding": "none",
        "payload_version": TEXTURE_PRODUCTION_VERSION,
        "payload_header_bytes": TEXTURE_HEADER_V3.size,
        "source_passthrough": bool(source_passthrough),
        "source_texture_format": source_format,
        "xbox_upload": "create matching DXT surface and copy compressed blocks directly",
    }
    return header + blocks, meta


def _project_root() -> Path:
    # .../CUPX_Tooling_x/cupx/compile/texture.py -> package root
    return Path(__file__).resolve().parents[2]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _ensure_texconv() -> Path:
    """Return a verified DirectXTex texconv executable.

    An explicit CUPX_TEXCONV path or PATH installation wins. Otherwise CUPX
    downloads the pinned Microsoft binary once into tools/ and verifies its
    release SHA-256 before use.
    """
    explicit = os.environ.get("CUPX_TEXCONV", "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        raise FileNotFoundError(f"CUPX_TEXCONV does not exist: {path}")

    for name in ("texconv.exe", "texconv"):
        found = shutil.which(name)
        if found:
            return Path(found)

    tools = _project_root() / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    target = tools / "texconv.exe"
    if target.is_file():
        digest = _sha256_file(target)
        if digest == DIRECTXTEX_TEXCONV_SHA256:
            return target
        try:
            target.unlink()
        except OSError:
            pass

    # Production use is Windows; on other platforms tests can provide an
    # explicit CUPX_TEXCONV override.
    if os.name != "nt":
        raise RuntimeError(
            "DirectXTex texconv is required for BC7 production mastering. "
            "Set CUPX_TEXCONV to a compatible texconv binary."
        )

    tmp = target.with_name(f"texconv.{os.getpid()}.tmp")
    print("CUPX: downloading verified Microsoft DirectXTex texconv (one-time, ~1 MiB)...")
    try:
        with urllib.request.urlopen(DIRECTXTEX_TEXCONV_URL, timeout=90) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        digest = _sha256_file(tmp)
        if digest != DIRECTXTEX_TEXCONV_SHA256:
            raise RuntimeError(
                f"texconv SHA-256 mismatch: expected {DIRECTXTEX_TEXCONV_SHA256}, got {digest}"
            )
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return target


def _make_dx10_dds(width: int, height: int, blocks: bytes, dxgi_format: int) -> bytes:
    """Wrap one base-level BC surface in a minimal DDS/DX10 header."""
    width = int(width); height = int(height)
    if width <= 0 or height <= 0:
        raise ValueError("invalid DDS dimensions")
    DDSD_CAPS = 0x00000001
    DDSD_HEIGHT = 0x00000002
    DDSD_WIDTH = 0x00000004
    DDSD_PIXELFORMAT = 0x00001000
    DDSD_LINEARSIZE = 0x00080000
    DDPF_FOURCC = 0x00000004
    DDSCAPS_TEXTURE = 0x00001000
    fourcc_dx10 = struct.unpack("<I", b"DX10")[0]

    header = bytearray()
    header += b"DDS "
    header += struct.pack("<I", 124)
    header += struct.pack(
        "<I", DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE
    )
    header += struct.pack("<IIIII", height, width, len(blocks), 0, 1)
    header += struct.pack("<11I", *([0] * 11))
    header += struct.pack("<IIIIIIII", 32, DDPF_FOURCC, fourcc_dx10, 0, 0, 0, 0, 0)
    header += struct.pack("<IIIII", DDSCAPS_TEXTURE, 0, 0, 0, 0)
    # DX10 header: format, D3D10_RESOURCE_DIMENSION_TEXTURE2D, misc, array, misc2
    header += struct.pack("<IIIII", int(dxgi_format), 3, 0, 1, 0)
    if len(header) != 148:
        raise AssertionError(f"internal DDS header size error: {len(header)}")
    return bytes(header) + bytes(blocks)


def _extract_dds_blocks(data: bytes, expected_fmt: int):
    """Extract base-level BC1/BC3 blocks from a DirectXTex DDS result."""
    if len(data) < 128 or data[:4] != b"DDS ":
        raise ValueError("texconv output is not a DDS file")
    header_size = struct.unpack_from("<I", data, 4)[0]
    if header_size != 124:
        raise ValueError(f"unsupported DDS header size {header_size}")
    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    fourcc = data[84:88]
    payload_off = 128
    actual_fmt = None
    if fourcc == b"DXT1":
        actual_fmt = TEXFMT_DXT1
    elif fourcc == b"DXT5":
        actual_fmt = TEXFMT_DXT5
    elif fourcc == b"DX10":
        if len(data) < 148:
            raise ValueError("truncated DX10 DDS header")
        dxgi = struct.unpack_from("<I", data, 128)[0]
        payload_off = 148
        if dxgi in (70, 71, 72):  # BC1 typeless/unorm/srgb
            actual_fmt = TEXFMT_DXT1
        elif dxgi in (76, 77, 78):  # BC3 typeless/unorm/srgb
            actual_fmt = TEXFMT_DXT5
    if actual_fmt != expected_fmt:
        raise ValueError(f"texconv returned unexpected DDS format: {fourcc!r}")
    expected = _dxt_base_bytes(width, height, actual_fmt)
    if len(data) < payload_off + expected:
        raise ValueError(
            f"texconv DDS truncated: expected {expected:,} BC bytes, "
            f"have {max(0, len(data)-payload_off):,}"
        )
    return width, height, bytes(data[payload_off:payload_off + expected])


def _run_texconv(input_path: Path, output_dir: Path, fmt: int, *, vflip: bool = False):
    exe = _ensure_texconv()
    target = "BC1_UNORM" if int(fmt) == TEXFMT_DXT1 else "BC3_UNORM"
    cmd = [
        str(exe), "-nologo", "-y", "-m", "1",
        "-f", target, "-dx9", "-o", str(output_dir),
    ]
    if vflip:
        cmd.append("-vflip")
    cmd.append(str(input_path))
    cp = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=300, check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"DirectXTex texconv failed ({cp.returncode}): {cp.stdout[-2000:].strip()}"
        )
    candidates = sorted(Path(output_dir).glob("*.dds"), key=lambda p: p.name.lower())
    if not candidates:
        raise RuntimeError("DirectXTex texconv produced no DDS output")
    return candidates[0]


def _run_texconv_to_png(input_path: Path, output_dir: Path, *, vflip: bool = False):
    """Decode a DDS through DirectXTex and return a PNG path.

    This is used only for non-power-of-two DXT1/DXT5 source textures. Those
    surfaces cannot use CUPX's direct source-block passthrough because the Xbox
    runtime allocates ordinary compressed textures, which need power-of-two
    storage dimensions. DirectXTex performs the compressed decode out of
    process, then CUPX edge-pads the resulting image and re-encodes it to the
    same DXT family. No UnityPy native image decoder is involved.
    """
    exe = _ensure_texconv()
    cmd = [
        str(exe), "-nologo", "-y", "-m", "1",
        "-ft", "png", "-o", str(output_dir),
    ]
    if vflip:
        cmd.append("-vflip")
    cmd.append(str(input_path))
    cp = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=300, check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"DirectXTex texconv PNG decode failed ({cp.returncode}): "
            f"{cp.stdout[-2000:].strip()}"
        )
    candidates = sorted(Path(output_dir).glob("*.png"), key=lambda p: p.name.lower())
    if not candidates:
        raise RuntimeError("DirectXTex texconv produced no PNG output")
    return candidates[0]


def _decode_unity_dxt_to_image_safe(raw: bytes, width: int, height: int, source_fmt: int):
    """Decode Unity DXT1/DXT5 through DirectXTex, never UnityPy/Pillow BCN.

    The returned PIL image is top-left oriented. This path exists specifically
    for NPOT retail textures which cannot be copied directly into an Xbox DXT
    surface without first padding their storage dimensions.
    """
    source_fmt = int(source_fmt)
    if source_fmt == UNITY_TEXFMT_DXT1:
        dxgi = 71  # DXGI_FORMAT_BC1_UNORM
        native_fmt = TEXFMT_DXT1
    elif source_fmt == UNITY_TEXFMT_DXT5:
        dxgi = 77  # DXGI_FORMAT_BC3_UNORM
        native_fmt = TEXFMT_DXT5
    else:
        raise ValueError(f"unsupported Unity DXT source format {source_fmt}")

    expected = _dxt_base_bytes(width, height, native_fmt)
    if len(raw) < expected:
        raise ValueError(
            f"Unity DXT source truncated: expected {expected:,}, got {len(raw):,}"
        )
    raw = bytes(raw[:expected])

    with tempfile.TemporaryDirectory(prefix="cupx_dxt_decode_") as td:
        td = Path(td)
        src = td / "source_dxt.dds"
        out = td / "out"
        out.mkdir()
        src.write_bytes(_make_dx10_dds(width, height, raw, dxgi))
        # Unity texture coordinates are bottom-left. Normalize to CUPX's
        # top-left convention while DirectXTex has the pixels decompressed.
        png = _run_texconv_to_png(src, out, vflip=True)
        with Image.open(png) as im:
            return im.convert("RGBA").copy()


def _transcode_unity_bc_to_dxt5(raw: bytes, width: int, height: int, source_fmt: int, *, preserve_unity_origin: bool = False):
    """Transcode Unity BC4/5/6H/7 blocks to Xbox DXT5 without UnityPy image decode.

    When ``preserve_unity_origin`` is true the encoded block rows are kept in
    Unity's original bottom-left texture convention.  This matters for packed
    SpriteAtlas pages: globally v-flipping the atlas changes the handedness of
    Unity's serialized packingRotation transform even though the rectangle
    coordinates are later converted correctly.
    """
    # DXGI equivalents for Unity TextureFormat values.
    dxgi = {
        UNITY_TEXFMT_BC4: 80,     # BC4_UNORM
        UNITY_TEXFMT_BC5: 83,     # BC5_UNORM
        UNITY_TEXFMT_BC6H: 95,    # BC6H_UF16
        UNITY_TEXFMT_BC7: 98,     # BC7_UNORM
    }.get(int(source_fmt))
    if dxgi is None:
        raise ValueError(f"unsupported DirectXTex source format {source_fmt}")
    block_bytes = 8 if int(source_fmt) == UNITY_TEXFMT_BC4 else 16
    expected = max(1, (int(width) + 3) // 4) * max(1, (int(height) + 3) // 4) * block_bytes
    if len(raw) < expected:
        raise ValueError(f"Unity BC source truncated: expected {expected:,}, got {len(raw):,}")
    raw = bytes(raw[:expected])

    with tempfile.TemporaryDirectory(prefix="cupx_texconv_") as td:
        td = Path(td)
        src = td / "source_bc.dds"
        out = td / "out"
        out.mkdir()
        src.write_bytes(_make_dx10_dds(width, height, raw, dxgi))
        dds = _run_texconv(src, out, TEXFMT_DXT5, vflip=not preserve_unity_origin)
        ow, oh, blocks = _extract_dds_blocks(dds.read_bytes(), TEXFMT_DXT5)
        if (ow, oh) != (int(width), int(height)):
            raise ValueError(f"texconv changed texture dimensions {(width, height)} -> {(ow, oh)}")
        return blocks


def _encode_image_to_dxt(image: Image.Image, fmt: int):
    """Encode a PIL image to Xbox BC1/BC3 using Microsoft DirectXTex."""
    padded, width, height, storage_width, storage_height = _edge_replicated_pot_rgba(image)
    with tempfile.TemporaryDirectory(prefix="cupx_texconv_") as td:
        td = Path(td)
        src = td / "source.png"
        out = td / "out"
        out.mkdir()
        padded.save(src, format="PNG")
        dds = _run_texconv(src, out, fmt, vflip=False)
        ow, oh, blocks = _extract_dds_blocks(dds.read_bytes(), fmt)
        if (ow, oh) != (storage_width, storage_height):
            raise ValueError(
                f"texconv changed storage dimensions {(storage_width, storage_height)} -> {(ow, oh)}"
            )

    has_alpha = image.convert("RGBA").getchannel("A").getextrema() != (255, 255)
    payload, meta = _build_compressed_texture_payload(
        blocks, storage_width, storage_height, fmt,
        has_alpha=has_alpha, source_passthrough=False, source_format="PC-converted", top_left=True,
    )
    vals = list(TEXTURE_HEADER_V3.unpack_from(payload, 0))
    vals[3] = width
    vals[4] = height
    vals[5] = storage_width
    vals[6] = storage_height
    payload = TEXTURE_HEADER_V3.pack(*vals) + payload[TEXTURE_HEADER_V3.size:]
    meta.update({
        "width": width, "height": height,
        "storage_width": storage_width, "storage_height": storage_height,
        "u_max": width / float(storage_width),
        "v_max": height / float(storage_height),
        "padding": "edge-replicated-pot",
        "encoder": "Microsoft DirectXTex texconv",
    })
    return payload, meta


def _run_texconv_resize(input_path: Path, output_dir: Path, fmt: int, width: int, height: int):
    """Resize an already-native BC texture through DirectXTex.

    Used by the 480p mastering profile to reduce oversized Unity atlas pages
    while keeping the runtime-native DXT family and top-left orientation.
    """
    exe = _ensure_texconv()
    target = "BC1_UNORM" if int(fmt) == TEXFMT_DXT1 else "BC3_UNORM"
    cmd = [
        str(exe), "-nologo", "-y", "-m", "1",
        "-f", target, "-dx9",
        "-w", str(int(width)), "-h", str(int(height)),
        "-o", str(output_dir), str(input_path),
    ]
    cp = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=300, check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"DirectXTex texconv resize failed ({cp.returncode}): {cp.stdout[-2000:].strip()}"
        )
    candidates = sorted(Path(output_dir).glob("*.dds"), key=lambda q: q.name.lower())
    if not candidates:
        raise RuntimeError("DirectXTex texconv resize produced no DDS output")
    return candidates[0]


def downscale_native_texture_payload(payload: bytes, scale: float):
    """Downscale a compiled CUPT payload without changing its asset ID.

    The returned CUPT remains runtime-compatible. Compressed DXT pages stay
    compressed, which is critical for the Xbox animation residency budget.
    This is deliberately a mastering-time operation; the Xbox never resizes.
    """
    scale = float(scale)
    if not (0.0 < scale < 1.0):
        raise ValueError("texture downscale must be between 0 and 1")
    meta, image = parse_texture_payload(payload)
    sw = int(meta["storage_width"]); sh = int(meta["storage_height"])
    lw = int(meta["width"]); lh = int(meta["height"])
    tw = max(4, int(round(sw * scale)))
    th = max(4, int(round(sh * scale)))
    tlw = max(1, int(round(lw * scale)))
    tlh = max(1, int(round(lh * scale)))
    # Native DXT surfaces must remain block aligned. The production atlases
    # targeted by this profile are POT, so this normally remains POT as well.
    tw = (tw + 3) & ~3; th = (th + 3) & ~3
    fmt = int(meta["format"])
    flags = int(meta["flags"])

    if fmt in (TEXFMT_DXT1, TEXFMT_DXT5):
        header_size = TEXTURE_HEADER_V3.size if int(meta["version"]) == 3 else TEXTURE_HEADER_V2.size
        blocks = payload[header_size:]
        dxgi = 71 if fmt == TEXFMT_DXT1 else 77
        with tempfile.TemporaryDirectory(prefix="cupx_480p_resize_") as td:
            td = Path(td)
            src = td / "source.dds"; out = td / "out"; out.mkdir()
            src.write_bytes(_make_dx10_dds(sw, sh, blocks, dxgi))
            dds = _run_texconv_resize(src, out, fmt, tw, th)
            ow, oh, new_blocks = _extract_dds_blocks(dds.read_bytes(), fmt)
            if (ow, oh) != (tw, th):
                raise ValueError(f"texconv resize mismatch {(tw, th)} -> {(ow, oh)}")
        out_payload, out_meta = _build_compressed_texture_payload(
            new_blocks, tw, th, fmt,
            has_alpha=bool(meta.get("has_alpha")),
            source_passthrough=False,
            source_format=str(meta.get("format_name") or "CUPT"),
            top_left=bool(flags & TEXFLAG_TOP_LEFT),
        )
        vals = list(TEXTURE_HEADER_V3.unpack_from(out_payload, 0))
        vals[3] = min(tlw, tw); vals[4] = min(tlh, th)
        out_payload = TEXTURE_HEADER_V3.pack(*vals) + out_payload[TEXTURE_HEADER_V3.size:]
    else:
        if image is None:
            raise ValueError("CUPT resize has no decodable image")
        resampling = getattr(Image, "Resampling", Image)
        resized = image.resize((tlw, tlh), resampling.LANCZOS)
        target_fmt = TEXFMT_DXT5 if bool(meta.get("has_alpha")) else TEXFMT_DXT1
        out_payload, out_meta = _encode_image_to_dxt(resized, target_fmt)

    out_meta.update({
        "cupx_480p_downscaled": True,
        "cupx_coordinate_scale": scale,
        "cupx_original_storage_width": sw,
        "cupx_original_storage_height": sh,
        "cupx_original_width": lw,
        "cupx_original_height": lh,
    })
    return out_payload, out_meta


def _type_name(obj):
    t = getattr(obj, "type", None)
    return getattr(t, "name", str(t or "Unknown"))


def _safe_name(value: str) -> str:
    s = (value or "unnamed").strip().replace("\\", "_").replace("/", "_")
    s = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in s)
    while "__" in s:
        s = s.replace("__", "_")
    return s[:96] or "unnamed"


def next_power_of_two(v: int) -> int:
    if v <= 0:
        raise ValueError("Texture dimension must be positive")
    p = 1
    while p < v:
        p <<= 1
    return p


def _edge_replicated_pot_rgba(image: Image.Image):
    rgba = image.convert("RGBA")
    width, height = rgba.size
    storage_width = next_power_of_two(width)
    storage_height = next_power_of_two(height)

    if storage_width == width and storage_height == height:
        return rgba, width, height, storage_width, storage_height

    # Fully replicate the final row/column through the padded region. This avoids
    # filtering against transparent black when UVs approach the logical edge.
    padded = Image.new("RGBA", (storage_width, storage_height), (0, 0, 0, 0))
    padded.paste(rgba, (0, 0))

    if width < storage_width:
        right = rgba.crop((width - 1, 0, width, height))
        right = right.resize((storage_width - width, height), Image.Resampling.NEAREST)
        padded.paste(right, (width, 0))

    if height < storage_height:
        bottom = padded.crop((0, height - 1, storage_width, height))
        bottom = bottom.resize((storage_width, storage_height - height), Image.Resampling.NEAREST)
        padded.paste(bottom, (0, height))

    return padded, width, height, storage_width, storage_height


def build_texture_payload(image: Image.Image):
    """Convert a PIL image to the exact native CUPT v2 payload consumed by Xbox."""
    padded, width, height, storage_width, storage_height = _edge_replicated_pot_rgba(image)

    # A8R8G8B8 on little-endian Xbox memory is B,G,R,A byte order.
    pixels = padded.tobytes("raw", "BGRA")
    row_pitch = storage_width * 4
    alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
    has_alpha = alpha_extrema != (255, 255)

    flags = (
        TEXFLAG_TOP_LEFT |
        TEXFLAG_LINEAR_SOURCE |
        TEXFLAG_POT_PADDED |
        TEXFLAG_EDGE_REPLICATED
    )
    if has_alpha:
        flags |= TEXFLAG_HAS_ALPHA

    header = TEXTURE_HEADER_V2.pack(
        TEXTURE_MAGIC,
        TEXTURE_VERSION,
        TEXTURE_HEADER_V2.size,
        width,
        height,
        storage_width,
        storage_height,
        TEXFMT_A8R8G8B8,
        1,              # mip count; M1 path is base level only
        flags,
        row_pitch,
        len(pixels),
    )

    meta = {
        "native_format": "A8R8G8B8",
        "native_format_id": TEXFMT_A8R8G8B8,
        "width": width,
        "height": height,
        "storage_width": storage_width,
        "storage_height": storage_height,
        "u_max": width / float(storage_width),
        "v_max": height / float(storage_height),
        "mip_count": 1,
        "row_pitch": row_pitch,
        "data_bytes": len(pixels),
        "has_alpha": bool(has_alpha),
        "origin": "top-left",
        "layout": "linear-bgra8-pot",
        "padding": "edge-replicated",
        "payload_version": TEXTURE_VERSION,
        "payload_header_bytes": TEXTURE_HEADER_V2.size,
    }
    return header + pixels, meta


def parse_texture_payload(data: bytes):
    """Read back CUPT v1/v2/v3 payloads for tooling validation/inspection."""
    if len(data) < 8:
        raise ValueError("Truncated CUPT texture header")

    magic, version, header_size = struct.unpack_from("<4sHH", data, 0)
    if magic != TEXTURE_MAGIC:
        raise ValueError("Bad CUPT texture magic")

    if version == 1:
        if len(data) < TEXTURE_HEADER_V1.size:
            raise ValueError("Truncated CUPT v1 header")
        values = TEXTURE_HEADER_V1.unpack_from(data, 0)
        _, _, hs, width, height, fmt, mips, flags, pitch, data_size = values
        storage_width = width
        storage_height = height
        expected_header = TEXTURE_HEADER_V1.size
    elif version in (2, 3):
        header = TEXTURE_HEADER_V2 if version == 2 else TEXTURE_HEADER_V3
        if len(data) < header.size:
            raise ValueError(f"Truncated CUPT v{version} header")
        values = header.unpack_from(data, 0)
        (
            _, _, hs, width, height, storage_width, storage_height,
            fmt, mips, flags, pitch, data_size
        ) = values
        expected_header = header.size
    else:
        raise ValueError(f"Unsupported CUPT texture version {version}")

    if hs != expected_header or header_size != expected_header:
        raise ValueError("Unexpected CUPT texture header size")
    if mips != 1:
        raise ValueError("CUPT texture must contain one mip")
    if width <= 0 or height <= 0 or storage_width < width or storage_height < height:
        raise ValueError("Invalid CUPT dimensions")
    if expected_header + data_size != len(data):
        raise ValueError("CUPT payload size mismatch")

    body = data[expected_header:]
    image = None
    if fmt == TEXFMT_A8R8G8B8:
        if pitch != storage_width * 4:
            raise ValueError("Invalid CUPT row pitch")
        if data_size != storage_width * storage_height * 4:
            raise ValueError("Invalid CUPT pixel byte count")
        storage_image = Image.frombytes(
            "RGBA", (storage_width, storage_height), body, "raw", "BGRA")
        image = storage_image.crop((0, 0, width, height))
        format_name = "A8R8G8B8"
        layout = "linear-bgra8-pot" if version >= 2 else "linear-bgra8"
    elif fmt in (TEXFMT_DXT1, TEXFMT_DXT5):
        expected = _dxt_base_bytes(storage_width, storage_height, fmt)
        block_bytes = 8 if fmt == TEXFMT_DXT1 else 16
        expected_pitch = max(1, (storage_width + 3) // 4) * block_bytes
        if pitch != expected_pitch:
            raise ValueError("Invalid CUPT DXT row pitch")
        if data_size != expected:
            raise ValueError("Invalid CUPT DXT byte count")
        format_name = "DXT1" if fmt == TEXFMT_DXT1 else "DXT5"
        layout = "linear-dxt-blocks"
    else:
        raise ValueError(f"Unsupported CUPT texture format {fmt}")

    meta = {
        "version": version,
        "width": width,
        "height": height,
        "storage_width": storage_width,
        "storage_height": storage_height,
        "u_max": width / float(storage_width),
        "v_max": height / float(storage_height),
        "format": fmt,
        "format_name": format_name,
        "mip_count": mips,
        "flags": flags,
        "row_pitch": pitch,
        "data_bytes": data_size,
        "has_alpha": bool(flags & TEXFLAG_HAS_ALPHA),
        "origin": ("top-left" if (flags & TEXFLAG_TOP_LEFT) else "unity-bottom-left" if (flags & TEXFLAG_UNITY_BOTTOM_LEFT) else "unknown"),
        "layout": layout,
        "source_passthrough": bool(flags & TEXFLAG_SOURCE_PASSTHROUGH),
    }
    return meta, image


def _load_env(path: Path, dependency_mode=False):
    import UnityPy
    env = UnityPy.load(str(path))
    if dependency_mode:
        for af in list(getattr(env, "assets", [])):
            try:
                af.load_dependencies()
            except Exception:
                pass
    return env


def compile_image_as_texture(image: Image.Image, asset_name: str, metadata: dict | None = None):
    payload, meta = build_texture_payload(image)
    if metadata:
        meta.update(metadata)
    return AssetSpec(
        name=asset_name,
        source=None,
        data=payload,
        kind="texture",
        group="diagnostic",
        type=TYPE_TEXTURE,
        metadata=meta,
    )


def _resource_candidates(container_path: Path, stream_path: str):
    """Return deterministic local candidates for a Unity external texture stream.

    Unity 5.x PC builds commonly place Texture2D payloads in sibling
    ``*.resource`` / ``*.resS`` files.  UnityPy normally resolves these itself,
    but some 5.6 retail layouts can hand its resource loader the containing
    directory instead of the actual file.  The full-game builder therefore has
    a narrow local-file fallback rather than treating those textures as failed.
    """
    container_path = Path(container_path)
    parent = container_path.parent
    hint = str(stream_path or "").replace("\\", "/")
    base = ntpath.basename(hint) if hint else ""
    names = []

    def add(name):
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)

    if base:
        add(base)
        stem, _ext = ntpath.splitext(base)
        add(f"{stem}.resource")
        add(f"{stem}.assets.resS")
        add(f"{stem}.resS")

    cbase = container_path.name
    cstem = cbase[:-7] if cbase.lower().endswith(".assets") else cbase
    add(f"{cstem}.resource")
    add(f"{cbase}.resS")
    add(f"{cstem}.resS")

    # resources.assets in Cuphead 5.6 conventionally streams from resources.resource.
    if cbase.lower() == "resources.assets":
        add("resources.resource")

    out = []
    seen = set()
    for name in names:
        candidate = parent / name
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def _mapping_value(mapping, *names, default=None):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name in mapping:
            return mapping[name]
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        if str(name).lower() in lowered:
            return lowered[str(name).lower()]
    return default


def _stream_info(data, obj=None):
    """Return (path, offset, size) from generated object or raw typetree.

    A small set of Cuphead Texture2Ds parse with an unusable/empty generated
    ``m_StreamData`` on some UnityPy versions.  The serialized typetree still
    contains the correct values, so recover them before giving up.
    """
    stream = getattr(data, "m_StreamData", None)

    def from_value(value):
        if value is None:
            return "", 0, 0
        if isinstance(value, dict):
            path = _mapping_value(value, "path", "m_Path", default="") or ""
            offset = _mapping_value(value, "offset", "m_Offset", default=0) or 0
            size = _mapping_value(value, "size", "m_Size", default=0) or 0
        else:
            path = getattr(value, "path", "") or getattr(value, "m_Path", "") or ""
            offset = getattr(value, "offset", 0) or getattr(value, "m_Offset", 0) or 0
            size = getattr(value, "size", 0) or getattr(value, "m_Size", 0) or 0
        try:
            offset = int(offset)
        except Exception:
            offset = 0
        try:
            size = int(size)
        except Exception:
            size = 0
        return str(path or ""), max(0, offset), max(0, size)

    path, offset, size = from_value(stream)
    if size > 0:
        return path, offset, size

    if obj is not None:
        for method in ("parse_as_dict", "read_typetree"):
            fn = getattr(obj, method, None)
            if not fn:
                continue
            try:
                raw = fn()
            except Exception:
                continue
            raw_stream = _mapping_value(raw, "m_StreamData", "streamData", "m_Streamdata")
            rpath, roffset, rsize = from_value(raw_stream)
            if rsize > 0:
                return rpath, roffset, rsize

    # Last-resort size hint for old serialized Texture2Ds whose stream path is
    # present but the generated StreamingInfo omitted size.  This value is the
    # encoded image byte count in Unity player data, not width*height*4.
    try:
        complete = int(getattr(data, "m_CompleteImageSize", 0) or 0)
    except Exception:
        complete = 0
    if complete > 0 and stream is not None:
        return path, offset, complete
    return path, offset, size


def _broad_resource_candidates(container_path: Path, exact_candidates):
    out = list(exact_candidates)
    seen = {str(x).lower() for x in out}
    parent = Path(container_path).parent
    try:
        for candidate in sorted(parent.iterdir(), key=lambda p: p.name.lower()):
            if not candidate.is_file():
                continue
            low = candidate.name.lower()
            if not (low.endswith(".resource") or low.endswith(".ress")):
                continue
            key = str(candidate).lower()
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    except OSError:
        pass
    return out


def _texture_encoded_bytes_with_fallback(data, container_path: Path | None = None, obj=None):
    """Return Unity's encoded Texture2D byte stream without decoding pixels.

    Production Cuphead atlases are commonly already DXT1/DXT5.  Preserving the
    encoded blocks avoids the former 4x RGBA expansion, removes a decode/re-encode
    step, and keeps the source artwork exactly as Unity stored it.
    """
    errors = []
    getter = getattr(data, "get_image_data", None)
    if getter:
        try:
            raw = getter()
            if raw:
                return bytes(raw), "unitypy-encoded"
        except Exception as e:
            errors.append(str(e))

    inline = getattr(data, "image_data", None)
    if inline:
        try:
            raw = bytes(inline)
            if raw:
                return raw, "inline-encoded"
        except Exception as e:
            errors.append(str(e))

    if container_path is not None:
        stream_path, offset, size = _stream_info(data, obj)
        if size > 0:
            exact = _resource_candidates(Path(container_path), stream_path)
            candidates = _broad_resource_candidates(Path(container_path), exact)
            for candidate in candidates:
                try:
                    if not candidate.is_file():
                        continue
                    if offset + size > candidate.stat().st_size:
                        continue
                    with candidate.open("rb") as f:
                        f.seek(offset)
                        raw = f.read(size)
                    if len(raw) == size:
                        return raw, f"direct-resource-encoded:{candidate.name}"
                except OSError as e:
                    errors.append(str(e))

    raise ValueError("Texture2D encoded source unavailable" + (": " + " | ".join(errors[-3:]) if errors else ""))



def _texture_image_from_encoded_safe(data, container_path: Path | None = None, obj=None):
    """Decode common Unity PC formats without calling UnityPy's image decoder.

    This intentionally handles the small uncompressed formats needed by the
    frontend and uses only Pillow raw decoders.  Production BC formats are
    handled by DirectXTex before this function is reached.
    """
    width = int(getattr(data, "m_Width", 0) or 0)
    height = int(getattr(data, "m_Height", 0) or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Texture2D has invalid dimensions")
    source_fmt = _texture_format_int(getattr(data, "m_TextureFormat", 0))
    raw, raw_source = _texture_encoded_bytes_with_fallback(data, container_path, obj=obj)

    table = {
        UNITY_TEXFMT_ALPHA8: (1, "RGBA", "A"),
        UNITY_TEXFMT_RGB24: (3, "RGB", "RGB"),
        UNITY_TEXFMT_RGBA32: (4, "RGBA", "RGBA"),
        UNITY_TEXFMT_ARGB32: (4, "RGBA", "ARGB"),
        UNITY_TEXFMT_RGB565: (2, "RGB", "BGR;16"),
        UNITY_TEXFMT_BGR24: (3, "RGB", "BGR"),
        UNITY_TEXFMT_RGBA4444: (2, "RGBA", "RGBA;4B"),
        UNITY_TEXFMT_BGRA32: (4, "RGBA", "BGRA"),
        UNITY_TEXFMT_R8: (1, "RGB", "R"),
    }
    spec = table.get(source_fmt)
    if spec is None:
        raise ValueError(
            f"production-safe decoder does not support Unity TextureFormat {source_fmt}; "
            "refusing unstable UnityPy native image decode"
        )
    bpp, mode, rawmode = spec
    expected = width * height * bpp
    if len(raw) < expected:
        raise ValueError(f"Texture2D raw source truncated: expected {expected:,}, got {len(raw):,}")
    image = Image.frombytes(mode, (width, height), bytes(raw[:expected]), "raw", rawmode)
    if mode != "RGBA":
        image = image.convert("RGBA")
    # Unity texture coordinates are bottom-left; CUPT production images are
    # normalized to top-left for the Xbox runtime.
    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return image, f"safe-encoded:{raw_source}"

def build_production_texture_payload(data, container_path: Path | None = None, obj=None, force_rgba: bool = False, preserve_unity_origin: bool = False):
    """Build the production CUPT payload without UnityPy native BC decoding.

    DXT1/DXT5 are preserved as source blocks.  BC4/5/6H/7 are wrapped as DDS
    and transcoded by the pinned Microsoft DirectXTex texconv executable.  The
    common uncompressed Unity formats are decoded directly from their encoded
    byte stream with Pillow raw decoders.  This removes the crash-prone path
    that terminated 0.8.0/0.8.1 workers on retail Cuphead atlas bundles.
    """
    width = int(getattr(data, "m_Width", 0) or 0)
    height = int(getattr(data, "m_Height", 0) or 0)
    if width <= 0 or height <= 0:
        raise ValueError("Texture2D has invalid dimensions")
    source_value = getattr(data, "m_TextureFormat", 0)
    source_fmt = _texture_format_int(source_value)
    source_fmt_name = str(source_value)

    # Retail DXT pages can go straight to NV2A when already POT. NPOT DXT
    # textures are common in resources.assets/sharedassets2.assets; those were
    # the last blocker in the 0.8.3 frontend report. Decode them out-of-process
    # through DirectXTex, edge-pad to POT storage, and re-encode to the same DXT
    # family. This keeps the production path entirely off UnityPy's native BCN
    # decoder while preserving logical dimensions in CUPT v3.
    if source_fmt in (UNITY_TEXFMT_DXT1, UNITY_TEXFMT_DXT5):
        raw, raw_source = _texture_encoded_bytes_with_fallback(data, container_path, obj=obj)
        native_fmt = TEXFMT_DXT1 if source_fmt == UNITY_TEXFMT_DXT1 else TEXFMT_DXT5
        if _is_power_of_two(width) and _is_power_of_two(height):
            payload, meta = _build_compressed_texture_payload(
                raw, width, height, native_fmt,
                has_alpha=(native_fmt == TEXFMT_DXT5),
                source_passthrough=True,
                source_format=source_fmt_name,
                top_left=False,
            )
            meta["decode_source"] = raw_source
            meta["conversion"] = "source-block-passthrough"
            return payload, meta

        image = _decode_unity_dxt_to_image_safe(raw, width, height, source_fmt)
        payload, meta = _encode_image_to_dxt(image, native_fmt)
        meta.update({
            "decode_source": raw_source,
            "conversion": "directxtex-npot-dxt-repack",
            "source_texture_format": source_fmt_name,
            "source_dimensions": [width, height],
        })
        return payload, meta

    # Cuphead's current PC atlas bundles are predominantly BC7. Never decode
    # them through UnityPy/Pillow inside the mastering process. DirectXTex reads
    # the exact validated block stream and produces DXT5 with a vertical flip,
    # preserving alpha while shrinking storage to 1 byte/pixel.
    if source_fmt in (UNITY_TEXFMT_BC4, UNITY_TEXFMT_BC5, UNITY_TEXFMT_BC6H, UNITY_TEXFMT_BC7):
        raw, raw_source = _texture_encoded_bytes_with_fallback(data, container_path, obj=obj)
        blocks = _transcode_unity_bc_to_dxt5(
            raw, width, height, source_fmt,
            preserve_unity_origin=bool(preserve_unity_origin),
        )
        payload, meta = _build_compressed_texture_payload(
            blocks, width, height, TEXFMT_DXT5,
            has_alpha=True,
            source_passthrough=False,
            source_format=source_fmt_name,
            top_left=not bool(preserve_unity_origin),
        )
        meta.update({
            "decode_source": raw_source,
            "conversion": (
                "directxtex-bc-to-dxt5-unity-origin"
                if preserve_unity_origin else
                "directxtex-bc-to-dxt5"
            ),
            "encoder": "Microsoft DirectXTex texconv",
            "preserved_unity_origin": bool(preserve_unity_origin),
        })
        return payload, meta

    # Crunched formats are uncommon in the title slice. Keep their existing
    # compatibility path, but a failure is a normal Python conversion error
    # rather than a reason to crash an entire worker pool.
    if source_fmt in (UNITY_TEXFMT_DXT1_CRUNCHED, UNITY_TEXFMT_DXT5_CRUNCHED):
        image, decode_source = _texture_image_with_fallback(data, container_path, obj=obj)
        native_fmt = TEXFMT_DXT1 if source_fmt == UNITY_TEXFMT_DXT1_CRUNCHED else TEXFMT_DXT5
        payload, meta = _encode_image_to_dxt(image, native_fmt)
        meta.update({
            "decode_source": decode_source,
            "conversion": "crunched-to-directxtex-dxt",
            "source_texture_format": source_fmt_name,
        })
        return payload, meta

    # Common PC uncompressed formats are decoded from validated encoded bytes;
    # do not call data.image here.
    image, decode_source = _texture_image_from_encoded_safe(data, container_path, obj=obj)
    storage_w = next_power_of_two(width)
    storage_h = next_power_of_two(height)
    rgba_bytes = storage_w * storage_h * 4

    if force_rgba or rgba_bytes <= PRODUCTION_KEEP_RGBA_MAX_BYTES:
        payload, meta = build_texture_payload(image)
        meta.update({
            "decode_source": decode_source,
            "conversion": ("rgba-safe-recovery" if force_rgba else "rgba-preserved-small"),
            "source_texture_format": source_fmt_name,
            "production_storage_degraded": bool(force_rgba),
        })
        return payload, meta

    alpha_extrema = image.convert("RGBA").getchannel("A").getextrema()
    native_fmt = TEXFMT_DXT1 if alpha_extrema == (255, 255) else TEXFMT_DXT5
    payload, meta = _encode_image_to_dxt(image, native_fmt)
    meta.update({
        "decode_source": decode_source,
        "conversion": "directxtex-dxt-compress",
        "source_texture_format": source_fmt_name,
    })
    return payload, meta

def _texture_image_with_fallback(data, container_path: Path | None = None, obj=None):
    """Decode a Texture2D, directly reading its stream file if UnityPy cannot.

    The normal ``Texture2D.image`` path remains preferred. The fallback also
    recovers StreamingInfo from the raw typetree and, when necessary, probes
    sibling ``.resource``/``.resS`` files using UnityPy's own decoder.
    """
    normal_error = None
    try:
        image = data.image
        if image is not None:
            return image, "unitypy"
    except Exception as e:
        normal_error = e

    if container_path is None:
        if normal_error:
            raise normal_error
        raise ValueError("UnityPy returned no image for Texture2D")

    stream_path, offset, size = _stream_info(data, obj)
    if size <= 0:
        detail = f"; stream={stream_path!r} offset={offset} size={size}"
        if normal_error:
            raise ValueError(f"{normal_error}{detail}") from normal_error
        raise ValueError("Texture2D has no recoverable external stream data" + detail)

    exact = _resource_candidates(Path(container_path), stream_path)
    candidates = _broad_resource_candidates(Path(container_path), exact)

    from UnityPy.export import Texture2DConverter
    reader = getattr(data, "object_reader", None)
    decode_errors = []
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            file_size = candidate.stat().st_size
            if offset + size > file_size:
                continue
            with candidate.open("rb") as f:
                f.seek(offset)
                raw = f.read(size)
            if len(raw) != size:
                continue
            try:
                image = Texture2DConverter.parse_image_data(
                    raw,
                    int(getattr(data, "m_Width", 0) or 0),
                    int(getattr(data, "m_Height", 0) or 0),
                    getattr(data, "m_TextureFormat", 0),
                    getattr(reader, "version", (0, 0, 0, 0)),
                    getattr(reader, "platform", 0),
                    getattr(data, "m_PlatformBlob", None),
                    True,
                )
                if image is not None:
                    return image, f"direct-resource:{candidate.name}"
            except Exception as e:
                decode_errors.append(f"{candidate.name}: {e}")
        except OSError:
            continue

    detail = (
        f"stream={stream_path!r} offset={offset} size={size}; "
        f"candidates={len(candidates)}"
    )
    if decode_errors:
        detail += "; decode=" + " | ".join(decode_errors[-3:])
    if normal_error:
        raise ValueError(f"Texture2D resource recovery failed ({detail}); UnityPy: {normal_error}") from normal_error
    raise FileNotFoundError(f"Texture2D external resource not found ({detail})")


def _compile_texture_object(obj, rel_container: str, container_path: Path | None = None, production: bool = False, force_rgba: bool = False, preserve_unity_origin: bool = False):
    if _type_name(obj) != "Texture2D":
        raise TypeError("Unity object is not Texture2D")

    data = obj.read()
    unity_name = str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "")
    path_id = int(getattr(obj, "path_id", 0) or 0)
    if production:
        payload, meta = build_production_texture_payload(
            data, container_path, obj=obj, force_rgba=force_rgba,
            preserve_unity_origin=preserve_unity_origin,
        )
        meta.update({
            "source_container": rel_container.replace("\\", "/"),
            "source_path_id": path_id,
            "source_name": unity_name,
            "source_type": "Texture2D",
        })
        return AssetSpec(
            name=(
                f"unity/{rel_container.replace('\\', '/')}/texture2d/"
                f"{path_id}/{_safe_name(unity_name)}"
            ),
            source=None,
            data=payload,
            kind="texture",
            group="diagnostic",
            type=TYPE_TEXTURE,
            metadata=meta,
        )

    image, decode_source = _texture_image_with_fallback(data, container_path, obj=obj)
    if image is None:
        raise ValueError("UnityPy returned no image for Texture2D")

    return compile_image_as_texture(
        image,
        (
            f"unity/{rel_container.replace('\\', '/')}/texture2d/"
            f"{path_id}/{_safe_name(unity_name)}"
        ),
        {
            "source_container": rel_container.replace("\\", "/"),
            "source_path_id": path_id,
            "source_name": unity_name,
            "source_texture_format": str(getattr(data, "m_TextureFormat", "")),
            "source_type": "Texture2D",
            "decode_source": decode_source,
        },
    )


def compile_texture2d(container: Path, path_id: int, asset_name: str | None = None):
    """Compile one Texture2D selected by PathID. Primarily useful for CLI inspection."""
    container = Path(container)
    env = _load_env(container)
    target = None
    for obj in env.objects:
        if int(getattr(obj, "path_id", 0) or 0) == int(path_id):
            target = obj
            break
    if target is None:
        raise KeyError(f"PathID {path_id} not found")
    spec = _compile_texture_object(target, container.name, container_path=container)
    if asset_name:
        spec.name = asset_name
    return spec


def _candidate_unity_rows(rows):
    for row in sorted(rows, key=lambda r: r["path"].lower()):
        if row.get("kind") != "unity":
            continue
        p = row["path"].lower()
        if p.endswith(".resource") or p.endswith(".ress"):
            continue
        yield row


def discover_texture_specs(
    root: Path,
    rows,
    limit: int = DEFAULT_TEXTURE_COUNT,
    min_dimension: int = DEFAULT_MIN_DIMENSION,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES,
):
    """Find and compile deterministic, real Cuphead Texture2D assets for M1."""
    root = Path(root)
    if limit <= 0:
        return [], []

    specs = []
    notes = []

    for row in _candidate_unity_rows(rows):
        rel = row["path"]
        path = root / rel
        try:
            env = _load_env(path)
        except Exception as e:
            if len(notes) < 24:
                notes.append(f"texture skip {rel}: UnityPy.load: {e}")
            continue

        for obj in env.objects:
            if _type_name(obj) != "Texture2D":
                continue
            try:
                data = obj.read()
                w = int(getattr(data, "m_Width", 0) or 0)
                h = int(getattr(data, "m_Height", 0) or 0)
                if w < min_dimension or h < min_dimension:
                    continue
                if w > max_dimension or h > max_dimension:
                    continue
                if next_power_of_two(w) * next_power_of_two(h) * 4 > max_decoded_bytes:
                    continue
                spec = _compile_texture_object(obj, rel, container_path=path)
                specs.append(spec)
                if len(specs) >= limit:
                    return specs, notes
            except Exception as e:
                if len(notes) < 24:
                    pid = int(getattr(obj, "path_id", 0) or 0)
                    notes.append(f"texture skip {rel} PathID {pid}: {e}")

    return specs, notes


def make_checkerboard_spec(size: int = 64):
    """Known-good control texture so Xbox renderer can be isolated from Unity extraction."""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    px = im.load()
    cell = max(4, size // 8)
    for y in range(size):
        for x in range(size):
            on = ((x // cell) + (y // cell)) & 1
            if on:
                px[x, y] = (235, 235, 235, 255)
            else:
                px[x, y] = (92, 28, 128, 255)

    return compile_image_as_texture(
        im,
        "diagnostic/native_checkerboard",
        {"generated": True, "purpose": "native renderer control texture"},
    )