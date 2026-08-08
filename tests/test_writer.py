"""Unit tests for ``engine.draft_writer`` (self-contained).

Builds a small Project in memory, writes it to a tempdir, and asserts:
  * the draft folder is created atomically (no leftover temp folders),
  * ``draft_content.json`` exists with expected serialized fields/times (µs),
  * ``validate_media_paths`` reports missing media,
  * overwriting an existing draft folder is prevented (FileExistsError).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")

from capcut_mcp.engine import draft_writer, models  # noqa: E402
from capcut_mcp.engine.models import (  # noqa: E402
    Material,
    MaterialType,
    Project,
    Segment,
    TextStyle,
    TimeRange,
    Track,
    TrackType,
    Transform,
)


def _make_project(video_path: str) -> Project:
    """Build a Project with a video track, an audio track, and a text track."""
    project = Project(name="Test Project", width=1920, height=1080, fps=30.0)

    # Video material + segment.
    project.add_material(
        Material(
            id="mat-video-1",
            material_type=MaterialType.VIDEO,
            path=video_path,
            name="clip.mp4",
            duration=models.sec_to_us(5.0),
            width=1920,
            height=1080,
        )
    )
    video_seg = Segment(
        id="seg-video-1",
        material_id="mat-video-1",
        target=TimeRange(start=0, duration=models.sec_to_us(4.0)),
        source_start=models.sec_to_us(0.5),
        source_duration=models.sec_to_us(4.0),
        speed=1.0,
        volume=0.8,
        transform=Transform(position_x=0.1, position_y=-0.2, scale_x=1.2, opacity=0.9),
    )
    project.tracks.append(
        Track(id="track-video", track_type=TrackType.VIDEO, index=0,
              segments=[video_seg])
    )

    # Audio material + segment with fades.
    project.add_material(
        Material(
            id="mat-audio-1",
            material_type=MaterialType.AUDIO,
            path=video_path,  # reuse same file path for the test
            name="music.mp3",
            duration=models.sec_to_us(10.0),
        )
    )
    audio_seg = Segment(
        id="seg-audio-1",
        material_id="mat-audio-1",
        target=TimeRange(start=0, duration=models.sec_to_us(4.0)),
        source_start=0,
        source_duration=models.sec_to_us(4.0),
        volume=0.5,
        fade_in=models.sec_to_us(1.0),
        fade_out=models.sec_to_us(2.0),
    )
    project.tracks.append(
        Track(id="track-audio", track_type=TrackType.AUDIO, index=1,
              segments=[audio_seg])
    )

    # Text material + segment.
    style = TextStyle(font="Arial", size=24.0, color=(1.0, 0.0, 0.0, 1.0),
                      alignment="center", position_y=-0.8)
    project.add_material(
        Material(
            id="mat-text-1",
            material_type=MaterialType.TEXT,
            name="caption",
            text="Hello world",
            text_style=style,
        )
    )
    text_seg = Segment(
        id="seg-text-1",
        material_id="mat-text-1",
        target=TimeRange(start=models.sec_to_us(1.0),
                         duration=models.sec_to_us(2.0)),
        text_style=style,
    )
    project.tracks.append(
        Track(id="track-text", track_type=TrackType.TEXT, index=2,
              segments=[text_seg])
    )

    return project


class WriteDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.drafts_dir = self.tmp / "drafts"
        # A real media file so validate_media_paths sees it as existing.
        self.media = self.tmp / "clip.mp4"
        self.media.write_bytes(b"\x00\x00fake media")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_creates_folder_and_content(self) -> None:
        project = _make_project(str(self.media))
        result = draft_writer.write_draft(project, self.drafts_dir)

        self.assertTrue(result.exists())
        self.assertTrue(result.is_dir())
        self.assertEqual(result.name, "Test Project")
        content_path = result / draft_writer.DRAFT_CONTENT_FILENAME
        self.assertTrue(content_path.is_file())

        with open(content_path, encoding="utf-8") as fh:
            content = json.load(fh)

        # Canvas config + fps.
        self.assertEqual(content["canvas_config"]["width"], 1920)
        self.assertEqual(content["canvas_config"]["height"], 1080)
        self.assertEqual(content["fps"], 30.0)

        # Duration in µs (max track end = 4.0s video/audio).
        self.assertEqual(content["duration"], models.sec_to_us(4.0))

        # Three tracks.
        self.assertEqual(len(content["tracks"]), 3)
        types = {t["type"] for t in content["tracks"]}
        self.assertEqual(types, {"video", "audio", "text"})

        # Video segment times in µs.
        vtrack = next(t for t in content["tracks"] if t["type"] == "video")
        vseg = vtrack["segments"][0]
        self.assertEqual(vseg["target_timerange"]["start"], 0)
        self.assertEqual(vseg["target_timerange"]["duration"],
                         models.sec_to_us(4.0))
        self.assertEqual(vseg["source_timerange"]["start"],
                         models.sec_to_us(0.5))
        self.assertIn("clip", vseg)

        # Audio fades in µs.
        atrack = next(t for t in content["tracks"] if t["type"] == "audio")
        aseg = atrack["segments"][0]
        self.assertEqual(aseg["audio_fade"]["fade_in_duration"],
                         models.sec_to_us(1.0))
        self.assertEqual(aseg["audio_fade"]["fade_out_duration"],
                         models.sec_to_us(2.0))

        # Text material content: CapCut renders from a styled JSON-string
        # ``content`` of shape {"text":...,"styles":[...]} (bug #6), NOT a bare
        # plain string.
        texts = content["materials"]["texts"]
        self.assertEqual(len(texts), 1)
        raw_content = texts[0]["content"]
        self.assertIsInstance(raw_content, str)
        parsed = json.loads(raw_content)
        self.assertEqual(parsed["text"], "Hello world")
        self.assertIsInstance(parsed.get("styles"), list)
        self.assertTrue(parsed["styles"])

        # Video material present with path + dims.
        videos = content["materials"]["videos"]
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["width"], 1920)
        self.assertTrue(videos[0]["path"].endswith("clip.mp4"))

    def test_atomic_no_temp_leftovers(self) -> None:
        project = _make_project(str(self.media))
        draft_writer.write_draft(project, self.drafts_dir)

        # Only the final folder should remain — no capcut_draft_* temp dirs.
        entries = os.listdir(self.drafts_dir)
        self.assertEqual(entries, ["Test Project"])
        leftovers = [e for e in entries if e.startswith("capcut_draft_")]
        self.assertEqual(leftovers, [])

    def test_overwrite_prevented(self) -> None:
        project = _make_project(str(self.media))
        draft_writer.write_draft(project, self.drafts_dir)

        project2 = _make_project(str(self.media))
        with self.assertRaises(FileExistsError):
            draft_writer.write_draft(project2, self.drafts_dir)

    def test_force_overwrite(self) -> None:
        project = _make_project(str(self.media))
        first = draft_writer.write_draft(project, self.drafts_dir)
        # Drop a marker file to prove the old folder was replaced.
        (first / "marker.txt").write_text("old")

        project2 = _make_project(str(self.media))
        second = draft_writer.write_draft(project2, self.drafts_dir, force=True)
        self.assertEqual(first, second)
        self.assertFalse((second / "marker.txt").exists())

    def test_validate_media_paths_reports_missing(self) -> None:
        missing = str(self.tmp / "does_not_exist.mp4")
        project = _make_project(missing)
        warnings = draft_writer.validate_media_paths(project)

        self.assertTrue(warnings)
        self.assertTrue(any("does_not_exist.mp4" in w for w in warnings))
        # The missing material should be flagged exists=False.
        vmat = project.get_material("mat-video-1")
        self.assertFalse(vmat.exists)
        # Path normalized to absolute.
        self.assertTrue(os.path.isabs(vmat.path))

    def test_validate_media_paths_present(self) -> None:
        project = _make_project(str(self.media))
        warnings = draft_writer.validate_media_paths(project)
        self.assertEqual(warnings, [])
        self.assertTrue(project.get_material("mat-video-1").exists)

    def test_sanitize_folder_name(self) -> None:
        self.assertEqual(
            draft_writer._sanitize_folder_name('a/b:c*d?'), "a_b_c_d_"
        )
        self.assertEqual(draft_writer._sanitize_folder_name("  spaced  "),
                         "spaced")
        # Empty -> generated fallback.
        self.assertTrue(draft_writer._sanitize_folder_name("").startswith(
            "project_"))


if __name__ == "__main__":
    unittest.main()
