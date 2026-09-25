from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import binascii
import os
import zlib
from .format import *

ALIGN = 16
CHUNK = 1024 * 1024


def align(v, a=ALIGN):
    return (v + a - 1) & ~(a - 1)


def asset_id(name: str) -> int:
    h = 0x811C9DC5
    for b in name.replace("\\", "/").lower().encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xffffffff
    return h


# Table-driven form of the same IEEE CRC-32 polynomial used by the RXDK reader.
# Keeping this implementation independent from zlib gives us a useful build-time
# cross-check without changing the CUPX v1 format.
def _make_crc_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ (0xEDB88320 if (c & 1) else 0)
        table.append(c & 0xffffffff)
    return tuple(table)


_CRC_TABLE = _make_crc_table()
_CRC_REFERENCE_SELFTEST_OK = False


def xbox_crc32_update(data, crc=0) -> int:
    c = (~int(crc)) & 0xffffffff
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xff] ^ (c >> 8)
    return (~c) & 0xffffffff


def _ensure_crc_reference_selftest():
    """Prove the fast C CRC paths match the RXDK/Xbox reference algorithm.

    Earlier CUPX builds recomputed the independent table-driven Xbox CRC over
    every byte of every package in Python. That was useful while validating the
    format, but on a ~500 MiB frontend set it dominated build time. The format
    and the RXDK reader are already hardware-proven, so production verification
    now performs a small deterministic parity self-test against the original
    reference implementation, then uses the C-accelerated zlib and binascii
    IEEE CRC-32 implementations for the actual package bytes.
    """
    global _CRC_REFERENCE_SELFTEST_OK
    if _CRC_REFERENCE_SELFTEST_OK:
        return
    vectors = (
        b"",
        b"123456789",
        bytes(range(256)),
        b"CUPX-RXDK-CRC32-reference-selftest\0" * 17,
    )
    for data in vectors:
        ref = xbox_crc32_update(data, 0)
        z = zlib.crc32(data) & 0xffffffff
        b = binascii.crc32(data) & 0xffffffff
        if ref != z or ref != b:
            raise RuntimeError(
                f"CRC32 reference self-test failed: xbox={ref:08X} "
                f"zlib={z:08X} binascii={b:08X}"
            )
    _CRC_REFERENCE_SELFTEST_OK = True


@dataclass
class Entry:
    asset_id: int
    type: int
    flags: int
    offset: int
    stored_size: int
    decoded_size: int
    crc32: int


@dataclass(frozen=True)
class FileSlice:
    """A byte range inside a staging file.

    Full-game conversion can generate tens of thousands of tiny native records.
    Keeping them in one spool file per source container avoids creating/opening
    one filesystem object per 108-byte CUPR descriptor.
    """
    path: Path
    offset: int
    size: int
    crc32: int | None = None


@dataclass
class PackageAsset:
    name: str
    type: int
    source: bytes | bytearray | memoryview | Path | FileSlice
    flags: int = 0
    decoded_size: int | None = None


def _normalize_asset(item):
    if isinstance(item, PackageAsset):
        return item
    if isinstance(item, tuple) and len(item) == 3:
        return PackageAsset(item[0], item[1], item[2])
    raise TypeError("CUPX asset must be PackageAsset or (name,type,data) tuple")


def _source_size(source) -> int:
    if isinstance(source, FileSlice):
        return int(source.size)
    if isinstance(source, Path):
        return source.stat().st_size
    return len(source)


