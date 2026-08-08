"""Unit tests for the read/write service layer (analyzer + builder).

Standard-library ``unittest`` only. Run with::

    python -m unittest tests.test_services

These tests exercise the pure SRT/color helpers directly and drive a full
build -> save_draft -> analyze round-trip through the real engine reader/writer
into a temporary drafts directory.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from capcut_mcp.services import analyzer, builder  # noqa: E402


class ParseSrtTests(unittest.TestCase):
    def test_basic_two_cues(self):
        caps = builder.parse_srt(
            "1\n00:00:01,000 --> 00:00:03,500\nHello\nWorld\n\n"
            "2\n00:00:04,000 --> 00:00:05,000\nBye\n"
        )
        self.assertEqual(len(caps), 2)
        self.assertEqual(caps[0].start, 1_000_000)
        self.assertEqual(caps[0].duration, 2_500_000)
        self.assertEqual(caps[0].text, "Hello\nWorld")
        self.assertEqual(caps[1].start, 4_000_000)

    def test_missing_index_and_dot_separator(self):
        caps = builder.parse_srt("00:00:00.000 --> 00:00:01.000\nNo index\n")
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].text, "No index")
        self.assertEqual(caps[0].duration, 1_000_000)

    def test_crlf_and_bom(self):
        caps = builder.parse_srt(
            "﻿1\r\n00:00:00,000 --> 00:00:02,000\r\nLine\r\n"
        )
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].text, "Line")

    def test_empty(self):
        self.assertEqual(builder.parse_srt(""), [])

    def test_inverted_cue_skipped(self):
        # Bug #19c: inverted (end < start) cues are skipped, not emitted as 0-len.
        caps = builder.parse_srt(
            "1\n00:00:05,000 --> 00:00:01,000\nBackwards\n"
        )
        self.assertEqual(caps, [])

    def test_zero_length_cue_skipped(self):
        caps = builder.parse_srt(
            "1\n00:00:02,000 --> 00:00:02,000\nInstant\n"
        )
        self.assertEqual(caps, [])

    def test_missing_blank_line_between_cues(self):
        # Bug #12: cues with no blank separator must still split correctly.
        caps = builder.parse_srt(
            "1\n00:00:01,000 --> 00:00:02,000\nFirst\n"
            "2\n00:00:03,000 --> 00:00:04,000\nSecond\n"
        )
        self.assertEqual(len(caps), 2)
        self.assertEqual(caps[0].text, "First")
        self.assertEqual(caps[0].start, 1_000_000)
        self.assertEqual(caps[0].duration, 1_000_000)
        self.assertEqual(caps[1].text, "Second")
        self.assertEqual(caps[1].start, 3_000_000)

    def test_missing_blank_line_no_index(self):
        # No index lines AND no blank separators — still two cues.
        caps = builder.parse_srt(
            "00:00:01,000 --> 00:00:02,000\nFirst\n"
            "00:00:03,000 --> 00:00:04,000\nSecond\n"
        )
        self.assertEqual(len(caps), 2)
        self.assertEqual(caps[0].text, "First")
        self.assertEqual(caps[1].text, "Second")

    def test_hours_over_99(self):
        # Bug #19a: hours >= 100 must not be dropped.
        caps = builder.parse_srt(
            "1\n100:00:00,000 --> 100:00:01,000\nLate\n"
        )
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0].start, 100 * 3_600_000 * 1_000)
        self.assertEqual(caps[0].duration, 1_000_000)

    def test_four_digit_milliseconds(self):
        # Bug #19b: over-long fractional field truncates, doesn't break the cue.
        caps = builder.parse_srt(
            "1\n00:00:00,1234 --> 00:00:02,0000\nText\n"
        )
        self.assertEqual(len(caps), 1)
        # ".1234" truncates to "123" ms -> 123_000 us start.
        self.assertEqual(caps[0].start, 123_000)
        self.assertEqual(caps[0].duration, 2_000_000 - 123_000)


class ParseColorTests(unittest.TestCase):
    def test_rrggbb(self):
        r, g, b, a = builder._parse_color("#FF8000")
        self.assertAlmostEqual(r, 1.0)
        self.assertAlmostEqual(b, 0.0)
        self.assertAlmostEqual(a, 1.0)

    def test_rrggbbaa(self):
        _, _, _, a = builder._parse_color("#00000080")
        self.assertAlmostEqual(a, 128 / 255.0)

    def test_none(self):
        self.assertIsNone(builder._parse_color(None))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            builder._parse_color("nothex")


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        # Fresh registry per test to avoid name collisions across tests.
        builder._PROJECTS.clear()
        self.drafts = Path(tempfile.mkdtemp())
        self.media = Path(tempfile.gettempdir()) / "svc_test_clip.mp4"
        self.media.write_bytes(b"\x00")

    def _build(self, name: str = "RT"):
        builder.create_project(name, 1920, 1080, 30.0)
        builder.add_text(name, "Title", 0, 2_000_000, color="#00FF00")
        builder.add_video(name, str(self.media), 0, duration_us=3_000_000)
        builder.add_audio(name, str(self.media), 0, duration_us=1_000_000)
        return name

    def test_create_duplicate_raises(self):
        builder.create_project("Dup", 1, 1)
        with self.assertRaises(ValueError):
            builder.create_project("Dup", 1, 1)

    def test_add_video_requires_duration_without_ffprobe(self):
        builder.create_project("NoDur", 1, 1)
        # A non-existent path with no explicit duration and (typically) no
        # ffprobe should raise a clear ValueError.
        with self.assertRaises(ValueError):
            builder.add_video("NoDur", "does_not_exist.mp4", 0)

    def test_full_roundtrip(self):
        name = self._build()
        res = builder.save_draft(name, drafts_dir=self.drafts, force=True)
        self.assertTrue(res["saved"])
        self.assertTrue(Path(res["draft_dir"]).exists())

        summary = analyzer.analyze_project(name, drafts_dir=self.drafts)
        self.assertEqual(summary["name"], name)
        self.assertEqual(summary["resolution"], [1920, 1080])
        self.assertEqual(summary["clip_count"], 3)
        self.assertTrue(summary["has_captions"])

        timeline = analyzer.get_timeline(name, drafts_dir=self.drafts)
        self.assertEqual(len(timeline), 3)

        video_only = analyzer.get_timeline(
            name, track_type="video", drafts_dir=self.drafts
        )
        self.assertTrue(all(r["track_type"] == "video" for r in video_only))

        captions = analyzer.get_captions(name, drafts_dir=self.drafts)
        self.assertTrue(any(c["text"] == "Title" for c in captions))

        detail = analyzer.get_segment_detail(
            name, timeline[0]["segment_id"], drafts_dir=self.drafts
        )
        self.assertIn("keyframes", detail)
        self.assertIn("effects", detail)

        with self.assertRaises(KeyError):
            analyzer.get_segment_detail(name, "no-such-id", drafts_dir=self.drafts)

    def test_bad_track_type_raises(self):
        name = self._build("BadTT")
        builder.save_draft(name, drafts_dir=self.drafts, force=True)
        with self.assertRaises(ValueError):
            analyzer.get_timeline(name, track_type="bogus", drafts_dir=self.drafts)

    def test_doctor_reports_temp_dir(self):
        report = analyzer.doctor(drafts_dir=self.drafts)
        self.assertTrue(report["drafts_dir_exists"])
        self.assertIn("candidate_dirs", report)
        self.assertIn("ffprobe_available", report)


class BuilderSpeedTests(unittest.TestCase):
    def setUp(self):
        builder._PROJECTS.clear()
        self.media = Path(tempfile.gettempdir()) / "svc_speed_clip.mp4"
        self.media.write_bytes(b"\x00")

    def test_speed_scales_target_duration(self):
        # Bug #11: target.duration reflects the sped-up timeline length.
        builder.create_project("Sp", 1920, 1080)
        res = builder.add_video(
            "Sp", str(self.media), 0, duration_us=5_000_000, speed=2.0
        )
        # 5_000_000 / 2.0 -> 2_500_000 on the timeline.
        self.assertEqual(res["duration_us"], 2_500_000)
        seg = builder._PROJECTS["Sp"].tracks[0].segments[0]
        self.assertEqual(seg.target.duration, 2_500_000)
        # source_duration stays the raw media span.
        self.assertEqual(seg.source_duration, 5_000_000)
        self.assertEqual(seg.speed, 2.0)

    def test_speed_one_is_unchanged(self):
        builder.create_project("Sp1", 1920, 1080)
        res = builder.add_video(
            "Sp1", str(self.media), 0, duration_us=4_000_000, speed=1.0
        )
        self.assertEqual(res["duration_us"], 4_000_000)

    def test_speed_half_doubles(self):
        builder.create_project("SpH", 1920, 1080)
        res = builder.add_video(
            "SpH", str(self.media), 0, duration_us=3_000_000, speed=0.5
        )
        self.assertEqual(res["duration_us"], 6_000_000)

    def test_zero_speed_guarded(self):
        builder.create_project("Sp0", 1920, 1080)
        res = builder.add_video(
            "Sp0", str(self.media), 0, duration_us=2_000_000, speed=0.0
        )
        # Guarded: falls back to no scaling rather than div-by-zero.
        self.assertEqual(res["duration_us"], 2_000_000)


class DedicatedSrtTrackTests(unittest.TestCase):
    def setUp(self):
        builder._PROJECTS.clear()
        self.srt = Path(tempfile.mkdtemp()) / "cues.srt"
        self.srt.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nA\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nB\n",
            encoding="utf-8",
        )

    def test_srt_uses_new_dedicated_track(self):
        # Bug #23: SRT batch gets its own fresh text track, not the manual one.
        builder.create_project("Srt", 1920, 1080)
        builder.add_text("Srt", "Manual", 0, 1_000_000)
        manual_tracks = builder._PROJECTS["Srt"].tracks_of_type(
            builder.TrackType.TEXT
        )
        self.assertEqual(len(manual_tracks), 1)

        res = builder.add_subtitles_from_srt("Srt", str(self.srt))
        self.assertEqual(res["added"], 2)

        text_tracks = builder._PROJECTS["Srt"].tracks_of_type(
            builder.TrackType.TEXT
        )
        # A new, separate text track was created for the SRT cues.
        self.assertEqual(len(text_tracks), 2)
        self.assertEqual(res["track_index"], text_tracks[1].index)
        # Manual text track still holds only the manual segment.
        self.assertEqual(len(text_tracks[0].segments), 1)
        self.assertEqual(len(text_tracks[1].segments), 2)


class SaveDraftSafetyTests(unittest.TestCase):
    def setUp(self):
        builder._PROJECTS.clear()
        self.drafts = Path(tempfile.mkdtemp())
        self.media = Path(tempfile.gettempdir()) / "svc_save_clip.mp4"
        self.media.write_bytes(b"\x00")

    def _build(self, name):
        builder.create_project(name, 1920, 1080)
        builder.add_video(name, str(self.media), 0, duration_us=2_000_000)
        return name

    def test_force_does_not_overwrite(self):
        # Bug #5/#20: saving twice must NOT clobber the first draft.
        name = self._build("NoClobber")
        first = builder.save_draft(name, drafts_dir=self.drafts, force=True)
        self.assertTrue(first["saved"])
        draft_dir = Path(first["draft_dir"])
        self.assertTrue(draft_dir.exists())
        # Marker file to prove the original folder is left intact.
        marker = draft_dir / "_marker.txt"
        marker.write_text("original", encoding="utf-8")

        second = builder.save_draft(name, drafts_dir=self.drafts, force=True)
        self.assertFalse(second["saved"])
        self.assertEqual(second["reason"], "draft_exists")
        self.assertIn("warning", second)
        # Original folder + marker untouched (no rmtree/overwrite).
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(encoding="utf-8"), "original")

    def test_already_exists_returns_tidy_dict(self):
        # Bug #20: FileExistsError must not bubble raw.
        name = self._build("Tidy")
        builder.save_draft(name, drafts_dir=self.drafts, force=True)
        res = builder.save_draft(name, drafts_dir=self.drafts, force=True)
        self.assertIsInstance(res, dict)
        self.assertFalse(res["saved"])
        self.assertIn("reason", res)

    def test_open_hint_toggle(self):
        # Bug #18: hint only present when open_hint is truthy.
        name = self._build("Hint")
        with_hint = builder.save_draft(
            name, drafts_dir=self.drafts, open_hint=True, force=True
        )
        self.assertIn("message", with_hint)

        builder._PROJECTS.clear()
        name2 = self._build("NoHint")
        drafts2 = Path(tempfile.mkdtemp())
        without = builder.save_draft(
            name2, drafts_dir=drafts2, open_hint=False, force=True
        )
        self.assertTrue(without["saved"])
        self.assertNotIn("message", without)

    def test_missing_media_warnings_surface(self):
        # Bug #22: missing-media warnings still reach the response dict.
        builder.create_project("Miss", 1920, 1080)
        builder.add_video(
            "Miss", "does_not_exist_xyz.mp4", 0, duration_us=1_000_000
        )
        res = builder.save_draft("Miss", drafts_dir=self.drafts, force=True)
        self.assertTrue(res["saved"])
        self.assertTrue(len(res["warnings"]) >= 1)


if __name__ == "__main__":
    unittest.main()
