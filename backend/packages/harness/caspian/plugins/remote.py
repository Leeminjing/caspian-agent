"""
本文件提供跨语言插件通道：插件以 plugin.json 清单声明可执行入口，系统以 stdio + 逐行 JSON
协议与插件进程通信，远程实现以 Python 代理对象进入既有注册表（系统无语言分支）。

对外提供:
    PluginManifest / EntryConfig — plugin.json 清单模型
    RemoteDeclaration / ToolDecl — 握手 declarations 的实现声明模型
    load_manifest — 读取并校验插件清单
    RemotePlugin — 远程插件进程：spawn、握手、调用往返、shutdown/terminate
    build_remote_tool — 远程 Tool 声明 → LangChain StructuredTool 代理
    build_remote_hook — 远程 Hook 声明 → async 可调用代理
    serialize_value / deserialize_value — 跨进程值序列化契约

输入:
    RemotePlugin(manifest, plugin_dir, config) — 清单、插件目录（cwd）、插件专属配置
    start() — 启动进程并完成 init → declarations 握手
    call(interface, value, context, timeout) — 发起一次调用往返，返回 JSON 值

输出:
    start → list[RemoteDeclaration]（握手失败抛异常，由 runtime 置 Unavailable）
    call → JSON 值；进程死亡/超时/插件报错时抛异常（归因该插件实现）

具体工作流:
    (1) 协议: stdout 只走逐行 JSON 协议消息（init/declarations/call/result/error/shutdown），
        stderr 进系统日志；消息单行无内嵌换行（json.dumps 默认满足）
    (2) 握手: spawn(cwd=插件目录) → 发 init{config} → 等待 declarations（30s 超时）
    (3) 调用: id 关联 asyncio.Future，读循环按 id 分发 result/error
    (4) 生命周期: 进程退出时未决调用全部失败；shutdown → 宽限 2s → terminate → kill
    (5) 序列化: before_agent/before_model 的 messages 经 model_dump 往返并按 type 注册表还原；
        tool_call/context 子集为 JSON 原生；after_* 载荷只序列化不回传

示例:
    manifest = load_manifest(plugin_dir)
    plugin = RemotePlugin(manifest, plugin_dir, {"api_key": "x"})
    declarations = await plugin.start()
    value = await plugin.call("before_model", {"messages": [...]}, {}, timeout=30)
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT_SECONDS = 30.0
SHUTDOWN_GRACE_SECONDS = 2.0
_MANIFEST_FILE = "plugin.json"

# ---------------------------------------------------------------------------
# 清单与声明模型
# ---------------------------------------------------------------------------


class EntryConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)


class PluginManifest(BaseModel):
    display_name: str = ""
    version: str = ""
    requires: list[str] = Field(default_factory=list)
    entry: EntryConfig


class ToolDecl(BaseModel):
    name: str
    description: str
    parameters: dict = Field(default_factory=dict)


class RemoteDeclaration(BaseModel):
    interface: str
    order: int = 0
    read_only: bool = False
    multi: bool = False
    timeout_seconds: float | None = None
    tool: ToolDecl | None = None


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """读取 plugin.json 清单；缺失或非法抛 PluginConfigError 语义异常。"""
    from caspian.plugins.errors import PluginConfigError

    path = plugin_dir / _MANIFEST_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PluginConfigError(f"缺少 {_MANIFEST_FILE}") from None
    except (ValueError, OSError) as exc:
        raise PluginConfigError(f"{_MANIFEST_FILE} 解析失败: {exc}") from None
    try:
        return PluginManifest.model_validate(raw)
    except Exception as exc:
        raise PluginConfigError(f"{_MANIFEST_FILE} 内容非法: {exc}") from None


# ---------------------------------------------------------------------------
# 值序列化契约
# ---------------------------------------------------------------------------

_MESSAGE_CLASSES: dict[str, type] = {
    "human": HumanMessage,
    "ai": AIMessage,
    "tool": ToolMessage,
    "system": SystemMessage,
}

_MESSAGES_INTERFACES = frozenset({"before_agent", "before_model"})


def _messages_to_wire(messages: list) -> list[dict]:
    return [message.model_dump() for message in messages]


def _messages_from_wire(items: list) -> list:
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"消息还原失败: 非对象 {type(item).__name__}")
        cls = _MESSAGE_CLASSES.get(item.get("type"))
        if cls is None:
            raise ValueError(f"消息还原失败: 未知 type '{item.get('type')}'")
        result.append(cls.model_validate(item))
    return result


def serialize_value(interface: str, value: Any) -> Any:
    """跨进程前序列化：messages 转 dict，tool_call 与原生 JSON 值直通。"""
    if interface in _MESSAGES_INTERFACES:
        return {"messages": _messages_to_wire(list(value.get("messages", [])))}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def deserialize_value(interface: str, value: Any) -> Any:
    """跨进程后还原：messages 按 type 注册表还原，其余直通。"""
    if value is None:
        return None
    if interface in _MESSAGES_INTERFACES:
        return {"messages": _messages_from_wire(list(value.get("messages", [])))}
    return value


def sanitize_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """提取 context 的 JSON 安全子集（跨进程传递）。"""
    keys = ("user_id", "thread_id", "model_name", "run_id", "selected_skills")
    return {key: ctx[key] for key in keys if key in ctx and _json_safe(ctx[key])}


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 远程插件进程
# ---------------------------------------------------------------------------


class RemotePlugin:
    """远程插件进程：spawn、握手、调用往返、关闭。"""

    def __init__(
        self,
        manifest: PluginManifest,
        plugin_dir: Path,
        config: dict,
        handshake_timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
    ) -> None:
        self._manifest = manifest
        self._cwd = plugin_dir
        self._config = config or {}
        self._handshake_timeout = handshake_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._declaration_future: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _send(self, message: dict) -> None:
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        await self._proc.stdin.drain()

    async def start(self) -> list[RemoteDeclaration]:
        """启动进程并完成握手，返回实现声明列表。失败抛异常。"""
        entry = self._manifest.entry
        self._proc = await asyncio.create_subprocess_exec(
            entry.command,
            *entry.args,
            cwd=str(self._cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._declaration_future = asyncio.get_running_loop().create_future()
        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._stderr_loop()),
            asyncio.create_task(self._exit_monitor()),
        ]
        await self._send({"type": "init", "config": self._config})
        raw = await asyncio.wait_for(
            self._declaration_future, timeout=self._handshake_timeout
        )
        declarations = [
            RemoteDeclaration.model_validate(item)
            for item in raw.get("implementations", [])
        ]
        if raw.get("display_name"):
            self._manifest.display_name = str(raw["display_name"])
        if raw.get("version"):
            self._manifest.version = str(raw["version"])
        return declarations

    async def call(
        self,
        interface: str,
        value: Any,
        context: dict,
        timeout: float | None = None,
    ) -> Any:
        """一次调用往返；进程死亡/超时/插件报错均抛异常（归因插件实现）。"""
        if not self.alive:
            raise RuntimeError("插件进程已退出")
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._send({
                "type": "call",
                "id": message_id,
                "interface": interface,
                "value": serialize_value(interface, value),
                "context": sanitize_context(context),
            })
            if timeout:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        finally:
            self._pending.pop(message_id, None)

    async def _read_loop(self) -> None:
        """stdout 读循环：按 type 分发 declarations / result / error。"""
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                logger.warning("插件协议非法行已忽略: %r", line[:120])
                continue
            kind = message.get("type")
            if kind == "declarations":
                if self._declaration_future and not self._declaration_future.done():
                    self._declaration_future.set_result(message)
                continue
            message_id = message.get("id")
            if kind in ("result", "error") and message_id in self._pending:
                future = self._pending.pop(message_id)
                if kind == "result":
                    future.set_result(message.get("value"))
                else:
                    future.set_exception(
                        RuntimeError(str(message.get("error") or "插件返回错误"))
                    )
                continue
            logger.warning("插件协议未知消息已忽略: %s", kind)

    async def _stderr_loop(self) -> None:
        """stderr 逐行进系统日志。"""
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.info("[远程插件 %s] %s", self._manifest.display_name or "?",
                        line.decode("utf-8", errors="replace").rstrip())

    async def _exit_monitor(self) -> None:
        """进程退出时使所有未决调用失败。"""
        code = await self._proc.wait()
        error = RuntimeError(f"插件进程已退出 (code={code})")
        if self._declaration_future and not self._declaration_future.done():
            self._declaration_future.set_exception(error)
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def shutdown(self) -> None:
        """发 shutdown 并终止进程：宽限 → terminate → kill。"""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            await self._send({"type": "shutdown"})
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for task in self._tasks:
            task.cancel()


# ---------------------------------------------------------------------------
# 代理 provider 构建
# ---------------------------------------------------------------------------

_SCHEMA_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _schema_to_fields(parameters: dict) -> dict[str, Any]:
    """JSON Schema parameters → pydantic 字段定义（v1 支持六类标量/容器）。"""
    fields: dict[str, Any] = {}
    required = set(parameters.get("required", []))
    for name, raw in (parameters.get("properties") or {}).items():
        raw = raw if isinstance(raw, dict) else {}
        py_type = _SCHEMA_TYPE_MAP.get(raw.get("type"), str)
        description = raw.get("description", "")
        default = ... if name in required else None
        fields[name] = (py_type, Field(default=default, description=description))
    return fields


def build_remote_tool(plugin: RemotePlugin, decl: RemoteDeclaration) -> BaseTool:
    """远程 Tool 声明 → StructuredTool 代理（args_schema 按 parameters 生成）。"""
    tool = decl.tool
    model = create_model(f"{tool.name}Input", **_schema_to_fields(tool.parameters))

    async def _invoke(**kwargs) -> str:
        value = await plugin.call(
            decl.interface,
            {"name": tool.name, "arguments": kwargs},
            {},
            timeout=None,
        )
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=tool.name,
        description=tool.description,
        args_schema=model,
    )


def build_remote_hook(plugin: RemotePlugin, decl: RemoteDeclaration):
    """远程 Hook 声明 → async 可调用代理（值序列化由本模块契约完成）。"""
    from caspian.plugins.spec import resolve_interface

    default_timeout = (
        resolve_interface(decl.interface).timeout_seconds
        if resolve_interface(decl.interface) is not None
        else 30.0
    )

    async def proxy(value, ctx):
        result = await plugin.call(
            decl.interface,
            value,
            ctx if isinstance(ctx, dict) else {},
            timeout=decl.timeout_seconds if decl.timeout_seconds else default_timeout,
        )
        return deserialize_value(decl.interface, result)

    return proxy
