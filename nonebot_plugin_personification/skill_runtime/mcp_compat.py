from __future__ import annotations

import asyncio
import contextlib
import json
import os
from itertools import count
from pathlib import Path
from typing import Any

from ..agent.tool_registry import AgentTool, ToolRegistry
from .source_resolver import skill_source_content_digest


_REGISTERED_MCP_TOOLS: dict[str, dict[str, Any]] = {}
PREFERRED_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PREFERRED_PROTOCOL_VERSION, "2025-06-18"})
MAX_TOOLS_LIST_PAGES = 20
MAX_TOOLS = 1000
MAX_CURSOR_CHARS = 65536


class McpProtocolError(RuntimeError):
    pass


def _bounded_tool_result(value: str, *, max_chars: int = 131072) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


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
        self._request_lock = asyncio.Lock()
        self.protocol_version = PREFERRED_PROTOCOL_VERSION
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self._transport_failed = False

    async def __aenter__(self) -> "McpStdioClient":
        try:
            self._transport_failed = False
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                limit=4 * 1024 * 1024,
            )
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            await self.initialize()
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    @property
    def is_running(self) -> bool:
        process = self._proc
        return process is not None and process.returncode is None and not self._transport_failed

    @property
    def returncode(self) -> int | None:
        process = self._proc
        return process.returncode if process is not None else None

    async def _close(self) -> None:
        process = self._proc
        self._proc = None
        if process is not None and process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except (TimeoutError, asyncio.TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        elif process is not None:
            with contextlib.suppress(Exception):
                await process.wait()
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

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
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self._proc.stdin.write(body + b"\n")
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._transport_failed = True
            raise McpProtocolError("MCP server closed stdin") from exc

    async def _read_message(self) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise McpProtocolError("MCP process is not ready")

        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self.timeout)
            if not line:
                self._transport_failed = True
                raise McpProtocolError("MCP server closed stdout")
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                continue
            try:
                parsed = json.loads(decoded)
            except Exception as exc:
                raise McpProtocolError("Invalid newline-delimited MCP message") from exc
            if not isinstance(parsed, dict):
                raise McpProtocolError("Invalid MCP message payload")
            if parsed.get("jsonrpc") != "2.0":
                raise McpProtocolError("Invalid MCP JSON-RPC version")
            return parsed

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._request_lock:
            return await asyncio.wait_for(self._request_unlocked(method, params), timeout=self.timeout)

    async def _request_unlocked(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
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
            response_id = message.get("id")
            if type(response_id) is not int or response_id != request_id:
                if response_id is not None and message.get("method"):
                    await self._write_message(
                        {
                            "jsonrpc": "2.0",
                            "id": response_id,
                            "error": {"code": -32601, "message": "Client capability not available"},
                        }
                    )
                continue
            if "error" in message:
                raise McpProtocolError("MCP server returned a JSON-RPC error")
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpProtocolError("MCP response result must be an object")
            return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def initialize(self) -> None:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "personification-skill-loader",
                    "version": "1.0.0",
                },
            },
        )
        negotiated = str(result.get("protocolVersion") or "").strip()
        if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpProtocolError("MCP server negotiated an unsupported protocol version")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise McpProtocolError("MCP server did not declare the tools capability")
        server_info = result.get("serverInfo")
        if server_info is not None and not isinstance(server_info, dict):
            raise McpProtocolError("MCP serverInfo must be an object")
        self.protocol_version = negotiated
        self.capabilities = dict(capabilities)
        self.server_info = dict(server_info or {})
        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        return await asyncio.wait_for(self._list_tools_unlocked(), timeout=self.timeout)

    async def _list_tools_unlocked(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for page_number in range(MAX_TOOLS_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params)
            page = result.get("tools")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise McpProtocolError("MCP tools/list returned an invalid tools page")
            tools.extend(page)
            if len(tools) > MAX_TOOLS:
                raise McpProtocolError("MCP server exposed too many tools")
            next_cursor = result.get("nextCursor")
            if next_cursor is None or next_cursor == "":
                return tools
            if not isinstance(next_cursor, str):
                raise McpProtocolError("MCP tools/list returned a non-string cursor")
            if len(next_cursor) > MAX_CURSOR_CHARS:
                raise McpProtocolError("MCP tools/list returned an oversized cursor")
            if next_cursor in seen_cursors:
                raise McpProtocolError("MCP tools/list returned a repeated cursor")
            if page_number + 1 >= MAX_TOOLS_LIST_PAGES:
                raise McpProtocolError("MCP tools/list did not terminate within the page limit")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise McpProtocolError("MCP tools/list did not terminate")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self.request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        if result.get("isError") is True:
            raise McpProtocolError(f"MCP tool execution failed: {name}")
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
                return _bounded_tool_result("\n".join(texts))
        if result.get("structuredContent") is not None:
            return _bounded_tool_result(json.dumps(result["structuredContent"], ensure_ascii=False))
        if result.get("content") is not None:
            return _bounded_tool_result(json.dumps(result["content"], ensure_ascii=False))
        return _bounded_tool_result(json.dumps(result, ensure_ascii=False))


def normalize_mcp_config(
    *,
    skill_dir: Path,
    raw: Any,
    default_timeout: int = 20,
    allowed_root: Path | None = None,
    restrict_paths: bool = False,
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
    cwd_path = (skill_dir / cwd_raw).resolve() if cwd_raw else skill_dir.resolve()
    approved_root = (allowed_root or skill_dir).resolve()

    def _inside(candidate: Path) -> bool:
        try:
            return os.path.commonpath([str(approved_root), str(candidate.resolve())]) == str(approved_root)
        except Exception:
            return False

    if restrict_paths and (not _inside(cwd_path) or cwd_path.is_symlink()):
        return None
    if restrict_paths and (Path(command).is_absolute() or "/" in command or "\\" in command):
        command_path = (skill_dir / command).resolve()
        if not _inside(command_path) or not command_path.is_file() or command_path.is_symlink():
            return None
        command = str(command_path)
    if restrict_paths:
        normalized_args: list[str] = []
        for arg in args:
            if arg.startswith("-"):
                normalized_args.append(arg)
                continue
            looks_like_path = Path(arg).is_absolute() or "/" in arg or "\\" in arg or Path(arg).suffix in {".py", ".js", ".mjs", ".cjs"}
            if not looks_like_path:
                normalized_args.append(arg)
                continue
            arg_path = (skill_dir / arg).resolve()
            if not _inside(arg_path) or not arg_path.is_file() or arg_path.is_symlink():
                return None
            normalized_args.append(str(arg_path))
        args = normalized_args
    cwd = str(cwd_path)
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
    approved_root: str = "",
    content_digest: str = "",
) -> None:
    _REGISTERED_MCP_TOOLS[registered_name] = {
        "remote_name": remote_name,
        "command": command,
        "args": list(args),
        "env": dict(env),
        "cwd": cwd,
        "timeout": max(1, int(timeout)),
        "approved_root": str(approved_root or ""),
        "content_digest": str(content_digest or ""),
    }


def get_registered_mcp_endpoint(tool_name: str) -> dict[str, Any] | None:
    config = _REGISTERED_MCP_TOOLS.get(str(tool_name))
    return dict(config) if isinstance(config, dict) else None


async def call_registered_mcp_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    config = get_registered_mcp_endpoint(tool_name)
    if config is None:
        raise McpProtocolError(f"Remote MCP tool '{tool_name}' is not registered")
    approved_root = str(config.get("approved_root") or "").strip()
    expected_digest = str(config.get("content_digest") or "").strip().lower()
    if approved_root and expected_digest:
        try:
            current_digest = skill_source_content_digest(Path(approved_root))
        except Exception:
            current_digest = ""
        if current_digest != expected_digest:
            raise McpProtocolError("MCP skill approved content digest changed")
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
    approved_root: Path | None = None,
    content_digest: str = "",
) -> int:
    env = os.environ.copy() if config.get("inherit_env", True) else {}
    env.update(config.get("env") or {})
    command = str(config.get("command") or "").strip()
    args = [str(item) for item in (config.get("args") or [])]
    timeout = max(1, int(config.get("timeout", 20) or 20))
    cwd = str(config.get("cwd") or "").strip() or str(skill_dir.resolve())
    name_prefix = str(config.get("name_prefix") or "").strip()
    allowed = set(str(item) for item in (config.get("tools") or []))
    if approved_root is not None and content_digest:
        try:
            current_digest = skill_source_content_digest(approved_root)
        except Exception:
            current_digest = ""
        if current_digest != content_digest:
            logger.warning(f"[mcp] approved content digest changed before registration: {base_name}")
            return 0

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
        annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
        output_schema = item.get("outputSchema") if isinstance(item.get("outputSchema"), dict) else {}
        title = str(item.get("title") or annotations.get("title") or "")

        register_mcp_endpoint(
            registered_name=registered_name,
            remote_name=remote_name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            timeout=timeout,
            approved_root=str(approved_root or ""),
            content_digest=content_digest,
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
                metadata={
                    "category": "mcp",
                    "source_kind": "mcp",
                    "mcp_base": base_name,
                    "remote_name": remote_name,
                    "title": title,
                    "output_schema": output_schema,
                    "annotations": annotations,
                    "transport": "stdio",
                },
            )
        )
        count += 1
    return count
