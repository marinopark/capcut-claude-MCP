"""Generic MCP JSON-RPC 2.0 stdio server (CapCut-agnostic).

This module implements a minimal Model Context Protocol server over a
newline-delimited JSON-RPC 2.0 transport on stdin/stdout, with zero external
dependencies (Python 3.11+ standard library only).

Design constraints (see docs/design.md section 2):
- Transport is newline-delimited JSON: one JSON-RPC message per input line.
  This is NOT LSP-style Content-Length framing.
- stdout carries ONLY JSON-RPC response lines (one per line, each flushed).
  All logs/diagnostics go to stderr via ``log(*args)``.
- This module knows NOTHING about CapCut. It exposes a tiny tool registry so a
  concrete server (e.g. server.py) can register tools with hand-written JSON
  Schema and plain handler callables.

Implemented JSON-RPC methods:
- ``initialize``               -> handshake result (echoes client protocolVersion)
- ``notifications/initialized``-> notification, no response
- ``notifications/*``          -> any notification, no response
- ``tools/list``               -> {"tools": [{name, description, inputSchema}, ...]}
- ``tools/call``               -> {"content": [{"type": "text", "text": ...}]}
                                  (handler exceptions -> isError result, NOT a
                                   JSON-RPC error)
- ``ping``                     -> {}
- unknown method               -> JSON-RPC error -32601 (Method not found)

Requests (messages with an ``id``) receive a response. Notifications (messages
without an ``id``) never receive a response.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: str = "2024-11-05"  # fallback when the client omits it
SERVER_NAME: str = "capcut-mcp"
SERVER_VERSION: str = "0.1.0"

# JSON-RPC error codes we use.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

# Byte-order mark (U+FEFF). Some clients prepend it to the first UTF-8 line;
# ``str.strip()`` does not remove it, so we strip it explicitly before parsing.
_BOM = "﻿"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@dataclass
class ToolDef:
    """A single registered MCP tool.

    Attributes:
        name: Unique tool name (used by ``tools/call``).
        description: Human-readable description shown by ``tools/list``.
        input_schema: A hand-written JSON Schema dict describing the arguments.
        handler: Callable receiving the arguments dict and returning a
            JSON-serializable result object.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], object]


# Module-level registry mapping tool name -> ToolDef.
_REGISTRY: dict[str, ToolDef] = {}


def register_tool(
    name: str,
    description: str,
    input_schema: dict,
    handler: Callable[[dict], object],
) -> None:
    """Register a tool with an explicit handler (non-decorator path)."""
    _REGISTRY[name] = ToolDef(
        name=name,
        description=description,
        input_schema=input_schema,
        handler=handler,
    )


def tool(
    name: str,
    description: str,
    input_schema: dict,
) -> Callable[[Callable[[dict], object]], Callable[[dict], object]]:
    """Decorator that registers the decorated function as a tool.

    The decorated function must accept a single ``args: dict`` argument and
    return a JSON-serializable object. The original function is returned
    unchanged so it remains directly callable/testable.
    """

    def _decorator(func: Callable[[dict], object]) -> Callable[[dict], object]:
        register_tool(name, description, input_schema, func)
        return func

    return _decorator


def get_tools() -> list[ToolDef]:
    """Return a snapshot list of all registered tools."""
    return list(_REGISTRY.values())


