"""
本文件对外提供 DelegationLedgerMiddleware：委派账本的模型可见注入。

对外提供:
    DelegationLedgerMiddleware(AgentMiddleware) — before_model 钩子注入渲染账本 SystemMessage

输入:
    state: AgentState — 当前图状态（含 delegations 账本）
    runtime: Runtime — 运行时（未使用，仅签名对齐）

输出:
    dict | None — messages 状态增量（追加账本 SystemMessage）；账本为空返回 None

具体工作流:
    (1) 每次模型调用前从 state.delegations 渲染账本
    (2) 账本非空 → 以 SystemMessage 追加到消息尾部
    (3) 账本为空 → 不追加

示例:
    middleware = DelegationLedgerMiddleware()
"""

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import SystemMessage
from typing_extensions import override

from caspian.agents.middlewares.delegation_ledger import render_delegation_ledger

logger = logging.getLogger(__name__)


class DelegationLedgerMiddleware(AgentMiddleware[AgentState]):
    """每次模型调用前把委派账本渲染为 SystemMessage 注入消息尾部。"""

    def _inject_ledger(self, state: AgentState) -> dict | None:
        rendered = render_delegation_ledger(list(state.get("delegations", [])))
        if not rendered:
            return None
        return {"messages": [SystemMessage(content=rendered)]}

    @override
    def before_model(self, state: AgentState, runtime) -> dict | None:
        return self._inject_ledger(state)

    @override
    async def abefore_model(self, state: AgentState, runtime) -> dict | None:
        return self._inject_ledger(state)
