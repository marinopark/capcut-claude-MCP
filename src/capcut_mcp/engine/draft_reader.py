"""CapCut ``draft_content.json`` -> internal :class:`~engine.models.Project`.

This is one of only TWO schema-aware files in the codebase (the other is
``draft_writer.py``). ALL knowledge of CapCut's on-disk JSON field names is
confined here. If the on-disk format changes, only this file and the writer
change.

Schema mapping is based on the reverse-engineered international CapCut / JianYing
``draft_content.json`` format (design.md section 9). Every non-trivial field
assumption is flagged with an ``ASSUMPTION:`` comment.

Time model: CapCut stores timeline values in microseconds already, which matches
our internal domain (all times are microseconds ints). No conversion needed.

Python 3.11+.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import locator
from .models import (
    EffectRef,
    Keyframe,
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

# ---------------------------------------------------------------------------
# Schema constants (CapCut field names live ONLY in this file)
# ---------------------------------------------------------------------------

# ASSUMPTION: CapCut track ``type`` strings map 1:1 onto our TrackType values.
_TRACK_TYPE_MAP: dict[str, TrackType] = {
    "video": TrackType.VIDEO,
    "audio": TrackType.AUDIO,
    "text": TrackType.TEXT,
    "effect": TrackType.EFFECT,
    "sticker": TrackType.STICKER,
    "filter": TrackType.FILTER,
}

# ASSUMPTION: materials are grouped into typed arrays under ``materials``.
# Each key maps to the MaterialType its entries carry.
_MATERIAL_GROUP_TYPES: dict[str, MaterialType] = {
    "videos": MaterialType.VIDEO,
    "audios": MaterialType.AUDIO,
    "texts": MaterialType.TEXT,
    "images": MaterialType.IMAGE,
    "effects": MaterialType.EFFECT,
    "stickers": MaterialType.STICKER,
    "filters": MaterialType.FILTER,
    "transitions": MaterialType.TRANSITION,
}

# Which track types produce a Transform digest for their segments.
_VISUAL_TRACK_TYPES = {TrackType.VIDEO, TrackType.STICKER, TrackType.TEXT}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_project(draft_dir: Path) -> Project:
    """Read a draft folder and map its JSON to a :class:`Project`.

    ``Project.name`` is taken from the folder name, ``draft_dir`` is recorded,
    and ``draft_format`` comes from ``locator.detect_draft_format``.

    Raises ``FileNotFoundError`` if no draft JSON exists, and ``ValueError`` if
    the JSON is structurally unusable.
    """
    draft_dir = Path(draft_dir)
    json_path = locator.draft_content_path(draft_dir)
    if json_path is None:
        raise FileNotFoundError(f"No draft JSON found in {draft_dir}")

    # Bug #10: encrypted / binary drafts (some CapCut projects store the content
    # as an opaque base64/binary blob) do NOT start with '{'. Detect this up
    # front and fail fast with a clean, typed error — no wasted retry sleeps.
    _reject_if_encrypted(json_path, draft_dir)

    try:
        raw = _read_json_with_retry(json_path)
    except json.JSONDecodeError as exc:
        # Bug #10: read_project's contract is to raise ValueError (not a bare
        # JSONDecodeError) for structurally-unusable JSON. Services catch
        # ValueError.
        raise ValueError(
            f"Draft JSON is unparseable: {json_path} ({exc})"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Draft JSON root is not an object: {json_path}")

    width, height, fps = _map_canvas(raw)

    project = Project(
        name=draft_dir.name,
        width=width,
        height=height,
        fps=fps,
        draft_format=locator.detect_draft_format(draft_dir),
        draft_dir=str(draft_dir),
    )

    # ASSUMPTION: top-level ``duration`` is the stored total timeline length (us).
    stored = raw.get("duration")
    if isinstance(stored, (int, float)):
        project.stored_duration = int(stored)

    # Materials pool: flatten the effect/media typed groups into the project
    # pool. Also build a full id->(MaterialType, name) index across EVERY group
    # (including non-effect groups like speeds/canvases) so segment
    # ``extra_material_refs`` can be resolved and correctly classified (#2).
    materials_raw = raw.get("materials")
    ref_index: dict[str, tuple[MaterialType | None, str]] = {}
    if isinstance(materials_raw, dict):
        for group_key, mat_type in _MATERIAL_GROUP_TYPES.items():
            group = materials_raw.get(group_key)
            if not isinstance(group, list):
                continue
            for entry in group:
                if not isinstance(entry, dict):
                    continue
                material = _map_material(entry, mat_type)
                if material.id:
                    project.add_material(material)

        ref_index = _build_ref_index(materials_raw)

    # Tracks -> segments.
    tracks_raw = raw.get("tracks")
    if isinstance(tracks_raw, list):
        for index, track_raw in enumerate(tracks_raw):
            if not isinstance(track_raw, dict):
                continue
            project.tracks.append(_map_track(track_raw, index, ref_index))

    return project


def read_project_by_name(name: str, drafts_dir: Path | None = None) -> Project:
    """Resolve a draft folder by ``name`` under ``drafts_dir`` and read it.

    ``drafts_dir`` defaults to ``locator.find_drafts_dir()``. Raises
    ``FileNotFoundError`` if the drafts directory or the named draft is absent.
    """
    if drafts_dir is None:
        drafts_dir = locator.find_drafts_dir()
    if drafts_dir is None:
        raise FileNotFoundError("Could not locate a CapCut drafts directory")

    drafts_dir = Path(drafts_dir)
    candidate = drafts_dir / name
    if candidate.is_dir() and locator.draft_content_path(candidate) is not None:
        return read_project(candidate)

    # Fall back to a case-insensitive scan of draft folders.
    for folder in locator.list_draft_folders(drafts_dir):
        if folder.name == name or folder.name.lower() == name.lower():
            return read_project(folder)

    raise FileNotFoundError(f"No draft named {name!r} in {drafts_dir}")


def _reject_if_encrypted(json_path: Path, draft_dir: Path) -> None:
    """Raise ``ValueError`` fast if the draft file is not JSON (encrypted/binary).

    Bug #10: some CapCut projects are stored encrypted — the file's first
    non-whitespace byte is not ``{``. There is no point retrying a mid-save read
    on these (they will never parse), so detect and reject them immediately with
    a clean, typed error instead of 3×0.5s of wasted sleeps + a raw
    ``JSONDecodeError``.

    A genuinely missing/unreadable file is left for the normal open() path to
    surface (so ``FileNotFoundError``/``PermissionError`` still propagate).
    """
    try:
        with open(json_path, "rb") as fh:
            # Read a small prefix and find the first non-whitespace byte.
            prefix = fh.read(64)
    except OSError:
        # Let the real read path raise the precise OSError subclass.
        return
    stripped = prefix.lstrip()
    if stripped and stripped[:1] != b"{":
        raise ValueError(
            f"Draft appears encrypted or in an unsupported format: {draft_dir}"
        )


def _read_json_with_retry(
    json_path: Path, attempts: int = 3, delay: float = 0.5
) -> dict:
    """Read and parse a JSON file, retrying ONLY on transient parse failures.

    CapCut may be mid-save, yielding a partially written / unparseable file —
    that is a ``json.JSONDecodeError`` and is worth retrying. Bug #13: we must
    NOT retry (and sleep ~1s) on non-transient ``OSError`` such as
    ``FileNotFoundError`` / ``PermissionError`` — those fail fast.
    """
    last_error: json.JSONDecodeError | None = None
    for attempt in range(attempts):
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay)
        # FileNotFoundError / PermissionError / other OSError propagate
        # immediately (no retry, no sleep).
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# Canvas / project-level mapping
# ---------------------------------------------------------------------------


def _map_canvas(raw: dict) -> tuple[int, int, float]:
    """Extract ``(width, height, fps)`` from the draft root.

    ASSUMPTION: resolution lives in ``canvas_config.width/height`` and ``fps``
    is a top-level float.
    """
    width = 0
    height = 0
    canvas = raw.get("canvas_config")
    if isinstance(canvas, dict):
        w = canvas.get("width")
        h = canvas.get("height")
        if isinstance(w, (int, float)):
            width = int(w)
        if isinstance(h, (int, float)):
            height = int(h)

    fps = 30.0
    raw_fps = raw.get("fps")
    if isinstance(raw_fps, (int, float)):
        fps = float(raw_fps)

    return width, height, fps


# ---------------------------------------------------------------------------
# Material mapping
# ---------------------------------------------------------------------------


def _map_material(raw: dict, material_type: MaterialType) -> Material:
    """Map one CapCut material entry to a :class:`Material`.

    ASSUMPTION: id under ``id``; media path under ``path``; intrinsic duration
    under ``duration`` (us); dimensions under ``width``/``height``; a display
    name under ``material_name`` or ``name``; text content under ``content``.
    """
    material_id = str(raw.get("id", "") or "")

    path = raw.get("path")
    path = str(path) if isinstance(path, str) and path else None

    name = raw.get("material_name") or raw.get("name") or ""
    name = str(name)

    duration = raw.get("duration")
    duration = int(duration) if isinstance(duration, (int, float)) else 0

    width = raw.get("width")
    width = int(width) if isinstance(width, (int, float)) else 0
    height = raw.get("height")
    height = int(height) if isinstance(height, (int, float)) else 0

    text = None
    text_style = None
    raw_content = None
    if material_type == MaterialType.TEXT:
        # Real CapCut text content lives under ``content`` as a JSON STRING:
        # ``{"text":"안녕하세요","styles":[{font,fill,size,...}]}``. We unwrap the
        # inner ``text`` for display (``text``) but ALSO preserve the original
        # raw string (``raw_content``) so the writer can re-emit the styled blob
        # CapCut needs to render (#6).
        content = raw.get("content")
        text = _extract_text_content(content)
        if isinstance(content, str) and content:
            raw_content = content
        text_style = _map_material_text_style(raw)

    return Material(
        id=material_id,
        material_type=material_type,
        path=path,
        name=name,
        duration=duration,
        width=width,
        height=height,
        text=text,
        text_style=text_style,
        raw_content=raw_content,
    )


def _extract_text_content(content: object) -> str | None:
    """Best-effort extraction of a display string from a text material.

    ASSUMPTION: ``content`` is either a plain string or a JSON-encoded object
    containing a ``text`` field (CapCut rich-text blob).
    """
    if content is None:
        return None
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                return content
            if isinstance(parsed, dict):
                inner = parsed.get("text")
                if isinstance(inner, str):
                    return inner
            return content
        return content
    if isinstance(content, dict):
        inner = content.get("text")
        if isinstance(inner, str):
            return inner
    return None


def _map_material_text_style(raw: dict) -> TextStyle | None:
    """Map font/size fields on a text material to a :class:`TextStyle`.

    ASSUMPTION: font name under ``font`` (or ``font_name``); size under
    ``font_size`` (or ``size``). Colours are typically per-segment, so left None.
    """
    font = raw.get("font") or raw.get("font_name")
    font = str(font) if isinstance(font, str) and font else None

    size = raw.get("font_size", raw.get("size"))
    if isinstance(size, (int, float)):
        size = float(size)
    else:
        size = 15.0

    if font is None and size == 15.0:
        return None
    return TextStyle(font=font, size=size)


# ---------------------------------------------------------------------------
# Segment mapping
# ---------------------------------------------------------------------------


def _map_timerange(raw: object) -> TimeRange:
    """Map a CapCut ``{start, duration}`` timerange (us) to :class:`TimeRange`."""
    start = 0
    duration = 0
    if isinstance(raw, dict):
        s = raw.get("start")
        d = raw.get("duration")
        if isinstance(s, (int, float)):
            start = int(s)
        if isinstance(d, (int, float)):
            duration = int(d)
    return TimeRange(start=start, duration=duration)


def _map_segment(
    raw: dict,
    track_type: TrackType,
    ref_index: dict[str, tuple[MaterialType | None, str]] | None = None,
) -> Segment:
    """Map one CapCut segment to a :class:`Segment`.

    ASSUMPTION: id under ``id``; material link under ``material_id``; timeline
    placement under ``target_timerange`` and trim under ``source_timerange``
    (both microseconds); ``speed`` and ``volume`` scalars; visual transform under
    ``clip`` (``transform.x/y``, ``scale.x/y``, ``rotation``, ``alpha``); audio
    fades under ``fade`` / ``fade_in``/``fade_out``.
    """
    segment_id = str(raw.get("id", "") or "")
    material_id = str(raw.get("material_id", "") or "")

    target = _map_timerange(raw.get("target_timerange"))
    source = _map_timerange(raw.get("source_timerange"))

    speed = raw.get("speed", 1.0)
    speed = float(speed) if isinstance(speed, (int, float)) else 1.0

    volume = raw.get("volume", 1.0)
    volume = float(volume) if isinstance(volume, (int, float)) else 1.0

    fade_in, fade_out = _map_fades(raw)

    transform = None
    if track_type in _VISUAL_TRACK_TYPES:
        transform = _map_transform(raw.get("clip"))

    effects = _map_effects(raw, ref_index or {})
    keyframes = _map_keyframes(raw)

    return Segment(
        id=segment_id,
        material_id=material_id,
        target=target,
        source_start=source.start,
        source_duration=source.duration,
        speed=speed,
        volume=volume,
        fade_in=fade_in,
        fade_out=fade_out,
        transform=transform,
        keyframes=keyframes,
        effects=effects,
    )


def _map_fades(raw: dict) -> tuple[int, int]:
    """Extract audio fade in/out durations in microseconds.

    ASSUMPTION: fades appear either as ``fade_in``/``fade_out`` scalars or nested
    under a ``fade`` object with ``fade_in_duration``/``fade_out_duration``.
    """
    fade_in = 0
    fade_out = 0
    fi = raw.get("fade_in")
    fo = raw.get("fade_out")
    if isinstance(fi, (int, float)):
        fade_in = int(fi)
    if isinstance(fo, (int, float)):
        fade_out = int(fo)

    fade = raw.get("fade")
    if isinstance(fade, dict):
        fid = fade.get("fade_in_duration")
        fod = fade.get("fade_out_duration")
        if isinstance(fid, (int, float)):
            fade_in = int(fid)
        if isinstance(fod, (int, float)):
            fade_out = int(fod)

    return fade_in, fade_out


def _map_transform(clip: object) -> Transform | None:
    """Map a CapCut ``clip`` block to a :class:`Transform`.

    ASSUMPTION: ``clip.transform.x/y`` position, ``clip.scale.x/y`` scale,
    ``clip.rotation`` degrees, ``clip.alpha`` opacity (0..1).
    """
    if not isinstance(clip, dict):
        return None

    def _num(container: object, key: str, default: float) -> float:
        if isinstance(container, dict):
            val = container.get(key)
            if isinstance(val, (int, float)):
                return float(val)
        return default

    transform_obj = clip.get("transform")
    scale_obj = clip.get("scale")

    return Transform(
        position_x=_num(transform_obj, "x", 0.0),
        position_y=_num(transform_obj, "y", 0.0),
        scale_x=_num(scale_obj, "x", 1.0),
        scale_y=_num(scale_obj, "y", 1.0),
        rotation=_num(clip, "rotation", 0.0),
        opacity=_num(clip, "alpha", 1.0),
    )


# Which material groups count as genuine "effects" for the purpose of
# ``Segment.effects``. Real ``extra_material_refs`` point to a mix of groups
# (speeds, canvases, material_animations, sound_channel_mappings, hsl, ...); only
# these resolve to an effect-like EffectRef (#2). Everything else is excluded.
_EFFECT_GROUP_TYPES: dict[str, MaterialType] = {
    "effects": MaterialType.EFFECT,
    "video_effects": MaterialType.EFFECT,
    "filters": MaterialType.FILTER,
    "transitions": MaterialType.TRANSITION,
}


def _build_ref_index(
    materials_raw: dict,
) -> dict[str, tuple[MaterialType | None, str]]:
    """Index every material id across ALL groups.

    Maps ``id -> (MaterialType | None, name)``. For groups we treat as
    effect-like (effects/video_effects, filters, transitions) the MaterialType
    is set; for every other group (speeds, canvases, material_animations, ...)
    the MaterialType is ``None`` (meaning: known id, but NOT an effect). This
    lets ``_map_effects`` keep only true effect refs (#2).
    """
    index: dict[str, tuple[MaterialType | None, str]] = {}
    for group_key, group in materials_raw.items():
        if not isinstance(group, list):
            continue
        eff_type = _EFFECT_GROUP_TYPES.get(group_key)
        for entry in group:
            if not isinstance(entry, dict):
                continue
            ent_id = str(entry.get("id", "") or "")
            if not ent_id:
                continue
            name = entry.get("material_name") or entry.get("name") or ent_id
            index[ent_id] = (eff_type, str(name))
    return index


def _map_effects(
    raw: dict, ref_index: dict[str, tuple[MaterialType | None, str]]
) -> list[EffectRef]:
    """Map applied effect/filter/transition references on a segment (#2).

    ``extra_material_refs`` is a list of material ids pointing at a MIX of
    groups. We resolve each id against ``ref_index`` and keep ONLY those that
    resolve to a genuine effect-like material (EFFECT / FILTER / TRANSITION).
    Refs that resolve to non-effect groups (speeds, canvases, placeholder_infos,
    sound_channel_mappings, vocal_separations, material_colors, ...) or that do
    not resolve at all are EXCLUDED.
    """
    refs = raw.get("extra_material_refs")
    if not isinstance(refs, list):
        return []
    result: list[EffectRef] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            continue
        resolved = ref_index.get(ref)
        if resolved is None:
            continue  # unknown id — not a material we pooled
        mat_type, name = resolved
        if mat_type is None:
            continue  # resolves to a non-effect group — exclude
        result.append(
            EffectRef(effect_id=ref, name=name or ref, material_type=mat_type)
        )
    return result


# Map CapCut ``common_keyframes[].property_type`` strings to domain property
# names. Real drafts use e.g. ``KFTypeVolume``; extend as more are confirmed.
_KEYFRAME_PROPERTY_MAP: dict[str, str] = {
    "KFTypeVolume": "volume",
    "KFTypePositionX": "position_x",
    "KFTypePositionY": "position_y",
    "KFTypeScaleX": "scale_x",
    "KFTypeScaleY": "scale_y",
    "KFTypeScale": "scale",
    "KFTypeRotation": "rotation",
    "KFTypeAlpha": "opacity",
}


def _keyframe_property_name(property_type: object) -> str:
    """Domain property name for a CapCut ``property_type`` (best-effort)."""
    if not isinstance(property_type, str) or not property_type:
        return "unknown"
    mapped = _KEYFRAME_PROPERTY_MAP.get(property_type)
    if mapped is not None:
        return mapped
    # Fall back to a de-prefixed, lower-cased form (KFTypeFoo -> foo).
    stripped = property_type[len("KFType"):] if property_type.startswith(
        "KFType"
    ) else property_type
    return stripped.lower() or "unknown"


def _map_keyframes(raw: dict) -> list["Keyframe"]:
    """Map segment ``common_keyframes`` into flat :class:`Keyframe` objects (#7).

    Real shape: ``common_keyframes`` is a LIST of
    ``{property_type, keyframe_list:[{time_offset, values, curveType,
    left_control, right_control}]}``. We flatten each ``keyframe_list`` entry
    into one :class:`Keyframe` (``time`` = ``time_offset`` µs, ``value`` =
    first of ``values``).
    """
    groups = raw.get("common_keyframes")
    if not isinstance(groups, list):
        return []
    result: list[Keyframe] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        prop = _keyframe_property_name(group.get("property_type"))
        kf_list = group.get("keyframe_list")
        if not isinstance(kf_list, list):
            continue
        for kf in kf_list:
            if not isinstance(kf, dict):
                continue
            time_offset = kf.get("time_offset")
            time_us = int(time_offset) if isinstance(
                time_offset, (int, float)
            ) else 0
            values = kf.get("values")
            value = 0.0
            if isinstance(values, list) and values and isinstance(
                values[0], (int, float)
            ):
                value = float(values[0])
            elif isinstance(values, (int, float)):
                value = float(values)
            curve = _keyframe_curve(kf)
            result.append(
                Keyframe(
                    property_name=prop,
                    time=time_us,
                    value=value,
                    curve=curve,
                )
            )
    return result


def _keyframe_curve(kf: dict) -> list[float] | None:
    """Extract bezier control handles [x1,y1,x2,y2] from a keyframe, or None."""
    left = kf.get("left_control")
    right = kf.get("right_control")

    def _pt(obj: object) -> tuple[float, float] | None:
        if isinstance(obj, dict):
            x = obj.get("x")
            y = obj.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                return float(x), float(y)
        return None

    lp = _pt(left)
    rp = _pt(right)
    if lp is None and rp is None:
        return None
    lp = lp or (0.0, 0.0)
    rp = rp or (0.0, 0.0)
    return [lp[0], lp[1], rp[0], rp[1]]


# ---------------------------------------------------------------------------
# Track mapping
# ---------------------------------------------------------------------------


# Bug #3: CapCut stores per-track mute in the integer ``attribute`` bitfield.
# The mute bit is the low bit (attribute & 1); a muted track has attribute 1.
_TRACK_ATTR_MUTE_BIT = 0x1


def _map_track(
    raw: dict,
    index: int,
    ref_index: dict[str, tuple[MaterialType | None, str]] | None = None,
) -> Track:
    """Map one CapCut track (and its segments) to a :class:`Track`.

    ASSUMPTION: track id under ``id``; kind under ``type``; segments under
    ``segments``; optional ``name``. Unknown ``type`` values default to VIDEO
    (they still carry segments we want to surface).

    Bug #3: mute is read from the integer ``attribute`` key (NOT ``mute`` /
    ``muted``, which real drafts do not carry). A nonzero mute bit => muted.
    """
    track_id = str(raw.get("id", "") or "")

    type_str = str(raw.get("type", "") or "").lower()
    track_type = _TRACK_TYPE_MAP.get(type_str, TrackType.VIDEO)

    name = raw.get("name") or ""
    name = str(name)

    muted = _read_track_muted(raw)

    track = Track(
        id=track_id,
        track_type=track_type,
        index=index,
        muted=muted,
        name=name,
    )

    segments_raw = raw.get("segments")
    if isinstance(segments_raw, list):
        for seg_raw in segments_raw:
            if isinstance(seg_raw, dict):
                track.segments.append(
                    _map_segment(seg_raw, track_type, ref_index)
                )

    return track


def _read_track_muted(raw: dict) -> bool:
    """Derive a track's muted flag from its integer ``attribute`` field (#3)."""
    attribute = raw.get("attribute")
    if isinstance(attribute, bool):
        return attribute
    if isinstance(attribute, (int, float)):
        return bool(int(attribute) & _TRACK_ATTR_MUTE_BIT)
    # Legacy / synthetic fallback: honour an explicit mute/muted key if present.
    legacy = raw.get("mute", raw.get("muted"))
    return bool(legacy)
