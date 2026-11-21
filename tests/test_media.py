from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import camera_test
from camera_runner import logcat, media
from tests.mp4_builder import audio_track, box, mp4, mvhd, trak, video_track


def parse(data):
    with TemporaryDirectory() as folder:
        path = Path(folder) / "clip.mp4"
        path.write_bytes(data)
        return media.inspect(path)


def error_code(data):
    try:
        parse(data)
    except media.MediaError as exc:
        return exc.code
    return None


class ParserTests(unittest.TestCase):
    def test_video_and_audio_are_summarized(self):
        info = parse(mp4([video_track(5.2), audio_track(5.12)], seconds=5.2))
        self.assertEqual(info, {"brand": "mp42", "duration_s": 5.2, "tracks": [
            {"type": "video", "codec": "hvc1", "duration_s": 5.2, "width": 1920, "height": 1080,
             "sample_count": 156, "fps": 30.0},
            {"type": "audio", "codec": "mp4a", "duration_s": 5.12, "width": None, "height": None,
             "sample_count": 240, "fps": None},
        ]})

    def test_version_1_headers_and_large_size_boxes(self):
        data = mp4([video_track(5.0, version=1), audio_track(5.0, version=1)], version=1,
                   mdat=box(b"mdat", bytes(32), large=True))
        info = parse(data)
        self.assertEqual((info["duration_s"], info["tracks"][0]["width"], info["tracks"][0]["fps"]), (5.0, 1920, 30.0))
        self.assertEqual(len(info["tracks"]), 2)

    def test_mdat_with_size_zero_runs_to_end_of_file(self):
        moov_first = (box(b"ftyp", b"mp42" + bytes(8)) + box(b"moov", mvhd(1000, 5000) + video_track())
                      + box(b"mdat", bytes(64), size=0))
        self.assertEqual(parse(moov_first)["tracks"][0]["sample_count"], 150)

    def test_unknown_handlers_and_codec_names_are_kept(self):
        info = parse(mp4([trak(b"meta", b"mett", entries=((3, 1000),)), video_track()]))
        self.assertEqual((info["tracks"][0]["type"], info["tracks"][0]["codec"], info["tracks"][0]["fps"]),
                         ("meta", "mett", None))

    def test_unset_mdhd_duration_falls_back_to_sample_deltas(self):
        info = parse(mp4([trak(b"vide", b"avc1", 30000, 0xFFFFFFFF, ((150, 1000),))], seconds=5.0))
        self.assertEqual((info["tracks"][0]["duration_s"], info["tracks"][0]["codec"]), (5.0, "avc1"))

    def test_missing_moov_is_reported_for_a_cut_off_recording(self):
        self.assertEqual(error_code(mp4([], moov=False)), "MEDIA_NO_MOOV")
        truncated = mp4([], moov=False, mdat=box(b"mdat", bytes(32), size=10_000))  # mdat claims more than the file holds
        self.assertEqual(error_code(truncated), "MEDIA_NO_MOOV")
        self.assertEqual(error_code(b""), "MEDIA_NO_MOOV")

    def test_malformed_boxes_are_rejected(self):
        overrun = box(b"moov", mvhd(1000, 5000) + box(b"trak", bytes(8), size=10_000))
        self.assertEqual(error_code(box(b"ftyp", b"mp42" + bytes(8)) + overrun), "MEDIA_MALFORMED")
        self.assertEqual(error_code(box(b"free", bytes(4), size=4)), "MEDIA_MALFORMED")  # smaller than a header
        self.assertEqual(error_code(box(b"moov", bytes(4), size=1_000_000)), "MEDIA_MALFORMED")  # top-level overrun
        self.assertEqual(error_code(b"\x00\x00\x00\x01moov\x00"), "MEDIA_MALFORMED")  # cut-off 64-bit size
        self.assertEqual(error_code(box(b"moov", mvhd(0, 5000))), "MEDIA_MALFORMED")  # zero timescale
        short_mdhd = box(b"moov", mvhd(1000, 5000) + box(b"trak", box(b"tkhd", bytes(84)) + box(b"mdia", box(b"mdhd", bytes(8)))))
        self.assertEqual(error_code(short_mdhd), "MEDIA_MALFORMED")

    def test_deeply_nested_boxes_are_not_followed(self):
        deep = box(b"free", bytes(8))
        for _ in range(5000):
            deep = box(b"moov", deep)
        self.assertEqual(parse(deep)["tracks"], [])  # Only the fixed container levels are read; no recursion error.


