"""Canonical internal domain model for the CapCut MCP server.

This module is the shared contract that EVERY other module depends on. It is
deliberately independent of CapCut's on-disk JSON schema: reader/writer adapt
between this domain model and CapCut's ``draft_content.json`` format, and the
service layer operates ONLY on these dataclasses.

Design rules enforced here:
  * Standard library only (``dataclasses``, ``enum``, ``typing``). No pydantic.
  * All time is stored internally in MICROSECONDS (int). Conversion to/from
    seconds happens only at the tool I/O boundary (server.py) via the
    ``us_to_sec`` / ``sec_to_us`` helpers below.
  * Field names describe the domain, not CapCut's field names.

Python 3.11+.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Time conversion helpers (the ONLY sanctioned microsecond<->second boundary)
# ---------------------------------------------------------------------------

# CapCut / JianYing store timeline time in microseconds (1e6 per second).
US_PER_SECOND: int = 1_000_000


def us_to_sec(us: int) -> float:
    """Convert an integer microsecond value to seconds (float)."""
    return us / US_PER_SECOND


def sec_to_us(sec: float) -> int:
    """Convert a seconds value (float) to integer microseconds (rounded)."""
    return int(round(sec * US_PER_SECOND))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TrackType(str, Enum):
    """The kind of content a track carries.

    Inherits ``str`` so values serialize cleanly and compare to plain strings.
    """

    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    EFFECT = "effect"
    STICKER = "sticker"
    FILTER = "filter"


class MaterialType(str, Enum):
    """The kind of source media/resource a segment references."""

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    TEXT = "text"
    EFFECT = "effect"
    STICKER = "sticker"
    FILTER = "filter"
    TRANSITION = "transition"


# ---------------------------------------------------------------------------
# Leaf value objects
# ---------------------------------------------------------------------------


@dataclass
class TimeRange:
    """A half-open time span in microseconds: ``[start, start + duration)``."""

    start: int  # microseconds
    duration: int  # microseconds

    @property
    def end(self) -> int:
        """Exclusive end of the range, in microseconds."""
        return self.start + self.duration


@dataclass
class Keyframe:
    """A single animation keyframe on a segment property.

    ``property_name`` is a domain-level name (e.g. ``"opacity"``, ``"scale_x"``,
    ``"position_x"``, ``"volume"``). ``time`` is relative to the segment start.
    """

    property_name: str
    time: int  # microseconds, relative to segment start
    value: float
    # Optional bezier control handles [x1, y1, x2, y2]; None == linear/default.
    curve: Optional[list[float]] = None


@dataclass
class EffectRef:
    """A reference to an applied effect / transition / filter and its params.

    Kept schema-agnostic: ``params`` is a free-form bag of already-digested
    numeric/string values, not raw CapCut JSON.
    """

    effect_id: str
    name: str
    material_type: MaterialType = MaterialType.EFFECT
    params: dict[str, object] = field(default_factory=dict)
    # For transitions the on-timeline duration; otherwise typically 0.
    duration: int = 0  # microseconds


@dataclass
class TextStyle:
    """Visual styling for a text/caption segment."""

    font: Optional[str] = None
    size: float = 15.0  # CapCut "font size" units (unitless design value)
    # Colour as normalized RGBA floats 0.0-1.0; None == CapCut default (white).
    color: Optional[tuple[float, float, float, float]] = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    alignment: str = "center"  # "left" | "center" | "right"
    # Normalized position of the text anchor within the canvas, range -1..1
    # where (0, 0) is centre. None == CapCut default placement.
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    # Optional outline / background stroke colour, normalized RGBA.
    stroke_color: Optional[tuple[float, float, float, float]] = None
    stroke_width: float = 0.0


@dataclass
class Transform:
    """Spatial transform of a visual segment within the canvas.

    Positions/scales are normalized design values (CapCut convention:
    position 0 == centre, scale 1.0 == fit). Rotation in degrees.
    """

    position_x: float = 0.0
    position_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation: float = 0.0
    opacity: float = 1.0


# ---------------------------------------------------------------------------
# Material (media/resource reference, pooled at project level)
# ---------------------------------------------------------------------------


@dataclass
class Material:
    """A source resource referenced by one or more segments.

    Materials are pooled on the :class:`Project` and referenced by ``id`` from
    segments (mirrors CapCut's materials[] + segment.material_id linkage, but is
    our own domain object). For media, ``path`` is the absolute source file and
    ``duration`` is the intrinsic media length in microseconds (0 if unknown).
    """

    id: str
    material_type: MaterialType
    # Absolute filesystem path for video/image/audio; None for synthetic
    # resources such as text, effects, transitions.
    path: Optional[str] = None
    name: str = ""
    duration: int = 0  # microseconds; intrinsic source length (0 == unknown)
    width: int = 0  # pixels, for visual media (0 == unknown/NA)
    height: int = 0  # pixels
    # For text materials: the rendered string content.
    text: Optional[str] = None
    # For text materials: styling captured at material level.
    text_style: Optional[TextStyle] = None
    # For text materials read from a draft: the ORIGINAL raw ``content`` value
    # (CapCut stores this as a JSON string ``{"text":...,"styles":[...]}``).
    # Preserved verbatim so the writer can re-emit the styled blob CapCut needs
    # to render correctly. None for freshly-built text (writer synthesizes one).
    raw_content: Optional[str] = None
    # True once the source path has been confirmed to exist on disk.
    exists: bool = True


# ---------------------------------------------------------------------------
# Segment (a placed clip on a track)
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """A single clip placed on a track's timeline.

    Time model:
      * ``target`` is where the clip sits on the PROJECT timeline (µs).
      * ``source_start`` / ``source_duration`` describe the trimmed-in span of
        the underlying material (µs). ``source_duration`` before speed scaling.
      * ``speed`` multiplies playback rate; the on-timeline ``target.duration``
        already reflects the sped-up length.
    """

    id: str
    material_id: str
    target: TimeRange  # position + duration on the project timeline (µs)
    source_start: int = 0  # in-point within the material (µs)
    source_duration: int = 0  # trimmed length within material, pre-speed (µs)
    speed: float = 1.0
    volume: float = 1.0  # 0.0-1.0 linear gain (audio/video segments)
    fade_in: int = 0  # microseconds (audio fade)
    fade_out: int = 0  # microseconds (audio fade)
    transform: Optional[Transform] = None  # visual segments only
    text_style: Optional[TextStyle] = None  # text segments only
    keyframes: list[Keyframe] = field(default_factory=list)
    effects: list[EffectRef] = field(default_factory=list)

    @property
    def start(self) -> int:
        """Timeline start of this segment, in microseconds."""
        return self.target.start

    @property
    def duration(self) -> int:
        """On-timeline duration of this segment, in microseconds."""
        return self.target.duration

    @property
    def end(self) -> int:
        """Exclusive timeline end of this segment, in microseconds."""
        return self.target.end


# ---------------------------------------------------------------------------
# Track (ordered lane of segments of a single type)
# ---------------------------------------------------------------------------


@dataclass
class Track:
    """An ordered lane of segments sharing one :class:`TrackType`."""

    id: str
    track_type: TrackType
    segments: list[Segment] = field(default_factory=list)
    # Render/stacking order; lower renders first (further back). Mirrors the
    # track's index in the CapCut tracks[] array.
    index: int = 0
    muted: bool = False
    name: str = ""

    @property
    def duration(self) -> int:
        """Timeline extent of the track (max segment end), in microseconds."""
        return max((seg.end for seg in self.segments), default=0)


# ---------------------------------------------------------------------------
# Caption (a flattened, read-friendly view of a text segment)
# ---------------------------------------------------------------------------


@dataclass
class Caption:
    """A digested caption line: text plus its timeline span (µs).

    Produced by analyzer from text-track segments; not a first-class node of the
    project graph.
    """

    text: str
    start: int  # microseconds
    duration: int  # microseconds

    @property
    def end(self) -> int:
        """Exclusive timeline end of the caption, in microseconds."""
        return self.start + self.duration


# ---------------------------------------------------------------------------
# Project (root aggregate)
# ---------------------------------------------------------------------------


@dataclass
class Project:
    """Root aggregate: canvas config, material pool, and ordered tracks."""

    name: str
    width: int  # canvas pixels
    height: int  # canvas pixels
    fps: float = 30.0
    tracks: list[Track] = field(default_factory=list)
    materials: dict[str, Material] = field(default_factory=dict)
    # Total timeline duration in microseconds; 0 until computed/set. Prefer the
    # ``duration`` property which derives it from tracks.
    stored_duration: int = 0
    # Draft format identifier detected/targeted (e.g. "draft_content").
    draft_format: str = "draft_content"
    # Absolute path to the draft folder on disk, if this project was read from
    # (or has been written to) disk. None for in-memory builds.
    draft_dir: Optional[str] = None

    @property
    def duration(self) -> int:
        """Overall timeline duration (max track end), in microseconds."""
        derived = max((track.duration for track in self.tracks), default=0)
        return max(derived, self.stored_duration)

    @property
    def resolution(self) -> tuple[int, int]:
        """Canvas resolution as a ``(width, height)`` tuple."""
        return (self.width, self.height)

    def get_material(self, material_id: str) -> Optional[Material]:
        """Return the pooled material for ``material_id`` or None."""
        return self.materials.get(material_id)

    def add_material(self, material: Material) -> None:
        """Register a material in the project pool (keyed by its id)."""
        self.materials[material.id] = material

    def tracks_of_type(self, track_type: TrackType) -> list[Track]:
        """Return all tracks matching ``track_type`` in project order."""
        return [t for t in self.tracks if t.track_type == track_type]

    def find_segment(self, segment_id: str) -> Optional[Segment]:
        """Locate a segment anywhere in the project by its id, or None."""
        for track in self.tracks:
            for seg in track.segments:
                if seg.id == segment_id:
                    return seg
        return None
