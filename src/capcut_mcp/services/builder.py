"""Write-side build-state logic for the CapCut MCP server.

Maintains an in-memory registry of in-progress :class:`Project` build states
keyed by name, mutates them via :mod:`engine.models` helpers, and persists them
atomically via :mod:`engine.draft_writer`. Uses :mod:`engine.locator` for media
probing and the CapCut-running safety check.

Operates EXCLUSIVELY on domain models; never touches CapCut JSON. Every time
value accepted here is an integer count of MICROSECONDS (``server.py`` converts
from seconds at the boundary).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Optional

from ..engine import draft_writer, locator
from ..engine.models import (
    Caption,
    Material,
    MaterialType,
    Project,
    Segment,
    TextStyle,
    TimeRange,
    Track,
    TrackType,
)


# ---------------------------------------------------------------------------
# Build-state registry
# ---------------------------------------------------------------------------

#: name -> in-progress Project. Populated by ``create_project``.
_PROJECTS: dict[str, Project] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _new_id() -> str:
    """Return a fresh unique id (uuid4 hex)."""
    return uuid.uuid4().hex


def _get_project(name: str) -> Project:
    """Return the in-progress project ``name`` or raise a clear error."""
    project = _PROJECTS.get(name)
    if project is None:
        raise KeyError(
            f"No in-progress project named {name!r}. Call create_project first."
        )
    return project


def _ensure_track(project: Project, track_type: TrackType, track_index: Optional[int] = None) -> Track:
    """Return an existing track of ``track_type`` or create a new one.

    If ``track_index`` is given and a track of that type already exists at that
    positional index (among tracks of the same type), it is reused. Otherwise a
    new track is appended. New tracks receive ``index`` equal to their position
    in ``project.tracks`` (mirrors CapCut render order).
    """
    same_type = project.tracks_of_type(track_type)

    if track_index is not None and 0 <= track_index < len(same_type):
        return same_type[track_index]

    if track_index is None and same_type:
        # Default: reuse the first track of this type.
        return same_type[0]

    track = Track(
        id=_new_id(),
        track_type=track_type,
        segments=[],
        index=len(project.tracks),
        name=track_type.value,
    )
    project.tracks.append(track)
    return track


def _new_text_track(project: Project) -> Track:
    """Always append and return a brand-new text track (never reuse existing)."""
    track = Track(
        id=_new_id(),
        track_type=TrackType.TEXT,
        segments=[],
        index=len(project.tracks),
        name=TrackType.TEXT.value,
    )
    project.tracks.append(track)
    return track


def _parse_color(hex_str: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    """Parse ``"#RRGGBB"`` (or ``"#RRGGBBAA"``) into normalized RGBA floats.

    Returns ``None`` for ``None`` input. Raises :class:`ValueError` for a
    malformed string so the caller gets a clear failure.
    """
    if hex_str is None:
        return None
    s = hex_str.strip().lstrip("#")
    if len(s) == 6:
        r, g, b = s[0:2], s[2:4], s[4:6]
        a = "ff"
    elif len(s) == 8:
        r, g, b, a = s[0:2], s[2:4], s[4:6], s[6:8]
    else:
        raise ValueError(
            f"Invalid color {hex_str!r}; expected #RRGGBB or #RRGGBBAA."
        )
    try:
        rf, gf, bf, af = (int(c, 16) / 255.0 for c in (r, g, b, a))
    except ValueError as exc:
        raise ValueError(f"Invalid color {hex_str!r}: {exc}") from exc
    return (rf, gf, bf, af)


def _speed_scaled_duration(source_duration: int, speed: float) -> int:
    """Return the on-timeline duration for ``source_duration`` played at ``speed``.

    A segment played at 2x occupies half the timeline; at 0.5x, double. Guards
    against non-positive ``speed`` (falls back to 1.0, i.e. no scaling).
    """
    if speed is None or speed <= 0:
        speed = 1.0
    return int(round(source_duration / speed))


def _resolve_media_duration(path: str, duration_us: Optional[int], kind: str) -> int:
    """Resolve a media duration, probing via ffprobe if not explicitly given.

    Raises :class:`ValueError` (asking for an explicit duration) when the
    duration is unknown and ffprobe cannot determine it.
    """
    if duration_us is not None:
        return duration_us
    probed = locator.probe_media_duration(Path(path))
    if probed is None:
        raise ValueError(
            f"Could not determine {kind} duration for {path!r} "
            "(ffprobe unavailable or failed). Provide an explicit duration."
        )
    return probed


def _resolve_video_dimensions(path: str, project: Project) -> tuple[int, int]:
    """Resolve a video's pixel dimensions for its material entry.

    Bug #27: CapCut refuses to open a project whose video material has
    ``width``/``height`` of 0 (0×0 breaks the editor's layout math). Probe via
    ffprobe when available; otherwise fall back to the project's canvas size so
    the material always carries non-zero, plausible dimensions.
    """
    dims = locator.probe_media_dimensions(Path(path))
    if dims is not None and dims[0] > 0 and dims[1] > 0:
        return int(dims[0]), int(dims[1])
    return int(project.width), int(project.height)


# ---------------------------------------------------------------------------
# Public API — project + segment construction
# ---------------------------------------------------------------------------


def create_project(name: str, width: int, height: int, fps: float = 30.0) -> dict:
    """Create a new in-memory project and register it.

    Raises :class:`ValueError` if a project of the same name is already in
    progress. Returns ``{"name","width","height","fps","created": true}``.
    """
    if name in _PROJECTS:
        raise ValueError(f"A project named {name!r} is already in progress.")
    project = Project(name=name, width=int(width), height=int(height), fps=float(fps))
    _PROJECTS[name] = project
    return {
        "name": name,
        "width": project.width,
        "height": project.height,
        "fps": project.fps,
        "created": True,
    }


def add_video(
    project: str,
    path: str,
    start_us: int,
    duration_us: Optional[int] = None,
    track: Optional[int] = None,
    speed: float = 1.0,
    volume: float = 1.0,
) -> dict:
    """Add a video clip (and its material) to a project's video track.

    If ``duration_us`` is ``None`` the intrinsic duration is probed via ffprobe;
    if that is unavailable a :class:`ValueError` requiring an explicit duration
    is raised.
    """
    proj = _get_project(project)
    abs_path = str(Path(path).expanduser().resolve())
    duration = _resolve_media_duration(abs_path, duration_us, "video")

    speed_val = float(speed)
    target_duration = _speed_scaled_duration(duration, speed_val)

    vid_width, vid_height = _resolve_video_dimensions(abs_path, proj)
    material = Material(
        id=_new_id(),
        material_type=MaterialType.VIDEO,
        path=abs_path,
        name=Path(abs_path).name,
        duration=duration,
        width=vid_width,
        height=vid_height,
    )
    proj.add_material(material)

    track_obj = _ensure_track(proj, TrackType.VIDEO, track)
    segment = Segment(
        id=_new_id(),
        material_id=material.id,
        target=TimeRange(start=int(start_us), duration=target_duration),
        source_start=0,
        source_duration=int(duration),
        speed=speed_val,
        volume=float(volume),
    )
    track_obj.segments.append(segment)

    return {
        "segment_id": segment.id,
        "track_index": track_obj.index,
        "duration_us": target_duration,
    }


def add_audio(
    project: str,
    path: str,
    start_us: int,
    duration_us: Optional[int] = None,
    volume: float = 1.0,
    fade_in_us: int = 0,
    fade_out_us: int = 0,
) -> dict:
    """Add an audio clip (and its material) to a project's audio track.

    Same ffprobe duration-fallback rule as :func:`add_video`.
    """
    proj = _get_project(project)
    abs_path = str(Path(path).expanduser().resolve())
    duration = _resolve_media_duration(abs_path, duration_us, "audio")

    material = Material(
        id=_new_id(),
        material_type=MaterialType.AUDIO,
        path=abs_path,
        name=Path(abs_path).name,
        duration=duration,
    )
    proj.add_material(material)

    track_obj = _ensure_track(proj, TrackType.AUDIO)
    segment = Segment(
        id=_new_id(),
        material_id=material.id,
        target=TimeRange(start=int(start_us), duration=int(duration)),
        source_start=0,
        source_duration=int(duration),
        volume=float(volume),
        fade_in=int(fade_in_us),
        fade_out=int(fade_out_us),
    )
    track_obj.segments.append(segment)

    return {
        "segment_id": segment.id,
        "track_index": track_obj.index,
        "duration_us": duration,
    }


def add_text(
    project: str,
    text: str,
    start_us: int,
    duration_us: int,
    font: Optional[str] = None,
    size: float = 15.0,
    color: Optional[str] = None,
    position: Optional[tuple[float, float]] = None,
) -> dict:
    """Add a text/caption segment (and its text material + style).

    ``color`` accepts a hex string (``"#RRGGBB"``). ``position`` is a normalized
    ``(x, y)`` pair in ``-1..1`` (0,0 == centre).
    """
    proj = _get_project(project)

    pos_x = position[0] if position is not None else None
    pos_y = position[1] if position is not None else None

    style = TextStyle(
        font=font,
        size=float(size),
        color=_parse_color(color),
        position_x=pos_x,
        position_y=pos_y,
    )

    material = Material(
        id=_new_id(),
        material_type=MaterialType.TEXT,
        name=text[:40],
        text=text,
        text_style=style,
    )
    proj.add_material(material)

    track_obj = _ensure_track(proj, TrackType.TEXT)
    segment = Segment(
        id=_new_id(),
        material_id=material.id,
        target=TimeRange(start=int(start_us), duration=int(duration_us)),
        text_style=style,
    )
    track_obj.segments.append(segment)

    return {
        "segment_id": segment.id,
        "track_index": track_obj.index,
        "duration_us": int(duration_us),
    }


def add_subtitles_from_srt(project: str, srt_path: str) -> dict:
    """Parse an SRT file and add one text segment per cue on a text track.

    Returns ``{"added": <count>, "track_index"}``.
    """
    proj = _get_project(project)

    captions = parse_srt_file(srt_path)
    # A dedicated fresh text track for this SRT batch — never reuse an existing
    # text track (which may hold manually-added text). (Bug #23.)
    track_obj = _new_text_track(proj)

    for cap in captions:
        style = TextStyle()
        material = Material(
            id=_new_id(),
            material_type=MaterialType.TEXT,
            name=cap.text[:40],
            text=cap.text,
            text_style=style,
        )
        proj.add_material(material)
        segment = Segment(
            id=_new_id(),
            material_id=material.id,
            target=TimeRange(start=cap.start, duration=cap.duration),
            text_style=style,
        )
        track_obj.segments.append(segment)

    return {"added": len(captions), "track_index": track_obj.index}


def save_draft(
    project: str,
    drafts_dir: Optional[Path] = None,
    open_hint: bool = True,
    force: bool = False,
) -> dict:
    """Persist a project to a new draft folder (atomically).

    SAFETY: two independent guards, never conflated:

      * ``force`` ONLY overrides the CapCut-running process check (safety rule
        #1). It does NOT enable overwriting an existing draft folder.
      * Writes are always new-folder-only (safety rule #2). If a draft folder of
        the same name already exists, this returns a graceful warning dict and
        NEVER deletes/overwrites the existing draft — regardless of ``force``.

    ``open_hint`` controls whether the "Restart CapCut to see it" guidance is
    included in a successful response.
    """
    proj = _get_project(project)

    # --- Safety rule #1: CapCut-running check (force overrides ONLY this). -----
    # Checked before staging; re-checked immediately before the atomic move to
    # narrow (best-effort) the TOCTOU window. is_capcut_running() is fail-open
    # (returns False on subprocess error), which we keep deliberately.
    if not force and locator.is_capcut_running():
        return {
            "saved": False,
            "capcut_running": True,
            "warning": (
                "CapCut appears to be running. Writing a draft while CapCut is "
                "open can corrupt or clobber the project on next save. Close "
                "CapCut and retry, or pass force=true to override."
            ),
        }

    base = Path(drafts_dir) if drafts_dir is not None else locator.find_drafts_dir()
    if base is None:
        raise ValueError(
            "Could not locate the CapCut drafts directory. Pass an explicit "
            "drafts_dir."
        )

    # Surface missing-media warnings for the response (also sets Material.exists).
    # write_draft performs its own internal validation; we call once here so the
    # warnings reach the returned dict (writer does not return them). (Bug #22.)
    warnings = draft_writer.validate_media_paths(proj)

    # --- Safety rule #1 (re-check): best-effort TOCTOU narrowing. --------------
    if not force and locator.is_capcut_running():
        return {
            "saved": False,
            "capcut_running": True,
            "warning": (
                "CapCut started while preparing the draft. Aborting to avoid "
                "clobbering an open project. Close CapCut and retry, or pass "
                "force=true to override."
            ),
        }

    # --- Safety rule #2: new-folder-only. Never overwrite. (Bugs #5, #20.) -----
    # Call write_draft WITHOUT its overwrite flag; force is NOT forwarded here.
    try:
        draft_dir = draft_writer.write_draft(proj, base)
    except FileExistsError as exc:
        return {
            "saved": False,
            "reason": "draft_exists",
            "warning": (
                f"A draft folder for {proj.name!r} already exists. Refusing to "
                "overwrite it (writes are new-folder-only in v1). Rename the "
                "project or remove the existing draft, then retry."
            ),
            "detail": str(exc),
        }

    result = {
        "saved": True,
        "draft_dir": str(draft_dir),
        "warnings": warnings,
    }
    if open_hint:
        result["message"] = "Restart CapCut to see this project in the list."
    return result


# ---------------------------------------------------------------------------
# SRT parsing (pure, standalone)
# ---------------------------------------------------------------------------

#: HH:MM:SS,mmm --> HH:MM:SS,mmm.
#: Hours allow up to 3 digits (>=100h, bug #19a). The fractional part is greedy
#: (\d+) so an over-long 4+ digit milliseconds field is captured whole and then
#: truncated in _srt_timestamp_to_us rather than breaking the cue (bug #19b).
#: Comma or dot decimal separator tolerated; fractional part optional.
_SRT_TIME_RE = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})(?:[,.](?P<ms>\d+))?"
    r"\s*-->\s*"
    r"(?P<eh>\d{1,3}):(?P<em>\d{2}):(?P<es>\d{2})(?:[,.](?P<ems>\d+))?"
)


def _srt_timestamp_to_us(h: str, m: str, s: str, ms: Optional[str]) -> int:
    """Convert SRT timestamp components to microseconds.

    ``ms`` is a decimal fraction-of-a-second string of arbitrary length: it is
    right-padded / truncated to exactly 3 digits (milliseconds), so a 4+ digit
    field degrades gracefully instead of overflowing (bug #19b).
    """
    ms_str = ms or ""
    total_ms = (
        int(h) * 3_600_000
        + int(m) * 60_000
        + int(s) * 1_000
        + (int(ms_str.ljust(3, "0")[:3]) if ms_str else 0)
    )
    return total_ms * 1_000


def parse_srt(text: str) -> list[Caption]:
    """Parse SRT subtitle text into a list of :class:`Caption` (times in µs).

    Cues are segmented by TIMING-LINE boundaries, not by blank lines: every line
    matching the ``HH:MM:SS,mmm --> HH:MM:SS,mmm`` pattern begins a new cue, and
    all subsequent lines (minus any index line for the NEXT cue) form the current
    cue's text until the next timing line. This means cues that are NOT separated
    by a blank line still parse correctly (bug #12).

    Robust to: BOM, CRLF/LF/CR line endings, present or absent blank separators,
    present or absent numeric index lines, and multi-line cue text. Comma or dot
    decimal separators are both accepted; hours may exceed 99 and over-long
    fractional-second fields are truncated (bug #19a/b). Inverted (end < start)
    and zero-length cues are SKIPPED rather than silently emitted (bug #19c).
    """
    if not text:
        return []

    # Strip a leading UTF-8 BOM (U+FEFF) and normalize line endings.
    text = text.lstrip("﻿")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    # Locate every timing line; each starts a new cue.
    timing_positions: list[tuple[int, re.Match]] = []
    for i, line in enumerate(lines):
        m = _SRT_TIME_RE.search(line)
        if m is not None:
            timing_positions.append((i, m))

    captions: list[Caption] = []
    for idx, (line_no, match) in enumerate(timing_positions):
        start = _srt_timestamp_to_us(
            match.group("h"), match.group("m"), match.group("s"), match.group("ms")
        )
        end = _srt_timestamp_to_us(
            match.group("eh"),
            match.group("em"),
            match.group("es"),
            match.group("ems"),
        )
        duration = end - start
        # Skip inverted or zero-length cues instead of emitting garbage.
        if duration <= 0:
            continue

        # Text runs from the line after this timing line up to (but excluding)
        # the next cue's index/timing line.
        if idx + 1 < len(timing_positions):
            next_timing_line = timing_positions[idx + 1][0]
        else:
            next_timing_line = len(lines)

        text_lines = lines[line_no + 1 : next_timing_line]
        # Drop a trailing numeric index line that belongs to the NEXT cue (it
        # sits just before the next timing line, separated by blanks).
        trimmed = list(text_lines)
        while trimmed and trimmed[-1].strip() == "":
            trimmed.pop()
        if trimmed and trimmed[-1].strip().isdigit():
            trimmed.pop()

        cue_text = "\n".join(trimmed).strip()
        captions.append(Caption(text=cue_text, start=start, duration=duration))

    return captions


def parse_srt_file(path: str) -> list[Caption]:
    """Read an SRT file (utf-8, BOM-tolerant) and parse it into captions."""
    raw = Path(path).read_text(encoding="utf-8-sig")
    return parse_srt(raw)
