"""End-to-end and unit tests for the generic MCP protocol server.

Run: python -m unittest tests.test_protocol -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

# Make ``capcut_mcp`` importable as a package (an empty
# src/capcut_mcp/__init__.py already exists).
sys.path.insert(0, "src")

from capcut_mcp import protocol  # noqa: E402


def _drive(lines: list[dict]) -> list[dict]:
    """Feed JSON-RPC messages through ``serve`` in-process and parse replies.

    Returns the list of parsed JSON-RPC response objects written to stdout.
    """
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in lines))
    stdout = io.StringIO()
    protocol.serve(stdin=stdin, stdout=stdout)
    out = stdout.getvalue().strip()
    if not out:
        return []
    return [json.loads(line) for line in out.splitlines()]


class ProtocolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        protocol.clear_registry()

        @protocol.tool(
            "echo",
            "Echo back the provided arguments.",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        )
        def _echo(args: dict) -> dict:
            return {"echoed": args}

        @protocol.tool(
            "boom",
            "A tool that always raises.",
            {"type": "object", "properties": {}},
        )
        def _boom(args: dict) -> dict:
            raise RuntimeError("kaboom")

    def tearDown(self) -> None:
        protocol.clear_registry()


class TestRegistry(ProtocolTestBase):
    def test_tool_registered(self):
        names = {t.name for t in protocol.get_tools()}
        self.assertIn("echo", names)
        self.assertIn("boom", names)

    def test_register_tool_non_decorator(self):
        protocol.register_tool(
            "plain", "desc", {"type": "object"}, lambda a: {"ok": True}
        )
        names = {t.name for t in protocol.get_tools()}
        self.assertIn("plain", names)

    def test_clear_registry(self):
        protocol.clear_registry()
        self.assertEqual(protocol.get_tools(), [])


class TestInProcess(ProtocolTestBase):
    def test_initialize_echoes_protocol_version(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            ]
        )
        self.assertEqual(len(responses), 1)
        result = responses[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"]["name"], protocol.SERVER_NAME)
        self.assertEqual(result["serverInfo"]["version"], protocol.SERVER_VERSION)

    def test_initialize_falls_back_when_version_omitted(self):
        responses = _drive(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        )
        self.assertEqual(
            responses[0]["result"]["protocolVersion"], protocol.PROTOCOL_VERSION
        )

    def test_notification_gets_no_response(self):
        responses = _drive(
            [{"jsonrpc": "2.0", "method": "notifications/initialized"}]
        )
        self.assertEqual(responses, [])

    def test_tools_list_contains_echo(self):
        responses = _drive([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        tools = responses[0]["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertIn("echo", names)
        echo = next(t for t in tools if t["name"] == "echo")
        self.assertIn("inputSchema", echo)
        self.assertIn("description", echo)

    def test_tools_call_echo_wraps_content(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"value": "hi"}},
                }
            ]
        )
        result = responses[0]["result"]
        self.assertNotIn("isError", result)
        content = result["content"]
        self.assertEqual(content[0]["type"], "text")
        payload = json.loads(content[0]["text"])
        self.assertEqual(payload, {"echoed": {"value": "hi"}})

    def test_tools_call_missing_arguments_defaults_empty(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "echo"},
                }
            ]
        )
        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(payload, {"echoed": {}})

    def test_tools_call_handler_exception_is_error_result(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "boom", "arguments": {}},
                }
            ]
        )
        result = responses[0]["result"]
        # It is a RESULT (not a JSON-RPC error) with isError true.
        self.assertNotIn("error", responses[0])
        self.assertTrue(result["isError"])
        self.assertIn("kaboom", result["content"][0]["text"])

    def test_tools_call_unknown_tool_is_error_result(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}},
                }
            ]
        )
        result = responses[0]["result"]
        self.assertNotIn("error", responses[0])
        self.assertTrue(result["isError"])

    def test_ping_returns_empty(self):
        responses = _drive([{"jsonrpc": "2.0", "id": 7, "method": "ping"}])
        self.assertEqual(responses[0]["result"], {})

    def test_unknown_method_returns_method_not_found(self):
        responses = _drive([{"jsonrpc": "2.0", "id": 8, "method": "does/not/exist"}])
        error = responses[0]["error"]
        self.assertEqual(error["code"], -32601)
        self.assertNotIn("result", responses[0])

    def test_unparseable_line_skipped_then_continues(self):
        stdin = io.StringIO(
            "this is not json\n"
            + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
            + "\n"
        )
        stdout = io.StringIO()
        protocol.serve(stdin=stdin, stdout=stdout)
        out = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
        # Only one response: the ping. The bad line produced nothing.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 9)
        self.assertEqual(out[0]["result"], {})

    def test_blank_lines_skipped(self):
        stdin = io.StringIO(
            "\n\n" + json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"}) + "\n"
        )
        stdout = io.StringIO()
        protocol.serve(stdin=stdin, stdout=stdout)
        out = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 10)

    def test_full_handshake_sequence(self):
        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"value": "x"}},
                },
            ]
        )
        # initialize + tools/list + tools/call => 3 responses (notification: 0)
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])


_SUBPROCESS_SCRIPT = r"""
import sys
sys.path.insert(0, "src")
from capcut_mcp import protocol

