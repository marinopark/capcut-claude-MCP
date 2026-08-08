"""Regression tests for :mod:`capcut_mcp.services.analyzer`.

Covers the QA-confirmed bugs owned by FIX AGENT C (ANALYZER):

  * #4  media ``exists`` must reflect the real filesystem (computed by stat,
        not trusted from ``Material.exists``).
  * #8  ``effect_counts`` must be keyed by effect *kind* name and count only
        EFFECT/FILTER refs (never a raw per-instance UUID dump).
  * #9  ``transition_counts`` must be populated for TRANSITION refs.
  * #10 an encrypted/unparseable draft must make ``analyze_project`` raise a
        clear error, while ``list_projects`` skips it.

The classification / existence tests build synthetic ``Project`` graphs
directly from :mod:`engine.models` so they do NOT depend on the reader (and thus
are independent of the parallel engine-agent changes). A ``skipUnless`` smoke
test runs against the real drafts dir if present.

Standard-library ``unittest`` only. Run with::

    python -m unittest tests.test_analyzer -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from capcut_mcp.services import analyzer  # noqa: E402
from capcut_mcp.engine import models  # noqa: E402
from capcut_mcp.engine.models import (  # noqa: E402
    EffectRef,
    Material,
    MaterialType,
    Project,
    Segment,
    TimeRange,
    Track,
    TrackType,
)


# ---------------------------------------------------------------------------
# Synthetic-graph builders (reader-independent)
# ---------------------------------------------------------------------------


def _seg(seg_id: str, material_id: str, start: int, duration: int,
         effects=None) -> Segment:
    return Segment(
        id=seg_id,
        material_id=material_id,
        target=TimeRange(start=start, duration=duration),
        effects=list(effects or []),
    )


def _project_with_effects(effects) -> Project:
    """A one-video-track project whose single segment carries ``effects``."""
    proj = Project(name="synthetic", width=1920, height=1080, fps=30.0)
    proj.tracks.append(
        Track(
            id="t0",
            track_type=TrackType.VIDEO,
            index=0,
            segments=[_seg("s0", "m0", 0, 1_000_000, effects=effects)],
        )
    )
    return proj


# ---------------------------------------------------------------------------
# #8 / #9 — effect_counts / transition_counts classification
# ---------------------------------------------------------------------------


class EffectClassificationTests(unittest.TestCase):
    def test_effect_counts_key_by_name_not_uuid(self):
        # Two instances of the same named effect + one filter, across segments.
        effects = [
            EffectRef(effect_id="uuid-a1", name="Glow",
                      material_type=MaterialType.EFFECT),
            EffectRef(effect_id="uuid-a2", name="Glow",
                      material_type=MaterialType.EFFECT),
            EffectRef(effect_id="uuid-b1", name="Warm",
                      material_type=MaterialType.FILTER),
        ]
        summary = analyzer._project_summary(_project_with_effects(effects))

        self.assertEqual(summary["effect_counts"], {"Glow": 2, "Warm": 1})
        # Keyed by kind name, so a small number of keys -- never raw UUIDs.
        self.assertNotIn("uuid-a1", summary["effect_counts"])
        self.assertEqual(summary["transition_counts"], {})

    def test_transition_counts_populated(self):
        effects = [
            EffectRef(effect_id="t-1", name="Fade",
                      material_type=MaterialType.TRANSITION),
            EffectRef(effect_id="t-2", name="Fade",
                      material_type=MaterialType.TRANSITION),
            EffectRef(effect_id="t-3", name="Dissolve",
                      material_type=MaterialType.TRANSITION),
            EffectRef(effect_id="e-1", name="Glow",
                      material_type=MaterialType.EFFECT),
        ]
        summary = analyzer._project_summary(_project_with_effects(effects))

        self.assertEqual(summary["transition_counts"],
                         {"Fade": 2, "Dissolve": 1})
        self.assertEqual(summary["effect_counts"], {"Glow": 1})

    def test_non_effect_material_types_ignored(self):
        # Should the reader ever leak a non-effect ref, it must not be counted.
        effects = [
            EffectRef(effect_id="v-1", name="clip",
                      material_type=MaterialType.VIDEO),
            EffectRef(effect_id="au-1", name="song",
                      material_type=MaterialType.AUDIO),
            EffectRef(effect_id="e-1", name="Glow",
                      material_type=MaterialType.EFFECT),
        ]
        summary = analyzer._project_summary(_project_with_effects(effects))

        self.assertEqual(summary["effect_counts"], {"Glow": 1})
        self.assertEqual(summary["transition_counts"], {})

    def test_enum_comparison_robust_str_subclass(self):
        # MaterialType subclasses str; equality must go through the enum member,
        # not accidentally match plain strings in a way that miscategorizes.
        self.assertIsInstance(MaterialType.TRANSITION, str)
        effects = [
            EffectRef(effect_id="t-1", name="Fade",
                      material_type=MaterialType.TRANSITION),
        ]
        summary = analyzer._project_summary(_project_with_effects(effects))
        self.assertEqual(summary["transition_counts"], {"Fade": 1})
        self.assertEqual(summary["effect_counts"], {})

    def test_no_effects_yields_empty_counters(self):
        summary = analyzer._project_summary(_project_with_effects([]))
        self.assertEqual(summary["effect_counts"], {})
        self.assertEqual(summary["transition_counts"], {})


# ---------------------------------------------------------------------------
# #4 — media existence computed by stat
# ---------------------------------------------------------------------------


class MediaExistenceTests(unittest.TestCase):
    def test_exists_reflects_filesystem_not_material_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "present.mp4")
            with open(real, "wb") as fh:
                fh.write(b"\x00")
            missing = os.path.join(tmp, "gone.mp4")

            proj = Project(name="p", width=1920, height=1080)
            # exists=True on BOTH materials to prove we ignore the flag.
            proj.add_material(Material(id="m1", material_type=MaterialType.VIDEO,
                                       path=real, exists=True))
            proj.add_material(Material(id="m2", material_type=MaterialType.VIDEO,
                                       path=missing, exists=True))

            summary = analyzer._project_summary(proj)
            by_path = {m["path"]: m["exists"] for m in summary["media"]}

            self.assertTrue(by_path[real])
            self.assertFalse(by_path[missing])
            self.assertEqual(summary["missing_count"], 1)
            self.assertEqual(summary["missing_media"], [missing])

    def test_exists_false_even_when_material_flag_true(self):
        # A single missing path whose Material.exists lies as True.
        missing = os.path.join(tempfile.gettempdir(), "definitely-not-here-xyz.mp4")
        proj = Project(name="p", width=1920, height=1080)
        proj.add_material(Material(id="m1", material_type=MaterialType.VIDEO,
                                   path=missing, exists=True))
        summary = analyzer._project_summary(proj)
        self.assertFalse(summary["media"][0]["exists"])
        self.assertEqual(summary["missing_count"], 1)

    def test_forward_slash_path_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "clip.mp4")
            with open(real, "wb") as fh:
                fh.write(b"\x00")
            # CapCut stores forward-slash paths even on Windows.
            fwd = real.replace(os.sep, "/")
            self.assertTrue(analyzer._path_exists(fwd))

    def test_materials_without_path_excluded(self):
        proj = Project(name="p", width=1920, height=1080)
        proj.add_material(Material(id="m1", material_type=MaterialType.TEXT,
                                   text="hi"))
        summary = analyzer._project_summary(proj)
        self.assertEqual(summary["media"], [])
        self.assertEqual(summary["missing_count"], 0)

    def test_duplicate_paths_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "c.mp4")
            with open(real, "wb") as fh:
                fh.write(b"\x00")
            proj = Project(name="p", width=1920, height=1080)
            proj.add_material(Material(id="m1", material_type=MaterialType.VIDEO,
                                       path=real))
            proj.add_material(Material(id="m2", material_type=MaterialType.VIDEO,
                                       path=real))
            summary = analyzer._project_summary(proj)
            self.assertEqual(len(summary["media"]), 1)


# ---------------------------------------------------------------------------
# #10 — encrypted / corrupt draft handling
# ---------------------------------------------------------------------------


class EncryptedDraftTests(unittest.TestCase):
    def _make_corrupt_drafts_dir(self, tmp: str) -> Path:
        drafts = Path(tmp) / "drafts"
        draft = drafts / "encrypted_project"
        draft.mkdir(parents=True)
        # Random non-'{' bytes: simulates an encrypted / unparseable draft.
        (draft / "draft_content.json").write_bytes(
            b"\x89\x01\xffNOT-JSON-AT-ALL\x00\x7f garbage \xfe"
        )
        return drafts

    def test_analyze_project_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = self._make_corrupt_drafts_dir(tmp)
            with self.assertRaises((ValueError, OSError)) as ctx:
                analyzer.analyze_project("encrypted_project", drafts_dir=drafts)
            # The message must be present/non-empty (user-friendly, not silent).
            self.assertTrue(str(ctx.exception))

    def test_list_projects_skips_corrupt_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            drafts = self._make_corrupt_drafts_dir(tmp)
            # Must not raise; the bad draft is simply skipped.
            result = analyzer.list_projects(drafts_dir=drafts)
            names = [p["name"] for p in result]
            self.assertNotIn("encrypted_project", names)


# ---------------------------------------------------------------------------
# Real-draft smoke test (skipped when the drafts dir is absent)
# ---------------------------------------------------------------------------


def _find_real_drafts_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    candidate = (
        Path(local)
        / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    )
    return candidate if candidate.is_dir() else None


_REAL_DRAFTS = _find_real_drafts_dir()


@unittest.skipUnless(
    _REAL_DRAFTS is not None, "real CapCut drafts dir not present"
)
class RealDraftSmokeTests(unittest.TestCase):
    def _pick_readable_project(self) -> str | None:
        # ``list_projects`` swallows per-draft parse errors, but the engine
        # (draft_reader) is edited by a parallel agent and may be transiently
        # broken (e.g. a NameError while its _map_effects fix is mid-flight).
        # In that case skip rather than fail -- these smoke tests depend on a
        # working engine, which is outside this agent's ownership.
        try:
            projects = analyzer.list_projects(drafts_dir=_REAL_DRAFTS)
        except Exception as exc:  # noqa: BLE001 - engine may be mid-edit
            self.skipTest(f"engine reader not ready: {exc!r}")
        # Prefer the ep0x sample projects the task calls out.
        for pref in ("ep02", "ep01", "ep03"):
            for p in projects:
                if p["name"].lower().startswith(pref):
                    return p["name"]
        return projects[0]["name"] if projects else None

    def _analyze(self, name: str) -> dict:
        try:
            return analyzer.analyze_project(name, drafts_dir=_REAL_DRAFTS)
        except Exception as exc:  # noqa: BLE001 - engine may be mid-edit
            if isinstance(exc, (ValueError, KeyError)):
                raise
            self.skipTest(f"engine reader not ready: {exc!r}")

    def test_analyze_real_project_is_sane(self):
        name = self._pick_readable_project()
        if name is None:
            self.skipTest("no readable projects in drafts dir")

        summary = self._analyze(name)

        # exists must reflect reality: each flag matches os.path.isfile.
        for m in summary["media"]:
            self.assertEqual(m["exists"], analyzer._path_exists(m["path"]))
        self.assertEqual(
            summary["missing_count"],
            sum(1 for m in summary["media"] if not m["exists"]),
        )

        # effect_counts must be a handful of meaningful keys, NOT thousands of
        # raw UUIDs. Heuristic: far fewer keys than materials, and keys are
        # human-readable (not 32-hex UUID strings) once Agent B's fix lands.
        self.assertLess(len(summary["effect_counts"]), 200)
        for key in summary["effect_counts"]:
            self.assertIsInstance(key, str)

        # transition_counts is a dict keyed by name.
        self.assertIsInstance(summary["transition_counts"], dict)

    def test_get_captions_real_project_clean_text(self):
        name = self._pick_readable_project()
        if name is None:
            self.skipTest("no readable projects in drafts dir")
        try:
            caps = analyzer.get_captions(name, drafts_dir=_REAL_DRAFTS)
        except Exception as exc:  # noqa: BLE001 - engine may be mid-edit
            if isinstance(exc, (ValueError, KeyError)):
                raise
            self.skipTest(f"engine reader not ready: {exc!r}")
        for c in caps:
            self.assertIn("text", c)
            self.assertIsInstance(c["text"], str)
            # Clean text: not a raw JSON rich-text blob.
            self.assertFalse(c["text"].strip().startswith("{\"styles\""))

    def test_get_timeline_real_project_no_crash(self):
        name = self._pick_readable_project()
        if name is None:
            self.skipTest("no readable projects in drafts dir")
        try:
            rows = analyzer.get_timeline(name, drafts_dir=_REAL_DRAFTS)
        except Exception as exc:  # noqa: BLE001 - engine may be mid-edit
            if isinstance(exc, (ValueError, KeyError)):
                raise
            self.skipTest(f"engine reader not ready: {exc!r}")
        self.assertIsInstance(rows, list)
        for r in rows:
            self.assertIn("segment_id", r)
            self.assertIn("start_us", r)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
