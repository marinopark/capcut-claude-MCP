# CapCut `draft_content.json` — Schema Notes (Phase 0 recon)

Verified findings from real CapCut (international) drafts on this machine, not
assumptions. This supersedes the "ASSUMPTIONS" block in `design.md` §9 for every
field listed here. Schema knowledge is confined to `engine/draft_reader.py` and
`engine/draft_writer.py`; this file is the human-readable record of what those two
modules encode.

## Source material

Drafts dir (Windows):
`%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`

Four folders were inspected (recon scripts `_inspect{,2,3,4}.py` at repo root):

| draft | videos | audios | texts | effects | transitions | notes |
|---|---|---|---|---|---|---|
| ep01 (인물화) | 104 | 6 | 142 | 284 | 5 | keyframes, transitions, styled text |
| ep02 (상어 오마주) | 64 | 86 | 262 | 522 | 6 | large, denoise/vocal materials |
| ep03 (쪽머리) | 6 | 12 | 0 | 0 | 0 | small — good round-trip fixture |
| "가위 안전 수칙" | — | — | — | — | — | **ENCRYPTED** (see below) |

## Top level

```jsonc
{
  "canvas_config": { "width": 1920, "height": 1080, "ratio": "original" },
  "fps": 30.0,
  "duration": 2107200000,        // microseconds — CONFIRMED
  "materials": { /* typed arrays, see below */ },
  "tracks": [ /* see below */ ]
}
```

- **Time unit is microseconds (int)** everywhere (`duration`, `target_timerange`,
  `source_timerange`, keyframe `time_offset`). Confirmed.
- Resolution in `canvas_config.width/height`; `fps` at top level.

## `materials` — typed arrays

`materials` is an object whose keys are plural bucket names, each an array of
material dicts referenced elsewhere by `id`. Buckets actually observed:

`videos, audios, texts, effects, canvases, transitions, audio_fades, beats,
material_animations, placeholder_infos, speeds, hsl, drafts, sound_channel_mappings,
material_colors, smart_crops, loudnesses, vocal_separations, vocal_beautifys,
realtime_denoises`

Only these are mapped to the domain model; the rest are ignored on read and not
emitted on write (writer produces the minimum CapCut needs to open the project):

- **videos** — keys: `id, type, duration, path, media_path, width, height,
  material_name, has_audio, ...`. Images are pooled here too.
