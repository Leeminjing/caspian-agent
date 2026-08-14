"""
本文件对外提供 load_mcp_tools、get_mcp_tools、get_context7_tools 三个异步函数。

对外提供:
    load_mcp_tools: 加载已翻译的 MCP server 配置，建立进程级持久 session 并绑定工具
    get_mcp_tools: 返回 extensions_config.json 中启用的常规 MCP 工具
    get_context7_tools: 返回承诺层独占的 Context7 工具

持久 session 说明:
    langchain-mcp-adapters 的 get_tools() 每次工具调用都会新建临时 session 并在调用后
    关闭（连带终止 stdio 子进程），导致 playwright 等有状态 server 的浏览器实例
    在每次调用间丢失（页面重置为 about:blank）。本模块改为进程级持有
    MultiServerMCPClient + AsyncExitStack，经 client.session(name) 建立的 session
    全程复用，工具绑定同一 session，连接与子进程生命周期 = 进程生命周期。
    单用户本地场景下多 run 共享 session 可接受；server 配置变化（server 集合或参数
    变更）时自动重建 client。

输入:
    无参数 — 函数内部自行调用 get_extensions_config() 加载 extensions_config.json

输出:
    list[BaseTool] — MCP Server 远端工具转换后的 LangChain Tool 列表

工作流:
    (1) 调用 get_extensions_config() 加载 extensions_config.json
    (2) 调用 build_servers_config() 将配置翻译为 MultiServerMCPClient 可接受的 dict
    (3) 若所有 server 均 disabled 返回 []
    (4) 合并新 server 配置；配置有变化时重建 client（含全部已注册 server）
    (5) 为每个 server 建立持久 session（AsyncExitStack 持有），load_mcp_tools(session) 绑定工具
    (6) 单 server 连接失败 log warn 跳过，不抛异常

示例:
    tools = await get_mcp_tools()
    for tool in tools:
        print(tool.name)
"""

import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.tools import BaseTool

from caspian.config.extensions_config import get_extensions_config
from caspian.mcp.client import build_servers_config

logger = logging.getLogger(__name__)

# 进程级持久 MCP 连接（模块级单例）
_mcp_client: Any | None = None
_mcp_exit_stack: AsyncExitStack | None = None
_mcp_servers: dict[str, dict] = {}


def _get_exit_stack() -> AsyncExitStack:
    """获取模块级 AsyncExitStack（受保护 helper）。"""
    global _mcp_exit_stack
    if _mcp_exit_stack is None:
        _mcp_exit_stack = AsyncExitStack()
    return _mcp_exit_stack


def _merge_servers(servers_config: dict[str, dict]) -> bool:
    """合并新 server 配置，返回是否有变化（受保护 helper）。

    输入:
        servers_config: dict[str, dict] — 本次请求的 server 配置

    输出:
        bool — True 表示配置有变化，需要重建 client
    """
    global _mcp_servers
    changed = False
    for name, params in servers_config.items():
        if _mcp_servers.get(name) != params:
            _mcp_servers[name] = params
            changed = True
    return changed


async def load_mcp_tools(servers_config: dict[str, dict]) -> list[BaseTool]:
    """加载 MCP server 工具，session 进程级持久复用。

    输入:
        servers_config: dict[str, dict] — {server_name: params}，由 build_servers_config 产出

    输出:
        list[BaseTool] — 各 server 的工具列表；单个 server 连接失败跳过（fail-soft）

    工作流:
        (1) 合并新 server 配置；配置变化时重建 MultiServerMCPClient（含全部已注册 server）
        (2) 为每个 server 建立持久 session 并绑定工具（AsyncExitStack 持有，进程退出时清理）
    """
    global _mcp_client

    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.tools import load_mcp_tools as _load_session_tools

    changed = _merge_servers(servers_config)
    if _mcp_client is None or changed:
        if _mcp_client is None:
            logger.info("MCP client 首次创建")
        else:
            logger.info("MCP server 配置变化，重建 client")
        _mcp_client = MultiServerMCPClient(dict(_mcp_servers))

    stack = _get_exit_stack()
    tools: list[BaseTool] = []
    for server_name, _ in servers_config.items():
        try:
            session = await stack.enter_async_context(_mcp_client.session(server_name))
            server_tools = await _load_session_tools(session)
            tools.extend(server_tools)
            logger.info(
                "MCP Server '%s' 持久连接成功，获取 %d 个工具",
                server_name,
                len(server_tools),
            )
        except Exception:
            logger.warning("MCP Server '%s' 连接失败，已跳过", server_name, exc_info=True)
    return tools


async def get_mcp_tools() -> list[BaseTool]:
    """返回 extensions_config.json 中启用的常规 MCP 工具。

    输入: 无

    输出:
        list[BaseTool] — 启用的 MCP server 工具列表；配置缺失/异常时返回 []

    工作流:
        (1) 加载 extensions_config.json 并翻译为 servers 配置
        (2) 无启用 server 时返回 []
        (3) 委托 load_mcp_tools 建立持久 session 并返回工具
    """
    try:
        extensions_config = get_extensions_config("extensions_config.json")
        servers_config = build_servers_config(extensions_config)
    except FileNotFoundError:
        logger.warning("extensions_config.json 不存在，MCP 工具不可用")
        return []
    except Exception:
        logger.error("加载 MCP 配置失败", exc_info=True)
        return []

    if not servers_config:
        return []

    return await load_mcp_tools(servers_config)


async def get_context7_tools(url: str) -> list[BaseTool]:
    """返回承诺层独占的 Context7 工具（同样走持久 session）。

    输入:
        url: str — Context7 MCP server 地址

    输出:
        list[BaseTool] — Context7 工具列表（resolve-library-id / query-docs）
    """
    params: dict[str, object] = {"transport": "http", "url": url}
    if api_key := os.environ.get("CONTEXT7_API_KEY"):
        params["headers"] = {"Authorization": f"Bearer {api_key}"}
    return await load_mcp_tools({"context7": params})
