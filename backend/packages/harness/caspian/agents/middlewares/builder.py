"""
本文件对外提供 `build_general_middlewares` 函数，作为通用中间件链的组装入口。

对外提供:
    build_general_middlewares — 返回 agent 通用的 AgentMiddleware 列表

输入:
    无参数 — 所有通用中间件均为不可舍（always included）

输出:
    list[AgentMiddleware] — 按固定顺序排列的通用中间件列表

具体工作流:
    (1) 实例化 UploadsMiddleware（No.1）
    (2) 实例化 SandboxAuditMiddleware（No.2）
    (3) 返回有序列表

示例:
    from caspian.agents.middlewares.builder import build_general_middlewares

    middlewares = build_general_middlewares()
    # → [UploadsMiddleware(), SandboxAuditMiddleware()]
"""

from langchain.agents.middleware import AgentMiddleware

from caspian.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware
from caspian.agents.middlewares.uploads_middleware import UploadsMiddleware


def build_general_middlewares() -> list[AgentMiddleware]:
    """组装通用中间件链。

    输入: 无

    输出:
        list[AgentMiddleware] — [UploadsMiddleware, SandboxAuditMiddleware]
    """
    return [
        UploadsMiddleware(),
        SandboxAuditMiddleware(),
    ]