- **audios** — `id, type, name, duration, path, ...`
- **texts** — `id, type, content, name, ...`. **`content` is a JSON STRING**, not
  a nested object:
  ```jsonc
  "{\"text\":\"안녕하세요\",\"styles\":[{\"fill\":{\"content\":{\"render_type\":\"solid\",\"solid\":{\"color\":[0,0,0]}}},\"font\":{\"path\":\"...Pretendard-Regular.otf\",\"id\":\"\"},\"size\":8,\"range\":[0,5]}]}"
  ```
  Reader unwraps `.text` for display and preserves the whole string as
  `Material.raw_content`; writer re-emits `raw_content` verbatim with the display
  text/`range` refreshed, so CapCut still renders styled text. A bare plain string
  in `content` does NOT render (Bug #6). Colours here are `[r,g,b]` in 0..1.
- **effects / transitions** — `id, name, effect_id, resource_id, path, type, ...`.
  Referenced from segments via `extra_material_refs` (a flat list of material ids).

## `tracks`

```jsonc
{ "id": "...", "type": "video|audio|text|effect|sticker|filter",
  "attribute": 0, "flag": 0, "name": "...", "segments": [ ... ] }
```

- `type` string maps directly to `TrackType`.
- **Mute is the integer `attribute` bitfield**, not a `mute` boolean (Bug #3). The
  `mute` key is absent on real tracks.

### Segments

```jsonc
{
  "id": "...",
  "material_id": "...",                          // -> materials pool
  "target_timerange": { "start": <us>, "duration": <us> },   // timeline position
  "source_timerange": { "start": <us>, "duration": <us> },   // in-media trim
  "speed": 1.0,
  "volume": 1.0,
  "extra_material_refs": ["<effect/transition/... id>", ...],
  "clip": { "transform": {"x","y"}, "scale": {"x","y"}, "rotation", "alpha" },
  "common_keyframes": [ ... ]                     // see below
}
```

- **Keyframes = `common_keyframes`, a LIST grouped by property** (Bug #7), not a
  dict:
  ```jsonc
  [ { "property_type": "KFTypePositionX",
      "keyframe_list": [ { "time_offset": <us>, "values": [<float>],
                           "curveType": "Line",
                           "left_control": {"x","y"}, "right_control": {"x","y"} } ] } ]
  ```
  `property_type` uses `KFType*` names (`KFTypeVolume`, `KFTypePositionX`,
  `KFTypeScaleX`, `KFTypeAlpha`, `KFTypeRotation`, …).

## Draft folder contents

A real folder carries ~25 files/dirs (`Resources/`, `Timelines/`, `draft_cover.jpg`,
`draft_meta_info.json`, `draft_virtual_store.json`, `.bak` copies, `crypto_key_store.dat`,
etc.). The **reader keys off `draft_content.json` only**. The **writer emits**:
`draft_content.json` + a full `draft_meta_info.json`.

### `draft_meta_info.json` is required to open (learned from open-in-CapCut)

CapCut's project browser validates/opens a project through `draft_meta_info.json`,
not just `draft_content.json`. A too-minimal meta made CapCut reject the project
with a **"비정상적인 경로 / abnormal path"** error. Three fixes were needed:

- **All paths use FORWARD slashes** (`C:/Users/...`). `os.path.abspath` yields
  back-slashes on Windows; a back-slash media path in `draft_content.json` (or
  `draft_meta_info.json`) triggers the abnormal-path rejection (Bug #24).
- **`draft_materials` media registry must be populated** (Bug #25). It is a list
  of groups keyed by `type`; group `type: 0` holds every A/V media file:
  ```jsonc
  { "type": 0, "value": [ {
      "file_Path": "C:/.../clip.mp4",   // forward slashes
      "metetype": "video",              // video | photo | music
      "duration": <us>, "width", "height",
      "id": "<uuid>", "extra_info": "clip.mp4",
      "roughcut_time_range": {"start": 0, "duration": <us>},
      "sub_time_range": {"start": -1, "duration": -1},
      "type": 0, "item_source": 1, "md5": "", ... } ] }
  ```
  Empty group slots `type ∈ {1,2,3,6,7,8,18}` are emitted for fidelity.
- **`draft_fold_path` / `draft_root_path` must record the FINAL folder**, not the
  temp staging dir the files are assembled in before the atomic move (Bug #26).

Other meta fields (`draft_id`, `draft_name`, `draft_cover`, `tm_duration`,
`tm_draft_create/modified` in µs, and many `draft_is_*` boolean flags) mirror the
real schema; see `draft_writer._write_sidecars`.

### Device provenance check — THE actual "abnormal path" cause (confirmed)

Fixes #24–#26 alone did NOT clear the "비정상적인 경로 / abnormal path" rejection.
The confirmed root cause (draft opens in CapCut after this fix) is the
**`platform` provenance block** in `draft_content.json`:

- CapCut validates `platform.device_id` — this machine's CapCut identity (e.g.
  `b423a02bc012c1eff680e8291a72df2d`). A draft with an empty or foreign
  `device_id` is treated as *moved from another machine* and rejected with the
  abnormal-path error (Bug #28). This is CapCut's draft-migration detection.
- Also required: a plausible `platform.app_version` (`"8.7.0"`, not an arbitrary
  string), `app_id` (359289), `app_source` (`"cc"`), a `last_modified_platform`
  twin block, and a non-empty `new_version` (`"175.0.0"`).

The writer does not hardcode these: `_harvest_environment()` scans sibling
drafts in the drafts dir for a readable `draft_content.json` and copies its
`platform` block + `new_version` (fallback constants only if no sibling
exists). This stamps the new draft with the machine's real identity.

Additional fixes needed to open:

- **Video material `width`/`height` must be non-zero** (Bug #27). Without
  ffprobe, dimensions fall back to the project canvas size. CapCut heals many
  missing fields when opening a draft (it rewrote our 9-key content to its full
  36-key schema and generated speed/canvas/sound_channel sub-materials), but it
  does NOT heal 0×0 dimensions or the provenance block.
- **`draft_virtual_store.json`** is written alongside the meta: a type-0 store
  entry plus one type-1 `{child_id: <material id>, parent_id: ""}` pair per
  material.

### Minimum file set to open in CapCut (confirmed working)

`draft_content.json` + `draft_meta_info.json` (full schema, populated
`draft_materials`) + `draft_virtual_store.json`. CapCut self-generates the rest
(`Timelines/`, `draft_settings`, `.bak`, covers, `root_meta_info.json` entry) on
first open.

## Encryption / format variants

- Some drafts store `draft_content.json` **encrypted** — the file does not begin
  with `{` (observed first bytes e.g. `b"49d6sCF8BeAj"`). Reader detects this
  (first non-whitespace byte ≠ `{`) and raises `ValueError`; these cannot be read
  (Bug #10). Chinese JianYing 6.x+ is out of scope for the same reason.
- Filename may be `draft_content.json` (newer) or `draft_info.json` (older) —
  `locator.detect_draft_format` handles both.
