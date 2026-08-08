"""Path & environment detection for CapCut drafts.

Standard library only. This module knows the on-disk *layout* (folder paths,
file names, external tools) but NOT the CapCut JSON *schema* — that knowledge
lives exclusively in ``draft_reader.py`` / ``draft_writer.py``.

Every environment-probing function is defensive: absence of CapCut, ffprobe, or
a supported OS yields ``None`` / ``False`` rather than raising.

Python 3.11+.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The CapCut international draft package folder name (last path component).
DRAFT_PACKAGE_NAME = "com.lveditor.draft"

#: Candidate draft JSON filenames, newest format first.
DRAFT_CONTENT_FILENAMES = ("draft_content.json", "draft_info.json")


# ---------------------------------------------------------------------------
# Drafts directory discovery
# ---------------------------------------------------------------------------


def candidate_drafts_dirs() -> list[Path]:
    """All plausible drafts locations for this OS, regardless of existence.

    Used by ``doctor`` reporting so the user can see where we looked even when
    nothing was found.
    """
    system = platform.system()
    candidates: list[Path] = []

    if system == "Windows":
        # %LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(
                Path(local) / "CapCut" / "User Data" / "Projects" / DRAFT_PACKAGE_NAME
            )
        # Some builds ship as "CapCut Drafts" or under APPDATA; include a fallback.
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(
                Path(appdata) / "CapCut" / "User Data" / "Projects" / DRAFT_PACKAGE_NAME
            )
    elif system == "Darwin":
        # ~/Movies/CapCut/User Data/Projects/com.lveditor.draft
        home = Path.home()
        candidates.append(
            home / "Movies" / "CapCut" / "User Data" / "Projects" / DRAFT_PACKAGE_NAME
        )
    # Linux / unknown: CapCut has no native build; leave empty.

    return candidates


def find_drafts_dir() -> Path | None:
    """Locate the CapCut drafts directory for the current OS.

    Returns the first *existing* candidate path, else ``None``.
    """
    for candidate in candidate_drafts_dirs():
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _looks_like_draft_folder(folder: Path) -> bool:
    """True if ``folder`` contains a recognized draft JSON file."""
    for name in DRAFT_CONTENT_FILENAMES:
        try:
            if (folder / name).is_file():
                return True
        except OSError:
            return False
    return False


def list_draft_folders(drafts_dir: Path) -> list[Path]:
    """Immediate subdirectories of ``drafts_dir`` that look like drafts.

    Sorted by modification time, most-recent first. Returns an empty list if
    ``drafts_dir`` does not exist or is unreadable.
    """
    try:
        entries = [p for p in drafts_dir.iterdir() if p.is_dir()]
    except OSError:
        return []

    drafts = [p for p in entries if _looks_like_draft_folder(p)]

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    drafts.sort(key=_mtime, reverse=True)
    return drafts


# ---------------------------------------------------------------------------
# Draft format detection
# ---------------------------------------------------------------------------


def detect_draft_format(draft_dir: Path) -> str:
    """Return a format id for a draft folder.

    ``"draft_content"`` if ``draft_content.json`` is present, ``"draft_info"``
    if ``draft_info.json`` is present, else ``"unknown"``.
    """
    try:
        if (draft_dir / "draft_content.json").is_file():
            return "draft_content"
        if (draft_dir / "draft_info.json").is_file():
            return "draft_info"
    except OSError:
        pass
    return "unknown"


def draft_content_path(draft_dir: Path) -> Path | None:
    """Resolve the actual draft JSON file inside a folder, or ``None``."""
    for name in DRAFT_CONTENT_FILENAMES:
        candidate = draft_dir / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# External tool / process detection
# ---------------------------------------------------------------------------


def is_ffprobe_available() -> bool:
    """True if ``ffprobe`` resolves on PATH."""
    return shutil.which("ffprobe") is not None


def is_capcut_running() -> bool:
    """Best-effort check whether CapCut is currently running.

    Windows: parse ``tasklist`` for ``CapCut.exe``.
    macOS: ``pgrep -x CapCut``.
    On any subprocess failure this returns ``False`` (fail-open); never raises.
    """
    system = platform.system()
    try:
        if system == "Windows":
            # tasklist lists running image names; grep for CapCut.exe.
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = (proc.stdout or "").lower()
            return "capcut.exe" in out
        if system == "Darwin":
            proc = subprocess.run(
                ["pgrep", "-x", "CapCut"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            # pgrep exits 0 and prints a PID when a match is found.
            return proc.returncode == 0 and bool((proc.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        return False
    return False


# ---------------------------------------------------------------------------
# Media probing (ffprobe) — all durations in MICROSECONDS
# ---------------------------------------------------------------------------


def _run_ffprobe_json(path: Path, *args: str) -> dict | None:
    """Invoke ffprobe with ``-of json`` and return parsed output, or ``None``.

    Never raises: returns ``None`` if ffprobe is missing or the call fails.
    """
    if not is_ffprobe_available():
        return None
    cmd = ["ffprobe", "-v", "quiet", "-of", "json", *args, str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def probe_media_duration(path: Path) -> int | None:
    """Intrinsic media duration in MICROSECONDS via ffprobe, or ``None``.

    ``None`` when ffprobe is unavailable or the duration cannot be determined.
    """
    data = _run_ffprobe_json(
        path,
        "-show_entries",
        "format=duration",
    )
    if not data:
        return None
    fmt = data.get("format") or {}
    raw = fmt.get("duration")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return int(round(seconds * 1_000_000))


def probe_media_dimensions(path: Path) -> tuple[int, int] | None:
    """(width, height) in pixels of the first video stream, or ``None``."""
    data = _run_ffprobe_json(
        path,
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
    )
    if not data:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    stream = streams[0]
    width = stream.get("width")
    height = stream.get("height")
    if width is None or height is None:
        return None
    try:
        return (int(width), int(height))
    except (TypeError, ValueError):
        return None
