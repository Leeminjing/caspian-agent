"""本文件验证界面改表 Run 的处理中、待审批、恢复及终态可观察。

输入为带编辑载荷的 Run 和绑定原 Run 的恢复请求；输出为持久操作、SSE 中断及 Run 状态。
工作流使用真实 Agent 图、checkpoint 和治理服务，替换外部模型与流传输以核对执行状态。
示例：`pytest tests/test_decision_run_api.py`。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from caspian.agents.middlewares.decision_table_edit_middleware import DecisionTableEditMiddleware
from caspian.decision_governance.convergence_runtime import branch_config
from caspian.decision_governance.policy import Actor, default_policy
from caspian.decision_governance.service import DecisionTableService
from caspian.decision_governance.run_audit import create_run_audit, get_run_audit, reconcile_incomplete_run_audits, update_run_audit
from caspian.persistence.base import Base
from caspian.runtime.runs.manager import RunManager
from caspian.runtime.runs.schemas import RunStatus
from caspian.runtime.runs.worker import run_agent


class CapturedBridge:
    def __init__(self):
        self.events = []

    def publish(self, run_id, event):
        self.events.append((run_id, event))

    def publish_end(self, run_id):
        self.events.append((run_id, "end"))

    def cleanup(self, _run_id, delay=300):
        return None


def test_ui_change_run_waits_for_bound_approval_then_finishes(tmp_path):
    async def scenario():
        database = tmp_path / "decision-run.sqlite"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async def no_findings(_candidate, _current):
            return ()
        table_service = DecisionTableService(factory, checker=no_findings)
        owner = Actor("u", "user")
        policy = default_policy("u")
        policy["subjects"]["user:u"] = {
            "view": ["add"], "propose": ["add"], "approve": ["add"],
        }
        await table_service.set_policy(user_id="u", thread_id="t", actor=owner, policy=policy)
        saver = InMemorySaver()
        graph = create_agent(
            FakeListChatModel(responses=["unused"]), tools=[],
            middleware=[DecisionTableEditMiddleware()], checkpointer=saver,
        )
        manager = RunManager()
        bridge = CapturedBridge()
        config = SimpleNamespace(
            models=[SimpleNamespace(name="fake")],
            commitment=SimpleNamespace(enabled=False),
            subagents=SimpleNamespace(enabled=False),
            goal_mode=SimpleNamespace(enabled=False),
        )
        first = manager.create(thread_id="t")
        input_message = HumanMessage(
            id="edit-request", content="改表",
            additional_kwargs={"decision_table_edit": {
                "base_revision": 0, "reason": "用户修改具体要求",
                "rows": [{"requirement": "必须加密", "decision": "保留", "priority": 3}],
            }},
        )
        async def run(record, graph_input, origin):
            await create_run_audit(
                factory, user_id="u", thread_id="t", run_id=record.run_id,
                origin_run_id=origin, kind="decision_edit",
            )
            async def persist(changed):
                await update_run_audit(
                    factory, user_id="u", thread_id="t",
                    run_id=changed.run_id, status=changed.status.value,
                )
                if origin != changed.run_id:
                    await update_run_audit(
                        factory, user_id="u", thread_id="t",
                        run_id=origin, status=changed.status.value,
                    )
            await run_agent(
                record=record, bridge=bridge, run_manager=manager,
                app_config=config, graph_input=graph_input,
                runnable_config={"configurable": {
                    **branch_config("t", origin)["configurable"], "run_id": record.run_id,
                }},
                stream_modes=["values"],
                langgraph_context={
                    "user_id": "u", "thread_id": "t", "run_id": record.run_id,
                    "origin_run_id": origin, "selected_skills": [],
                },
                checkpointer=saver,
                status_sink=persist,
            )
        try:
            with patch("caspian.runtime.runs.worker.make_lead_agent", new=AsyncMock(return_value=graph)), patch(
                "caspian.agents.middlewares.decision_table_edit_middleware.service", return_value=table_service,
            ):
                await run(first, {"messages": [input_message]}, first.run_id)
                assert first.status == RunStatus.waiting_approval
                assert (await get_run_audit(factory, user_id="u", thread_id="t", run_id=first.run_id)).status == "waiting_approval"
                operation = (await table_service.history("u", "t"))[0]
                assert operation.run_id == first.run_id and operation.status == "awaiting_approval"
                assert (await table_service.current("u", "t", owner)).revision == 0
                assert any(
                    getattr(event, "event", None) == "interrupt"
                    and event.data["value"].get("operation_id") == operation.operation_id
                    for _, event in bridge.events
                )
                resumed = manager.create(thread_id="t")
                await run(resumed, Command(resume={"decision": "adopt"}), first.run_id)
                assert resumed.status == RunStatus.success
                assert first.status == RunStatus.success
                assert (await table_service.history("u", "t"))[0].status == "committed"
                assert (await table_service.current("u", "t", owner)).revision == 1
                assert (await get_run_audit(factory, user_id="u", thread_id="t", run_id=first.run_id)).status == "success"
                persisted_run_id = first.run_id
        finally:
            await engine.dispose()
        reopened = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            restarted_factory = async_sessionmaker(reopened, expire_on_commit=False)
            restored = await get_run_audit(
                restarted_factory, user_id="u", thread_id="t", run_id=persisted_run_id,
            )
            assert restored.status == "success" and restored.kind == "decision_edit"
        finally:
            await reopened.dispose()
    asyncio.run(scenario())


def test_restart_marks_unfinished_runs_without_changing_waiting_approval(tmp_path):
    async def scenario():
        database = tmp_path / "run-recovery.sqlite"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(
                sync, tables=[table for table in Base.metadata.tables.values() if table.name.startswith("decision_table_")]
            ))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        for run_id, status in (("pending", "pending"), ("running", "running"), ("approval", "waiting_approval")):
            await create_run_audit(factory, user_id="u", thread_id="t", run_id=run_id, origin_run_id=run_id, kind="decision_edit")
            await update_run_audit(factory, user_id="u", thread_id="t", run_id=run_id, status=status)
        await engine.dispose()
        reopened = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            restored_factory = async_sessionmaker(reopened, expire_on_commit=False)
            assert await reconcile_incomplete_run_audits(restored_factory) == 2
            for run_id in ("pending", "running"):
                restored = await get_run_audit(restored_factory, user_id="u", thread_id="t", run_id=run_id)
                assert restored.status == "error" and "进程重启" in restored.error
            waiting = await get_run_audit(restored_factory, user_id="u", thread_id="t", run_id="approval")
            assert waiting.status == "waiting_approval"
        finally:
            await reopened.dispose()
    asyncio.run(scenario())
