from __future__ import annotations

import asyncio
import json
import os
from itertools import count
from pathlib import Path
from typing import Any

from ..agent.tool_registry import AgentTool, ToolRegistry


_REGISTERED_MCP_TOOLS: dict[str, dict[str, Any]] = {}


class McpProtocolError(RuntimeError):
    pass


class McpStdioClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        env: dict[str, str],
        cwd: str | None,
        timeout: int,
    ) -> None:
        self.command = command
        self.args = args
        self.env = env
        self.cwd = cwd
        self.timeout = max(1, timeout)
        self._seq = count(1)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> "McpStdioClient":
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except Exception:
                self._proc.kill()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        while True:
            chunk = await self._proc.stderr.read(1024)
            if not chunk:
                return

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise McpProtocolError("MCP process is not ready")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + body)
        await self._proc.stdin.drain()

    async def _read_message(self) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise McpProtocolError("MCP process is not ready")

        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
            if not line:
                raise McpProtocolError("MCP server closed stdout")
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                break
            if ":" in decoded:
                key, value = decoded.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length") or 0)
        if length <= 0:
            raise McpProtocolError("Missing Content-Length")
        body = await asyncio.wait_for(self._proc.stdout.readexactly(length), timeout=self.timeout)
        parsed = json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise McpProtocolError("Invalid MCP message payload")
        return parsed

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = next(self._seq)
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            message = await self._read_message()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise McpProtocolError(str(message.get("error")))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "personification-skill-loader",
                    "version": "1.0.0",
                },
            },
        )
        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {})
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self.request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        content = result.get("content")
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if text is not None:
                    texts.append(str(text))
                elif item.get("type") == "text" and item.get("content") is not None:
                    texts.append(str(item.get("content")))
            if texts:
                return "\n".join(texts)
        if result.get("structuredContent") is not None:
            return json.dumps(result["structuredContent"], ensure_ascii=False)
        if result.get("content") is not None:
            return json.dumps(result["content"], ensure_ascii=False)
        return json.dumps(result, ensure_ascii=False)


def normalize_mcp_config(
    *,
    skill_dir: Path,
    raw: Any,
    default_timeout: int = 20,
) -> dict[str, Any] | None:
    block = raw if isinstance(raw, dict) else None
    if not block:
        return None
    transport = str(block.get("transport") or "stdio").strip().lower()
    if transport != "stdio":
        return None
    command = str(block.get("command") or "").strip()
    args_raw = block.get("args")
    args = [str(item) for item in args_raw] if isinstance(args_raw, list) else []
    if not command:
        return None
    env_raw = block.get("env")
    env = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {}
    inherit_env = bool(block.get("inherit_env", True))
    cwd_raw = str(block.get("cwd") or "").strip()
    cwd = str((skill_dir / cwd_raw).resolve()) if cwd_raw else str(skill_dir.resolve())
    tool_names_raw = block.get("tools")
    tool_names = [str(item).strip() for item in tool_names_raw] if isinstance(tool_names_raw, list) else []
    single_tool = str(block.get("tool") or "").strip()
    if single_tool:
        tool_names.append(single_tool)
    name_prefix = str(block.get("name_prefix") or "").strip()
    timeout = int(block.get("timeout", default_timeout) or default_timeout)
    return {
        "transport": transport,
        "command": command,
        "args": args,
        "env": env,
        "inherit_env": inherit_env,
        "cwd": cwd,
        "tools": [name for name in tool_names if name],
        "name_prefix": name_prefix,
        "timeout": max(1, timeout),
    }


def register_mcp_endpoint(
    *,
    registered_name: str,
    remote_name: str,
    command: str,
    args: list[str],
    env: dict[str, str],
    cwd: str,
    timeout: int,
) -> None:
    _REGISTERED_MCP_TOOLS[registered_name] = {
        "remote_name": remote_name,
        "command": command,
        "args": list(args),
        "env": dict(env),
        "cwd": cwd,
        "timeout": max(1, int(timeout)),
    }


def get_registered_mcp_endpoint(tool_name: str) -> dict[str, Any] | None:
    config = _REGISTERED_MCP_TOOLS.get(str(tool_name))
    return dict(config) if isinstance(config, dict) else None


async def call_registered_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    config = get_registered_mcp_endpoint(tool_name)
    if config is None:
        raise McpProtocolError(f"Remote MCP tool '{tool_name}' is not registered")
    async with McpStdioClient(
        command=str(config["command"]),
        args=[str(item) for item in config.get("args", [])],
        env={str(k): str(v) for k, v in dict(config.get("env") or {}).items()},
        cwd=str(config.get("cwd") or ""),
        timeout=max(1, int(config.get("timeout", 20) or 20)),
    ) as client:
        return await client.call_tool(
            str(config.get("remote_name") or tool_name),
            arguments,
        )


async def register_mcp_tools(
    *,
    registry: ToolRegistry,
    logger: Any,
    skill_dir: Path,
    base_name: str,
    config: dict[str, Any],
) -> int:
    env = os.environ.copy() if config.get("inherit_env", True) else {}
    env.update(config.get("env") or {})
    command = str(config.get("command") or "").strip()
    args = [str(item) for item in (config.get("args") or [])]
    timeout = max(1, int(config.get("timeout", 20) or 20))
    cwd = str(config.get("cwd") or "").strip() or str(skill_dir.resolve())
    name_prefix = str(config.get("name_prefix") or "").strip()
    allowed = set(str(item) for item in (config.get("tools") or []))

    try:
        async with McpStdioClient(
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            timeout=timeout,
        ) as client:
            remote_tools = await client.list_tools()
    except Exception as e:
        logger.warning(f"[mcp] list tools failed {base_name}: {e}")
        return 0

    count = 0
    for item in remote_tools:
        if not isinstance(item, dict):
            continue
        remote_name = str(item.get("name") or "").strip()
        if not remote_name:
            continue
        if allowed and remote_name not in allowed:
            continue
        registered_name = f"{name_prefix}{remote_name}" if name_prefix else remote_name
        description = str(item.get("description") or f"MCP tool from {base_name}: {remote_name}")
        parameters = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {
            "type": "object",
            "properties": {},
            "required": [],
        }

        register_mcp_endpoint(
            registered_name=registered_name,
            remote_name=remote_name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )

        async def _handler(_tool_name=registered_name, **kwargs) -> str:
            return await call_registered_mcp_tool(_tool_name, kwargs)

        registry.register(
            AgentTool(
                name=registered_name,
                description=description,
                parameters=parameters,
                handler=_handler,
                local=False,
            )
        )
        count += 1
    return count