class VerdictTests(unittest.TestCase):
    def verdict(self, tracks, seconds, requested=5):
        failures, warnings = media.verdict(parse(mp4(tracks, seconds=seconds)), requested)
        return [code for code, _ in failures], [code for code, _ in warnings]

    def test_healthy_recording_passes(self):
        self.assertEqual(self.verdict([video_track(5.2), audio_track(5.2)], 5.2), ([], []))

    def test_missing_audio_only_warns(self):
        self.assertEqual(self.verdict([video_track(5.2)], 5.2), ([], ["MEDIA_NO_AUDIO"]))

    def test_no_video_and_no_frames_fail(self):
        self.assertEqual(self.verdict([audio_track(5.2)], 5.2), (["MEDIA_NO_VIDEO"], []))
        empty = trak(b"vide", b"hvc1", 30000, 150000, ())
        self.assertEqual(self.verdict([empty, audio_track()], 5.0), (["MEDIA_NO_FRAMES"], []))

    def test_duration_window_is_requested_minus_half_second_to_plus_two(self):
        tracks = [video_track(), audio_track()]
        self.assertEqual(self.verdict(tracks, 4.5)[0], [])
        self.assertEqual(self.verdict(tracks, 7.0)[0], [])
        self.assertEqual(self.verdict(tracks, 4.4)[0], ["MEDIA_DURATION"])
        self.assertEqual(self.verdict(tracks, 7.1)[0], ["MEDIA_DURATION"])

    def test_check_summary_names_each_check(self):
        failures, warnings = media.verdict(parse(mp4([audio_track(9.0)], seconds=9.0)), 5)
        self.assertEqual(media.check_summary(failures, warnings),
                         {"video_track": "FAIL", "frames": "SKIPPED", "duration": "FAIL", "audio": "PASS"})
        failures, warnings = media.verdict(parse(mp4([video_track(5.0)])), 5)
        self.assertEqual(media.check_summary(failures, warnings),
                         {"video_track": "PASS", "frames": "PASS", "duration": "PASS", "audio": "WARN"})


class MediaInspectCliTests(unittest.TestCase):
    def cli(self, *arguments):
        output = StringIO()
        with patch.object(sys, "argv", ["camera-test", *map(str, arguments)]), redirect_stdout(output):
            code = camera_test.main()
        return code, json.loads(output.getvalue())

    def test_inspect_prints_info_or_an_error_object(self):
        with TemporaryDirectory() as folder:
            good, bad = Path(folder) / "good.mp4", Path(folder) / "bad.mp4"
            good.write_bytes(mp4([video_track(), audio_track()]))
            bad.write_bytes(mp4([], moov=False))
            code, info = self.cli("media", "inspect", good)
            self.assertEqual((code, info["tracks"][0]["codec"]), (0, "hvc1"))
            code, result = self.cli("media", "inspect", bad)
            self.assertEqual((code, result["error"]["code"]), (1, "MEDIA_NO_MOOV"))
            code, result = self.cli("media", "inspect", Path(folder) / "missing.mp4")
            self.assertEqual((code, result["error"]["code"]), (1, "IO_ERROR"))


class LogClockTests(unittest.TestCase):
    def test_elapsed_ms_between_year_less_stamps(self):
        self.assertEqual(logcat.elapsed_ms("11-21 09:10:02.100", "11-21 09:10:02.350"), 250)
        self.assertEqual(logcat.elapsed_ms("11-21 09:59:59.900", "11-21 10:00:00.100"), 200)
        self.assertEqual(logcat.elapsed_ms("11-21 23:59:59.900", "11-22 00:00:00.100"), 200)

    def test_backwards_or_unreadable_stamps_give_no_value(self):
        self.assertIsNone(logcat.elapsed_ms("12-31 23:59:59.900", "01-01 00:00:00.100"))
        self.assertIsNone(logcat.elapsed_ms("11-21 09:10:02.100", None))
        self.assertIsNone(logcat.elapsed_ms("11-21 09:10:02", "11-21 09:10:03.000"))

    def test_line_stamp_reads_threadtime_lines(self):
        self.assertEqual(logcat.line_stamp("11-21 09:10:12.100  1234  1234 I Tag: msg"), "11-21 09:10:12.100")
        self.assertIsNone(logcat.line_stamp("--------- beginning of main"))
