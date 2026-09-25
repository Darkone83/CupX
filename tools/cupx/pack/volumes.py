from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import hashlib
from .io import build_package, read_package_header, verify_package_detailed, PackageAsset, FileSlice
from .format import TYPE_RAW

DEFAULT_VOLUME_LIMIT = 1024 * 1024 * 1024       # 1 GiB
HARD_VOLUME_LIMIT = 0xF0000000                   # 3.75 GiB safety ceiling
PACK_OVERHEAD = 1024 * 1024                     # conservative index/alignment budget


@dataclass
class AssetSpec:
    name: str
    source: str | FileSlice | None
    kind: str = "raw"
    group: str = "common"
    type: int = TYPE_RAW
    dependencies: list[int] | None = None
    data: bytes | None = None
    metadata: dict | None = None


def _sanitize_full_name(s):
    out = "".join(c.lower() if c.isalnum() else "_" for c in str(s)).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or "pack"


def sanitize_name(s):
    # General short identifier used by manifests/build IDs.  Package filenames
    # need stronger collision handling; see _volume_group_stem below.
    return _sanitize_full_name(s)[:24]


def _volume_group_stem(group: str, used: dict[str, str]) -> str:
    """Return a deterministic FATX-safe package stem unique within this set.

    FATX filenames are limited to 42 characters.  ``_000.cupx`` consumes 9,
    leaving up to 33 characters for the group stem.  Older CUPX builds simply
    truncated groups to 24 characters, which made Cuphead's many
    ``scene_level_dice_palace_*`` groups overwrite one another.

    Keep short readable names unchanged.  Long names, or rare sanitizer
    collisions, receive an 8-hex SHA-1 suffix while remaining within FATX's
    filename limit.
    """
    full = _sanitize_full_name(group)
    stem = full[:24]
    owner = used.get(stem)
    needs_hash = len(full) > 24 or (owner is not None and owner != group)
    if needs_hash:
        digest = hashlib.sha1(str(group).encode("utf-8")).hexdigest()[:8]
        # 24 + '_' + 8 = 33 chars; + '_000.cupx' = 42 exactly.
        stem = f"{full[:24]}_{digest}"
        owner = used.get(stem)
        if owner is not None and owner != group:
            # Extremely unlikely SHA-1-prefix collision.  Fall back to a
            # longer digest and shorter readable prefix, still <= 33 chars.
            digest = hashlib.sha1(str(group).encode("utf-8")).hexdigest()[:12]
            stem = f"{full[:20]}_{digest}"
            owner = used.get(stem)
            if owner is not None and owner != group:
                raise ValueError(f"Unable to create unique CUPX filename for group: {group}")
    used[stem] = group
    return stem


def _spec_payload(root: Path, spec: AssetSpec):
    if spec.data is not None:
        return spec.data
    if not spec.source:
        raise ValueError(f"Asset has neither source nor compiled data: {spec.name}")
    if isinstance(spec.source, FileSlice):
        return spec.source
    return root / spec.source


def _spec_size(root: Path, spec: AssetSpec):
    src = _spec_payload(root, spec)
    if isinstance(src, FileSlice):
        return int(src.size)
    if isinstance(src, Path):
        return src.stat().st_size
    return len(src)


def split_assets(root: Path, specs: list[AssetSpec], max_bytes: int):
    if max_bytes <= PACK_OVERHEAD:
        raise ValueError("Volume size is too small")
    if max_bytes > HARD_VOLUME_LIMIT:
        raise ValueError("Volume size exceeds CUPX FATX safety ceiling")
    vols = []
    cur = []
    used = PACK_OVERHEAD
    for s in specs:
        size = _spec_size(root, s)
        if size + PACK_OVERHEAD > max_bytes:
            raise ValueError(f"Single asset cannot fit a volume: {s.name} ({size:,} bytes)")
        if cur and used + size > max_bytes:
            vols.append(cur)
            cur = []
            used = PACK_OVERHEAD
        cur.append(s)
        used += size
    if cur:
        vols.append(cur)
    return vols


