"""
本文件对外提供 `build_general_middlewares` 函数，作为通用中间件链的组装入口。

对外提供:
    build_general_middlewares — 返回 agent 通用的 AgentMiddleware 列表

输入:
    commitment_enabled: bool — 是否装配 CommitmentMiddleware
    model/context7_tools — CommitmentMiddleware 的内部依赖
    skill_names: frozenset[str] | None — 当前用户 enabled 技能名集合，透传给承诺层剥离前导 skill token

输出:
    list[AgentMiddleware] — 按固定顺序排列的通用中间件列表

具体工作流:
    (1) 实例化 UploadsMiddleware（No.1）
    (2) 开启时实例化 CommitmentMiddleware（No.2）
    (3) 实例化 SandboxAuditMiddleware
    (4) 返回有序列表

示例:
    from caspian.agents.middlewares.builder import build_general_middlewares

    middlewares = build_general_middlewares()
    # → [UploadsMiddleware(), SandboxAuditMiddleware()]
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from caspian.agents.commitment import CommitmentMiddleware
from caspian.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware
from caspian.agents.middlewares.uploads_middleware import UploadsMiddleware


def build_general_middlewares(
    *,
    commitment_enabled: bool = False,
    model: BaseChatModel | None = None,
    context7_tools: list[BaseTool] | None = None,
    skill_names: frozenset[str] | None = None,
) -> list[AgentMiddleware]:
    """组装通用中间件链。

    输入: 无

    输出:
        list[AgentMiddleware] — [UploadsMiddleware, SandboxAuditMiddleware]
    """
    middlewares: list[AgentMiddleware] = [UploadsMiddleware()]
    if commitment_enabled:
        if model is None:
            raise ValueError("启用 CommitmentMiddleware 时必须提供 model")
        middlewares.append(
            CommitmentMiddleware(model, context7_tools or [], skill_names or frozenset())
        )
    middlewares.append(SandboxAuditMiddleware())
    return middlewares
