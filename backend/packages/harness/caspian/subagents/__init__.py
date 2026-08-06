"""
本文件为 caspian.subagents 包入口，重导出注册表与执行引擎的公开 API。

对外提供:
    SubagentConfig — subagent 类型配置
    SubagentExecutor — subagent 执行引擎（延迟导入，避免与 tools/builtins 循环依赖）
    SubagentResult — 单次执行的结果档案
    get_subagent_config — 注册表解析
    get_available_subagent_names — 可用类型列表

示例:
    from caspian.subagents import SubagentConfig, get_subagent_config
"""

from .config import SubagentConfig
from .registry import get_available_subagent_names, get_subagent_config

__all__ = [
    "SubagentConfig",
    "SubagentExecutor",
    "SubagentResult",
    "get_available_subagent_names",
    "get_subagent_config",
]


def __getattr__(name: str):
    # 延迟导入 executor：executor 在 _build_initial_state 中导入
    # tools.builtins.tool_search，后者会反向导入本包，延迟可打破初始化期循环
    if name in {"SubagentExecutor", "SubagentResult"}:
        from .executor import SubagentExecutor, SubagentResult

        exports = {
            "SubagentExecutor": SubagentExecutor,
            "SubagentResult": SubagentResult,
        }
        globals().update(exports)
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
