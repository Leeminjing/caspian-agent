"""
本文件对外提供 `build_general_middlewares` 与 `build_subagent_middlewares` 函数，
作为中间件链的组装入口。

对外提供:
    build_general_middlewares — 返回 lead agent 通用的 AgentMiddleware 列表
    build_subagent_middlewares — 返回 subagent 专用的 AgentMiddleware 列表

输入:
    build_general_middlewares:
        commitment_enabled: bool — 是否装配 CommitmentMiddleware
        model/context7_tools — CommitmentMiddleware 的内部依赖
        skill_names: frozenset[str] | None — 当前用户 enabled 技能名集合，透传给承诺层剥离前导 skill token

    build_subagent_middlewares:
        model: BaseChatModel | None — 预留（签名对齐）
        skill_names: frozenset[str] | None — subagent 可用技能名集合（预留）

输出:
    list[AgentMiddleware] — 按固定顺序排列的中间件列表

具体工作流:
    build_general_middlewares:
    (1) 实例化 UploadsMiddleware（No.1）
    (2) 实例化 DecisionTableMiddleware（No.2，始终装配，无等级表时自动跳过）
    (3) 开启时实例化 CommitmentMiddleware（No.3）
    (4) 实例化 SandboxAuditMiddleware
    (5) 返回有序列表

    build_subagent_middlewares:
    (1) 只装配 SandboxAuditMiddleware（shell 安全审计）
    (2) 不装配 UploadsMiddleware / CommitmentMiddleware（子上下文干净、防嵌套承诺）
    (3) 返回列表

示例:
    from caspian.agents.middlewares.builder import build_general_middlewares

    middlewares = build_general_middlewares()
    # → [UploadsMiddleware(), DecisionTableMiddleware(), SandboxAuditMiddleware()]
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from typing import TYPE_CHECKING

from caspian.agents.commitment import CommitmentMiddleware
from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware
from caspian.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware
from caspian.agents.middlewares.uploads_middleware import UploadsMiddleware

if TYPE_CHECKING:
    from caspian.config.context_compression_config import ContextCompressionConfig


def build_general_middlewares(
    *,
    commitment_enabled: bool = False,
    model: BaseChatModel | None = None,
    context7_tools: list[BaseTool] | None = None,
    skill_names: frozenset[str] | None = None,
    context_compression: "ContextCompressionConfig | None" = None,
) -> list[AgentMiddleware]:
    """组装 lead agent 通用中间件链。

    输出:
        list[AgentMiddleware] — [(ContextCompressionMiddleware), UploadsMiddleware,
        DecisionTableMiddleware(, CommitmentMiddleware), SandboxAuditMiddleware]

    工作流:
        (1) context_compression.enabled 时在链首装配 ContextCompressionMiddleware
            (wrap_model_call 最外层可捕获内层溢出;before_model 链首先对完整历史压缩)
        (2) 其余按既有顺序装配
    """
    from caspian.agents.middlewares.context_compression import (
        ContextCompressionMiddleware,
    )

    middlewares: list[AgentMiddleware] = []
    if context_compression is not None and context_compression.enabled:
        middlewares.append(ContextCompressionMiddleware(context_compression))
    middlewares.extend([
        UploadsMiddleware(),
        DecisionTableMiddleware(),
    ])
    if commitment_enabled:
        if model is None:
            raise ValueError("启用 CommitmentMiddleware 时必须提供 model")
        middlewares.append(
            CommitmentMiddleware(model, context7_tools or [], skill_names or frozenset())
        )
    middlewares.append(SandboxAuditMiddleware())
    return middlewares


def build_subagent_middlewares(
    *,
    model: BaseChatModel | None = None,
    skill_names: frozenset[str] | None = None,
) -> list[AgentMiddleware]:
    """组装 subagent 专用中间件链。

    输出:
        list[AgentMiddleware] — [SandboxAuditMiddleware]

    工作流:
        (1) 只保留 SandboxAuditMiddleware（shell 高危命令审计不可丢）
        (2) 不含 UploadsMiddleware（子上下文只含任务输入）与 CommitmentMiddleware（防嵌套承诺流程）
    """
    middlewares: list[AgentMiddleware] = [SandboxAuditMiddleware()]
    return middlewares
