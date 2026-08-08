"""Round-trip consistency test: writer → reader.

Builds a Project, writes it to a tempdir via ``draft_writer.write_draft``,
reads it back via ``draft_reader.read_project``, and asserts the reloaded
Project matches the original (track/segment counts, times in µs, resolution,
fps, material paths, captions/text).

``draft_reader`` is authored by a parallel agent and may not exist yet. When it
is absent (or lacks ``read_project``) the whole case is skipped so the rest of
the suite still runs.
"""

from __future__ import annotations

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

try:
    from capcut_mcp.engine import draft_reader  # noqa: E402
    _HAS_READER = hasattr(draft_reader, "read_project")
except Exception:  # pragma: no cover - reader not written yet
    draft_reader = None  # type: ignore[assignment]
    _HAS_READER = False


def _make_project(video_path: str) -> Project:
    project = Project(name="Roundtrip Project", width=1280, height=720, fps=24.0)

    project.add_material(
        Material(
            id="mat-video-1",
            material_type=MaterialType.VIDEO,
            path=video_path,
            name="clip.mp4",
            duration=models.sec_to_us(8.0),
            width=1280,
            height=720,
        )
    )
    project.tracks.append(
        Track(
            id="track-video",
            track_type=TrackType.VIDEO,
            index=0,
            segments=[
                Segment(
                    id="seg-video-1",
                    material_id="mat-video-1",
                    target=TimeRange(start=0, duration=models.sec_to_us(6.0)),
                    source_start=models.sec_to_us(1.0),
                    source_duration=models.sec_to_us(6.0),
                    speed=1.0,
                    volume=1.0,
                    transform=Transform(scale_x=1.0, scale_y=1.0),
                )
            ],
        )
    )

    style = TextStyle(font="Arial", size=20.0, alignment="center")
    project.add_material(
        Material(
            id="mat-text-1",
            material_type=MaterialType.TEXT,
            name="cap",
            text="Round trip caption",
            text_style=style,
        )
    )
    project.tracks.append(
        Track(
            id="track-text",
            track_type=TrackType.TEXT,
            index=1,
            segments=[
                Segment(
                    id="seg-text-1",
                    material_id="mat-text-1",
                    target=TimeRange(
                        start=models.sec_to_us(2.0),
                        duration=models.sec_to_us(3.0),
                    ),
                    text_style=style,
                )
            ],
        )
    )
    return project


@unittest.skipUnless(
    _HAS_READER, "draft_reader.read_project not available yet (parallel agent)"
)
class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.drafts_dir = self.tmp / "drafts"
        self.media = self.tmp / "clip.mp4"
        self.media.write_bytes(b"fake media bytes")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        original = _make_project(str(self.media))
        draft_dir = draft_writer.write_draft(original, self.drafts_dir)

        reloaded = draft_reader.read_project(draft_dir)

        # Canvas / resolution / fps.
        self.assertEqual(reloaded.resolution, original.resolution)
        self.assertEqual(reloaded.width, 1280)
        self.assertEqual(reloaded.height, 720)
        self.assertAlmostEqual(reloaded.fps, 24.0, places=3)

        # Track + segment counts.
        self.assertEqual(len(reloaded.tracks), len(original.tracks))
        by_type = {t.track_type: t for t in reloaded.tracks}
        self.assertIn(TrackType.VIDEO, by_type)
        self.assertIn(TrackType.TEXT, by_type)

        vtrack = by_type[TrackType.VIDEO]
        self.assertEqual(len(vtrack.segments), 1)
        vseg = vtrack.segments[0]

        # Times in µs preserved.
        self.assertEqual(vseg.target.start, 0)
        self.assertEqual(vseg.target.duration, models.sec_to_us(6.0))
        self.assertEqual(vseg.source_start, models.sec_to_us(1.0))
        self.assertEqual(vseg.source_duration, models.sec_to_us(6.0))

        # Material path preserved (absolute).
        vmat = reloaded.get_material(vseg.material_id)
        self.assertIsNotNone(vmat)
        self.assertTrue(vmat.path.endswith("clip.mp4"))

        # Text / caption content preserved.
        ttrack = by_type[TrackType.TEXT]
        self.assertEqual(len(ttrack.segments), 1)
        tseg = ttrack.segments[0]
        self.assertEqual(tseg.target.start, models.sec_to_us(2.0))
        self.assertEqual(tseg.target.duration, models.sec_to_us(3.0))
        tmat = reloaded.get_material(tseg.material_id)
        self.assertIsNotNone(tmat)
        self.assertEqual(tmat.text, "Round trip caption")

        # Overall duration preserved (µs).
        self.assertEqual(reloaded.duration, original.duration)


if __name__ == "__main__":
    unittest.main()
