"""``draft_writer.py`` — internal domain model → CapCut draft folder (atomic).

One of only TWO schema-aware modules (the other is ``draft_reader.py``). All
knowledge of CapCut's on-disk ``draft_content.json`` layout is confined here.

Implements the design.md section 5 contract:
  * ``write_draft(project, drafts_dir, force=False) -> Path``
  * ``validate_media_paths(project) -> list[str]``
  * ``_build_draft_content(project) -> dict``
  * ``_serialize_material`` / ``_serialize_segment`` / ``_serialize_track``
  * ``_sanitize_folder_name``

Safety rules honoured (design.md section 0):
  * New-folder-only: never overwrite an existing draft folder. If the target
    exists and ``force`` is False → ``FileExistsError``.
  * Atomic write: the complete draft folder is assembled in a temp dir, then
    renamed into ``drafts_dir`` via ``os.replace`` (fallback ``shutil.move``).
  * Path validation: media paths normalized to absolute; missing files are
    reported as warnings, not silently dropped, and do not abort the write.

Times are emitted in MICROSECONDS (int), matching the domain model and the
schema assumptions in design.md section 9. Standard library only.

Python 3.11+.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from . import models
from .models import (
    Material,
    MaterialType,
    Project,
    Segment,
    TextStyle,
    Track,
    TrackType,
    Transform,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DRAFT_CONTENT_FILENAME = "draft_content.json"

# Materials in CapCut are split into typed arrays keyed by a plural bucket name.
# This map ties a domain MaterialType to the array it lives under in
# ``draft_content.json``'s ``materials`` object.
_MATERIAL_BUCKETS: dict[MaterialType, str] = {
    MaterialType.VIDEO: "videos",
    MaterialType.IMAGE: "videos",  # CapCut pools images alongside videos
    MaterialType.AUDIO: "audios",
    MaterialType.TEXT: "texts",
    MaterialType.EFFECT: "effects",
    MaterialType.STICKER: "stickers",
    MaterialType.FILTER: "effects",
    MaterialType.TRANSITION: "transitions",
}

# All material buckets we always emit (so the reader can iterate predictably).
_ALL_BUCKETS = ("videos", "audios", "texts", "effects", "transitions", "stickers")

# Illegal / reserved characters for a folder name on Windows + macOS.
_ILLEGAL_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# id generation
# ---------------------------------------------------------------------------


def _new_id() -> str:
    """Generate a fresh CapCut-style uppercase UUID string."""
    return str(uuid.uuid4()).upper()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Platform identity harvesting (Bug #28)
# ---------------------------------------------------------------------------

# CapCut validates a draft's provenance via the ``platform`` block in
# ``draft_content.json`` — most importantly ``device_id``, this machine's CapCut
# identity. A draft whose device_id is empty/foreign is treated as moved from
# another machine and rejected with an "abnormal path" (비정상적인 경로) error.
# We therefore HARVEST the platform block from an existing real draft in the
# same drafts dir and stamp it onto drafts we write. Fallback values below are
# only used when no readable sibling draft exists.
_FALLBACK_PLATFORM = {
    "os": "windows",
    "os_version": "",
    "app_id": 359289,
    "app_version": "8.7.0",
    "app_source": "cc",
    "device_id": "",
    "hard_disk_id": "",
    "mac_address": "",
}
_FALLBACK_NEW_VERSION = "175.0.0"


def _harvest_environment(drafts_dir: Path) -> tuple[dict, str]:
    """Extract ``(platform_block, new_version)`` from a sibling real draft.

    Scans ``drafts_dir`` for a readable (non-encrypted) ``draft_content.json``
    and returns its ``platform`` (or ``last_modified_platform``) block plus its
    ``new_version`` string. Best-effort: on any failure returns fallbacks.
    """
    try:
        candidates = sorted(
            drafts_dir.glob("*/draft_content.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        candidates = []
    for candidate in candidates:
        try:
            with open(candidate, "rb") as fh:
                head = fh.read(1)
                if head != b"{":
                    continue  # encrypted draft
                raw = head + fh.read()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            continue
        platform = data.get("platform") or data.get("last_modified_platform")
        if not isinstance(platform, dict) or not platform.get("device_id"):
            continue
        new_version = data.get("new_version")
        if not isinstance(new_version, str) or not new_version:
            new_version = _FALLBACK_NEW_VERSION
        return dict(platform), new_version
    return dict(_FALLBACK_PLATFORM), _FALLBACK_NEW_VERSION


def write_draft(project: Project, drafts_dir: Path, force: bool = False) -> Path:
    """Serialize ``project`` into a new draft folder under ``drafts_dir``.

    The folder is assembled fully in a temp directory, then atomically renamed
    into ``drafts_dir``. Returns the final draft folder path.

    Raises ``FileExistsError`` if the target folder already exists and ``force``
    is False (new-folder-only safety rule). This function does NOT perform the
    CapCut-process check — that belongs to the builder/``save_draft`` layer.
    """
    drafts_dir = Path(drafts_dir)

    folder_name = _sanitize_folder_name(project.name)
    target_dir = drafts_dir / folder_name

    if target_dir.exists() and not force:
        raise FileExistsError(
            f"Draft folder already exists: {target_dir}. "
            f"Refusing to overwrite (new-folder-only rule). "
            f"Pass force=True to override."
        )

    # Normalize media paths / existence flags before serializing.
    validate_media_paths(project)

    # Harvest this machine's CapCut platform identity from a sibling draft so
    # CapCut does not reject the new draft as foreign ("abnormal path", #28).
    platform, new_version = _harvest_environment(drafts_dir)
    content = _build_draft_content(project, platform=platform, new_version=new_version)

    drafts_dir.mkdir(parents=True, exist_ok=True)

    # Assemble the whole folder in a temp dir, then move it atomically.
    tmp_parent = tempfile.mkdtemp(prefix="capcut_draft_", dir=str(drafts_dir))
    try:
        staging_dir = Path(tmp_parent) / folder_name
        staging_dir.mkdir(parents=True, exist_ok=False)

        _write_json(staging_dir / DRAFT_CONTENT_FILENAME, content)
        # Bug #26: sidecar path fields (draft_fold_path/root_path) must record
        # the FINAL location, not the temp staging dir the files are written to.
        _write_sidecars(staging_dir, target_dir, project, content)

        # If force and target exists, remove it just before the atomic move so
        # the window of a missing folder is minimal.
        if target_dir.exists():
            if not force:
                # Re-check (defensive): another process may have created it.
                raise FileExistsError(
                    f"Draft folder already exists: {target_dir}."
                )
            shutil.rmtree(target_dir)

        try:
            os.replace(str(staging_dir), str(target_dir))
        except OSError:
            # Cross-device or other rename failure: fall back to shutil.move.
            shutil.move(str(staging_dir), str(target_dir))
    finally:
        # Clean up the temp parent (staging_dir was moved out on success).
        shutil.rmtree(tmp_parent, ignore_errors=True)

    project.draft_dir = str(target_dir)
    return target_dir


def _to_slash_path(path: str) -> str:
    """Absolute path with FORWARD slashes, as CapCut stores every path.

    Bug #24: on Windows ``os.path.abspath`` yields back-slash paths
    (``C:\\Users\\...``). Real CapCut drafts store every path with forward
    slashes (``C:/Users/...``); a back-slash media path makes CapCut reject the
    project with an "abnormal path" error. Normalize to forward slashes here so
    both ``draft_content.json`` and ``draft_meta_info.json`` stay consistent.
    """
    return os.path.abspath(path).replace("\\", "/")


def validate_media_paths(project: Project) -> list[str]:
    """Normalize each material path to absolute and flag missing files.

    Mutates each :class:`Material` in place: ``path`` becomes an absolute,
    forward-slash path string and ``exists`` reflects on-disk presence. Returns
    a list of human-readable warnings for materials whose media file is missing.
    """
    warnings: list[str] = []
    for material in project.materials.values():
        if not material.path:
            continue
        abs_path = _to_slash_path(material.path)
        material.path = abs_path
        exists = os.path.isfile(abs_path)
        material.exists = exists
        if not exists:
            label = material.name or material.id
            warnings.append(f"Missing media file for '{label}': {abs_path}")
    return warnings


# ---------------------------------------------------------------------------
# draft_content.json construction
# ---------------------------------------------------------------------------


def _build_draft_content(
    project: Project,
    platform: dict | None = None,
    new_version: str | None = None,
) -> dict:
    """Serialize the domain :class:`Project` into a ``draft_content.json`` dict.

    Emits: ``canvas_config`` (width/height/ratio), top-level ``fps`` and
    ``duration`` (µs), a ``materials`` object of typed arrays, and ``tracks``
    each with serialized ``segments``. Times are microseconds throughout.
    """
    materials: dict[str, list[dict]] = {bucket: [] for bucket in _ALL_BUCKETS}
    for material in project.materials.values():
        bucket = _MATERIAL_BUCKETS.get(material.material_type, "videos")
        materials[bucket].append(_serialize_material(material))

    tracks = [_serialize_track(track) for track in project.tracks]

    platform_block = dict(platform) if platform else dict(_FALLBACK_PLATFORM)
    return {
        "id": _new_id(),
        "canvas_config": {
            "width": int(project.width),
            "height": int(project.height),
            "ratio": "original",
        },
        "fps": float(project.fps),
        "duration": int(project.duration),
        "materials": materials,
        "tracks": tracks,
        # Sidecar-ish metadata some CapCut versions expect at the top level.
        "draft_id": _new_id(),
        "version": 360000,
        "new_version": new_version or _FALLBACK_NEW_VERSION,
        # Provenance blocks: MUST carry this machine's device_id or CapCut
        # rejects the draft as moved from another machine (#28).
        "platform": platform_block,
        "last_modified_platform": dict(platform_block),
    }


def _serialize_material(m: Material) -> dict:
    """Serialize one :class:`Material` into its CapCut materials[] entry."""
    out: dict[str, object] = {
        "id": m.id,
        "type": _material_type_value(m.material_type),
        "material_name": m.name,
        "name": m.name,
        "duration": int(m.duration),
        "width": int(m.width),
        "height": int(m.height),
    }
    if m.path is not None:
        out["path"] = m.path
        # CapCut mirrors the media path under ``media_path`` for A/V materials.
        if m.material_type in (
            MaterialType.VIDEO,
            MaterialType.IMAGE,
            MaterialType.AUDIO,
        ):
            out["media_path"] = m.path
    if m.material_type == MaterialType.TEXT:
        # Bug #6: CapCut renders text from a JSON-STRING ``content`` of shape
        # ``{"text":...,"styles":[...]}``. A bare plain string does not render.
        out["content"] = _text_content_json(m)
        if m.text_style is not None:
            out["text_style"] = _serialize_text_style(m.text_style)
    return out


def _text_content_json(m: Material) -> str:
    """Build the styled JSON-string ``content`` CapCut expects for text (#6).

    If the material was read from a draft it carries the original raw content
    string in ``raw_content`` — re-emit that verbatim (preserving CapCut's
    styles) but with the (possibly edited) ``text`` swapped in. For freshly
    built text (no ``raw_content``) synthesize a minimal valid wrapper with the
    font/size/colour fields CapCut expects.
    """
    text = m.text if m.text is not None else ""

    # Preserve the original styled blob when we have one (read from a draft).
    if m.raw_content:
        try:
            parsed = json.loads(m.raw_content)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            # Keep original styles; refresh the display text.
            parsed["text"] = text
            styles = parsed.get("styles")
            if isinstance(styles, list) and styles:
                first = styles[0]
                if isinstance(first, dict):
                    first["range"] = [0, len(text)]
            return json.dumps(parsed, ensure_ascii=False)

    # Freshly-built text: synthesize a minimal valid styled wrapper.
    ts = m.text_style
    size = float(ts.size) if ts is not None else 15.0
    color = _content_color(ts.color if ts is not None else None)
    style: dict[str, object] = {
        "fill": {
            "content": {
                "render_type": "solid",
                "solid": {"color": color},
            }
        },
        "size": size,
        "range": [0, len(text)],
    }
    if ts is not None and ts.font:
        style["font"] = {"path": "", "id": "", "name": ts.font}
    return json.dumps({"text": text, "styles": [style]}, ensure_ascii=False)


def _content_color(color: object) -> list[float]:
    """RGB list (0..1) for the styled-content ``solid.color``; default black."""
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        try:
            return [float(color[0]), float(color[1]), float(color[2])]
        except (TypeError, ValueError):
            pass
    return [0.0, 0.0, 0.0]


def _serialize_track(t: Track) -> dict:
    """Serialize one :class:`Track` (with its segments)."""
    return {
        "id": t.id,
        "type": _track_type_value(t.track_type),
        "attribute": 1 if t.muted else 0,
        "flag": 0,
        "name": t.name,
        "segments": [_serialize_segment(seg) for seg in t.segments],
    }


def _serialize_segment(s: Segment) -> dict:
    """Serialize one :class:`Segment` into a CapCut track segment entry."""
    out: dict[str, object] = {
        "id": s.id,
        "material_id": s.material_id,
        "target_timerange": {
            "start": int(s.target.start),
            "duration": int(s.target.duration),
        },
        "source_timerange": {
            "start": int(s.source_start),
            "duration": int(s.source_duration),
        },
        "speed": float(s.speed),
        "volume": float(s.volume),
        "extra_material_refs": [e.effect_id for e in s.effects],
    }

    # Audio fades (µs).
    if s.fade_in or s.fade_out:
        out["audio_fade"] = {
            "fade_in_duration": int(s.fade_in),
            "fade_out_duration": int(s.fade_out),
        }

    # Visual transform → CapCut ``clip`` block.
    if s.transform is not None:
        out["clip"] = _serialize_transform(s.transform)

    # Per-segment text style (text segments).
    if s.text_style is not None:
        out["text_style"] = _serialize_text_style(s.text_style)

    # Bug #7: keyframes serialize to CapCut's ``common_keyframes`` LIST shape:
    # ``[{property_type, keyframe_list:[{time_offset, values, curveType,
    # left_control, right_control}]}]`` — grouped by property.
    if s.keyframes:
        out["common_keyframes"] = _serialize_common_keyframes(s.keyframes)

    return out


# Domain property name -> CapCut ``property_type`` string (inverse of the
# reader's map). Used to emit ``common_keyframes`` (#7).
_KEYFRAME_TYPE_MAP: dict[str, str] = {
    "volume": "KFTypeVolume",
    "position_x": "KFTypePositionX",
    "position_y": "KFTypePositionY",
    "scale_x": "KFTypeScaleX",
    "scale_y": "KFTypeScaleY",
    "scale": "KFTypeScale",
    "rotation": "KFTypeRotation",
    "opacity": "KFTypeAlpha",
}


def _keyframe_property_type(property_name: str) -> str:
    """CapCut ``property_type`` for a domain property name (best-effort)."""
    mapped = _KEYFRAME_TYPE_MAP.get(property_name)
    if mapped is not None:
        return mapped
    return "KFType" + property_name.title().replace("_", "")


def _serialize_common_keyframes(keyframes: list) -> list[dict]:
    """Group :class:`Keyframe` objects into CapCut ``common_keyframes`` (#7)."""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for kf in keyframes:
        if kf.property_name not in grouped:
            grouped[kf.property_name] = []
            order.append(kf.property_name)
        entry: dict[str, object] = {
            "id": _new_id(),
            "time_offset": int(kf.time),
            "values": [float(kf.value)],
            "curveType": "Line",
        }
        left, right = _curve_controls(kf.curve)
        entry["left_control"] = left
        entry["right_control"] = right
        grouped[kf.property_name].append(entry)

    out: list[dict] = []
    for prop in order:
        out.append(
            {
                "id": _new_id(),
                "material_id": "",
                "property_type": _keyframe_property_type(prop),
                "keyframe_list": grouped[prop],
            }
        )
    return out


def _curve_controls(curve: object) -> tuple[dict, dict]:
    """Split a [x1,y1,x2,y2] bezier list into left/right control dicts."""
    if isinstance(curve, (list, tuple)) and len(curve) >= 4:
        try:
            return (
                {"x": float(curve[0]), "y": float(curve[1])},
                {"x": float(curve[2]), "y": float(curve[3])},
            )
        except (TypeError, ValueError):
            pass
    return ({"x": 0.0, "y": 0.0}, {"x": 0.0, "y": 0.0})


# ---------------------------------------------------------------------------
# Leaf serializers
# ---------------------------------------------------------------------------


def _serialize_transform(tr: Transform) -> dict:
    """Serialize a :class:`Transform` into CapCut's ``clip`` block."""
    return {
        "transform": {"x": float(tr.position_x), "y": float(tr.position_y)},
        "scale": {"x": float(tr.scale_x), "y": float(tr.scale_y)},
        "rotation": float(tr.rotation),
        "alpha": float(tr.opacity),
    }


def _serialize_text_style(ts: TextStyle) -> dict:
    """Serialize a :class:`TextStyle`."""
    out: dict[str, object] = {
        "font": ts.font,
        "size": float(ts.size),
        "bold": bool(ts.bold),
        "italic": bool(ts.italic),
        "underline": bool(ts.underline),
        "alignment": ts.alignment,
        "stroke_width": float(ts.stroke_width),
    }
    if ts.color is not None:
        out["color"] = list(ts.color)
    if ts.stroke_color is not None:
        out["stroke_color"] = list(ts.stroke_color)
    if ts.position_x is not None:
        out["position_x"] = float(ts.position_x)
    if ts.position_y is not None:
        out["position_y"] = float(ts.position_y)
    return out


# ---------------------------------------------------------------------------
# Enum value helpers (str-enums serialize to their value)
# ---------------------------------------------------------------------------


def _material_type_value(mt: MaterialType) -> str:
    return mt.value if isinstance(mt, MaterialType) else str(mt)


def _track_type_value(tt: TrackType) -> str:
    return tt.value if isinstance(tt, TrackType) else str(tt)


# ---------------------------------------------------------------------------
# Folder name sanitization
# ---------------------------------------------------------------------------


def _sanitize_folder_name(name: str) -> str:
    """Turn a project name into a safe draft folder name.

    Strips illegal path characters, collapses whitespace, and trims trailing
    dots/spaces (Windows-hostile). Falls back to a generated name if empty.
    """
    cleaned = _ILLEGAL_FOLDER_CHARS.sub("_", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = f"project_{uuid.uuid4().hex[:8]}"
    return cleaned


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: dict) -> None:
    """Write ``obj`` as UTF-8 JSON to ``path``."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


# metetype for the draft_materials media registry, keyed by domain type.
_METETYPE: dict[MaterialType, str] = {
    MaterialType.VIDEO: "video",
    MaterialType.IMAGE: "photo",
    MaterialType.AUDIO: "music",
}


def _draft_material_entry(m: Material) -> dict:
    """One entry in the ``draft_meta_info.json`` ``draft_materials`` registry.

    Bug #25: CapCut resolves a project's media through this registry (NOT just
    ``draft_content.json``). An empty registry — or one with back-slash
    ``file_Path`` values — makes CapCut treat the media as missing / the path as
    abnormal. Mirrors the fields observed on real drafts.
    """
    now_s = int(time.time())
    now_us = now_s * 1_000_000
    duration = int(m.duration)
    return {
        "ai_group_type": "",
        "create_time": now_s,
        "duration": duration,
        "enter_from": 0,
        "extra_info": os.path.basename(m.path) if m.path else m.name,
        "file_Path": m.path or "",
        "height": int(m.height),
        "id": str(uuid.uuid4()),
        "import_time": now_s,
        "import_time_ms": now_us,
        "item_source": 1,
        "md5": "",
        "metetype": _METETYPE.get(m.material_type, "video"),
        "roughcut_time_range": {"duration": duration, "start": 0},
        "sub_time_range": {"duration": -1, "start": -1},
        "type": 0,
        "width": int(m.width),
    }


def _build_draft_materials(project: Project) -> list[dict]:
    """Build the grouped ``draft_materials`` list CapCut expects.

    Group ``type: 0`` holds all A/V media entries; the other group slots
    (observed on real drafts) are emitted empty for structural fidelity.
    """
    media = [
        _draft_material_entry(m)
        for m in project.materials.values()
        if m.path and m.material_type in _METETYPE
    ]
    groups = [{"type": 0, "value": media}]
    for empty_type in (1, 2, 3, 6, 7, 8, 18):
        groups.append({"type": empty_type, "value": []})
    return groups


def _write_sidecars(
    staging_dir: Path, final_dir: Path, project: Project, content: dict
) -> None:
    """Write the sidecar metadata files a CapCut draft folder needs to open.

    Files are written into ``staging_dir`` (the temp assembly dir), but path
    fields record ``final_dir`` — the location the folder is atomically moved to
    (#26).

    The reader keys off ``draft_content.json`` only, but CapCut's project
    browser reads ``draft_meta_info.json`` — including the ``draft_materials``
    media registry — to list and validate a project. We emit a full meta file
    matching the real schema, with forward-slash paths (#24) and a populated
    media registry (#25).
    """
    draft_id = content.get("draft_id", _new_id())
    fold_path = str(final_dir).replace("\\", "/")
    root_path = str(final_dir.parent).replace("\\", "/")
    now_us = int(time.time() * 1_000_000)
    duration = int(project.duration)

    meta = {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "draft_cover": "draft_cover.jpg",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "",
            "draft_enterprise_id": "",
            "draft_enterprise_name": "",
            "enterprise_material": [],
        },
        "draft_fold_path": fold_path,
        "draft_id": draft_id,
        "draft_is_ae_produce": False,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": _build_draft_materials(project),
        "draft_materials_copied_info": [],
        "draft_name": project.name,
        "draft_need_rename_folder": False,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": root_path,
        "draft_segment_extra_info": [],
        "draft_timeline_materials_size_": 0,
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": duration,
    }
    _write_json(staging_dir / "draft_meta_info.json", meta)

    # draft_virtual_store.json — material-id registry CapCut keeps alongside
    # the meta. Mirrors the real structure: a type-0 store entry plus one
    # type-1 (child_id, parent_id) pair per material.
    virtual_store = {
        "draft_materials": [],
        "draft_virtual_store": [
            {
                "type": 0,
                "value": [
                    {
                        "creation_time": 0,
                        "display_name": "",
                        "filter_type": 0,
                        "id": "",
                        "import_time": 0,
                        "import_time_us": 0,
                        "sort_sub_type": 0,
                        "sort_type": 0,
                        "subdraft_filter_type": 0,
                    }
                ],
            },
            {
                "type": 1,
                "value": [
                    {"child_id": m.id, "parent_id": ""}
                    for m in project.materials.values()
                ],
            },
            {"type": 2, "value": []},
        ],
    }
    _write_json(staging_dir / "draft_virtual_store.json", virtual_store)
