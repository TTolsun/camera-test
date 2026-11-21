"""MP4 inspection and verdict. Pure file parsing: no ADB."""

import struct

# Thresholds for the media verdict. A real 5 s Portrait Video from this tool (SM-S936N, Camera 17.0.00.43)
# lasted 5.173 s (HEVC 1920x1080 at 29.6 fps plus AAC audio, 0.17 s longer than requested). The recorder starts
# after the start tap and stops after the stop tap is processed, and the scenario spends a few hundred
# milliseconds of ADB calls between the sleep and the stop tap, so the file runs a bit long rather than short.
# The window keeps that margin on the long side and allows only a little shortfall.
DURATION_SHORT_TOLERANCE_S = 0.5  # file may be at most this much shorter than requested
DURATION_LONG_TOLERANCE_S = 2.0  # file may be at most this much longer than requested

MAX_PAYLOAD_BYTES = 32 * 1024 * 1024  # Largest leaf box payload read into memory (stts of a very long clip).
UNKNOWN_DURATIONS = {0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}
HANDLER_NAMES = {b"vide": "video", b"soun": "audio"}

CHECK_NAMES = ("video_track", "frames", "duration", "audio")
CHECK_BY_CODE = {"MEDIA_NO_VIDEO": "video_track", "MEDIA_NO_FRAMES": "frames",
                 "MEDIA_DURATION": "duration", "MEDIA_NO_AUDIO": "audio"}


class MediaError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def malformed(message):
    return MediaError("MEDIA_MALFORMED", message)


def unpack(fmt, data, offset=0):
    try:
        return struct.unpack_from(fmt, data, offset)
    except struct.error as exc:
        raise malformed("A box is shorter than its declared fields.") from exc


def iter_boxes(file, start, end, top_level=False):
    """Yield (type, payload_start, payload_end) for the sibling boxes in [start, end). Never descends."""
    offset = start
    while end - offset >= 8:
        file.seek(offset)
        head = file.read(8)
        size, kind = struct.unpack(">I4s", head)
        header = 8
        if size == 1:
            if end - offset < 16:
                raise malformed("A 64-bit box size is cut off.")
            size, header = struct.unpack(">Q", file.read(8))[0], 16
        elif size == 0:
            size = end - offset  # The box extends to the end of its parent (or the file).
        if size < header:
            raise malformed(f"Box {kind!r} declares a size smaller than its own header.")
        if offset + size > end:
            if top_level and kind == b"mdat":
                return  # A recording cut off mid-mdat: the caller reports the missing moov.
            raise malformed(f"Box {kind!r} extends past its parent.")
        yield kind, offset + header, offset + size
        offset += size


def read_payload(file, start, end):
    if end - start > MAX_PAYLOAD_BYTES:
        raise malformed("A metadata box is implausibly large.")
    file.seek(start)
    data = file.read(end - start)
    if len(data) != end - start:
        raise malformed("The file ends inside a box.")
    return data


def first_child(file, start, end, kind):
    return next(((s, e) for k, s, e in iter_boxes(file, start, end) if k == kind), None)


def times(data, what):
    """creation, modification, timescale, duration of an mvhd or mdhd payload (version 0 or 1)."""
    version = unpack(">B", data)[0]
    if version == 1:
        timescale, duration = unpack(">IQ", data, 20)
    elif version == 0:
        timescale, duration = unpack(">II", data, 12)
    else:
        raise malformed(f"{what} has unsupported version {version}.")
    if timescale == 0:
        raise malformed(f"{what} has a zero timescale.")
    return timescale, duration


def seconds(duration, timescale):
    return None if duration in UNKNOWN_DURATIONS else duration / timescale


def parse_tkhd(data):
    version = unpack(">B", data)[0]
    if version not in (0, 1):
        raise malformed(f"tkhd has unsupported version {version}.")
    width, height = unpack(">II", data, 88 if version == 1 else 76)
    return width / 65536, height / 65536


def parse_stts(data):
    count = unpack(">I", data, 4)[0]
    if len(data) < 8 + count * 8:
        raise malformed("stts has fewer entries than it declares.")
    samples = delta = 0
    for index in range(count):
        entry_count, entry_delta = unpack(">II", data, 8 + index * 8)
        samples += entry_count
        delta += entry_count * entry_delta
    return samples, delta


