"""本文件对外提供 build_general_middlewares 与 build_subagent_middlewares。

输入为承诺层开关、模型、Context7 工具加载器和技能名；输出为按执行顺序装配的中间件列表。
工作流为主 Agent 装配上传、界面改表、承诺流程、表工具守卫、动作前复核及模型表边界；执行子 Agent 使用同一表边界和工具闸门。
示例：`middlewares = build_general_middlewares(commitment_enabled=False)`。"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from caspian.agents.commitment import CommitmentMiddleware
from caspian.agents.middlewares.decision_action_review_middleware import DecisionActionReviewMiddleware
from caspian.agents.middlewares.decision_table_edit_middleware import DecisionTableEditMiddleware
from caspian.agents.middlewares.decision_table_guard_middleware import DecisionTableGuardMiddleware
from caspian.agents.middlewares.decision_table_middleware import DecisionTableMiddleware
from caspian.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware
from caspian.agents.middlewares.tool_error_middleware import ToolErrorMiddleware
from caspian.agents.middlewares.uploads_middleware import UploadsMiddleware

if TYPE_CHECKING:
    from caspian.config.context_compression_config import ContextCompressionConfig


def build_general_middlewares(
    *,
    commitment_enabled: bool = False,
    model: BaseChatModel | None = None,
    context7_loader: Callable[[], Awaitable[list[BaseTool]]] | None = None,
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
        (3) commitment_enabled 时装配 CommitmentMiddleware：优先使用 context7_loader
            （懒加载）；未提供 loader 时回退用 context7_tools 包装的兼容 loader
    """
    from caspian.agents.middlewares.context_compression import (
        ContextCompressionMiddleware,
    )

    middlewares: list[AgentMiddleware] = []
    if context_compression is not None and context_compression.enabled:
        middlewares.append(ContextCompressionMiddleware(context_compression))
    middlewares.append(ToolErrorMiddleware())
    middlewares.extend([
        UploadsMiddleware(),
        DecisionTableEditMiddleware(),
    ])
    if commitment_enabled:
        if model is None:
            raise ValueError("启用 CommitmentMiddleware 时必须提供 model")
        effective_loader = context7_loader
        if effective_loader is None and context7_tools is not None:
            resolved_tools = list(context7_tools)

            async def _compat_loader() -> list[BaseTool]:
                return resolved_tools

            effective_loader = _compat_loader
        middlewares.append(
            CommitmentMiddleware(model, effective_loader, skill_names or frozenset())
        )
    middlewares.append(DecisionTableGuardMiddleware())
    middlewares.append(DecisionActionReviewMiddleware())
    middlewares.append(SandboxAuditMiddleware())
    middlewares.append(DecisionTableMiddleware(actor_id="lead"))
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
    middlewares: list[AgentMiddleware] = [
        ToolErrorMiddleware(),
        DecisionTableGuardMiddleware(),
        DecisionActionReviewMiddleware(),
        SandboxAuditMiddleware(),
        DecisionTableMiddleware(actor_id="subagent"),
    ]
    return middlewares
