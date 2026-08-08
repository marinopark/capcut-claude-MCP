"""CapCut MCP server — tool declarations, schemas, and dispatch.

This module contains NO business logic. Its only jobs are:

  1. Declare the 12 MCP tools with explicit, hand-written JSON Schema dicts.
  2. Convert the seconds<->microseconds boundary: tool inputs/outputs use
     SECONDS (float); services and the whole engine use MICROSECONDS (int).
     Conversion happens ONLY here, via ``models.us_to_sec`` / ``models.sec_to_us``.
  3. Dispatch each tool call to the appropriate service (``analyzer`` for reads,
     ``builder`` for writes) and return a JSON-serializable digest.

Layering: this file imports ``protocol`` (generic MCP stdio server), the two
services, and ``engine.models`` (for the time helpers). It must never import
``engine.draft_reader`` / ``engine.draft_writer`` or otherwise touch CapCut JSON.

Run directly (``python src/capcut_mcp/server.py``) or import as
``capcut_mcp.server``. The sibling services use package-relative imports
(``from ..engine import ...``), so everything is imported via the ``capcut_mcp``
package prefix; the ``src`` directory (parent of the package) is added to
``sys.path`` when this file is run as a script. NEVER write anything but
JSON-RPC to stdout — diagnostics go to stderr (``protocol.log``).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
import sys

# The services layer (analyzer/builder) uses package-relative imports such as
# ``from ..engine import draft_reader``. Those only resolve when this whole tree
# is imported as the ``capcut_mcp`` package. When this file is executed directly
# as a script, its own directory (``src/capcut_mcp``) — not ``src`` — is on
# sys.path, so ``capcut_mcp`` would not be importable. Fix that by putting the
# package PARENT (``src``) on sys.path, then import everything package-qualified.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))  # .../src/capcut_mcp
_SRC_DIR = os.path.dirname(_PKG_DIR)  # .../src
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from capcut_mcp import protocol  # noqa: E402  generic MCP JSON-RPC stdio server
from capcut_mcp.engine import models  # noqa: E402  us_to_sec / sec_to_us
from capcut_mcp.services import analyzer  # noqa: E402  read-side logic
from capcut_mcp.services import builder  # noqa: E402  write-side build state


# ---------------------------------------------------------------------------
# Schema-driven required-argument validation
# ---------------------------------------------------------------------------


def _validate_required(args: dict, input_schema: dict) -> None:
    """Raise a clear ``ValueError`` if any schema-``required`` arg is missing.

    Without this, handlers index required args directly (``args["name"]``) and a
    client that omits a field gets an opaque ``KeyError: 'name'``. We surface a
    user/LLM-friendly message instead. ``ValueError`` (not ``KeyError``) is used
    so protocol's ``str(exc)`` wrapping does not add stray quotes around the
    message.
    """
    required = input_schema.get("required") or []
    missing = [key for key in required if key not in args]
    if missing:
        names = ", ".join(repr(m) for m in missing)
        raise ValueError(f"Missing required argument: {names}")


def tool(name: str, description: str, input_schema: dict):
    """Like ``protocol.tool`` but validates the schema's ``required`` args first.

    Wraps the decorated handler so that missing required arguments produce a
    clear ``ValueError`` (surfaced by protocol as an ``isError`` result) rather
    than a bare ``KeyError``. The validation stays in sync with each tool's
    declared JSON Schema ``required`` list.
    """

    def _decorator(func):
        def _wrapped(args: dict):
            _validate_required(args, input_schema)
            return func(args)

        protocol.register_tool(name, description, input_schema, _wrapped)
        return func

    return _decorator


# ---------------------------------------------------------------------------
# Boundary conversion helpers (the ONLY microsecond<->second boundary here)
# ---------------------------------------------------------------------------


def _sec_to_us(value):
    """Seconds (float) -> microseconds (int), or None passed through."""
    if value is None:
        return None
    return models.sec_to_us(value)


def _us_to_sec(value):
    """Microseconds (int) -> seconds (float), or None passed through."""
    if value is None:
        return None
    return models.us_to_sec(value)


def _convert_us_fields(d, mapping):
    """Return a copy of dict ``d`` with ``_us`` keys renamed to ``_sec`` values.

    ``mapping`` maps a source ``*_us`` key to its destination ``*_sec`` key.
    Any key present in ``d`` is converted; other keys are copied verbatim. Keys
    named in ``mapping`` are removed after conversion.
    """
    out = {}
    for key, value in d.items():
        if key in mapping:
            out[mapping[key]] = _us_to_sec(value)
        else:
            out[key] = value
    # Ensure destination keys exist even if source was absent but expected.
    return out


# Standard µs->sec key renames used across the read tools.
_TIMELINE_US_KEYS = {
    "start_us": "start_sec",
    "duration_us": "duration_sec",
    "end_us": "end_sec",
}


def _convert_start_end_only(d):
    """µs->sec convert start/end and DROP any duration field.

    design.md §8 freezes ``get_timeline`` rows to
    ``{track_index, track_type, segment_id, start_sec, end_sec, source_name,
    speed}`` and ``get_captions`` rows to ``{start_sec, end_sec, text}`` — with
    NO ``duration`` field. Analyzer rows carry ``duration_us``; we convert
    start/end and drop duration so output matches the frozen contract.
    """
    out = {}
    for key, value in d.items():
        if key in ("duration_us", "duration_sec"):
            continue  # not part of the frozen contract for these two tools
        if key == "start_us":
            out["start_sec"] = _us_to_sec(value)
        elif key == "end_us":
            out["end_sec"] = _us_to_sec(value)
        else:
            out[key] = value
    return out


def _convert_segment_detail(d):
    """Recursively convert a segment-detail digest's ``_us`` keys to ``_sec``.

    Handles the top-level time fields, per-keyframe ``time_us``, and per-effect
    ``duration_us`` if present. Everything else is copied verbatim.
    """
    out = {}
    for key, value in d.items():
        if key.endswith("_us"):
            out[key[:-3] + "_sec"] = _us_to_sec(value)
        elif key == "keyframes" and isinstance(value, list):
            out[key] = [_convert_keyframe(kf) for kf in value]
        elif key == "effects" and isinstance(value, list):
            out[key] = [_convert_effect(ef) for ef in value]
        else:
            out[key] = value
    return out


def _convert_keyframe(kf):
    if not isinstance(kf, dict):
        return kf
    out = {}
    for key, value in kf.items():
        if key.endswith("_us"):
            out[key[:-3] + "_sec"] = _us_to_sec(value)
        else:
            out[key] = value
    return out


def _convert_effect(ef):
    if not isinstance(ef, dict):
        return ef
    out = {}
    for key, value in ef.items():
        if key.endswith("_us"):
            out[key[:-3] + "_sec"] = _us_to_sec(value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Read tools (5) + doctor
# ---------------------------------------------------------------------------


@tool(
    "list_projects",
    "List all CapCut projects (drafts) found on this machine, with name, last "
    "modified time, total duration in seconds, resolution [width, height], and "
    "fps. Takes no arguments.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def _list_projects(args: dict) -> object:
    rows = analyzer.list_projects()
    out = []
    for r in rows:
        out.append(
            {
                "name": r.get("name"),
                "modified_at": r.get("modified_at"),
                "duration_sec": _us_to_sec(r.get("duration_us")),
                "resolution": r.get("resolution"),
                "fps": r.get("fps"),
            }
        )
    return out


@tool(
    "analyze_project",
    "Analyze one CapCut project: returns a digest with resolution, fps, total "
    "duration (seconds), a per-track summary, clip count, media list (path + "
    "exists), effect/transition counts, and whether it has captions.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Project (draft folder) name.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)
def _analyze_project(args: dict) -> object:
    d = analyzer.analyze_project(args["name"])
    out = dict(d)
    if "duration_us" in out:
        out["duration_sec"] = _us_to_sec(out.pop("duration_us"))
    return out


@tool(
    "get_timeline",
    "Get a compressed timeline view of a project: one row per segment with "
    "track index/type, segment id, start/end (seconds), source media name, and "
    "speed. Keyframes and effect params are omitted. Optional track_type filter.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Project (draft folder) name.",
            },
            "track_type": {
                "type": "string",
                "description": "Optional track type filter.",
                "enum": ["video", "audio", "text", "effect", "sticker", "filter"],
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)
def _get_timeline(args: dict) -> object:
    rows = analyzer.get_timeline(args["name"], args.get("track_type"))
    return [_convert_start_end_only(r) for r in rows]


@tool(
    "get_captions",
    "Get all captions/subtitles from a project's text tracks as a list of "
    "{start_sec, end_sec, text}, ordered by start time.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Project (draft folder) name.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)
def _get_captions(args: dict) -> object:
    rows = analyzer.get_captions(args["name"])
    return [_convert_start_end_only(r) for r in rows]


@tool(
    "get_segment_detail",
    "Get the full detail of a single segment (clip) by id: times (seconds), "
    "speed, volume, fades, transform, text style, keyframes, and effects with "
    "their parameters.",
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Project (draft folder) name.",
            },
            "segment_id": {
                "type": "string",
                "description": "The segment id (from get_timeline).",
            },
        },
        "required": ["name", "segment_id"],
        "additionalProperties": False,
    },
)
def _get_segment_detail(args: dict) -> object:
    d = analyzer.get_segment_detail(args["name"], args["segment_id"])
    return _convert_segment_detail(d)


@tool(
    "doctor",
    "Diagnostic report: drafts directory detection, whether CapCut is running, "
    "detected draft format, ffprobe availability, read/write permissions, and "
    "draft count. Takes no arguments.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def _doctor(args: dict) -> object:
    return analyzer.doctor()


# ---------------------------------------------------------------------------
# Write tools (6)
# ---------------------------------------------------------------------------


@tool(
    "create_project",
    "Create a new in-memory CapCut project (not yet written to disk). Give it a "
    "name, canvas width/height in pixels, and fps. Use add_* tools then "
    "save_draft to persist.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "New project name."},
            "width": {
                "type": "integer",
                "description": "Canvas width in pixels.",
            },
            "height": {
                "type": "integer",
                "description": "Canvas height in pixels.",
            },
            "fps": {
                "type": "number",
                "description": "Frames per second.",
                "default": 30,
            },
        },
        "required": ["name", "width", "height"],
        "additionalProperties": False,
    },
)
def _create_project(args: dict) -> object:
    return builder.create_project(
        args["name"],
        args["width"],
        args["height"],
        args.get("fps", 30.0),
    )


@tool(
    "add_video",
    "Add a video clip to a project. start_sec is the timeline position. If "
    "duration_sec is omitted, it is auto-detected via ffprobe (error if ffprobe "
    "is unavailable). Optional track index, speed, and volume.",
    {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Target project name."},
            "path": {
                "type": "string",
                "description": "Absolute path to the video file.",
            },
            "start_sec": {
                "type": "number",
                "description": "Timeline start position in seconds.",
            },
            "duration_sec": {
                "type": "number",
                "description": "Clip duration in seconds (optional; ffprobe "
                "auto-detects if omitted).",
            },
            "track": {
                "type": "integer",
                "description": "Optional target track index.",
            },
            "speed": {
                "type": "number",
                "description": "Playback speed multiplier.",
                "default": 1,
            },
            "volume": {
                "type": "number",
                "description": "Volume 0.0-1.0.",
                "default": 1,
            },
        },
        "required": ["project", "path", "start_sec"],
        "additionalProperties": False,
    },
)
def _add_video(args: dict) -> object:
    d = builder.add_video(
        args["project"],
        args["path"],
        _sec_to_us(args["start_sec"]),
        duration_us=_sec_to_us(args.get("duration_sec")),
        track=args.get("track"),
        speed=args.get("speed", 1.0),
        volume=args.get("volume", 1.0),
    )
    return _convert_us_fields(d, _TIMELINE_US_KEYS)


@tool(
    "add_audio",
    "Add an audio clip to a project. start_sec is the timeline position. If "
    "duration_sec is omitted, it is auto-detected via ffprobe (error if ffprobe "
    "is unavailable). Optional volume and fade in/out (seconds).",
    {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Target project name."},
            "path": {
                "type": "string",
                "description": "Absolute path to the audio file.",
            },
            "start_sec": {
                "type": "number",
                "description": "Timeline start position in seconds.",
            },
            "duration_sec": {
                "type": "number",
                "description": "Clip duration in seconds (optional; ffprobe "
                "auto-detects if omitted).",
            },
            "volume": {
                "type": "number",
                "description": "Volume 0.0-1.0.",
                "default": 1,
            },
            "fade_in": {
                "type": "number",
                "description": "Fade-in duration in seconds.",
                "default": 0,
            },
            "fade_out": {
                "type": "number",
                "description": "Fade-out duration in seconds.",
                "default": 0,
            },
        },
        "required": ["project", "path", "start_sec"],
        "additionalProperties": False,
    },
)
def _add_audio(args: dict) -> object:
    d = builder.add_audio(
        args["project"],
        args["path"],
        _sec_to_us(args["start_sec"]),
        duration_us=_sec_to_us(args.get("duration_sec")),
        volume=args.get("volume", 1.0),
        fade_in_us=_sec_to_us(args.get("fade_in", 0.0)),
        fade_out_us=_sec_to_us(args.get("fade_out", 0.0)),
    )
    return _convert_us_fields(d, _TIMELINE_US_KEYS)


@tool(
    "add_text",
    "Add a text/caption segment to a project. start_sec and duration_sec set "
    "its timeline span. Optional font, size, color (#RRGGBB), and position "
    "[x, y] (normalized -1..1).",
    {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Target project name."},
            "text": {"type": "string", "description": "Text content."},
            "start_sec": {
                "type": "number",
                "description": "Timeline start position in seconds.",
            },
            "duration_sec": {
                "type": "number",
                "description": "Duration in seconds.",
            },
            "font": {"type": "string", "description": "Optional font name."},
            "size": {
                "type": "number",
                "description": "Font size.",
                "default": 15,
            },
            "color": {
                "type": "string",
                "description": "Hex color like #RRGGBB.",
            },
            "position": {
                "type": "array",
                "description": "Normalized [x, y] position, each -1..1.",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
        },
        "required": ["project", "text", "start_sec", "duration_sec"],
        "additionalProperties": False,
    },
)
def _add_text(args: dict) -> object:
    position = args.get("position")
    if position is not None:
        position = tuple(position)
    d = builder.add_text(
        args["project"],
        args["text"],
        _sec_to_us(args["start_sec"]),
        _sec_to_us(args["duration_sec"]),
        font=args.get("font"),
        size=args.get("size", 15.0),
        color=args.get("color"),
        position=position,
    )
    return _convert_us_fields(d, _TIMELINE_US_KEYS)


@tool(
    "add_subtitles_from_srt",
    "Parse an SRT file and add every cue as a text segment on a dedicated "
    "subtitle track. Returns how many cues were added and the track index.",
    {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Target project name."},
            "srt_path": {
                "type": "string",
                "description": "Absolute path to the .srt file.",
            },
        },
        "required": ["project", "srt_path"],
        "additionalProperties": False,
    },
)
def _add_subtitles_from_srt(args: dict) -> object:
    d = builder.add_subtitles_from_srt(args["project"], args["srt_path"])
    return _convert_us_fields(d, _TIMELINE_US_KEYS)


@tool(
    "save_draft",
    "Write the in-memory project to disk as a new CapCut draft folder "
    "(atomically). Refuses to write while CapCut is running unless force=true. "
    "Returns the draft directory and any missing-media warnings. Restart CapCut "
    "to see the new project.",
    {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "Project name to save."},
            "open_hint": {
                "type": "boolean",
                "description": "Include a hint about restarting CapCut.",
                "default": True,
            },
            "force": {
                "type": "boolean",
                "description": "Write even if CapCut is running.",
                "default": False,
            },
        },
        "required": ["project"],
        "additionalProperties": False,
    },
)
def _save_draft(args: dict) -> object:
    return builder.save_draft(
        args["project"],
        open_hint=args.get("open_hint", True),
        force=args.get("force", False),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    protocol.serve(sys.stdin, sys.stdout)
