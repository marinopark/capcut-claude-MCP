"""Unit tests for engine.locator and engine.draft_reader.

No real CapCut draft exists yet, so these tests build a synthetic minimal
``draft_content.json`` fixture matching the schema draft_reader maps, then assert
the resulting Project is correct.

Run: ``python -m unittest tests.test_reader -v``
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make the package importable without installation.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from capcut_mcp.engine import draft_reader, locator, models  # noqa: E402


def _sample_draft_content() -> dict:
    """A minimal but representative CapCut draft_content.json structure."""
    return {
        "canvas_config": {"width": 1920, "height": 1080, "ratio": "original"},
        "fps": 30.0,
        "duration": 12_000_000,
        "materials": {
            "videos": [
                {
                    "id": "vid-mat-1",
                    "path": "C:/media/clip1.mp4",
                    "duration": 8_000_000,
                    "width": 1920,
                    "height": 1080,
                    "material_name": "clip1.mp4",
                    "type": "video",
                }
            ],
            "audios": [
                {
                    "id": "aud-mat-1",
                    "path": "C:/media/song.mp3",
                    "duration": 12_000_000,
                    "name": "song.mp3",
                }
            ],
            "texts": [
                {
                    "id": "txt-mat-1",
                    "content": "Hello World",
                    "font": "Roboto",
                    "font_size": 24.0,
                }
            ],
            "effects": [
                {"id": "fx-1", "name": "Glitch", "type": "video_effect"}
            ],
        },
        "tracks": [
            {
                "id": "track-video",
                "type": "video",
                "segments": [
                    {
                        "id": "seg-v1",
                        "material_id": "vid-mat-1",
                        "target_timerange": {"start": 0, "duration": 5_000_000},
                        "source_timerange": {"start": 1_000_000, "duration": 5_000_000},
                        "speed": 1.0,
                        "volume": 0.8,
                        "clip": {
                            "transform": {"x": 0.1, "y": -0.2},
                            "scale": {"x": 1.5, "y": 1.5},
                            "rotation": 90.0,
                            "alpha": 0.9,
                        },
                        "extra_material_refs": ["fx-1"],
                    }
                ],
            },
            {
                "id": "track-audio",
                "type": "audio",
                "segments": [
                    {
                        "id": "seg-a1",
                        "material_id": "aud-mat-1",
                        "target_timerange": {"start": 0, "duration": 12_000_000},
                        "source_timerange": {"start": 0, "duration": 12_000_000},
                        "volume": 1.0,
                        "fade": {
                            "fade_in_duration": 500_000,
                            "fade_out_duration": 750_000,
                        },
                    }
                ],
            },
            {
                "id": "track-text",
                "type": "text",
                "segments": [
                    {
                        "id": "seg-t1",
                        "material_id": "txt-mat-1",
                        "target_timerange": {"start": 2_000_000, "duration": 3_000_000},
                        "source_timerange": {"start": 0, "duration": 3_000_000},
                    }
                ],
            },
        ],
    }


class ReadProjectTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.draft_dir = Path(self._tmp.name) / "My Test Project"
        self.draft_dir.mkdir()
        (self.draft_dir / "draft_content.json").write_text(
            json.dumps(_sample_draft_content()), encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_project_basics(self):
        project = draft_reader.read_project(self.draft_dir)
        self.assertEqual(project.name, "My Test Project")
        self.assertEqual(project.width, 1920)
        self.assertEqual(project.height, 1080)
        self.assertEqual(project.resolution, (1920, 1080))
        self.assertEqual(project.fps, 30.0)
        self.assertEqual(project.draft_format, "draft_content")
        self.assertEqual(project.draft_dir, str(self.draft_dir))

    def test_track_and_segment_counts(self):
        project = draft_reader.read_project(self.draft_dir)
        self.assertEqual(len(project.tracks), 3)
        types = [t.track_type for t in project.tracks]
        self.assertEqual(
            types,
            [models.TrackType.VIDEO, models.TrackType.AUDIO, models.TrackType.TEXT],
        )
        for track in project.tracks:
            self.assertEqual(len(track.segments), 1)
        # Track index mirrors position in tracks[].
        self.assertEqual([t.index for t in project.tracks], [0, 1, 2])

    def test_segment_times_in_microseconds(self):
        project = draft_reader.read_project(self.draft_dir)
        video_seg = project.tracks[0].segments[0]
        self.assertEqual(video_seg.id, "seg-v1")
        self.assertEqual(video_seg.material_id, "vid-mat-1")
        self.assertEqual(video_seg.start, 0)
        self.assertEqual(video_seg.duration, 5_000_000)
        self.assertEqual(video_seg.end, 5_000_000)
        self.assertEqual(video_seg.source_start, 1_000_000)
        self.assertEqual(video_seg.source_duration, 5_000_000)
        self.assertAlmostEqual(video_seg.volume, 0.8)

    def test_segment_transform(self):
        project = draft_reader.read_project(self.draft_dir)
        tf = project.tracks[0].segments[0].transform
        self.assertIsNotNone(tf)
        self.assertAlmostEqual(tf.position_x, 0.1)
        self.assertAlmostEqual(tf.position_y, -0.2)
        self.assertAlmostEqual(tf.scale_x, 1.5)
        self.assertAlmostEqual(tf.rotation, 90.0)
        self.assertAlmostEqual(tf.opacity, 0.9)

    def test_segment_effects(self):
        project = draft_reader.read_project(self.draft_dir)
        effects = project.tracks[0].segments[0].effects
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0].effect_id, "fx-1")
        # Frozen contract: resolved material_type + human name (bug #2).
        self.assertEqual(effects[0].material_type, models.MaterialType.EFFECT)
        self.assertEqual(effects[0].name, "Glitch")

    def test_audio_fades(self):
        project = draft_reader.read_project(self.draft_dir)
        audio_seg = project.tracks[1].segments[0]
        self.assertEqual(audio_seg.fade_in, 500_000)
        self.assertEqual(audio_seg.fade_out, 750_000)
        # Audio segments carry no visual transform.
        self.assertIsNone(audio_seg.transform)

    def test_materials_pool(self):
        project = draft_reader.read_project(self.draft_dir)
        # video + audio + text + effect materials.
        self.assertEqual(len(project.materials), 4)

        vid = project.get_material("vid-mat-1")
        self.assertIsNotNone(vid)
        self.assertEqual(vid.material_type, models.MaterialType.VIDEO)
        self.assertEqual(vid.path, "C:/media/clip1.mp4")
        self.assertEqual(vid.duration, 8_000_000)
        self.assertEqual(vid.width, 1920)
        self.assertEqual(vid.height, 1080)
        self.assertEqual(vid.name, "clip1.mp4")

        aud = project.get_material("aud-mat-1")
        self.assertEqual(aud.material_type, models.MaterialType.AUDIO)
        self.assertEqual(aud.path, "C:/media/song.mp3")

        txt = project.get_material("txt-mat-1")
        self.assertEqual(txt.material_type, models.MaterialType.TEXT)
        self.assertEqual(txt.text, "Hello World")
        self.assertIsNotNone(txt.text_style)
        self.assertEqual(txt.text_style.font, "Roboto")
        self.assertAlmostEqual(txt.text_style.size, 24.0)

    def test_project_duration(self):
        project = draft_reader.read_project(self.draft_dir)
        # Max track end == audio track end == 12s; also matches stored_duration.
        self.assertEqual(project.duration, 12_000_000)
        self.assertEqual(project.stored_duration, 12_000_000)

    def test_text_content_json_blob(self):
        """A JSON-encoded rich-text content blob should be unwrapped to text."""
        content = _sample_draft_content()
        content["materials"]["texts"][0]["content"] = json.dumps({"text": "Rich Text"})
        (self.draft_dir / "draft_content.json").write_text(
            json.dumps(content), encoding="utf-8"
        )
        project = draft_reader.read_project(self.draft_dir)
        self.assertEqual(project.get_material("txt-mat-1").text, "Rich Text")

    def test_missing_json_raises(self):
        empty = Path(self._tmp.name) / "Empty"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError):
            draft_reader.read_project(empty)

    def test_read_project_by_name(self):
        drafts_dir = Path(self._tmp.name)
        project = draft_reader.read_project_by_name("My Test Project", drafts_dir)
        self.assertEqual(project.name, "My Test Project")
        with self.assertRaises(FileNotFoundError):
            draft_reader.read_project_by_name("Nonexistent", drafts_dir)


class ReadJsonRetryTest(unittest.TestCase):
    def test_succeeds_after_transient_failure(self):
        """First read sees a truncated file; a later attempt sees valid JSON."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "draft_content.json"
        path.write_text("{ this is not valid json", encoding="utf-8")

        good = {"canvas_config": {"width": 10, "height": 20}, "fps": 24.0}
        calls = {"n": 0}
        real_sleep = draft_reader.time.sleep

        def fake_sleep(_seconds):
            # On the first retry gap, repair the file so the next attempt parses.
            calls["n"] += 1
            path.write_text(json.dumps(good), encoding="utf-8")

        draft_reader.time.sleep = fake_sleep
        try:
            result = draft_reader._read_json_with_retry(path, attempts=3, delay=0.0)
        finally:
            draft_reader.time.sleep = real_sleep

        self.assertEqual(result, good)
        self.assertGreaterEqual(calls["n"], 1)

    def test_raises_after_exhausting_attempts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "draft_content.json"
        path.write_text("{ still broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            draft_reader._read_json_with_retry(path, attempts=2, delay=0.0)


class LocatorTest(unittest.TestCase):
    def test_detect_draft_format(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)

        newer = base / "newer"
        newer.mkdir()
        (newer / "draft_content.json").write_text("{}", encoding="utf-8")
        self.assertEqual(locator.detect_draft_format(newer), "draft_content")

        older = base / "older"
        older.mkdir()
        (older / "draft_info.json").write_text("{}", encoding="utf-8")
        self.assertEqual(locator.detect_draft_format(older), "draft_info")

        unknown = base / "unknown"
        unknown.mkdir()
        self.assertEqual(locator.detect_draft_format(unknown), "unknown")

    def test_draft_content_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "d"
        folder.mkdir()
        self.assertIsNone(locator.draft_content_path(folder))
        (folder / "draft_content.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            locator.draft_content_path(folder), folder / "draft_content.json"
        )

    def test_list_draft_folders(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        good = base / "proj"
        good.mkdir()
        (good / "draft_content.json").write_text("{}", encoding="utf-8")
        (base / "not-a-draft").mkdir()

        folders = locator.list_draft_folders(base)
        names = [f.name for f in folders]
        self.assertIn("proj", names)
        self.assertNotIn("not-a-draft", names)

    def test_environment_probes_never_raise(self):
        # These depend on the host OS/tools; they must return without raising.
        self.assertIsInstance(locator.is_ffprobe_available(), bool)
        self.assertIsInstance(locator.is_capcut_running(), bool)
        self.assertIsInstance(locator.candidate_drafts_dirs(), list)

    def test_probe_media_none_on_missing_file(self):
        missing = Path(tempfile.gettempdir()) / "definitely-not-here.mp4"
        # Either ffprobe is absent (None) or it fails on the missing file (None).
        self.assertIsNone(locator.probe_media_duration(missing))
        self.assertIsNone(locator.probe_media_dimensions(missing))


if __name__ == "__main__":
    unittest.main()