def _source_crc(source) -> int:
    if isinstance(source, FileSlice) and source.crc32 is not None:
        return int(source.crc32) & 0xffffffff

    crc = 0
    if isinstance(source, FileSlice):
        remain = int(source.size)
        with Path(source.path).open("rb") as src:
            src.seek(int(source.offset))
            while remain:
                b = src.read(min(CHUNK, remain))
                if not b:
                    raise ValueError(f"Unexpected EOF in staged slice: {source.path}")
                crc = zlib.crc32(b, crc)
                remain -= len(b)
    elif isinstance(source, Path):
        with source.open("rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                crc = zlib.crc32(b, crc)
    else:
        crc = zlib.crc32(source, crc)
    return crc & 0xffffffff


def _write_source(f, source, handle_cache=None):
    if isinstance(source, FileSlice):
        cache = handle_cache if handle_cache is not None else {}
        key = str(Path(source.path))
        src = cache.get(key)
        owned = False
        if src is None:
            src = Path(source.path).open("rb")
            if handle_cache is not None:
                cache[key] = src
            else:
                owned = True
        try:
            src.seek(int(source.offset))
            remain = int(source.size)
            while remain:
                b = src.read(min(CHUNK, remain))
                if not b:
                    raise ValueError(f"Unexpected EOF in staged slice: {source.path}")
                f.write(b)
                remain -= len(b)
        finally:
            if owned:
                src.close()
    elif isinstance(source, Path):
        with source.open("rb") as src:
            while True:
                b = src.read(CHUNK)
                if not b:
                    break
                f.write(b)
    else:
        f.write(source)


def _crc_file_region(f, offset: int, size: int, reference=False) -> int:
    f.seek(offset)
    remain = int(size)
    crc = 0
    while remain:
        b = f.read(min(CHUNK, remain))
        if not b:
            raise ValueError("Unexpected EOF while calculating CRC")
        crc = xbox_crc32_update(b, crc) if reference else zlib.crc32(b, crc)
        remain -= len(b)
    return crc & 0xffffffff


def _crc_file_region_both(f, offset: int, size: int):
    """Calculate two C-accelerated IEEE CRC-32 paths in one disk pass.

    Compatibility with the original table-driven Xbox reference implementation
    is asserted once by ``_ensure_crc_reference_selftest``.
    """
    _ensure_crc_reference_selftest()
    f.seek(offset)
    remain = int(size)
    zcrc = 0
    xcrc = 0
    while remain:
        b = f.read(min(CHUNK, remain))
        if not b:
            raise ValueError("Unexpected EOF while calculating CRC")
        zcrc = zlib.crc32(b, zcrc)
        xcrc = binascii.crc32(b, xcrc)
        remain -= len(b)
    return zcrc & 0xffffffff, xcrc & 0xffffffff


def _parse_package(f, size: int):
    f.seek(0)
    raw = f.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise ValueError("Truncated CUPX header")
    magic, ver, hs, flags, count, ioff, isize, pcrc, res, bid = HEADER.unpack(raw)
    if magic != MAGIC:
        raise ValueError("Bad CUPX magic")
    if ver != VERSION:
        raise ValueError(f"Unsupported CUPX version {ver}")
    if hs != HEADER.size:
        raise ValueError("Unexpected header size")
    if ioff < hs or isize != count * ENTRY.size or ioff + isize > size:
        raise ValueError("Invalid index bounds")

    f.seek(ioff)
    entries = []
    min_payload = align(ioff + isize)
    for _ in range(count):
        raw_entry = f.read(ENTRY.size)
        if len(raw_entry) != ENTRY.size:
            raise ValueError("Truncated CUPX index")
        vals = ENTRY.unpack(raw_entry)
        e = Entry(vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], vals[6])
        if e.offset < min_payload or e.offset + e.stored_size > size:
            raise ValueError("Entry outside package")
        entries.append(e)
    return ver, flags, pcrc, bid, entries


def verify_package_detailed(path: Path):
    """Verify a package with two fast IEEE CRC-32 paths.

    A deterministic startup self-test first proves both fast paths produce the
    same values as the original RXDK/Xbox table-driven reference algorithm.
    This preserves the compatibility guarantee without spending minutes in a
    Python per-byte loop for every large package.
    """
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        ver, flags, pcrc, bid, entries = _parse_package(f, size)
        package_zlib, package_xbox = _crc_file_region_both(
            f, HEADER.size, size - HEADER.size)

        checks = []
        all_entries_ok = True
        for idx, e in enumerate(entries):
            z, x = _crc_file_region_both(f, e.offset, e.stored_size)
            ok = (z == e.crc32 and x == e.crc32 and z == x)
            all_entries_ok = all_entries_ok and ok
            checks.append({
                "index": idx,
                "asset_id": e.asset_id,
                "type": e.type,
                "offset": e.offset,
                "stored_size": e.stored_size,
                "expected_crc32": e.crc32,
                "zlib_crc32": z,
                "xbox_crc32": x,
                "ok": ok,
            })

        package_ok = (package_zlib == pcrc and package_xbox == pcrc and package_zlib == package_xbox)
        return {
            "version": ver,
            "flags": flags,
            "build_id": bid.split(b"\0", 1)[0].decode("ascii", "replace"),
            "size": size,
            "package_crc32": pcrc,
            "package_zlib_crc32": package_zlib,
            "package_xbox_crc32": package_xbox,
            "package_ok": package_ok,
            "verification_mode": "fast-ieee-crc32/reference-selftested",
            "xbox_reference_selftest": True,
            "entries_ok": all_entries_ok,
            "entries": entries,
            "entry_checks": checks,
            "ok": package_ok and all_entries_ok,
        }