protocol.clear_registry()


@protocol.tool("echo", "Echo tool", {"type": "object", "properties": {}})
def _echo(args):
    return {"echoed": args}


protocol.serve()
"""


class TestSubprocessE2E(unittest.TestCase):
    """Drive the loop as a real subprocess over stdin/stdout pipes."""

    def test_end_to_end_over_pipes(self):
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"value": "hi"}},
            },
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        stdin_data = "".join(json.dumps(m) + "\n" for m in messages)

        proc = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT],
            input=stdin_data,
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=30,
        )
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        responses = [json.loads(l) for l in lines]
        by_id = {r.get("id"): r for r in responses}

        # 4 responses expected (the notification produced none).
        self.assertEqual(sorted(by_id), [1, 2, 3, 4])
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2024-11-05")
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertIn("echo", names)
        payload = json.loads(by_id[3]["result"]["content"][0]["text"])
        self.assertEqual(payload, {"echoed": {"value": "hi"}})
        self.assertEqual(by_id[4]["result"], {})

        # stdout must be pure JSON-RPC: every non-blank line parses as JSON.
        for line in lines:
            json.loads(line)


class TestUtf8AndFraming(ProtocolTestBase):
    """UTF-8 on the wire, newline framing, BOM handling, missing-arg errors."""

    def test_korean_result_serializes_as_valid_utf8_in_process(self):
        # Register a tool returning Korean text and drive it in-process.
        @protocol.tool(
            "korean", "Return Korean text.", {"type": "object", "properties": {}}
        )
        def _korean(args: dict) -> dict:
            return {"text": "안녕하세요"}

        responses = _drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "korean", "arguments": {}},
                }
            ]
        )
        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(payload, {"text": "안녕하세요"})

        # The serialized JSON-RPC line must encode losslessly as UTF-8 bytes and
        # contain the raw (non-escaped) Korean characters (ensure_ascii=False).
        obj = protocol._make_response(1, {"content": [{"text": "안녕하세요"}]})
        line = json.dumps(obj, ensure_ascii=False)
        raw = line.encode("utf-8")
        self.assertEqual(raw.decode("utf-8"), line)
        self.assertIn("안녕하세요", raw.decode("utf-8"))

    def test_korean_roundtrips_over_real_subprocess_pipe(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        script = (
            "import sys\n"
            "sys.path.insert(0, 'src')\n"
            "from capcut_mcp import protocol\n"
            "protocol.clear_registry()\n"
            "@protocol.tool('korean', 'k', {'type':'object','properties':{}})\n"
            "def _k(args):\n"
            "    return {'text': '안녕하세요'}\n"
            "protocol.serve()\n"
        )
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "korean", "arguments": {}},
            }
        ]
        stdin_data = "".join(json.dumps(m) + "\n" for m in messages)
        # Force a non-UTF-8 child locale so we exercise the reconfigure() fix:
        # without it, ensure_ascii=False + cp949/ascii stdout would corrupt or
        # raise. We read raw BYTES (text=False) and decode as UTF-8 ourselves.
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "ascii"
        proc = subprocess.run(
            [sys.executable, "-c", script],
            input=stdin_data.encode("utf-8"),
            capture_output=True,
            cwd=repo_root,
            env=env,
            timeout=30,
        )
        stdout_bytes = proc.stdout
        # Bytes must decode cleanly as UTF-8 (not the OS locale codec).
        decoded = stdout_bytes.decode("utf-8")
        # No carriage returns: frames are exactly \n-delimited (bug #15).
        self.assertNotIn(b"\r", stdout_bytes)
        lines = [l for l in decoded.strip().splitlines() if l.strip()]
        responses = [json.loads(l) for l in lines]
        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(payload, {"text": "안녕하세요"})

    def test_no_carriage_return_in_emitted_frames(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        )
        stdout = io.StringIO()
        protocol.serve(stdin=stdin, stdout=stdout)
        self.assertNotIn("\r", stdout.getvalue())

    def test_bom_prefixed_initialize_is_parsed(self):
        # A leading UTF-8 BOM on the first line must not break the handshake.
        stdin = io.StringIO(
            "﻿"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        protocol.serve(stdin=stdin, stdout=stdout)
        out = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[0]["result"]["protocolVersion"], "2024-11-05")


class TestServerMissingArg(unittest.TestCase):
    """server.py surfaces missing required args as a clear, named error."""

    def test_missing_required_arg_returns_named_error(self):
        try:
            from capcut_mcp import server  # noqa: F401
        except Exception as exc:  # pragma: no cover - sibling module mid-edit
            self.skipTest(f"server.py not importable: {exc}")

        stdin = io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    # analyze_project requires 'name'; omit it.
                    "params": {"name": "analyze_project", "arguments": {}},
                }
            )
            + "\n"
        )
        stdout = io.StringIO()
        protocol.serve(stdin=stdin, stdout=stdout)
        out = [json.loads(l) for l in stdout.getvalue().strip().splitlines()]
        result = out[0]["result"]
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        # Must name the missing argument, and must NOT be a bare KeyError.
        self.assertIn("name", text)
        self.assertIn("Missing required argument", text)
        self.assertNotIn("KeyError", text)


if __name__ == "__main__":
    unittest.main()
