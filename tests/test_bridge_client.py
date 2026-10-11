from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
import pytest

from agent_orchestrator.bridge_client import BridgeClient, BridgeError


@pytest.fixture
def mini_mcp_server(tmp_path: Path) -> Path:
    """Creates a temporary python script acting as an MCP server for test scenarios."""
    server_code = """
import sys
import json
import time

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line)
    except Exception:
        continue

    method = req.get("method")
    req_id = req.get("id")

    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "test-bridge-server", "version": "1.0.0"}
            }
        }
        sys.stdout.write(json.dumps(resp) + "\\n")
        sys.stdout.flush()

    elif method == "notifications/initialized":
        # Notification, no response needed
        continue

    elif method == "tools/call":
        params = req.get("params", {})
        tool_name = params.get("name")
        args = params.get("arguments", {})

        if tool_name == "hang":
            # Hang without printing to stdout
            time.sleep(10.0)

        elif tool_name == "delay_then_respond":
            time.sleep(0.4)
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": True, "source": "delayed_1"})}]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        elif tool_name == "flood_stderr":
            # Write 200KB of stderr to test buffer drainer
            sys.stderr.write("E" * 200000 + "\\n")
            sys.stderr.flush()
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": True, "flooded": True})}]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        elif tool_name == "crash":
            sys.stderr.write("CRITICAL ERROR: worker segfault simulated\\n")
            sys.stderr.flush()
            sys.exit(42)

        elif tool_name == "noisy_logs":
            # Print non-JSON lines and blank lines first
            sys.stdout.write("WARNING: log line without json\\n")
            sys.stdout.write("   \\n")
            sys.stdout.write("INFO: another noise line\\n")
            sys.stdout.flush()
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": True, "noise_handled": True})}]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        elif tool_name == "get_result":
            cursor = args.get("cursor", 0)
            if cursor == 0:
                chunk_resp = {
                    "ok": True,
                    "task_id": args.get("task_id"),
                    "result_text": "Chunk 1; ",
                    "has_more": True,
                    "next_cursor": 10,
                }
            else:
                chunk_resp = {
                    "ok": True,
                    "task_id": args.get("task_id"),
                    "result_text": "Chunk 2.",
                    "has_more": False,
                }
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(chunk_resp)}]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        elif tool_name == "wait_task":
            task_id = args.get("task_id")
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({
                                "ok": True,
                                "task_id": task_id,
                                "status": "completed",
                                "result_text": "Done",
                            }),
                        }
                    ]
                },
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        elif tool_name == "plain_text":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "Raw non-JSON textual output"}]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        elif tool_name == "trigger_rpc_error":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": "Internal bridge error simulated"}
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": True, "tool": tool_name})}]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
"""
    script_file = tmp_path / "mini_server.py"
    script_file.write_text(server_code, encoding="utf-8")
    return script_file


def test_bridge_client_stdout_hang_timeout(mini_mcp_server: Path):
    """Verify stdout hanging triggers TimeoutError reliably rather than hanging."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        start = time.time()
        with pytest.raises(TimeoutError):
            client.call_tool("hang", {}, timeout=0.5)
        elapsed = time.time() - start
        assert 0.4 <= elapsed < 2.0
    finally:
        client.close()


def test_bridge_client_stderr_pipe_saturation(mini_mcp_server: Path):
    """Verify child writing 200KB to stderr does not deadlock the process or pipe."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        res = client.call_tool("flood_stderr", {}, timeout=5.0)
        assert res.get("ok") is True
        assert res.get("flooded") is True
        # Verify stderr was drained into buffer
        stderr_tail = client.get_stderr_tail()
        assert len(stderr_tail) > 0
    finally:
        client.close()


