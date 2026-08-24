"""
本文件为 caspian.goal 包的入口，重导出目标域公开 API。

对外提供:
    GoalService — store 持久化的目标域服务（CAS 读改写）
    GoalRoundDriver — 一次 run 内的目标自动推进决策器
    build_goal_tools — 模型可见 get_goal / create_goal / update_goal 工具工厂
    goal_guidance — 模型可见的策略段文本
    GoalError / GoalRecord / GoalRef / GoalBlockReason — 域类型
"""

from caspian.goal.domain import GoalBlockReason, GoalError, GoalRecord, GoalRef
from caspian.goal.guidance import goal_guidance
from caspian.goal.round_driver import GoalRoundDriver
from caspian.goal.service import GoalService
from caspian.goal.tools import build_goal_tools

__all__ = [
    "GoalService",
    "GoalRoundDriver",
    "build_goal_tools",
    "goal_guidance",
    "GoalError",
    "GoalRecord",
    "GoalRef",
    "GoalBlockReason",
]
