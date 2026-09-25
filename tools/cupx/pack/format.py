from __future__ import annotations
import struct
MAGIC = b"CUPX"
VERSION = 1
HEADER = struct.Struct("<4sHHIIQQII32s")
ENTRY = struct.Struct("<IHHQQQII")
# Header: magic, version, header_size, flags, entry_count, index_off, index_size,
#         package_crc32, reserved, build_id[32]
# Entry: asset_id, type, flags, offset, stored_size, decoded_size, crc32, reserved

TYPE_RAW = 0
TYPE_TEXTURE = 1
TYPE_SPRITE = 2
TYPE_ANIMATION = 3
TYPE_AUDIO = 4
TYPE_VIDEO = 5
TYPE_SCENE = 6
TYPE_DEBUG = 0x7FFF
