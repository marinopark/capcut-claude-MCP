"""Read-side business logic for the CapCut MCP server.

This module turns on-disk CapCut drafts into *digested* summaries suitable for
returning over MCP. It operates EXCLUSIVELY on :mod:`engine.models` dataclasses
(loaded via :mod:`engine.draft_reader`) and :mod:`engine.locator` for path /
environment detection. It never reads or writes CapCut JSON directly, and never
returns raw CapCut JSON.

Time convention: every time value in a returned dict is an integer count of
MICROSECONDS under a key suffixed ``_us``. ``server.py`` converts these to
seconds at the tool I/O boundary.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..engine import draft_reader, locator
from ..engine.models import (
    Caption,
    MaterialType,
    Project,
    Segment,
    Track,
    TrackType,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _path_exists(path: str) -> bool:
    """Return whether ``path`` points to an existing regular file on disk.

    The reader does NOT stat material paths, so ``Material.exists`` is not
    trustworthy (it defaults to True). The analyzer therefore computes existence
    itself: the raw path is normalized (CapCut stores forward-slash absolute
    paths even on Windows) and checked with ``os.path.isfile``. Any OS-level
    error (e.g. an over-long or malformed path) is treated as "does not exist".
    """
    if not path:
        return False
    try:
        normalized = os.path.normpath(path)
        return os.path.isfile(normalized)
    except OSError:
        return False


def _resolve_drafts_dir(drafts_dir: Optional[Path]) -> Optional[Path]:
    """Normalize a drafts-dir argument to a ``Path`` (or ``None``)."""
    if drafts_dir is None:
        return None
    return Path(drafts_dir)


def _load_project(name: str, drafts_dir: Optional[Path]) -> Project:
    """Load a project by name via the reader, honouring an explicit drafts dir."""
    return draft_reader.read_project_by_name(name, _resolve_drafts_dir(drafts_dir))


def _source_name(project: Project, segment: Segment) -> str:
    """Human-friendly source label for a segment (material name or path stem)."""
    material = project.get_material(segment.material_id)
    if material is None:
        return ""
    if material.name:
        return material.name
    if material.text:
        # For text materials, a short preview of the text is the best label.
        return material.text
    if material.path:
        return Path(material.path).name
    return ""


def _segment_row(project: Project, track: Track, segment: Segment) -> dict:
    """Compressed timeline row for one segment (no keyframes/effect params)."""
    return {
        "track_index": track.index,
        "track_type": track.track_type.value,
        "segment_id": segment.id,
        "start_us": segment.start,
        "duration_us": segment.duration,
        "end_us": segment.end,
        "source_name": _source_name(project, segment),
        "speed": segment.speed,
    }


def _project_summary(project: Project) -> dict:
    """Full analytical digest of a project (used by :func:`analyze_project`).

    Effect/transition counts are keyed by human-readable *kind* name (from
    ``EffectRef.name``), NOT by raw per-instance UUID. Classification is by
    ``EffectRef.material_type``: TRANSITION refs feed ``transition_counts``;
    EFFECT/FILTER refs feed ``effect_counts``. Any other material_type (which
    should not appear in ``Segment.effects`` per the engine contract) is ignored,
    so we never emit thousands of raw-UUID keys.

    Media existence is computed here by stat-ing each material path, because the
    reader does not stat and ``Material.exists`` is unreliable.
    """
    track_summary: list[dict] = []
    clip_count = 0
    effect_counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    has_captions = False

    for track in project.tracks:
        track_summary.append(
            {
                "track_index": track.index,
                "track_type": track.track_type.value,
                "segment_count": len(track.segments),
            }
        )
        clip_count += len(track.segments)
        if track.track_type == TrackType.TEXT and track.segments:
            has_captions = True

        for segment in track.segments:
            for eff in segment.effects:
                key = eff.name or eff.effect_id
                if eff.material_type == MaterialType.TRANSITION:
                    transition_counts[key] += 1
                elif eff.material_type in (MaterialType.EFFECT, MaterialType.FILTER):
                    effect_counts[key] += 1
                # Any other material_type is not a countable effect/transition;
                # ignore it rather than polluting the counts.

    # Media list: pooled materials with a filesystem path, plus computed
    # existence. Existence is stat-ed here (see #4) rather than trusting
    # ``Material.exists``. Missing paths are also surfaced separately so callers
    # can see "경로 + 존재 여부" at a glance.
    media: list[dict] = []
    missing_paths: list[str] = []
    seen_paths: set[str] = set()
    for material in project.materials.values():
        if not material.path:
            continue
        if material.path in seen_paths:
            continue
        seen_paths.add(material.path)
        exists = _path_exists(material.path)
        media.append({"path": material.path, "exists": exists})
        if not exists:
            missing_paths.append(material.path)

    return {
        "name": project.name,
        "resolution": [project.width, project.height],
        "fps": project.fps,
        "duration_us": project.duration,
        "track_summary": track_summary,
        "clip_count": clip_count,
        "media": media,
        "missing_count": len(missing_paths),
        "missing_media": missing_paths,
        "effect_counts": dict(effect_counts),
        "transition_counts": dict(transition_counts),
        "has_captions": has_captions,
    }


def _folder_mtime_iso(folder: Path) -> Optional[str]:
    """ISO-8601 modification timestamp for a draft folder, or ``None``."""
    try:
        ts = folder.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_projects(drafts_dir: Optional[Path] = None) -> list[dict]:
    """List all readable projects in the drafts directory.

    Returns a list of light summaries::

        [{"name", "modified_at", "duration_us", "resolution": [w, h], "fps"}]

    Projects that fail to parse are skipped (a robust listing must not crash on
    one bad draft). Returns an empty list if no drafts directory is found.
    """
    base = _resolve_drafts_dir(drafts_dir)
    if base is None:
        base = locator.find_drafts_dir()
    if base is None:
        return []

    results: list[dict] = []
    for folder in locator.list_draft_folders(base):
        try:
            project = draft_reader.read_project(folder)
        except (ValueError, OSError, FileNotFoundError):
            # Skip unreadable/corrupt drafts rather than failing the whole list.
            continue
        results.append(
            {
                "name": project.name,
                "modified_at": _folder_mtime_iso(folder),
                "duration_us": project.duration,
                "resolution": [project.width, project.height],
                "fps": project.fps,
            }
        )
    return results


def analyze_project(name: str, drafts_dir: Optional[Path] = None) -> dict:
    """Return a hierarchical analytical digest of one project.

    Includes track composition, clip count, total duration, the media list with
    computed existence flags (plus ``missing_count`` / ``missing_media``),
    effect/transition counts keyed by kind name, and whether the project has
    captions.

    If the draft is encrypted or otherwise unparseable, the reader raises a
    clean ``ValueError`` (e.g. "Draft appears encrypted...") which is allowed to
    propagate unchanged so the caller sees a helpful message.
    """
    project = _load_project(name, drafts_dir)
    return _project_summary(project)


def get_timeline(
    name: str,
    track_type: Optional[str] = None,
    drafts_dir: Optional[Path] = None,
) -> list[dict]:
    """Return the compressed per-segment timeline view.

    Each row is::

        {"track_index", "track_type", "segment_id", "start_us", "duration_us",
         "end_us", "source_name", "speed"}

    No keyframes or effect params (that detail lives in
    :func:`get_segment_detail`). ``track_type`` optionally filters to one track
    type (case-insensitive, e.g. ``"video"``). Rows are ordered by track index
    then segment start.
    """
    project = _load_project(name, drafts_dir)

    wanted: Optional[TrackType] = None
    if track_type is not None:
        try:
            wanted = TrackType(track_type.lower())
        except ValueError as exc:
            raise ValueError(f"Unknown track_type: {track_type!r}") from exc

    rows: list[dict] = []
    for track in project.tracks:
        if wanted is not None and track.track_type != wanted:
            continue
        for segment in track.segments:
            rows.append(_segment_row(project, track, segment))

    rows.sort(key=lambda r: (r["track_index"], r["start_us"]))
    return rows


def get_captions(name: str, drafts_dir: Optional[Path] = None) -> list[dict]:
    """Return the project's captions from text tracks, time-ordered.

    Each item is ``{"start_us", "duration_us", "end_us", "text"}``.
    """
    project = _load_project(name, drafts_dir)

    captions: list[Caption] = []
    for track in project.tracks_of_type(TrackType.TEXT):
        for segment in track.segments:
            material = project.get_material(segment.material_id)
            text = ""
            if material is not None and material.text is not None:
                text = material.text
            captions.append(
                Caption(text=text, start=segment.start, duration=segment.duration)
            )

    captions.sort(key=lambda c: c.start)
    return [
        {
            "start_us": c.start,
            "duration_us": c.duration,
            "end_us": c.end,
            "text": c.text,
        }
        for c in captions
    ]


def get_segment_detail(
    name: str,
    segment_id: str,
    drafts_dir: Optional[Path] = None,
) -> dict:
    """Return the full digest of ONE segment.

    Includes ids, times (``_us``), speed, volume, fades, transform, text style,
    keyframes, and effects with their params. Raises :class:`KeyError` if no
    segment with ``segment_id`` exists in the project.
    """
    project = _load_project(name, drafts_dir)

    segment: Optional[Segment] = None
    owning_track: Optional[Track] = None
    for track in project.tracks:
        for seg in track.segments:
            if seg.id == segment_id:
                segment = seg
                owning_track = track
                break
        if segment is not None:
            break

    if segment is None or owning_track is None:
        raise KeyError(f"Segment {segment_id!r} not found in project {name!r}")

    material = project.get_material(segment.material_id)

    transform = None
    if segment.transform is not None:
        t = segment.transform
        transform = {
            "position_x": t.position_x,
            "position_y": t.position_y,
            "scale_x": t.scale_x,
            "scale_y": t.scale_y,
            "rotation": t.rotation,
            "opacity": t.opacity,
        }

    text_style = None
    if segment.text_style is not None:
        ts = segment.text_style
        text_style = {
            "font": ts.font,
            "size": ts.size,
            "color": list(ts.color) if ts.color is not None else None,
            "bold": ts.bold,
            "italic": ts.italic,
            "underline": ts.underline,
            "alignment": ts.alignment,
            "position_x": ts.position_x,
            "position_y": ts.position_y,
            "stroke_color": (
                list(ts.stroke_color) if ts.stroke_color is not None else None
            ),
            "stroke_width": ts.stroke_width,
        }

    keyframes = [
        {
            "property_name": kf.property_name,
            "time_us": kf.time,
            "value": kf.value,
            "curve": list(kf.curve) if kf.curve is not None else None,
        }
        for kf in segment.keyframes
    ]

    effects = [
        {
            "effect_id": eff.effect_id,
            "name": eff.name,
            "material_type": eff.material_type.value,
            "params": dict(eff.params),
            "duration_us": eff.duration,
        }
        for eff in segment.effects
    ]

    return {
        "segment_id": segment.id,
        "material_id": segment.material_id,
        "track_index": owning_track.index,
        "track_type": owning_track.track_type.value,
        "source_name": _source_name(project, segment),
        "source_path": material.path if material is not None else None,
        "start_us": segment.start,
        "duration_us": segment.duration,
        "end_us": segment.end,
        "source_start_us": segment.source_start,
        "source_duration_us": segment.source_duration,
        "speed": segment.speed,
        "volume": segment.volume,
        "fade_in_us": segment.fade_in,
        "fade_out_us": segment.fade_out,
        "transform": transform,
        "text_style": text_style,
        "text": material.text if material is not None else None,
        "keyframes": keyframes,
        "effects": effects,
    }


def doctor(drafts_dir: Optional[Path] = None) -> dict:
    """Report the environment / diagnostics for the CapCut integration.

    Uses :mod:`engine.locator` for all detection. Returns::

        {"drafts_dir", "drafts_dir_exists", "candidate_dirs", "capcut_running",
         "draft_format", "ffprobe_available", "readable", "writable",
         "draft_count"}
    """
    base = _resolve_drafts_dir(drafts_dir)
    if base is None:
        base = locator.find_drafts_dir()

    candidate_dirs = [str(p) for p in locator.candidate_drafts_dirs()]

    drafts_dir_exists = False
    readable = False
    writable = False
    draft_count = 0
    draft_format = "unknown"
    draft_folders: list[Path] = []

    if base is not None:
        try:
            drafts_dir_exists = base.is_dir()
        except OSError:
            drafts_dir_exists = False

        if drafts_dir_exists:
            draft_folders = locator.list_draft_folders(base)
            draft_count = len(draft_folders)
            # Readability: we could enumerate the directory.
            try:
                _ = list(base.iterdir())
                readable = True
            except OSError:
                readable = False
            # Writability: OS-level access check.
            try:
                import os as _os

                writable = _os.access(str(base), _os.W_OK)
            except OSError:
                writable = False

    if draft_folders:
        draft_format = locator.detect_draft_format(draft_folders[0])

    return {
        "drafts_dir": str(base) if base is not None else None,
        "drafts_dir_exists": drafts_dir_exists,
        "candidate_dirs": candidate_dirs,
        "capcut_running": locator.is_capcut_running(),
        "draft_format": draft_format,
        "ffprobe_available": locator.is_ffprobe_available(),
        "readable": readable,
        "writable": writable,
        "draft_count": draft_count,
    }
