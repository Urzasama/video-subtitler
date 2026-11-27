"""Unit tests for subs_core - run with:  python -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

import subs_core as core  # noqa: E402


class TimecodeTests(unittest.TestCase):
    def test_fmt_srt(self):
        self.assertEqual(core.fmt_srt(0), "00:00:00,000")
        self.assertEqual(core.fmt_srt(1.25), "00:00:01,250")
        self.assertEqual(core.fmt_srt(3661.007), "01:01:01,007")
        self.assertEqual(core.fmt_srt(-3), "00:00:00,000")

    def test_fmt_vtt_uses_dot(self):
        self.assertEqual(core.fmt_vtt(1.5), "00:00:01.500")

    def test_parse_roundtrip(self):
        for seconds in (0.0, 0.5, 12.345, 3725.678):
            self.assertAlmostEqual(core.parse_timecode(core.fmt_srt(seconds)), seconds, places=3)

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            core.parse_timecode("not a time")


class SplitTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(core.split_long("hello world", 0), ["hello world"])
        self.assertEqual(core.split_long("hi", 40), ["hi"])

    def test_english_split_on_space(self):
        text = "this is a fairly long english sentence that should be broken into pieces"
        parts = core.split_long(text, 30)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 45 for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())

    def test_cjk_split_on_punctuation(self):
        text = "第一句话比较长。第二句话也不短，需要切开。第三句收尾。"
        parts = core.split_long(text, 12)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)

    def test_segments_to_lines_shares_time(self):
        seg = core.Segment(0.0, 4.0, "a" * 20 + " " + "b" * 20)
        out = core.segments_to_lines([seg], 25)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0].start, 0.0)
        self.assertAlmostEqual(out[-1].end, 4.0)
        self.assertAlmostEqual(out[0].end, out[1].start, places=3)


class MergeTests(unittest.TestCase):
    def test_merge_close_segments(self):
        segs = [core.Segment(0.0, 1.0, "hello"), core.Segment(1.1, 2.0, "world")]
        merged = core.merge_segments(segs, gap=0.35, max_chars=20)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, "hello world")
        self.assertAlmostEqual(merged[0].end, 2.0)

    def test_keep_far_segments(self):
        segs = [core.Segment(0.0, 1.0, "hello"), core.Segment(3.0, 4.0, "world")]
        self.assertEqual(len(core.merge_segments(segs)), 2)

    def test_respects_max_chars(self):
        segs = [core.Segment(0.0, 1.0, "hello"), core.Segment(1.1, 2.0, "world")]
        self.assertEqual(len(core.merge_segments(segs, max_chars=8)), 2)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.segs = [core.Segment(0.0, 1.5, "你好"), core.Segment(2.0, 3.25, "world")]

    def test_srt_layout(self):
        path = os.path.join(self.tmp, "a.srt")
        core.write_srt(self.segs, path)
        text = read_text(path)
        self.assertTrue(text.startswith("1\n00:00:00,000 --> 00:00:01,500\n你好"))
        self.assertIn("2\n00:00:02,000 --> 00:00:03,250\nworld", text)

    def test_srt_roundtrip(self):
        path = os.path.join(self.tmp, "b.srt")
        core.write_srt(self.segs, path)
        back = core.read_srt(path)
        self.assertEqual(len(back), 2)
        self.assertEqual(back[0].text, "你好")
        self.assertAlmostEqual(back[1].start, 2.0, places=2)

    def test_vtt_header(self):
        path = os.path.join(self.tmp, "a.vtt")
        core.write_vtt(self.segs, path)
        text = read_text(path)
        self.assertTrue(text.startswith("WEBVTT"))
        self.assertIn("00:00:00.000 --> 00:00:01.500", text)

    def test_bilingual(self):
        path = os.path.join(self.tmp, "bi.srt")
        other = [core.Segment(0.0, 1.5, "hello"), core.Segment(2.0, 3.25, "世界")]
        core.write_bilingual(self.segs, other, path)
        text = read_text(path)
        self.assertIn("你好\nhello", text)
        self.assertIn("world\n世界", text)

    def test_json_meta(self):
        path = os.path.join(self.tmp, "a.json")
        core.write_json(self.segs, path, meta={"model": "tiny"})
        import json
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["meta"]["model"], "tiny")
        self.assertEqual(len(data["segments"]), 2)


class FfmpegTests(unittest.TestCase):
    def test_ffmpeg_available(self):
        try:
            core.ffmpeg_bin()
        except RuntimeError:
            self.skipTest("ffmpeg not installed")


if __name__ == "__main__":
    unittest.main()
