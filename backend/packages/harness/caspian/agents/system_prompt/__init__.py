"""
本文件对外汇总完整 system snapshot 领域的公开 API。

输入:
    静态 prompt、运行期状态、route capability 与 authoritative Decision Table

输出:
    SystemSnapshot、SystemSnapshotBuilder、SystemSnapshotMiddleware、消息标记与重基线工具

具体工作流:
    builder 生成完整不可变快照，strategy 决定 replace 或 in-history，middleware 在模型边界编排，
    metadata 负责托管消息识别和压缩重基线。

示例:
    SystemSnapshotBuilder("base").build(plan_active=False)
"""

from caspian.agents.system_prompt.metadata import (
    SYSTEM_FRAGMENT_MARKER_KEY,
    rebase_system_history,
)
from caspian.agents.system_prompt.middleware import SystemSnapshotMiddleware
from caspian.agents.system_prompt.snapshot import (
    SystemFragment,
    SystemSnapshot,
    SystemSnapshotBuilder,
)

__all__ = [
    "SYSTEM_FRAGMENT_MARKER_KEY",
    "SystemFragment",
    "SystemSnapshot",
    "SystemSnapshotBuilder",
    "SystemSnapshotMiddleware",
    "rebase_system_history",
]
