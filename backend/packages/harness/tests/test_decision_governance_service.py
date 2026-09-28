"""本文件验证决策表治理服务的原子提交、权限、审批与幂等行为。

输入为隔离 SQLite 数据库中的认证主体和候选表；输出为表头、操作、事件的一致性断言。
工作流模拟正常提交、提交前失败、重复请求及审批期间基准变化。
示例：`pytest tests/test_decision_governance_service.py`。
"""

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.decision_governance.domain import Row
from caspian.decision_governance.events import deliver_pending, read_events
from caspian.decision_governance.legacy import LegacyTable
from caspian.decision_governance.operations import transition
from caspian.decision_governance.policy import Actor, default_policy
from caspian.decision_governance.service import DecisionTableService, IdempotencyConflict
from caspian.persistence.base import Base


async def _fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async def no_findings(_candidate, _current):
        return ()
    return engine, DecisionTableService(factory, checker=no_findings)


def _submit(service, *, key="k", base=0, actor=None, rows=None):
    return service.submit(
        user_id="u", thread_id="t", actor=actor or Actor("u", "user"),
        run_id="run-1", source="ui", idempotency_key=key,
        base_revision=base, rows=rows or (Row("r", "要求", "保留", 2),), reason="需求变更",
    )


def test_atomic_success_idempotency_and_failure():
    async def scenario():
        engine, service = await _fixture()
        try:
            first = await _submit(service)
            assert first.status == "committed"
            assert first.result_revision == 1
            assert (await _submit(service)).operation_id == first.operation_id
            assert (await service.current("u", "t", Actor("u", "user"))).revision == 1
            assert [event.event_type for event in await service.operation_events(first.operation_id)] == ["processing", "committed"]
            detail = await service.operation_detail("u", "t", first.operation_id)
            assert detail["operation"].run_id == "run-1"
            assert detail["before"].revision == 0
            assert detail["after"].revision == 1
            with pytest.raises(IdempotencyConflict):
                await _submit(service, rows=(Row("r", "另一要求", "保留", 2),))

            async def fail_before_commit(*_args):
                raise RuntimeError("simulated failure")

            with patch("caspian.decision_governance.service.commit_candidate", fail_before_commit):
                with pytest.raises(RuntimeError):
                    await _submit(service, key="k2", base=1, rows=(Row("r", "要求", "保留", 3),))
            assert (await service.current("u", "t", Actor("u", "user"))).revision == 1
            assert [op.operation_id for op in await service.history("u", "t")] == [first.operation_id]
            with pytest.raises(RuntimeError, match="after commit"):
                await _submit(service, key="k3", base=1, rows=(Row("r", "要求", "保留", 3),))
                raise RuntimeError("after commit")
            assert (await service.current("u", "t", Actor("u", "user"))).revision == 2
            assert len(await service.history("u", "t")) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_proposal_waits_and_stale_approval_cannot_commit():
    async def scenario():
        engine, service = await _fixture()
        try:
            owner = Actor("u", "user")
            policy = default_policy("u")
            policy["subjects"]["agent:a"] = {"view": ["add"], "propose": ["add"]}
            await service.set_policy(user_id="u", thread_id="t", actor=owner, policy=policy)
            proposal = await _submit(service, actor=Actor("a", "agent"))
            assert proposal.status == "awaiting_approval"
            assert (await service.current("u", "t", owner)).revision == 0
            winner = await _submit(service, key="other", rows=(Row("other", "其他", "保留", 2),))
            assert winner.status == "committed"
            stale = await service.decide(operation_id=proposal.operation_id, actor=owner, decision="approve", reason="同意")
            assert stale.status == "version_conflict"
            events = await service.operation_events(proposal.operation_id)
            assert events[-1].payload["intervening_changes"][0]["kind"] == "add"
            assert (await service.current("u", "t", owner)).revision == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_read_only_actor_is_rejected_and_terminal_state_cannot_transition():
    async def scenario():
        engine, service = await _fixture()
        try:
            owner = Actor("u", "user")
            policy = default_policy("u")
            policy["subjects"]["agent:reader"] = {"view": ["add"]}
            await service.set_policy(user_id="u", thread_id="t", actor=owner, policy=policy)
            rejected = await _submit(service, actor=Actor("reader", "agent"))
            assert rejected.status == "rejected"
            assert (await service.current("u", "t", owner)).revision == 0
            assert [event.event_type for event in await service.operation_events(rejected.operation_id)] == ["processing", "rejected"]
            with pytest.raises(ValueError):
                transition("rejected", "committed")
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_outbox_retries_without_duplicate_session_events():
    async def scenario():
        engine, service = await _fixture()
        try:
            operation = await _submit(service)
            factory = service._session_factory
            assert await deliver_pending(factory) == 2
            assert await deliver_pending(factory) == 0
            events = await read_events(factory, "u", "t")
            assert [event.event_type for event in events] == ["processing", "committed"]
            assert all(event.operation_id == operation.operation_id for event in events)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_legacy_genesis_has_auditable_operation():
    async def scenario():
        engine, service = await _fixture()
        try:
            legacy = LegacyTable(
                path="legacy/decision-table.md", file_hash="a" * 64,
                legacy_version="old-v1", format=1,
                rows=(Row("old", "旧要求", "保留", 2),),
            )
            with patch("caspian.decision_governance.repository.load_legacy", return_value=legacy):
                current = await service.current("u", "t", Actor("u", "user"))
            assert current.revision == 0
            assert current.rows == legacy.rows
            history = await service.history("u", "t")
            assert len(history) == 1
            assert history[0].source == "legacy_import"
            assert history[0].status == "committed"
            assert history[0].checks[0]["file_hash"] == legacy.file_hash
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_check_failure_is_recorded_without_changing_table():
    async def scenario():
        engine, service = await _fixture()
        try:
            async def broken(_candidate, _current):
                raise RuntimeError("scan unavailable")
            service._checker = broken
            operation = await _submit(service)
            assert operation.status == "failed"
            assert "scan unavailable" in operation.error
            assert (await service.current("u", "t", Actor("u", "user"))).revision == 0
            assert [event.event_type for event in await service.operation_events(operation.operation_id)] == ["processing", "failed"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_permission_update_invalidates_pending_approval():
    async def scenario():
        engine, service = await _fixture()
        try:
            owner = Actor("u", "user")
            policy = default_policy("u")
            policy["subjects"]["agent:a"] = {"propose": ["add"]}
            await service.set_policy(user_id="u", thread_id="t", actor=owner, policy=policy)
            operation = await _submit(service, actor=Actor("a", "agent"))
            assert operation.status == "awaiting_approval"
            await service.set_policy(user_id="u", thread_id="t", actor=owner, policy=default_policy("u"))
            detail = await service.operation_detail("u", "t", operation.operation_id)
            assert detail["operation"].status == "version_conflict"
            assert detail["approvals"] == []
            with pytest.raises(ValueError):
                await service.decide(operation_id=operation.operation_id, actor=owner, decision="approve", reason="同意")
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_authorized_owner_can_delete_and_lower_old_decisions_with_audit():
    async def scenario():
        engine, service = await _fixture()
        try:
            owner = Actor("u", "user")
            first = await _submit(service, rows=(Row("r", "旧高等级要求", "保留", 3),))
            lowered = await _submit(
                service, key="lower", base=1,
                rows=(Row("r", "旧高等级要求", "保留", 1),),
            )
            assert lowered.status == "committed"
            assert {change["kind"] for change in lowered.changes} == {"change_priority"}
            removed = await service.submit(
                user_id="u", thread_id="t", actor=owner, run_id="run-remove",
                source="ui", idempotency_key="remove", base_revision=2,
                rows=(), reason="用户撤销旧要求",
            )
            assert removed.status == "committed"
            assert {change["kind"] for change in removed.changes} == {"delete"}
            assert (await service.current("u", "t", owner)).rows == ()
            assert [item.operation_id for item in await service.history("u", "t")] == [
                first.operation_id, lowered.operation_id, removed.operation_id,
            ]
            detail = await service.operation_detail("u", "t", removed.operation_id)
            assert detail["before"].revision == 2
            assert detail["after"].revision == 3
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_stale_submission_exposes_intervening_diff_and_cannot_overwrite():
    async def scenario():
        engine, service = await _fixture()
        try:
            winner = await _submit(service, key="winner", rows=(Row("r", "第一个修改", "保留", 3),))
            stale = await _submit(service, key="stale", rows=(Row("s", "第二个修改", "保留", 3),))
            assert winner.status == "committed"
            assert stale.status == "version_conflict"
            assert (await service.current("u", "t", Actor("u", "user"))).revision == 1
            events = await service.operation_events(stale.operation_id)
            assert events[-1].payload["current_revision"] == 1
            assert events[-1].payload["intervening_changes"][0]["kind"] == "add"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_approved_operation_event_contains_bound_request_and_approval():
    async def scenario():
        engine, service = await _fixture()
        try:
            owner = Actor("u", "user")
            policy = default_policy("u")
            policy["subjects"]["agent:a"] = {"propose": ["add"]}
            await service.set_policy(user_id="u", thread_id="t", actor=owner, policy=policy)
            proposal = await _submit(service, actor=Actor("a", "agent"))
            approved = await service.decide(
                operation_id=proposal.operation_id, actor=owner,
                decision="approve", reason="批准这个具体候选",
            )
            assert approved.status == "committed"
            events = await service.operation_events(proposal.operation_id)
            assert events[0].payload["request"]["candidate_hash"] == approved.candidate_hash
            assert events[-1].payload["submission_mode"] == "approved"
            assert events[-1].payload["approval"]["reason"] == "批准这个具体候选"
            assert events[-1].payload["approval"]["base_revision"] == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())
