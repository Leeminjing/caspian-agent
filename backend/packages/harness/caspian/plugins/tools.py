"""
本文件提供插件 Tool 贡献汇集，作为工具集合的第四类来源（加法层，去重优先级最低）。

对外提供:
    plugin_tools — 从 PluginRuntime 取 Tool 实现，过滤与既有工具同名的冲突后返回

输入:
    plugin_tools(runtime=None, user_id=None, existing_names=None) —
        runtime: PluginRuntime | None，None 时取进程单例；existing_names: 既有三层工具名集合，
        提供时做同名冲突过滤，冲突的插件 Tool 被跳过并在插件状态中报告（幂等）

输出:
    list[BaseTool] — 无冲突的插件 Tool 实现（稳定顺序 = 注入序号）

具体工作流:
    (1) runtime 为空 → 返回空列表（无插件零开销）
    (2) 取 runtime.registry.tool_entries(user_id)（public + 指定用户 custom）
    (3) 同名冲突: provider.name 在 existing_names 中 → 跳过并把冲突写入插件状态 issues（幂等）
    (4) 其余按注入顺序返回

示例:
    tools = plugin_tools(existing_names={"bash", "read_file"})
    # 插件 Browser 无冲突 → 返回；插件注入的 read_file → 跳过并写入插件状态 issues
"""

import logging

from langchain_core.tools import BaseTool

from caspian.plugins.runtime import PluginRuntime, get_plugin_runtime

logger = logging.getLogger(__name__)


def plugin_tools(
    runtime: PluginRuntime | None = None,
    user_id: str | None = None,
    existing_names: set[str] | None = None,
) -> list[BaseTool]:
    """汇集插件 Tool 实现，过滤同名冲突后返回。"""
    runtime = runtime if runtime is not None else get_plugin_runtime()
    if runtime is None:
        return []
    existing_names = existing_names or set()
    result: list[BaseTool] = []
    for plugin_name, owner, impl in runtime.registry.tool_entries(user_id):
        provider = impl.provider
        if provider.name in existing_names:
            message = f"Tool '{provider.name}' 与既有工具同名，已跳过（不覆盖已有）"
            runtime.report_issue(owner, plugin_name, message)
            logger.warning("插件 %s 的 %s", plugin_name, message)
            continue
        result.append(provider)
    return result
