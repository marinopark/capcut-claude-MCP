# CapCut MCP Server — Interface Contract (design.md)

Authoritative shared contract for the 5 parallel implementation agents. Every
signature below is FROZEN: implementers code strictly against these names,
parameters, types, and return types. Do not change a signature without updating
this document first (it would break other agents).

Target: **Python 3.11+, standard library ONLY** (`json`, `sys`, `pathlib`,
`dataclasses`, `enum`, `shutil`, `tempfile`, `subprocess`, `uuid`, `platform`,
`re`, `time`). No external packages, no MCP SDK, no pydantic. Tests use
`unittest`.

---

## 0. Layering rules (enforced)

```
protocol.py    generic JSON-RPC/MCP stdio server — knows NOTHING about CapCut
     │
server.py      declares MCP tools + JSON Schemas, converts sec<->us, dispatches
     │
services/      analyzer.py (read) + builder.py (write) — use ONLY models.py
     │
engine/        models.py (domain), locator.py (paths), 
               draft_reader.py + draft_writer.py (ONLY files that know CapCut JSON)
```

Hard rules:
- **protocol.py** is CapCut-agnostic and reusable by any MCP server. It never
  imports from `engine/`, `services/`, or `server.py` domain logic.
- **server.py** contains NO business logic: only tool schema declarations, the
  seconds<->microseconds conversion at the boundary, and calls into services.
