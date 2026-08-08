from __future__ import annotations

import json
import os
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    try:
        message = json.loads(raw)
    except Exception:
        continue
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        continue
    if method == "initialize":
        requested = str((message.get("params") or {}).get("protocolVersion") or "")
        negotiated = os.environ.get("FAKE_MCP_PROTOCOL", requested)
        capabilities = {} if os.environ.get("FAKE_MCP_NO_TOOLS") == "1" else {"tools": {"listChanged": True}}
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": negotiated, "capabilities": capabilities, "serverInfo": {"name": "fake-mcp", "title": "Fake MCP", "version": "1.0.0"}}})
    elif method == "tools/list":
        cursor = str((message.get("params") or {}).get("cursor") or "")
        if not cursor:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "read_demo", "title": "Read Demo", "description": "Read demo data", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, "outputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}, "annotations": {"readOnlyHint": True}}], "nextCursor": "page-2"}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "write_demo", "description": "Write demo data", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"destructiveHint": True}}]}})
    elif method == "tools/call":
        params = message.get("params") or {}
        send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"called:{params.get('name')}:{json.dumps(params.get('arguments') or {}, sort_keys=True)}"}], "isError": False}})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown method"}})