def test_bridge_client_process_crash_unexpected_exit(mini_mcp_server: Path):
    """Verify sudden exit of child process raises BridgeError with stderr and exit code."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        with pytest.raises(BridgeError) as exc_info:
            client.call_tool("crash", {}, timeout=3.0)
        err_msg = str(exc_info.value)
        assert "exited unexpectedly" in err_msg or "closed stdout" in err_msg
        assert "42" in err_msg
        assert "worker segfault simulated" in err_msg
    finally:
        client.close()


def test_bridge_client_noisy_non_json_output(mini_mcp_server: Path):
    """Verify non-JSON lines and empty lines on stdout are ignored until valid JSON-RPC arrives."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        res = client.call_tool("noisy_logs", {}, timeout=3.0)
        assert res.get("ok") is True
        assert res.get("noise_handled") is True
    finally:
        client.close()


def test_bridge_client_get_result_pagination(mini_mcp_server: Path):
    """Verify get_result paginates cursors and concatenates full text chunks."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        res = client.get_result("task_paginated")
        assert res.get("ok") is True
        assert res.get("result_text") == "Chunk 1; Chunk 2."
    finally:
        client.close()


def test_bridge_client_wait_task_tool(mini_mcp_server: Path):
    """Verify wait_task tool call succeeds."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        res = client.wait_task("task_123", timeout_sec=2.0)
        assert res.get("ok") is True
        assert res.get("status") == "completed"
    finally:
        client.close()


def test_bridge_client_tool_returns_plain_text(mini_mcp_server: Path):
    """Verify tool returning plain text is parsed gracefully as raw_text without error."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        res = client.call_tool("plain_text", {}, timeout=2.0)
        assert res.get("raw_text") == "Raw non-JSON textual output"
    finally:
        client.close()


def test_bridge_client_rpc_error_handling(mini_mcp_server: Path):
    """Verify JSON-RPC error response raises BridgeError with error message."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        with pytest.raises(BridgeError) as exc_info:
            client.call_tool("trigger_rpc_error", {}, timeout=2.0)
        assert "Internal bridge error simulated" in str(exc_info.value)
    finally:
        client.close()


def test_bridge_client_delayed_response_not_confused_with_next_request(mini_mcp_server: Path):
    """Verify delayed response from a timed-out request is ignored and not returned for subsequent requests."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()

        # 1. First request has a tiny timeout (0.1s), but takes 0.4s on server
        with pytest.raises(TimeoutError):
            client.call_tool("delay_then_respond", {}, timeout=0.1)

        # 2. Wait 0.45s so the delayed response arrives on stdout and enters the client queue
        time.sleep(0.45)

        # 3. Second request is dispatched with normal timeout
        res2 = client.call_tool("normal_tool", {"arg": "val"}, timeout=3.0)

        # 4. Verify request 2 received its own response, NOT the delayed response from request 1!
        assert res2.get("ok") is True
        assert res2.get("tool") == "normal_tool"
        assert "delayed_1" not in res2.get("source", "")
    finally:
        client.close()


def test_bridge_client_repeated_connect_close_no_leaks(mini_mcp_server: Path):
    """Verify repeated connect and close cycles do not leak processes or background threads."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    for i in range(5):
        client.connect()
        assert client.proc is not None
        assert client.proc.poll() is None
        assert client._stdout_thread is not None and client._stdout_thread.is_alive()
        assert client._stderr_thread is not None and client._stderr_thread.is_alive()

        proc_ref = client.proc
        client.close()

        assert client.proc is None
        assert proc_ref.poll() is not None
        assert client._stdout_thread is None
        assert client._stderr_thread is None


def test_bridge_client_reconnect_method(mini_mcp_server: Path):
    """Verify reconnect() cleans up existing process and establishes a fresh working connection."""
    client = BridgeClient(command=[sys.executable, "-u", str(mini_mcp_server)])
    try:
        client.connect()
        old_pid = client.proc.pid
        client.reconnect()
        new_pid = client.proc.pid

        assert new_pid != old_pid
        res = client.call_tool("normal_tool", {}, timeout=2.0)
        assert res.get("ok") is True
    finally:
        client.close()


