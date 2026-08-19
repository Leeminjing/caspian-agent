"""
本文件对外提供 get_available_tools 异步函数，作为工具汇集的唯一对外入口。

输入:
    app_config: AppConfig | None  — 应用配置对象，None 时内部自动加载
    tool_groups: list[str] | None  — 需要加载的工具分组名列表，None 表示加载全部
    subagent_enabled: bool        — True 时包含 task 委托工具，False（subagent 场景）时排除防递归
    plugin_user_id: str | None    — 提供时加载该用户 custom 插件并追加插件 Tool 层

输出:
    list[BaseTool] — 经过去重和过滤的可用 LangChain Tool 列表

具体工作流:
    (1) app_config 为 None 时内部调用 get_app_config() 自动加载 config.yaml
    (2) 并行获取三类工具: config.yaml 声明式工具、built-in 工具、MCP 远端工具
    (3) 若指定 tool_groups，仅保留 group 匹配的 config.yaml 工具，built-in 和 MCP 不受过滤
    (4) subagent_enabled=False 时从 built-in 排除 task 工具（防递归嵌套）
    (5) 三类工具按名称去重，优先级 config.yaml > built-in > MCP
    (6) plugin_user_id 提供且插件运行时可用时: 确保该用户 custom 插件已加载，
        追加插件 Tool 层（第四层，去重优先级最低，同名冲突在注入期已拒绝）
    (7) 返回完整工具列表

示例:
    tools = await get_available_tools(tool_groups=["file:read", "bash"])
    subagent_tools = await get_available_tools(subagent_enabled=False)
    tools = await get_available_tools(plugin_user_id="uuid-xxx")
    agent = create_agent(model, tools=tools)
"""

import logging

from langchain_core.tools import BaseTool

from caspian.config import AppConfig, get_app_config
from caspian.reflection.resolvers import resolve_class

logger = logging.getLogger(__name__)


def _load_config_tools(app_config: AppConfig) -> list[BaseTool]:
    tools: list[BaseTool] = []
    for item in app_config.tools:
        try:
            tool_fn = resolve_class(item.use)
            if hasattr(tool_fn, "name") and hasattr(tool_fn, "description"):
                tools.append(tool_fn)
            else:
                logger.warning("config.yaml 工具 '%s' 的 use 字段指向的对象不是 LangChain Tool: %s", item.name, item.use)
        except Exception:
            logger.error("config.yaml 工具 '%s' 加载失败 (use=%s)，已跳过", item.name, item.use, exc_info=True)
    return tools


def _load_builtin_tools(subagent_enabled: bool = True) -> list[BaseTool]:
    from caspian.tools.builtins import (
        add_knowledge,
        knowledge_query,
        list_uploaded_files,
        present_file_tool,
        task_tool,
        update_decision_table,
        view_image_tool,
    )

    tools: list[BaseTool] = [
        present_file_tool,
        view_image_tool,
        list_uploaded_files,
        update_decision_table,
        add_knowledge,
        knowledge_query,
    ]
    if subagent_enabled:
        tools.append(task_tool)
    return tools


async def _load_mcp_tools() -> list[BaseTool]:
    try:
        from caspian.mcp.cache import get_mcp_tools_cached

        return await get_mcp_tools_cached()
    except Exception:
        logger.error("MCP 工具加载失败", exc_info=True)
        return []


def _filter_config_tools(
    config_tools: list[BaseTool],
    tool_groups: list[str],
    app_config: AppConfig,
) -> list[BaseTool]:
    group_set = frozenset(tool_groups)
    # use 声明后段即模块级工具名（caspian.sandbox.tools:read_file_tool → read_file_tool）
    tools_by_name = {t.name: t for t in config_tools}
    result: list[BaseTool] = []
    for item in app_config.tools:
        tool_name = item.use.rsplit(":", 1)[-1]
        if item.group in group_set and tool_name in tools_by_name:
            result.append(tools_by_name[tool_name])
    return result


def _deduplicate_tools(
    config_tools: list[BaseTool],
    builtin_tools: list[BaseTool],
    mcp_tools: list[BaseTool],
) -> list[BaseTool]:
    seen: dict[str, BaseTool] = {}
    for t in mcp_tools:
        seen[t.name] = t
    for t in builtin_tools:
        seen[t.name] = t
    for t in config_tools:
        seen[t.name] = t
    return list(seen.values())


async def get_available_tools(
    app_config: AppConfig | None = None,
    tool_groups: list[str] | None = None,
    subagent_enabled: bool = True,
    plugin_user_id: str | None = None,
) -> list[BaseTool]:
    if app_config is None:
        app_config = get_app_config("config.yaml")

    config_tools = _load_config_tools(app_config)

    if tool_groups is not None:
        config_tools = _filter_config_tools(config_tools, tool_groups, app_config)

    builtin_tools = _load_builtin_tools(subagent_enabled=subagent_enabled)
    mcp_tools = await _load_mcp_tools()

    tools = _deduplicate_tools(config_tools, builtin_tools, mcp_tools)

    # (6) 插件 Tool 层（第四层，去重优先级最低）
    if plugin_user_id is not None:
        from caspian.plugins.runtime import get_plugin_runtime
        from caspian.plugins.tools import plugin_tools

        runtime = get_plugin_runtime()
        if runtime is not None:
            await runtime.ensure_user(plugin_user_id)
            tools.extend(
                plugin_tools(
                    runtime=runtime,
                    user_id=plugin_user_id,
                    existing_names={t.name for t in tools},
                )
            )

    return tools
