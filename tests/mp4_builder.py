"""Assemble small synthetic MP4 byte strings for tests."""

import struct


def box(kind, payload=b"", large=False, size=None):
    """A plain box. `large` writes a 64-bit size; `size` overrides the declared size (0 = to end of file)."""
    if large:
        return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload
    declared = 8 + len(payload) if size is None else size
    return struct.pack(">I4s", declared, kind) + payload


def full(kind, version, body):
    return box(kind, bytes([version, 0, 0, 0]) + body)


def times(timescale, duration, version):
    if version:
        return struct.pack(">QQIQ", 0, 0, timescale, duration)
    return struct.pack(">IIII", 0, 0, timescale, duration)


def mvhd(timescale, duration, version=0):
    return full(b"mvhd", version, times(timescale, duration, version) + bytes(80))


def tkhd(width=0, height=0, version=0):
    head = struct.pack(">QQIIQ", 0, 0, 1, 0, 0) if version else struct.pack(">IIIII", 0, 0, 1, 0, 0)
    reserved_layer_volume_matrix = bytes(8 + 2 + 2 + 2 + 2 + 36)
    return full(b"tkhd", version, head + reserved_layer_volume_matrix + struct.pack(">II", width << 16, height << 16))


def mdhd(timescale, duration, version=0):
    return full(b"mdhd", version, times(timescale, duration, version) + bytes(4))


def hdlr(handler):
    return full(b"hdlr", 0, struct.pack(">I4s", 0, handler) + bytes(12) + b"h\0")


def stsd(codec):
    return full(b"stsd", 0, struct.pack(">I", 1) + box(codec, bytes(8)))


def stts(entries):
    return full(b"stts", 0, struct.pack(">I", len(entries)) + b"".join(struct.pack(">II", *entry) for entry in entries))


def trak(handler=b"vide", codec=b"hvc1", timescale=30000, duration=150000, entries=((150, 1000),),
         width=1920, height=1080, version=0):
    stbl = box(b"stbl", stsd(codec) + stts(list(entries)))
    mdia = box(b"mdia", mdhd(timescale, duration, version) + hdlr(handler) + box(b"minf", stbl))
    return box(b"trak", tkhd(width, height, version) + mdia)


def video_track(seconds=5.0, fps=30, **options):
    return trak(b"vide", b"hvc1", 30000, round(seconds * 30000), ((round(seconds * fps), 1000),), **options)


def audio_track(seconds=5.0, **options):
    frames = round(seconds * 48000 / 1024)
    return trak(b"soun", b"mp4a", 48000, round(seconds * 48000), ((frames, 1024),), width=0, height=0, **options)


def mp4(tracks, seconds=5.0, brand=b"mp42", version=0, moov=True, mdat=None):
    """ftyp, mdat, then moov (the layout Samsung Camera writes). `mdat` replaces the default mdat box."""
    data = box(b"ftyp", brand + bytes(4) + b"isom")
    data += box(b"mdat", bytes(32)) if mdat is None else mdat
    if moov:
        data += box(b"moov", mvhd(1000, round(seconds * 1000), version) + b"".join(tracks))
    return data
