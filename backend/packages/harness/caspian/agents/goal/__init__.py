"""
本文件为 caspian.agents.goal 包的入口，重导出目标模式中间件。

对外提供:
    GoalModeMiddleware — /goal 命令拦截中间件
"""

from caspian.agents.goal.middleware import GoalModeMiddleware

__all__ = ["GoalModeMiddleware"]
