"""本文件对外提供承诺阶段决策行到统一改表操作的适配函数。

输入为承诺子图中的线程、用户、Run 及阶段2/3产物；输出为可追溯的改表操作。
工作流从阶段结果生成稳定行 ID，以 Agent 身份提交候选，不借用合同审批取得改表权。
示例：`operation = await propose_contract_table(state, artifacts)`。
"""

from __future__ import annotations

from typing import Any

from caspian.agents.commitment.decision_table import _build_body_rows
from caspian.decision_governance.adapter import service, to_rows
from caspian.decision_governance.policy import Actor


async def propose_contract_table(state: dict[str, Any], artifacts: dict[str, Any]):
    user_id = state.get("user_id")
    thread_id = state.get("thread_id")
    run_id = state.get("run_id")
    if not all((user_id, thread_id, run_id)):
        raise ValueError("承诺阶段缺少用户、会话或 Run，决策表未修改")
    table_service = service()
    key = f"commitment:{run_id}:stage7"
    previous = await table_service.find_by_key(str(user_id), str(thread_id), key)
    base = (
        await table_service.revision(str(user_id), str(thread_id), previous.base_revision)
        if previous else await table_service.current_for_submission(str(user_id), str(thread_id))
    )
    rows = to_rows(_build_body_rows(artifacts.get("2", {}), artifacts.get("3", {})))
    return await table_service.submit(
        user_id=str(user_id), thread_id=str(thread_id),
        actor=Actor("commitment", "agent"), run_id=str(run_id),
        source="commitment", idempotency_key=key, base_revision=base.revision,
        rows=rows, reason="承诺阶段2/3决策产物，随阶段7合同提交",
    )