def build_asset_set(
    source_root: Path,
    output_dir: Path,
    groups: dict[str, list[AssetSpec]],
    max_volume_bytes=DEFAULT_VOLUME_LIMIT,
    set_name="cuphead",
    manifest_extra: dict | None = None,
):
    source_root = Path(source_root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": "CUPM",
        "version": 1,
        "set": set_name,
        "max_volume_bytes": max_volume_bytes,
        "groups": [],
        "assets": {},
    }
    if manifest_extra:
        for key, value in manifest_extra.items():
            if key in manifest:
                raise ValueError(f"Manifest extra field collides with reserved key: {key}")
            manifest[key] = value

    used_group_stems = {}
    for group, specs in groups.items():
        vols = split_assets(source_root, specs, max_volume_bytes)
        g = {"name": group, "volumes": []}
        group_stem = _volume_group_stem(group, used_group_stems)
        for vi, items in enumerate(vols):
            fn = f"{group_stem}_{vi:03d}.cupx"
            out = output_dir / fn
            payload = [
                PackageAsset(
                    name=s.name,
                    type=s.type,
                    source=_spec_payload(source_root, s),
                )
                for s in items
            ]
            entries = build_package(
                out, payload, build_id=f"CUPX084:{group_stem}:{vi}")
            # build_package already performed full temp-file CRC verification;
            # only read the published header here to populate the manifest.
            info = read_package_header(out)
            g["volumes"].append({
                "file": fn,
                "bytes": info["size"],
                "crc32": f'{info["package_crc32"]:08X}',
                "entries": len(entries),
            })
            for s, e in zip(items, entries):
                rec = {
                    "name": s.name,
                    "kind": s.kind,
                    "type": s.type,
                    "group": group,
                    "volume": vi,
                    "file": fn,
                    "offset": e.offset,
                    "stored_size": e.stored_size,
                    "decoded_size": e.decoded_size,
                    "crc32": f"{e.crc32:08X}",
                    "dependencies": [f"{x:08X}" for x in (s.dependencies or [])],
                }
                if s.metadata:
                    rec["metadata"] = s.metadata
                manifest["assets"][f"{e.asset_id:08X}"] = rec
        manifest["groups"].append(g)

    mp = output_dir / f"{sanitize_name(set_name)}.cupm"
    mp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return mp, manifest


def verify_asset_set(manifest_path: Path, write_report=True, report_dir: Path | None = None):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    base = Path(manifest_path).parent
    checked = []
    type_counts = {}
    detailed_volumes = []
    asset_names = manifest.get("assets", {})

    for g in manifest["groups"]:
        for v in g["volumes"]:
            p = base / v["file"]
            detail = verify_package_detailed(p)
            if detail["size"] != v["bytes"]:
                raise ValueError(f"Size mismatch: {p.name} manifest={v['bytes']} disk={detail['size']}")
            if f'{detail["package_crc32"]:08X}' != v["crc32"]:
                raise ValueError(f"Header/manifest CRC mismatch: {p.name}")
            if not detail["ok"]:
                raise ValueError(f"Xbox-reference verification failed: {p.name}")

            entries = []
            for c in detail["entry_checks"]:
                aid = f'{c["asset_id"]:08X}'
                rec = asset_names.get(aid, {})
                entries.append({
                    "index": c["index"],
                    "asset_id": aid,
                    "name": rec.get("name", ""),
                    "type": c["type"],
                    "offset": c["offset"],
                    "stored_size": c["stored_size"],
                    "expected_crc32": f'{c["expected_crc32"]:08X}',
                    "zlib_crc32": f'{c["zlib_crc32"]:08X}',
                    "xbox_crc32": f'{c["xbox_crc32"]:08X}',
                    "ok": c["ok"],
                })

            detailed_volumes.append({
                "file": p.name,
                "bytes": detail["size"],
                "header_crc32": f'{detail["package_crc32"]:08X}',
                "zlib_crc32": f'{detail["package_zlib_crc32"]:08X}',
                "xbox_crc32": f'{detail["package_xbox_crc32"]:08X}',
                "ok": detail["ok"],
                "entries": entries,
            })
            checked.append(p.name)

    for a in manifest["assets"].values():
        key = str(a.get("kind", "unknown"))
        type_counts[key] = type_counts.get(key, 0) + 1

    result = {
        "manifest": str(manifest_path),
        "volumes": len(checked),
        "files": checked,
        "assets": len(manifest["assets"]),
        "asset_kinds": type_counts,
        "xbox_reference_verified": True,
        "verification_mode": "fast-ieee-crc32/reference-selftested",
        "volume_details": detailed_volumes,
    }
    if "diagnostic_tests" in manifest:
        result["diagnostic_tests"] = manifest["diagnostic_tests"]

    if write_report:
        report_base = Path(report_dir) if report_dir is not None else base
        report_base.mkdir(parents=True, exist_ok=True)
        report_path = report_base / (Path(manifest_path).stem + ".transfer_verify.json")
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["transfer_report"] = str(report_path)

    return result
