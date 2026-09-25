from __future__ import annotations

from pathlib import Path
import struct

from ..pack.format import TYPE_AUDIO
from ..pack.volumes import AssetSpec
from .texture import _load_env, _safe_name

# Native one-shot PCM payload v1.
#
# Runtime contract intentionally mirrors the simplest, proven Xbox path used in
# DarkDash / EOS / invaderz: create a PCM WAVEFORMATEX, CreateSoundBuffer,
# Lock/copy/Unlock, rewind, Play. No codec exists on the Xbox side for M1.
AUDIO_MAGIC = b"CUPS"
AUDIO_VERSION = 1
AUDIO_HEADER = struct.Struct("<4sHHIIIIII")

AUDIOFMT_PCM_S16LE = 1
AUDIOFLAG_INTERLEAVED = 0x00000001

DEFAULT_MAX_SECONDS = 8.0
DEFAULT_MAX_PCM_BYTES = 4 * 1024 * 1024


def _type_name(obj):
    t = getattr(obj, "type", None)
    return getattr(t, "name", str(t or "Unknown"))


def _candidate_unity_rows(rows):
    for row in sorted(rows, key=lambda r: r["path"].lower()):
        if row.get("kind") != "unity":
            continue
        p = row["path"].lower()
        if p.endswith(".resource") or p.endswith(".ress"):
            continue
        yield row


