"""本文件对外提供两阶段动作记录的登记、条件状态迁移、原子执行认领与查询。

输入为绑定的工具调用、当前表修订、上下文及结构化结论；输出为可追溯的动作状态。
工作流以 Run、tool_call_id、参数、主体、表修订和上下文去重；对权威表修订做事务内核对后认领执行，结果写回同一记录。
示例：`review = await register_review(factory, ...)`。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from caspian.decision_governance.models import TableActionReview, TableHead
from caspian.decision_governance.review_protocol import ActionBinding

_NEXT = {
    "risk_pending": {"risk_pending", "low_ready", "review_pending", "stale"},
    "low_ready": {"executing", "paused", "stale"},
    "review_pending": {"review_pending", "reviewed", "paused", "stale"},
    "reviewed": {"reviewed", "executing", "awaiting_human", "modified", "cancelled", "paused", "stale"},
    "awaiting_human": {"reviewed", "cancelled", "stale"},
    "paused": {"stale"},
    "executing": {"executed", "execution_failed", "indeterminate"},
    "pending_review": {"risk_pending", "stale"},
}


def _check_transition(current: str, target: str) -> None:
    if target not in _NEXT.get(current, set()):
        raise ValueError(f"非法动作状态跃迁：{current} → {target}")


async def register_review(
    factory: Callable[[], AsyncSession], *, user_id: str, thread_id: str,
    run_id: str, tool_call_id: str, binding: ActionBinding, content_hash: str,
    action_args: dict,
) -> TableActionReview:
    async with factory() as session, session.begin():
        prior = await session.scalar(select(TableActionReview).where(
            TableActionReview.run_id == run_id,
            TableActionReview.tool_call_id == tool_call_id,
            TableActionReview.status.in_(("executed", "executing", "execution_failed", "indeterminate")),
        ).order_by(TableActionReview.created_at.desc(), TableActionReview.review_id.desc()).limit(1))
        if prior is not None:
            if prior.action_name != binding.action_name or prior.args_hash != binding.args_hash or prior.actor_id != binding.actor_id:
                raise RuntimeError("已执行的 tool_call_id 不能改动作或参数，请提交新的工具调用")
            return prior
        existing = await session.scalar(select(TableActionReview).where(
            TableActionReview.run_id == run_id,
            TableActionReview.tool_call_id == tool_call_id,
            TableActionReview.action_name == binding.action_name,
            TableActionReview.args_hash == binding.args_hash,
            TableActionReview.actor_id == binding.actor_id,
            TableActionReview.table_revision == binding.table_revision,
            TableActionReview.context_hash == binding.context_hash,
        ))
        if existing is not None:
            if existing.status == "stale":
                existing.status = "risk_pending"
                existing.conclusion = None
                existing.risk_outcome = None
                existing.risk_reason = None
                existing.error = None
            return existing
        stale = (await session.scalars(select(TableActionReview).where(
            TableActionReview.run_id == run_id,
            TableActionReview.tool_call_id == tool_call_id,
            TableActionReview.status.in_(("risk_pending", "low_ready", "review_pending", "pending_review", "reviewed", "awaiting_human", "paused")),
        ).with_for_update())).all()
        for item in stale:
            item.status = "stale"
            item.error = "待执行动作、参数、主体、上下文或决策表修订已变化，需要重新复核"
        review = TableActionReview(
            review_id=str(uuid4()), user_id=user_id, thread_id=thread_id,
            run_id=run_id, tool_call_id=tool_call_id,
            action_name=binding.action_name, args_hash=binding.args_hash,
            action_args=action_args,
            actor_id=binding.actor_id, table_revision=binding.table_revision,
            content_hash=content_hash, context_hash=binding.context_hash,
            trigger_reason="action_risk", status="risk_pending",
            created_at=datetime.now(timezone.utc),
        )
        session.add(review)
        return review


async def update_review(
    factory: Callable[[], AsyncSession], review_id: str, *,
    status: str, conclusion: dict | None = None, result: dict | None = None,
    error: str | None = None, risk_outcome: str | None = None,
    risk_reason: str | None = None,
) -> None:
    async with factory() as session, session.begin():
        review = await session.get(TableActionReview, review_id)
        if review is None:
            raise RuntimeError("动作复核记录缺失")
        _check_transition(review.status, status)
        review.status = status
        if conclusion is not None:
            review.conclusion = conclusion
        if result is not None:
            review.result = result
        if risk_outcome is not None:
            review.risk_outcome = risk_outcome
            review.risk_checked_at = datetime.now(timezone.utc)
        if risk_reason is not None:
            review.risk_reason = risk_reason
        review.error = error
        if status in {"executed", "execution_failed", "indeterminate", "modified", "cancelled"}:
            review.completed_at = datetime.now(timezone.utc)


async def transition_review(
    factory: Callable[[], AsyncSession], review_id: str, *,
    expected_status: str, status: str, conclusion: dict | None = None,
    error: str | None = None, risk_outcome: str | None = None,
    risk_reason: str | None = None,
) -> bool:
    _check_transition(expected_status, status)
    values = {"status": status, "error": error}
    if conclusion is not None:
        values["conclusion"] = conclusion
    if risk_outcome is not None:
        values["risk_outcome"] = risk_outcome
        values["risk_checked_at"] = datetime.now(timezone.utc)
    if risk_reason is not None:
        values["risk_reason"] = risk_reason
    async with factory() as session, session.begin():
        result = await session.execute(update(TableActionReview).where(
            TableActionReview.review_id == review_id,
            TableActionReview.status == expected_status,
        ).values(**values))
        return result.rowcount == 1


async def claim_execution(
    factory: Callable[[], AsyncSession], review_id: str, expected_status: str,
) -> bool:
    _check_transition(expected_status, "executing")
    async with factory() as session, session.begin():
        review = await session.get(TableActionReview, review_id)
        if review is None or review.status != expected_status:
            return False
        head = await session.scalar(select(TableHead).where(
            TableHead.user_id == review.user_id,
            TableHead.thread_id == review.thread_id,
        ).with_for_update())
        if head is None or head.revision != review.table_revision or head.content_hash != review.content_hash:
            review.status = "stale"
            review.error = "执行前决策表版本已变化，需要重新判断"
            return False
        result = await session.execute(update(TableActionReview).where(
            TableActionReview.review_id == review_id,
            TableActionReview.status == expected_status,
        ).values(status="executing"))
        return result.rowcount == 1


async def get_review(factory: Callable[[], AsyncSession], review_id: str) -> TableActionReview:
    async with factory() as session:
        review = await session.get(TableActionReview, review_id)
        if review is None:
            raise RuntimeError("动作复核记录缺失")
        return review


async def read_reviews(
    factory: Callable[[], AsyncSession], user_id: str, thread_id: str,
) -> list[TableActionReview]:
    async with factory() as session:
        return list((await session.scalars(select(TableActionReview).where(
            TableActionReview.user_id == user_id,
            TableActionReview.thread_id == thread_id,
        ).order_by(TableActionReview.created_at, TableActionReview.review_id))).all())
