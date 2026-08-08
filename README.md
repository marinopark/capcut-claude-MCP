# CapCut MCP Server

A **zero-dependency** [Model Context Protocol](https://modelcontextprotocol.io)
server that lets Claude (or any MCP client) **read, analyze, and generate**
CapCut projects on your machine — no CapCut export API, no cloud, no packages to
install.

CapCut has no official editing API. This server works entirely against CapCut's
local *draft* folders (`draft_content.json` / `draft_info.json`): it reads them
to answer questions about your timeline, and writes brand-new draft folders you
can open and finish inside CapCut. The deliverable is an **editable project**,
not a rendered video — rendering/export is out of scope.

## Requirements

- **Python 3.11+** — that's it. **Nothing to install, no `pip`, no MCP SDK.**
  The whole server is Python standard library only. Run it with
  `python src/capcut_mcp/server.py`.
- **CapCut (international edition)** installed locally, with its default drafts
  directory:
  - Windows: `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`
  - macOS: `~/Movies/CapCut/User Data/Projects/com.lveditor.draft`
- **ffprobe** (optional, part of FFmpeg) — only used to auto-detect video/audio
  duration when you add media without specifying `duration_sec`. If ffprobe is
  not on your `PATH`, just pass `duration_sec` explicitly.

## Installation

Add the server to Claude Code:

```bash
claude mcp add capcut -- python src/capcut_mcp/server.py
```

Or rely on the bundled `.mcp.json` (Claude Code auto-discovers it in the project
root):

```json
{
  "mcpServers": {
    "capcut": {
      "command": "python",
      "args": ["src/capcut_mcp/server.py"]
    }
  }
}
```

As a Claude Code **plugin**, the `.claude-plugin/plugin.json` manifest describes
the package (`name`, `version`, `description`).

Verify the connection, then run the `doctor` tool first — it reports whether the
drafts directory was found, whether CapCut is running, the detected draft
format, ffprobe availability, and read/write permissions.

## Tools

All times in tool **inputs and outputs are in SECONDS** (floats). Microseconds
are used only internally.

### Read / analyze

| Tool | Input | Returns |
|---|---|---|
| `list_projects` | — | `[{name, modified_at, duration_sec, resolution, fps}]` |
| `analyze_project` | `name` | Track summary, clip count, total duration, media list (path + exists), effect/transition counts, has-captions flag |
| `get_timeline` | `name`, `track_type?` | Compressed rows: `{track_index, track_type, segment_id, start_sec, end_sec, source_name, speed}` |
| `get_captions` | `name` | `[{start_sec, end_sec, text}]` |
| `get_segment_detail` | `name`, `segment_id` | Full detail for one clip: keyframes, effect params, volume, transform, text style |

### Diagnostics

| Tool | Input | Returns |
|---|---|---|
| `doctor` | — | Drafts path detection, CapCut-running check, draft format, ffprobe availability, read/write permissions, draft count |

### Write / build

Building a project is a two-step flow: create an in-memory project, add media/
text to it, then save it to disk.

| Tool | Input |
|---|---|
| `create_project` | `name`, `width`, `height`, `fps?` (default 30) |
| `add_video` | `project`, `path`, `start_sec`, `duration_sec?`, `track?`, `speed?`, `volume?` |
| `add_audio` | `project`, `path`, `start_sec`, `duration_sec?`, `volume?`, `fade_in?`, `fade_out?` |
| `add_text` | `project`, `text`, `start_sec`, `duration_sec`, `font?`, `size?`, `color?` (`#RRGGBB`), `position?` (`[x, y]`) |
| `add_subtitles_from_srt` | `project`, `srt_path` |
| `save_draft` | `project`, `open_hint?`, `force?` |

## Safety rules

The write path is deliberately conservative:

1. **Process check before write.** `save_draft` refuses to write while CapCut is
   running (to avoid corrupting an open project) unless you pass `force=true`.
2. **New folders only (v1).** Writes always create a *new* draft folder. No tool
   overwrites or modifies an existing draft, so your current projects are never
   touched.
3. **Read retries.** If a draft's JSON fails to parse (CapCut may be mid-save),
   reads retry 3× at 0.5 s intervals.
4. **Atomic writes.** A new draft is fully assembled in a temp directory and
   then atomically moved into the drafts folder — you never see a half-written
   draft.
5. **Path validation.** Media paths are normalized to absolute and checked for
   existence before saving; missing files are surfaced as warnings, not silently
   dropped.

After `save_draft`, **restart CapCut** for the new project to appear in its
project list.

## Known constraints & risks

- **Unofficial format.** CapCut's draft format is reverse-engineered and
  undocumented. A CapCut update may change the schema and break reading or
  writing. Only two files (`engine/draft_reader.py`, `engine/draft_writer.py`)
  encode that schema, so fixes are localized — but breakage is possible.
- **International CapCut only.** The Chinese **JianYing 6.x+** editor encrypts
  its drafts and is **not supported**. This server targets the international
  CapCut edition.
- **No export automation.** The server generates an editable draft; it does not
  render or export finished video. Do that in CapCut.
- **Draft filename varies.** Depending on CapCut version the draft file may be
  `draft_content.json` (newer) or `draft_info.json` (older). The locator detects
  both.
- **ffprobe needed for auto-duration.** Without ffprobe on `PATH`, `add_video` /
  `add_audio` cannot auto-detect media length — pass `duration_sec` explicitly.

## Architecture

```
protocol.py    generic JSON-RPC/MCP stdio server (CapCut-agnostic, reusable)
server.py      declares MCP tools + JSON Schemas, converts sec<->us, dispatches
services/      analyzer.py (read) + builder.py (write) — operate on models only
engine/        models.py (domain), locator.py (paths),
               draft_reader.py + draft_writer.py (only files that know CapCut JSON)
```

- stdout carries **only** JSON-RPC; all logs/diagnostics go to stderr.
- Internal/domain/engine code uses **microseconds (int)**; tool I/O uses
  **seconds (float)**; conversion happens only at the `server.py` boundary.