def _parse_pcm16_wav(data: bytes):
    """Return (channels, sample_rate, pcm_bytes) for a simple PCM16 RIFF/WAVE."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE sample")

    fmt = None
    pcm = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        end = start + chunk_size
        if end > len(data):
            raise ValueError("truncated WAV chunk")

        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise ValueError("WAV fmt chunk is too small")
            fmt = struct.unpack_from("<HHIIHH", data, start)
        elif chunk_id == b"data":
            pcm = bytes(data[start:end])

        pos = end + (chunk_size & 1)

    if fmt is None or pcm is None:
        raise ValueError("WAV is missing fmt or data")

    format_tag, channels, sample_rate, avg_bps, block_align, bits = fmt
    if format_tag != 1:
        raise ValueError(f"WAV encoding {format_tag} is not PCM")
    if channels not in (1, 2):
        raise ValueError(f"unsupported channel count {channels}")
    if bits != 16:
        raise ValueError(f"unsupported bit depth {bits}; expected PCM16")
    if sample_rate < 8000 or sample_rate > 48000:
        raise ValueError(f"unsupported sample rate {sample_rate}")

    expected_align = channels * 2
    if block_align != expected_align:
        raise ValueError("invalid PCM16 block alignment")
    if avg_bps not in (0, sample_rate * block_align):
        # Some exporters leave this unusual; alignment and actual bytes are the
        # safety-critical fields, so do not reject solely on average rate.
        pass
    if len(pcm) == 0 or len(pcm) % block_align:
        raise ValueError("PCM data is empty or not frame aligned")

    frames = len(pcm) // block_align
    return channels, sample_rate, frames, pcm


def build_audio_payload(pcm: bytes, channels: int, sample_rate: int):
    channels = int(channels)
    sample_rate = int(sample_rate)
    if channels not in (1, 2):
        raise ValueError("CUPS v1 supports mono or stereo")
    if sample_rate < 8000 or sample_rate > 48000:
        raise ValueError("CUPS v1 sample rate outside supported range")

    block_align = channels * 2
    if not pcm or len(pcm) % block_align:
        raise ValueError("CUPS PCM bytes are not frame aligned")

    frame_count = len(pcm) // block_align
    header = AUDIO_HEADER.pack(
        AUDIO_MAGIC,
        AUDIO_VERSION,
        AUDIO_HEADER.size,
        AUDIOFMT_PCM_S16LE,
        channels,
        sample_rate,
        frame_count,
        AUDIOFLAG_INTERLEAVED,
        len(pcm),
    )
    meta = {
        "payload_version": AUDIO_VERSION,
        "payload_header_bytes": AUDIO_HEADER.size,
        "native_format": "PCM_S16LE",
        "native_format_id": AUDIOFMT_PCM_S16LE,
        "channels": channels,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "data_bytes": len(pcm),
        "duration_ms": int(round(frame_count * 1000.0 / sample_rate)),
        "interleaved": True,
    }
    return header + pcm, meta


def parse_audio_payload(data: bytes):
    if len(data) < AUDIO_HEADER.size:
        raise ValueError("Truncated CUPS header")

    (
        magic, version, header_size, fmt, channels,
        sample_rate, frame_count, flags, data_bytes,
    ) = AUDIO_HEADER.unpack_from(data, 0)

    if magic != AUDIO_MAGIC:
        raise ValueError("Bad CUPS magic")
    if version != AUDIO_VERSION:
        raise ValueError(f"Unsupported CUPS version {version}")
    if header_size != AUDIO_HEADER.size:
        raise ValueError("Unexpected CUPS header size")
    if fmt != AUDIOFMT_PCM_S16LE:
        raise ValueError(f"Unsupported CUPS format {fmt}")
    if channels not in (1, 2):
        raise ValueError("Invalid CUPS channel count")
    if sample_rate < 8000 or sample_rate > 48000:
        raise ValueError("Invalid CUPS sample rate")
    if data_bytes != frame_count * channels * 2:
        raise ValueError("CUPS frame/data byte mismatch")
    if header_size + data_bytes != len(data):
        raise ValueError("CUPS payload size mismatch")

    return {
        "version": version,
        "format": fmt,
        "format_name": "PCM_S16LE",
        "channels": channels,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "flags": flags,
        "data_bytes": data_bytes,
        "duration_ms": int(round(frame_count * 1000.0 / sample_rate)),
        "pcm": data[header_size:],
    }


def _iter_clip_samples(clip):
    """Yield (sample_name, bytes) across UnityPy AudioClip API variants."""
    samples = getattr(clip, "samples", None)
    if callable(samples):
        samples = samples()

    if isinstance(samples, dict):
        for name in sorted(samples, key=lambda x: str(x).lower()):
            blob = samples[name]
            if isinstance(blob, memoryview):
                blob = blob.tobytes()
            if isinstance(blob, bytearray):
                blob = bytes(blob)
            if isinstance(blob, bytes):
                yield str(name), blob
        return

    # Some UnityPy versions expose a single exported byte stream.
    if isinstance(samples, memoryview):
        samples = samples.tobytes()
    if isinstance(samples, bytearray):
        samples = bytes(samples)
    if isinstance(samples, bytes):
        yield "sample.wav", samples


def discover_audio_spec(
    root: Path,
    rows,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_pcm_bytes: int = DEFAULT_MAX_PCM_BYTES,
):
    """Find one real Cuphead AudioClip that UnityPy can export as PCM16 WAV."""
    root = Path(root)
    max_seconds = max(0.1, float(max_seconds))
    notes = []

    for row in _candidate_unity_rows(rows):
        rel = row["path"]
        path = root / rel
        try:
            # Audio payloads frequently reside in external .resource files.
            env = _load_env(path, dependency_mode=True)
        except Exception as e:
            if len(notes) < 40:
                notes.append(f"audio skip {rel}: UnityPy.load: {e}")
            continue

        for obj in env.objects:
            if _type_name(obj) != "AudioClip":
                continue

            try:
                clip = obj.read()
                clip_name = str(
                    getattr(clip, "m_Name", "") or
                    getattr(clip, "name", "") or
                    f"audio_{getattr(obj, 'path_id', 0)}"
                )

                found_sample = False
                for sample_name, wav in _iter_clip_samples(clip):
                    found_sample = True
                    try:
                        channels, sample_rate, frames, pcm = _parse_pcm16_wav(wav)
                    except Exception as e:
                        if len(notes) < 40:
                            notes.append(
                                f"audio sample skip {rel} PathID {getattr(obj,'path_id','?')} "
                                f"{sample_name}: {e}")
                        continue

                    duration = frames / float(sample_rate)
                    if duration < 0.05:
                        continue
                    if duration > max_seconds:
                        if len(notes) < 40:
                            notes.append(
                                f"audio skip {clip_name}: {duration:.2f}s exceeds "
                                f"diagnostic limit {max_seconds:.2f}s")
                        continue
                    if len(pcm) > int(max_pcm_bytes):
                        if len(notes) < 40:
                            notes.append(
                                f"audio skip {clip_name}: PCM {len(pcm):,} bytes exceeds limit")
                        continue

                    payload, meta = build_audio_payload(pcm, channels, sample_rate)
                    meta.update({
                        "source_type": "AudioClip",
                        "source_container": rel.replace("\\", "/"),
                        "source_path_id": int(getattr(obj, "path_id", 0) or 0),
                        "source_name": clip_name,
                        "source_sample_name": sample_name,
                    })
                    spec = AssetSpec(
                        name=f"diagnostic/audio/{_safe_name(clip_name)}/sfx",
                        source=None,
                        data=payload,
                        kind="audio",
                        group="diagnostic",
                        type=TYPE_AUDIO,
                        metadata=meta,
                    )
                    return spec, notes

                if not found_sample and len(notes) < 40:
                    notes.append(
                        f"audio skip {rel} PathID {getattr(obj,'path_id','?')}: "
                        "UnityPy exposed no decoded sample bytes")
            except Exception as e:
                if len(notes) < 40:
                    notes.append(
                        f"audio clip parse skip {rel} PathID {getattr(obj,'path_id','?')}: {e}")

    detail = "\n".join(notes[-10:]) if notes else "No eligible AudioClip was found."
    raise RuntimeError(
        "M1 media build could not compile a real PCM16 AudioClip.\n" + detail)