- **services/** operate exclusively on `engine.models` dataclasses. They never
  read or write CapCut JSON, never touch `draft_content.json` keys.
- **CapCut JSON schema knowledge lives ONLY in `draft_reader.py` and
  `draft_writer.py`.** If the on-disk format changes, only these two files change.
- **Microsecond/second boundary rule:** internal domain + all `engine/` and
  `services/` code uses microseconds (int). Tool inputs/outputs use seconds
  (float). Conversion happens ONLY in `server.py` using
  `models.us_to_sec` / `models.sec_to_us`.
- **stdout is sacred:** `protocol.py` writes ONLY JSON-RPC lines to stdout. All
  logs/diagnostics go to stderr.

### Safety rules (implemented in writer + builder + locator)
1. **Process check before write:** `save_draft` calls `locator.is_capcut_running()`;
   if running and `force` is False, it returns a warning and does NOT write.
2. **New-folder-only (v1):** writes always create a NEW draft folder. No tool
   overwrites/modifies an existing draft. Writer errors if target exists and
   `force` is False.
3. **Read retry:** JSON parse failures retry 3× at 0.5s intervals (app may be
   mid-save) — `draft_reader._read_json_with_retry`.
4. **Atomic write:** draft folder is fully assembled in a temp dir, then
   `os.replace`/`shutil.move`-renamed into the drafts directory.
5. **Path validation:** media paths are normalized to absolute and existence is
   checked before save; missing files are reported in a warnings list, not
   silently dropped.

---

## 1. `engine/models.py` — DONE (canonical, already implemented)

Module-level helpers (the sanctioned time boundary):
- `US_PER_SECOND: int = 1_000_000`
- `us_to_sec(us: int) -> float`
- `sec_to_us(sec: float) -> int`

Enums (both subclass `str`):
- `TrackType`: `VIDEO, AUDIO, TEXT, EFFECT, STICKER, FILTER`
- `MaterialType`: `VIDEO, IMAGE, AUDIO, TEXT, EFFECT, STICKER, FILTER, TRANSITION`

Dataclasses (fields authoritative — see source for docstrings):
- `TimeRange(start:int, duration:int)` → prop `end:int`
- `Keyframe(property_name:str, time:int, value:float, curve:Optional[list[float]]=None)`
- `EffectRef(effect_id:str, name:str, material_type:MaterialType=EFFECT, params:dict[str,object]={}, duration:int=0)`
- `TextStyle(font, size:float=15.0, color:Optional[tuple4]=None, bold, italic, underline, alignment:str="center", position_x, position_y, stroke_color, stroke_width:float=0.0)`
- `Transform(position_x=0, position_y=0, scale_x=1, scale_y=1, rotation=0, opacity=1)`
- `Material(id:str, material_type:MaterialType, path:Optional[str]=None, name:str="", duration:int=0, width:int=0, height:int=0, text:Optional[str]=None, text_style:Optional[TextStyle]=None, exists:bool=True)`
- `Segment(id:str, material_id:str, target:TimeRange, source_start:int=0, source_duration:int=0, speed:float=1.0, volume:float=1.0, fade_in:int=0, fade_out:int=0, transform:Optional[Transform]=None, text_style:Optional[TextStyle]=None, keyframes:list[Keyframe]=[], effects:list[EffectRef]=[])` → props `start:int`, `duration:int`, `end:int`
- `Track(id:str, track_type:TrackType, segments:list[Segment]=[], index:int=0, muted:bool=False, name:str="")` → prop `duration:int`
- `Caption(text:str, start:int, duration:int)` → prop `end:int`
- `Project(name:str, width:int, height:int, fps:float=30.0, tracks:list[Track]=[], materials:dict[str,Material]={}, stored_duration:int=0, draft_format:str="draft_content", draft_dir:Optional[str]=None)`
  - props: `duration:int`, `resolution:tuple[int,int]`
  - methods: `get_material(id)->Optional[Material]`, `add_material(Material)->None`, `tracks_of_type(TrackType)->list[Track]`, `find_segment(id)->Optional[Segment]`

All time fields are **microseconds (int)**.

---

## 2. `protocol.py` — generic MCP JSON-RPC stdio server

CapCut-agnostic. No imports from engine/services.

### Registry data structures
```python
@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict          # hand-written JSON Schema dict
    handler: Callable[[dict], object]   # receives validated args dict, returns JSON-serializable result

# Module-level registry:
_REGISTRY: dict[str, ToolDef] = {}
```

### Public API
- `tool(name: str, description: str, input_schema: dict) -> Callable[[Callable], Callable]`
  Decorator. Registers the decorated function as a `ToolDef` in `_REGISTRY`.
  The wrapped function takes one arg (`args: dict`) and returns a
  JSON-serializable object. Returns the original function unchanged.
- `register_tool(name: str, description: str, input_schema: dict, handler: Callable[[dict], object]) -> None`
  Non-decorator registration path (server.py may use either).
- `get_tools() -> list[ToolDef]` — snapshot of registered tools.
- `clear_registry() -> None` — test helper; empties `_REGISTRY`.
- `serve(stdin=sys.stdin, stdout=sys.stdout) -> None`
  Main blocking loop. Reads newline-delimited JSON-RPC from `stdin`, writes
  single-line JSON responses to `stdout` (flush each). Runs until EOF, then
  returns cleanly.
- `log(*args) -> None` — writes to stderr only (never stdout).

### Loop / method handling (inside `serve`)
- Read line-by-line. Blank lines skipped. Unparseable lines: `log(...)` to
  stderr and continue (do NOT crash, do NOT respond).
- Dispatch by `method`:
  - `initialize` → result
    `{"protocolVersion": <echo client value or "2024-11-05">, "capabilities": {"tools": {}}, "serverInfo": {"name": "capcut-mcp", "version": <VERSION>}}`
  - `notifications/initialized` (and any `notifications/*`) → notification, no response.
  - `tools/list` → `{"tools": [ {name, description, inputSchema} for each ToolDef ]}`
  - `tools/call` → params `{name, arguments}`. Look up tool; call
    `handler(arguments or {})`. On success:
    `{"content": [{"type": "text", "text": json.dumps(result)}]}`.
    On handler exception: `{"content": [{"type": "text", "text": <error msg>}], "isError": true}`
    (this is a RESULT, not a JSON-RPC error). Unknown tool name → same isError result.
  - `ping` → `{}`
  - unknown method → JSON-RPC error object `{"code": -32601, "message": "Method not found"}`
- Requests are distinguished from notifications by presence of `id`. Never emit a
  response for a message without `id`.
- Helpers (private, names suggested): `_make_response(id, result)`,
  `_make_error(id, code, message)`, `_write(stdout, obj)`.

### Constants
- `PROTOCOL_VERSION: str = "2024-11-05"` (fallback when client omits it)
- `SERVER_NAME: str = "capcut-mcp"`, `SERVER_VERSION: str = "0.1.0"`

### How server.py registers
`server.py` imports `protocol` and calls `protocol.tool(...)` as a decorator on
each tool function (or `register_tool`), then calls `protocol.serve()` under
`if __name__ == "__main__":`.

---

## 3. `engine/locator.py` — path & environment detection

Standard library only (`os`, `pathlib`, `platform`, `subprocess`, `shutil`).
Returns primitives/`Path`; knows OS layout, NOT CapCut JSON.

- `find_drafts_dir() -> Path | None`
  Locate the CapCut drafts directory for the current OS.
  Windows: `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`.
  macOS: `~/Movies/CapCut/User Data/Projects/com.lveditor.draft`.
  Returns the first existing path, else None.
- `candidate_drafts_dirs() -> list[Path]`
  All plausible drafts locations for this OS (for `doctor` reporting), regardless
  of existence.
- `list_draft_folders(drafts_dir: Path) -> list[Path]`
  Immediate subdirectories that look like drafts (contain a recognized draft
  file). Sorted by mtime descending.
- `detect_draft_format(draft_dir: Path) -> str`
  Inspect a draft folder and return a format id: `"draft_content"` if
  `draft_content.json` present, `"draft_info"` if `draft_info.json` present,
  else `"unknown"`.
- `draft_content_path(draft_dir: Path) -> Path | None`
  Resolve the actual draft JSON file inside a folder (`draft_content.json` or
  `draft_info.json`), or None if neither exists.
- `is_ffprobe_available() -> bool`
  True if `ffprobe` resolves on PATH (`shutil.which`).
- `is_capcut_running() -> bool`
  Process check. Windows: `tasklist` parsed for `CapCut.exe`. macOS: `pgrep -x`
  for `CapCut`. On any subprocess failure, returns False (fail-open for reads;
  writer treats unknown as not-running but should be conservative — see below).
- `probe_media_duration(path: Path) -> int | None`
  If ffprobe available, return intrinsic media duration in **microseconds**;
  else None. (Used by builder for `add_video`/`add_audio` auto-detect.)
- `probe_media_dimensions(path: Path) -> tuple[int, int] | None`
  ffprobe width/height in pixels, or None.

Constants: `DRAFT_PACKAGE_NAME = "com.lveditor.draft"`,
`DRAFT_CONTENT_FILENAMES = ("draft_content.json", "draft_info.json")`.

---

## 4. `engine/draft_reader.py` — CapCut JSON → `Project`

One of only TWO schema-aware files. Imports `engine.models`, `engine.locator`.

- `read_project(draft_dir: Path) -> Project`
  Read the draft folder's JSON, map CapCut schema → `Project` domain model
  (populate materials pool, tracks, segments, times in µs). `Project.name` from
  folder name, `Project.draft_dir` set, `draft_format` from
  `locator.detect_draft_format`.
- `read_project_by_name(name: str, drafts_dir: Path | None = None) -> Project`
  Convenience: resolve `drafts_dir` (default `locator.find_drafts_dir()`), find
  the folder named `name`, then `read_project`. Raises `FileNotFoundError` if
  absent.
- `_read_json_with_retry(json_path: Path, attempts: int = 3, delay: float = 0.5) -> dict`
  Read+parse JSON with retry on `JSONDecodeError`/`OSError` (app may be
  mid-save). Sleeps `delay` between attempts; raises last error after `attempts`.
- `_map_canvas(raw: dict) -> tuple[int, int, float]` → `(width, height, fps)`
- `_map_material(raw: dict) -> Material`
- `_map_segment(raw: dict, track_type: TrackType) -> Segment`
- `_map_track(raw: dict, index: int) -> Track`

Raises `ValueError` on structurally invalid/unrecognized JSON.

---

## 5. `engine/draft_writer.py` — `Project` → draft folder (atomic)

The other schema-aware file. Imports `engine.models`, `engine.locator`.

- `write_draft(project: Project, drafts_dir: Path, force: bool = False) -> Path`
  Serialize `Project` → CapCut JSON, assemble a complete draft folder in a temp
  dir (`tempfile.mkdtemp`), then atomically rename into `drafts_dir`. Returns the
  final draft folder Path. Behavior:
  - Target folder name derived from `project.name` (sanitized). If it already
    exists and `force` is False → raise `FileExistsError` (new-folder-only rule).
  - Validates media paths first via `validate_media_paths`; missing files do not
    abort (still written) but are surfaced by caller (builder returns warnings).
  - Atomic move via `os.replace` (fallback `shutil.move`).
- `validate_media_paths(project: Project) -> list[str]`
  Normalize each material path to absolute, set `Material.exists`, and return a
  list of human-readable warnings for missing files.
- `_build_draft_content(project: Project) -> dict`
  Domain model → CapCut `draft_content.json` dict (times back to µs, generate
  ids/uuids, tracks[]/materials[]/canvas_config).
- `_serialize_material(m: Material) -> dict`
- `_serialize_segment(s: Segment) -> dict`
- `_serialize_track(t: Track) -> dict`
- `_sanitize_folder_name(name: str) -> str`

Constants: `DRAFT_CONTENT_FILENAME = "draft_content.json"`.

---

## 6. `services/analyzer.py` — read-side business logic

Operates ONLY on `engine.models` via `draft_reader`. Returns plain
dict/list (JSON-serializable, digested — never raw CapCut JSON). All time fields
in returned dicts are in **microseconds** named with `_us` suffix; server.py
converts to seconds. (See note below.)

> **Time convention in service return values:** services return time as integer
> microseconds under keys suffixed `_us` (e.g. `start_us`, `duration_us`).
> `server.py` maps these to `*_sec` floats at the boundary. This keeps the
> microsecond rule intact inside services.

- `list_projects(drafts_dir: Path | None = None) -> list[dict]`
  → `[{"name", "modified_at" (ISO str), "duration_us", "resolution" [w,h], "fps"}]`
- `analyze_project(name: str, drafts_dir: Path | None = None) -> dict`
  → `{"name", "resolution", "fps", "duration_us", "track_summary":[{"track_index","track_type","segment_count"}], "clip_count", "media": [{"path","exists"}], "effect_counts": {name:count}, "transition_counts": {name:count}, "has_captions": bool}`
- `get_timeline(name: str, track_type: str | None = None, drafts_dir: Path | None = None) -> list[dict]`
  → `[{"track_index","track_type","segment_id","start_us","duration_us","end_us","source_name","speed"}]`
  (compressed view — no keyframes/effect params). Optional `track_type` filter.
- `get_captions(name: str, drafts_dir: Path | None = None) -> list[dict]`
  → `[{"start_us","duration_us","end_us","text"}]` (from text tracks, ordered).
- `get_segment_detail(name: str, segment_id: str, drafts_dir: Path | None = None) -> dict`
  → full digest of ONE segment: ids, times (`_us`), speed, volume, fades,
  transform, text_style, keyframes[], effects[] with params. Raises
  `KeyError`/`ValueError` if not found.
- `doctor(drafts_dir: Path | None = None) -> dict`
  → `{"drafts_dir", "drafts_dir_exists", "candidate_dirs":[...], "capcut_running", "draft_format", "ffprobe_available", "readable", "writable", "draft_count"}`
  Uses `locator` for all detection.

Private helpers (suggested): `_project_summary(Project)->dict`,
`_segment_row(Segment, Track, Project)->dict`.

---

## 7. `services/builder.py` — write-side build state

Operates ONLY on `engine.models`; persists via `draft_writer`. Maintains an
in-memory registry of `Project` build states keyed by project name.

### Build-state registry
```python
_PROJECTS: dict[str, Project] = {}   # name -> in-progress Project
```

### Public API (all times IN are microseconds; server.py converts from seconds)
- `create_project(name: str, width: int, height: int, fps: float = 30.0) -> dict`
  Create a new in-memory `Project`, store in `_PROJECTS`. Returns
  `{"name","width","height","fps","created": true}`. Errors if name exists.
- `add_video(project: str, path: str, start_us: int, duration_us: int | None = None, track: int | None = None, speed: float = 1.0, volume: float = 1.0) -> dict`
  Add a video segment (+ material). If `duration_us` is None, use
  `locator.probe_media_duration`; if ffprobe unavailable → raise `ValueError`
  requiring explicit duration. Returns `{"segment_id","track_index","duration_us"}`.
- `add_audio(project: str, path: str, start_us: int, duration_us: int | None = None, volume: float = 1.0, fade_in_us: int = 0, fade_out_us: int = 0) -> dict`
  Add an audio segment. Same ffprobe fallback rule. Returns segment info dict.
- `add_text(project: str, text: str, start_us: int, duration_us: int, font: str | None = None, size: float = 15.0, color: str | None = None, position: tuple[float,float] | None = None) -> dict`
  Add a text segment (+ text material + `TextStyle`). `color` accepts a hex
  string (`"#RRGGBB"`); parsing helper `_parse_color`. Returns segment info.
- `add_subtitles_from_srt(project: str, srt_path: str) -> dict`
  Parse SRT and create one text segment per cue on a dedicated text track.
  Returns `{"added": <count>, "track_index"}`.
- `save_draft(project: str, drafts_dir: Path | None = None, open_hint: bool = True, force: bool = False) -> dict`
  Safety check `locator.is_capcut_running()`: if running and not `force` →
  return `{"saved": false, "warning": <msg>, "capcut_running": true}` WITHOUT
  writing. Else call `draft_writer.write_draft`. Returns
  `{"saved": true, "draft_dir", "warnings":[...paths...], "message": "Restart CapCut to see this project in the list."}`.
- `parse_srt(text: str) -> list[Caption]`
  Regex SRT parser → list of `Caption` (times in µs). Standalone/pure so tests
  can call it directly. Also usable as `parse_srt_file(path: str) -> list[Caption]`.

Private helpers (suggested): `_get_project(name)->Project` (raises if missing),
`_ensure_track(project, TrackType)->Track`, `_new_id()->str` (uuid4 hex),
`_parse_color(hex_str)->tuple[float,float,float,float]`.

---

## 8. `server.py` — MCP tool declarations + dispatch

Imports `protocol`, `services.analyzer`, `services.builder`, and
`engine.models` (for `us_to_sec`/`sec_to_us`). NO business logic. Each tool:
declares an explicit JSON Schema dict, converts seconds→microseconds on input,
calls a service, converts microseconds→seconds on output, returns a
JSON-serializable object. Ends with `if __name__ == "__main__": protocol.serve()`.

**Boundary rule:** every `*_sec` field in a tool's I/O is converted via
`sec_to_us` (in) / `us_to_sec` (out). Services only ever see `_us`.

### Read tools (5) + doctor
| tool | input schema (properties) | returns (seconds at boundary) |
|---|---|---|
| `list_projects` | `{}` | `[{name, modified_at, duration_sec, resolution:[w,h], fps}]` |
| `analyze_project` | `{name: string (req)}` | analyze digest with `duration_sec` |
| `get_timeline` | `{name: string (req), track_type: string enum[video,audio,text,effect,sticker,filter] (opt)}` | `[{track_index, track_type, segment_id, start_sec, end_sec, source_name, speed}]` |
| `get_captions` | `{name: string (req)}` | `[{start_sec, end_sec, text}]` |
| `get_segment_detail` | `{name: string (req), segment_id: string (req)}` | full segment digest, times in `_sec` |
| `doctor` | `{}` | doctor report dict |

### Write tools (6)
| tool | input schema (properties) |
|---|---|
| `create_project` | `{name:string(req), width:int(req), height:int(req), fps:number(default 30)}` |
| `add_video` | `{project:string(req), path:string(req), start_sec:number(req), duration_sec:number(opt), track:int(opt), speed:number(default 1), volume:number(default 1)}` |
| `add_audio` | `{project:string(req), path:string(req), start_sec:number(req), duration_sec:number(opt), volume:number(default 1), fade_in:number(default 0), fade_out:number(default 0)}` |
| `add_text` | `{project:string(req), text:string(req), start_sec:number(req), duration_sec:number(req), font:string(opt), size:number(default 15), color:string(opt "#RRGGBB"), position:[number,number](opt)}` |
| `add_subtitles_from_srt` | `{project:string(req), srt_path:string(req)}` |
| `save_draft` | `{project:string(req), open_hint:boolean(default true), force:boolean(default false)}` |

Each tool function has the shape:
```python
@protocol.tool("get_timeline", "…", {"type":"object","properties":{...},"required":[...]})
def _get_timeline(args: dict) -> object:
    rows = analyzer.get_timeline(args["name"], args.get("track_type"))
    return [_sec_row(r) for r in rows]   # us -> sec at boundary
```
Boundary conversion helpers (private in server.py):
`_us_to_sec_fields(d, keys)`, `_sec_to_us(value)`.

---

## 9. CapCut JSON schema — ASSUMPTIONS (verify against real draft)

> Phase 0 recon was skipped; no real `draft_content.json` sample exists yet.
> The mapping below is based on the reverse-engineered international
> CapCut/JianYing format. **Everything in this section is an ASSUMPTION** that
> `draft_reader.py`/`draft_writer.py` implementers MUST verify against a real
> draft before Phase 2/3 sign-off. Confine all such knowledge to those two files.

Assumed top-level `draft_content.json` shape:
```jsonc
{
  "canvas_config": { "width": 1920, "height": 1080, "ratio": "original" },
  "fps": 30.0,
  "duration": 12000000,                 // microseconds
  "materials": {
    "videos":  [ { "id", "path", "duration", "width", "height", "material_name", "type" } ],
    "audios":  [ { "id", "path", "duration", "name" } ],
    "texts":   [ { "id", "content", "font", ... } ],
    "effects": [ { "id", "name", ... } ],
    "transitions": [ ... ],
    "stickers": [ ... ]
  },
  "tracks": [
    {
      "id", "type": "video|audio|text|effect|sticker|filter",
      "segments": [
        {
          "id",
          "material_id",
          "target_timerange": { "start": <us>, "duration": <us> },
          "source_timerange": { "start": <us>, "duration": <us> },
          "speed": 1.0,
          "volume": 1.0,
          "clip": { "transform": {"x","y"}, "scale": {"x","y"}, "rotation", "alpha" },
          "keyframes": { "position_x": [ {"time_offset","values"} ], ... },
          "extra_material_refs": [ effect/filter/transition ids ]
        }
      ]
    }
  ]
}
```
Key assumptions to verify:
- **Time unit is microseconds** in `target_timerange`/`source_timerange`. (High
  confidence — matches known format.)
- Materials are split into typed arrays under `materials` and referenced by
  `segment.material_id`; effects/transitions attach via `extra_material_refs`.
- `canvas_config.width/height` holds resolution; `fps` at top level.
- Text content lives in `materials.texts[].content` (may be a JSON-encoded
  rich-text blob — verify).
- Draft folder also contains sidecar files (`draft_meta_info.json`,
  `draft.json`, cover, etc.). Writer must produce the MINIMUM set CapCut needs to
  open the project; determine that set from a real draft. **Unverified.**
- Filename may be `draft_content.json` (newer) or `draft_info.json` (older) —
  `locator.detect_draft_format` handles both.

Reader/writer implementers: update this section with confirmed facts once a real
draft is inspected, and record findings in `docs/schema-notes.md` (Phase 0).