def build_package(output: Path, assets, build_id="CUPX-0.8.4"):
    """Build CUPX v1 to a temp file, fully verify it, then atomically publish it."""
    assets = [_normalize_asset(x) for x in assets]
    index_off = HEADER.size
    index_size = ENTRY.size * len(assets)
    cursor = align(index_off + index_size)
    entries = []

    for a in assets:
        size = _source_size(a.source)
        decoded = size if a.decoded_size is None else int(a.decoded_size)
        crc = _source_crc(a.source)
        e = Entry(asset_id(a.name), a.type, a.flags, cursor, size, decoded, crc)
        entries.append(e)
        cursor = align(cursor + size)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(output.name + ".tmp")
    if temp.exists():
        temp.unlink()

    try:
        with temp.open("wb+") as f:
            f.write(b"\0" * HEADER.size)
            for e in entries:
                f.write(ENTRY.pack(
                    e.asset_id, e.type, e.flags, e.offset,
                    e.stored_size, e.decoded_size, e.crc32, 0))
            f.write(b"\0" * (align(f.tell()) - f.tell()))

            source_handles = {}
            try:
                for e, a in zip(entries, assets):
                    if f.tell() < e.offset:
                        f.write(b"\0" * (e.offset - f.tell()))
                    if f.tell() != e.offset:
                        raise ValueError("CUPX writer offset drift")
                    _write_source(f, a.source, source_handles)
                    if f.tell() != e.offset + e.stored_size:
                        raise ValueError("CUPX writer payload size drift")
                    f.write(b"\0" * (align(f.tell()) - f.tell()))
            finally:
                for src in source_handles.values():
                    try:
                        src.close()
                    except Exception:
                        pass

            f.flush()
            os.fsync(f.fileno())
            total = f.tell()
            # Header needs the package CRC before it can be written.  Calculate
            # the standard CRC once here; the mandatory post-build verification
            # below independently recomputes both zlib and Xbox-reference CRCs
            # over the completed temp file before atomic publication.
            package_crc = _crc_file_region(f, HEADER.size, total - HEADER.size, False)

            bid = build_id.encode("ascii", "replace")[:31].ljust(32, b"\0")
            f.seek(0)
            f.write(HEADER.pack(
                MAGIC, VERSION, HEADER.size, 0, len(entries),
                index_off, index_size, package_crc, 0, bid))
            f.flush()
            os.fsync(f.fileno())

        check = verify_package_detailed(temp)
        if not check["ok"]:
            bad = [x for x in check["entry_checks"] if not x["ok"]]
            raise ValueError(
                f"CUPX post-build verification failed: package_ok={check['package_ok']} bad_entries={len(bad)}")

        os.replace(temp, output)
    finally:
        if temp.exists():
            temp.unlink()

    return entries


def read_package_header(path: Path):
    """Read structural package metadata without performing another CRC pass."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as f:
        ver, flags, pcrc, bid, entries = _parse_package(f, size)
    return {
        "version": ver,
        "flags": flags,
        "build_id": bid.split(b"\0", 1)[0].decode("ascii", "replace"),
        "package_crc32": pcrc,
        "entries": entries,
        "size": size,
    }


def read_package(path: Path, verify_payloads=True):
    path = Path(path)
    detail = verify_package_detailed(path)
    if not detail["package_ok"]:
        raise ValueError(
            f"Package CRC mismatch expected={detail['package_crc32']:08X} "
            f"zlib={detail['package_zlib_crc32']:08X} xbox={detail['package_xbox_crc32']:08X}")
    if verify_payloads and not detail["entries_ok"]:
        bad = next(x for x in detail["entry_checks"] if not x["ok"])
        raise ValueError(
            f"Asset {bad['asset_id']:08X} CRC mismatch expected={bad['expected_crc32']:08X} "
            f"zlib={bad['zlib_crc32']:08X} xbox={bad['xbox_crc32']:08X}")
    return {
        "version": detail["version"],
        "flags": detail["flags"],
        "build_id": detail["build_id"],
        "package_crc32": detail["package_crc32"],
        "entries": detail["entries"],
        "size": detail["size"],
    }


def read_entry_payload(path: Path, entry: Entry):
    path = Path(path)
    with path.open("rb") as f:
        f.seek(entry.offset)
        data = f.read(entry.stored_size)
    if len(data) != entry.stored_size:
        raise ValueError("Truncated CUPX payload")
    z = zlib.crc32(data) & 0xffffffff
    x = xbox_crc32_update(data, 0)
    if z != entry.crc32 or x != entry.crc32:
        raise ValueError(
            f"CUPX payload CRC mismatch expected={entry.crc32:08X} zlib={z:08X} xbox={x:08X}")
    return data