def parse_track(file, start, end):
    tkhd = first_child(file, start, end, b"tkhd")
    mdia = first_child(file, start, end, b"mdia")
    if tkhd is None or mdia is None:
        raise malformed("A trak box has no tkhd or mdia.")
    width, height = parse_tkhd(read_payload(file, *tkhd))
    mdhd = first_child(file, *mdia, b"mdhd")
    hdlr = first_child(file, *mdia, b"hdlr")
    if mdhd is None or hdlr is None:
        raise malformed("A track has no mdhd or hdlr.")
    timescale, duration = times(read_payload(file, *mdhd), "mdhd")
    handler = unpack(">4s", read_payload(file, *hdlr), 8)[0]
    codec, samples, delta = None, 0, 0
    minf = first_child(file, *mdia, b"minf")
    stbl = first_child(file, *minf, b"stbl") if minf else None
    if stbl:
        if stsd := first_child(file, *stbl, b"stsd"):
            data = read_payload(file, *stsd)
            if unpack(">I", data, 4)[0] > 0:
                codec = unpack(">4s", data, 12)[0].decode("latin-1")
        if stts := first_child(file, *stbl, b"stts"):
            samples, delta = parse_stts(read_payload(file, *stts))
    length = seconds(duration, timescale)
    if not length:
        length = delta / timescale  # A writer that left mdhd duration unset still wrote sample deltas.
    kind = HANDLER_NAMES.get(handler, handler.decode("latin-1"))
    return {
        "type": kind,
        "codec": codec,
        "duration_s": round(length, 3),
        "width": round(width) if kind == "video" and width else None,
        "height": round(height) if kind == "video" and height else None,
        "sample_count": samples,
        "fps": round(samples / length, 3) if kind == "video" and length else None,
    }


def parse_moov(file, start, end):
    movie, tracks = None, []
    for kind, payload_start, payload_end in iter_boxes(file, start, end):
        if kind == b"mvhd":
            timescale, duration = times(read_payload(file, payload_start, payload_end), "mvhd")
            movie = seconds(duration, timescale)
        elif kind == b"trak":
            tracks.append(parse_track(file, payload_start, payload_end))
    if movie is None:
        movie = max((track["duration_s"] for track in tracks), default=0.0)
    return round(movie, 3), tracks


def inspect(path):
    """Parse an MP4 and return {"brand", "duration_s", "tracks"}. Raises MediaError."""
    brand, moov = None, None
    with open(path, "rb") as file:
        file.seek(0, 2)
        size = file.tell()
        for kind, start, end in iter_boxes(file, 0, size, top_level=True):
            if kind == b"ftyp" and brand is None:
                brand = unpack(">4s", read_payload(file, start, min(end, start + 4)))[0].decode("latin-1")
            elif kind == b"moov" and moov is None:
                moov = (start, end)
        if moov is None:
            raise MediaError("MEDIA_NO_MOOV", "The MP4 has no moov box. The recording was likely cut off before it was finalized.")
        duration, tracks = parse_moov(file, *moov)
    return {"brand": brand, "duration_s": duration, "tracks": tracks}


def verdict(info, requested_seconds):
    """Return (failures, warnings), each a list of (code, message)."""
    failures, warnings = [], []
    videos = [track for track in info["tracks"] if track["type"] == "video"]
    if not videos:
        failures.append(("MEDIA_NO_VIDEO", "The MP4 has no video track."))
    elif not any(track["sample_count"] for track in videos):
        failures.append(("MEDIA_NO_FRAMES", "The video track contains no frames."))
    low, high = requested_seconds - DURATION_SHORT_TOLERANCE_S, requested_seconds + DURATION_LONG_TOLERANCE_S
    if not low <= info["duration_s"] <= high:
        failures.append(("MEDIA_DURATION", f"The MP4 lasts {info['duration_s']} s, outside {low:g}-{high:g} s "
                                           f"for a {requested_seconds} s request."))
    if not any(track["type"] == "audio" for track in info["tracks"]):
        warnings.append(("MEDIA_NO_AUDIO", "The MP4 has no audio track."))
    return failures, warnings


def check_summary(failures, warnings):
    """Map each check to PASS, FAIL or WARN for result.json."""
    checks = dict.fromkeys(CHECK_NAMES, "PASS")
    for status, items in (("FAIL", failures), ("WARN", warnings)):
        for code, _ in items:
            checks[CHECK_BY_CODE[code]] = status
    if checks["video_track"] == "FAIL":
        checks["frames"] = "SKIPPED"
    return checks