def clear_registry() -> None:
    """Empty the tool registry. Test helper."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Logging (stderr only)
# ---------------------------------------------------------------------------

def log(*args) -> None:
    """Write a diagnostic line to stderr (never stdout).

    stdout is reserved exclusively for JSON-RPC response lines, so all logs,
    warnings, and debug output must go here.
    """
    try:
        message = " ".join(str(a) for a in args)
        print(message, file=sys.stderr, flush=True)
    except Exception:
        # Logging must never raise into the main loop.
        pass


# ---------------------------------------------------------------------------
# JSON-RPC message helpers
# ---------------------------------------------------------------------------

def _make_response(id_, result) -> dict:
    """Build a JSON-RPC success response object."""
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _make_error(id_, code: int, message: str) -> dict:
    """Build a JSON-RPC error response object."""
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _write(stdout, obj: dict) -> None:
    """Serialize ``obj`` to a single JSON line and flush to ``stdout``."""
    line = json.dumps(obj, ensure_ascii=False)
    stdout.write(line + "\n")
    stdout.flush()


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------

def _handle_initialize(params: dict) -> dict:
    """Return the MCP initialize result, echoing the client protocolVersion."""
    client_version = None
    if isinstance(params, dict):
        client_version = params.get("protocolVersion")
    protocol_version = client_version or PROTOCOL_VERSION
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list() -> dict:
    """Return the tools/list result payload."""
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in get_tools()
        ]
    }


def _handle_tools_call(params: dict) -> dict:
    """Dispatch a tools/call request and wrap the result as MCP content.

    On success returns ``{"content": [{"type": "text", "text": <json>}]}``.
    On unknown tool name or handler exception returns the same content shape
    with ``"isError": true`` (a RESULT, not a JSON-RPC error).
    """
    if not isinstance(params, dict):
        params = {}
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    tool_def = _REGISTRY.get(name)
    if tool_def is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name!r}"}],
            "isError": True,
        }

    try:
        result = tool_def.handler(arguments)
        text = json.dumps(result, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}]}
    except Exception as exc:  # noqa: BLE001 - surface any handler failure as isError
        return {
            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            "isError": True,
        }


# ---------------------------------------------------------------------------
# Dispatch for a single parsed message
# ---------------------------------------------------------------------------

def _dispatch(message: dict, stdout) -> None:
    """Handle one parsed JSON-RPC message, writing a response if warranted."""
    is_request = "id" in message
    id_ = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    # Notifications (no id) never get a response.
    if not is_request:
        # Nothing to emit; we simply acknowledge internally.
        return

    if not isinstance(method, str):
        _write(stdout, _make_error(id_, _INVALID_REQUEST, "Invalid Request"))
        return

    if method == "initialize":
        _write(stdout, _make_response(id_, _handle_initialize(params)))
    elif method == "tools/list":
        _write(stdout, _make_response(id_, _handle_tools_list()))
    elif method == "tools/call":
        _write(stdout, _make_response(id_, _handle_tools_call(params)))
    elif method == "ping":
        _write(stdout, _make_response(id_, {}))
    elif method.startswith("notifications/"):
        # A notification method sent with an id is unusual; treat as no-op
        # success to be lenient (spec: notifications carry no id).
        _write(stdout, _make_response(id_, {}))
    else:
        _write(stdout, _make_error(id_, _METHOD_NOT_FOUND, "Method not found"))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def serve(stdin=None, stdout=None) -> None:
    """Run the blocking JSON-RPC stdio loop until EOF, then return cleanly.

    Reads newline-delimited JSON-RPC messages from ``stdin`` and writes
    single-line JSON responses to ``stdout`` (each flushed). Blank lines are
    skipped. Unparseable lines are logged to stderr and skipped (the loop never
    crashes on bad input). EOF ends the loop.
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout

    # JSON-RPC/MCP requires UTF-8 on the wire. On a pipe, ``sys.stdout`` uses the
    # OS locale codec (e.g. cp949 on Korean Windows), which corrupts non-ASCII
    # payloads or raises UnicodeEncodeError. Force both streams to UTF-8 and use
    # ``newline=""`` so Windows text mode does not translate ``\n`` -> ``\r\n``
    # (frames stay exactly newline-delimited). StringIO objects passed by tests
    # do not support ``reconfigure`` — guard so those paths keep working.
    for _stream in (stdin, stdout):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if callable(_reconfigure):
            try:
                _reconfigure(encoding="utf-8", newline="")
            except (ValueError, OSError, TypeError):
                # Stream already detached/unsupported; leave it as-is.
                pass

    first_line = True
    for raw_line in stdin:
        line = raw_line.strip()
        # A UTF-8 BOM (U+FEFF) may prefix the very first line; ``strip()`` does
        # not remove it, which would break the ``initialize`` handshake.
        if first_line:
            line = line.lstrip(_BOM)
            first_line = False
        if not line:
            continue
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            log("protocol: skipping unparseable line:", repr(line), "-", exc)
            continue

        if not isinstance(message, dict):
            log("protocol: skipping non-object message:", repr(message))
            continue

        try:
            _dispatch(message, stdout)
        except Exception as exc:  # noqa: BLE001 - loop must survive dispatch errors
            log("protocol: dispatch error:", type(exc).__name__, exc)
            # If it was a request we can still try to inform the client.
            if isinstance(message, dict) and "id" in message:
                try:
                    _write(
                        stdout,
                        _make_error(message.get("id"), _INTERNAL_ERROR, "Internal error"),
                    )
                except Exception:
                    pass


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    serve()
