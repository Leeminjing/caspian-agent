"""目标模式（goal-mode）单元测试：配置、目标服务、自动推进、authority、工具工厂。"""

import asyncio
import json

from langchain_core.messages import HumanMessage
from langgraph.store.memory import InMemoryStore

from caspian.agents.goal import GoalModeMiddleware
from caspian.config.goal_mode_config import GoalModeConfig
from caspian.goal import (
    GoalRoundDriver,
    GoalService,
    build_goal_tools,
    goal_guidance,
)
from caspian.goal import authority, wrapup, prompt
from caspian.goal.domain import GoalError


def _run(coro):
    return asyncio.run(coro)


class TestGoalModeConfig:
    def test_defaults(self):
        cfg = GoalModeConfig()
        assert cfg.enabled is False
        assert cfg.default_max_goal_rounds == 256
        assert cfg.blocked_after_consecutive_rounds == 3

    def test_zero_cap_rejected(self):
        try:
            GoalModeConfig(enabled=True, default_max_goal_rounds=0)
        except ValueError:
            return
        raise AssertionError("default_max_goal_rounds<1 应被拒绝")

    def test_zero_threshold_rejected(self):
        try:
            GoalModeConfig(blocked_after_consecutive_rounds=0)
        except ValueError:
            return
        raise AssertionError("blocked_after_consecutive_rounds<1 应被拒绝")

    def test_unknown_key_rejected(self):
        try:
            GoalModeConfig(enabled=True, bogus="y")
        except ValueError:
            return
        raise AssertionError("未知键应被拒绝")


class TestGoalService:
    def _svc(self, cap=5):
        return GoalService(store=InMemoryStore(), user_id="u1", thread_id="t1", default_max_goal_rounds=cap)

    def test_create_defaults(self):
        svc = self._svc()
        g = _run(svc.create("ship the api", max_goal_rounds=3))
        assert (g.phase, g.revision, g.armed, g.rounds_started, g.max_goal_rounds) == ("active", 1, True, 0, 3)

    def test_default_cap_applied(self):
        svc = self._svc(cap=17)
        g = _run(svc.create("x"))
        assert g.max_goal_rounds == 17

    def test_create_over_active_rejected(self):
        svc = self._svc()
        _run(svc.create("a"))
        try:
            _run(svc.create("b"))
        except GoalError as exc:
            assert exc.code == "GOAL_ALREADY_EXISTS"
            return
        raise AssertionError("重叠创建应被拒绝")

    def test_stale_revision_rejected(self):
        svc = self._svc()
        g = _run(svc.create("a"))
        try:
            _run(svc.pause({"id": g.id, "revision": 99}))
        except GoalError as exc:
            assert exc.code == "GOAL_STALE_REVISION"
            return
        raise AssertionError("陈旧 revision 应被拒绝")

    def test_lifecycle_transitions(self):
        svc = self._svc()
        g = _run(svc.create("a", max_goal_rounds=3))
        g = _run(svc.edit({"id": g.id, "revision": 1}, objective="a2"))
        assert g.objective == "a2" and g.revision == 2
        g = _run(svc.pause({"id": g.id, "revision": 2}))
        assert g.phase == "paused" and not g.armed
        g = _run(svc.resume({"id": g.id, "revision": 3}))
        assert g.phase == "active" and g.armed and g.revision == 4

    def test_advance_round_no_revision_bump(self):
        svc = self._svc()
        g = _run(svc.create("a", max_goal_rounds=2))
        g = _run(svc.advance_round({"id": g.id, "revision": 1}))
        assert g.rounds_started == 1 and g.revision == 1
        g = _run(svc.advance_round({"id": g.id, "revision": 1}))
        assert g.rounds_started == 2 and g.revision == 1
        try:
            _run(svc.advance_round({"id": g.id, "revision": 1}))
        except GoalError as exc:
            assert exc.code == "GOAL_INVALID_TRANSITION"
            return
        raise AssertionError("超上限推进应被拒绝")

    def test_block_then_resume_at_cap_rejected(self):
        svc = self._svc()
        g = _run(svc.create("a", max_goal_rounds=1))
        _run(svc.advance_round({"id": g.id, "revision": 1}))
        g = _run(svc.block({"id": g.id, "revision": 1}, code="round-limit", message="limit"))
        assert g.phase == "blocked" and g.blocked_reason.code == "round-limit"
        try:
            _run(svc.resume({"id": g.id, "revision": 2}))
        except GoalError as exc:
            assert exc.code == "GOAL_INVALID_TRANSITION"
            return
        raise AssertionError("预算耗尽恢复应被拒绝")

    def test_clear_tombstone(self):
        svc = self._svc()
        g = _run(svc.create("a"))
        tomb = _run(svc.clear({"id": g.id, "revision": 1}))
        assert tomb.revision == 2
        assert _run(svc.get()) is None


class TestGoalRoundDriver:
    def test_continue_then_auto_block(self):
        store = InMemoryStore()
        svc = GoalService(store=store, user_id="u1", thread_id="t1", default_max_goal_rounds=2)
        driver = GoalRoundDriver(store, "u1", "t1", 2)
        _run(driver.disarm_on_run_start())
        g = _run(svc.create("ship", max_goal_rounds=2))

        assert _run(driver.decide_after_round()) == "continue"
        nxt = _run(driver.build_next_round_input())
        assert nxt["messages"][0].content.startswith("<goal_round>")
        marker = nxt["messages"][0].additional_kwargs["goal_round"]
        assert marker["round"] == 1 and marker["goal_id"] == g.id

        assert _run(driver.decide_after_round()) == "continue"
        nxt2 = _run(driver.build_next_round_input())
        assert nxt2["messages"][0].additional_kwargs["goal_round"]["round"] == 2

        assert _run(driver.decide_after_round()) == "stop"
        g2 = _run(svc.get())
        assert g2 is not None and g2.phase == "blocked" and g2.blocked_reason.code == "round-limit"

    def test_complete_stops(self):
        store = InMemoryStore()
        svc = GoalService(store=store, user_id="u1", thread_id="t1")
        driver = GoalRoundDriver(store, "u1", "t1", 5)
        g = _run(svc.create("ship", max_goal_rounds=5))
        _run(svc.complete({"id": g.id, "revision": 1}))
        assert _run(driver.decide_after_round()) == "stop"

    def test_run_start_disarm(self):
        store = InMemoryStore()
        svc = GoalService(store=store, user_id="u1", thread_id="t1")
        driver = GoalRoundDriver(store, "u1", "t1", 5)
        g = _run(svc.create("ship", max_goal_rounds=5))
        _run(driver.disarm_on_run_start())
        assert _run(svc.get()).armed is False


class TestGoalAuthority:
    def test_direct_human(self):
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="build it")]
        assert authority.is_direct_human(msgs) is True

    def test_goal_round_match(self):
        from langchain_core.messages import HumanMessage
        store = InMemoryStore()
        svc = GoalService(store=store, user_id="u1", thread_id="t1")
        goal = _run(svc.create("x"))
        msgs = [HumanMessage(content="<goal_round>", additional_kwargs={
            "goal_round": {"goal_id": goal.id, "revision": goal.revision, "round": 0},
        })]
        assert authority.is_direct_human(msgs) is False
        assert authority.is_matching_goal_round(msgs, goal) is True
        assert authority.completion_authority(msgs, goal)["kind"] == "goal-round"

    def test_stale_round_unknown(self):
        from langchain_core.messages import HumanMessage
        store = InMemoryStore()
        svc = GoalService(store=store, user_id="u1", thread_id="t1")
        goal = _run(svc.create("x"))
        msgs = [HumanMessage(content="<goal_round>", additional_kwargs={
            "goal_round": {"goal_id": goal.id, "revision": goal.revision, "round": 99},
        })]
        assert authority.completion_authority(msgs, goal)["kind"] == "unknown"


class TestGoalToolsAndText:
    def test_tool_names(self):
        tools = build_goal_tools(3)
        assert [t.name for t in tools] == ["get_goal", "create_goal", "update_goal"]

    def test_guidance(self):
        text = goal_guidance(3)
        assert "create_goal" in text and "blocked" in text and "3" in text

    def test_wrapup(self):
        assert wrapup.render_wrapup_context("x").startswith("<goal_complete>")
        assert wrapup.render_wrapup_context("x", "blocked").startswith("<goal_blocked>")

    def test_prompt(self):
        assert prompt.render_goal_round_prompt("x", 1, 7).startswith("<goal_round>")


class TestGoalCommandParser:
    def test_parse_command(self):
        from caspian.agents.goal.middleware import _parse_goal_command
        assert _parse_goal_command("/goal") == ("show", None)
        assert _parse_goal_command("/goal build the api") == ("create", "build the api")
        assert _parse_goal_command("/goal edit build the api") == ("edit", "build the api")
        assert _parse_goal_command("/goal pause") == ("pause", None)
        assert _parse_goal_command("/goal resume") == ("resume", None)
        assert _parse_goal_command("/goal clear") == ("clear", None)
        assert _parse_goal_command("hello") is None
        assert _parse_goal_command("/skill /goal build it", frozenset({"skill"})) == ("create", "build it")


class _FakeToolRuntime:
    """模拟 ToolRuntime / Runtime，提供 goal 工具与中间件所需的 store/config/context/state。"""

    def __init__(self, store, user_id="u1", thread_id="t1", messages=None):
        self.store = store
        self.config = {"configurable": {"thread_id": thread_id}}
        self.context = {"user_id": user_id}
        self.state = {"messages": messages or []}


def _goal_round_state(goal_id, revision, round_no):
    """构造携带 goal-round 标记的 state（最后一条 HumanMessage 带标记）。"""
    from langchain_core.messages import HumanMessage
    return {"messages": [HumanMessage(content="<goal_round>", additional_kwargs={
        "goal_round": {"goal_id": goal_id, "revision": revision, "round": round_no},
    })]}


class TestGoalToolsExecution:
    """用真实 InMemoryStore + 假 runtime 驱动 get_goal/create_goal/update_goal 的执行路径。"""

    def _tools(self, blocked=3):
        return build_goal_tools(blocked)

    def test_get_goal(self):
        store = InMemoryStore()
        _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        tools = self._tools()
        rt = _FakeToolRuntime(store)
        result = json.loads(_run(tools[0].coroutine(rt)))
        assert result["goal"]["objective"] == "x"

    def test_get_goal_none(self):
        store = InMemoryStore()
        tools = self._tools()
        result = json.loads(_run(tools[0].coroutine(_FakeToolRuntime(store))))
        assert result == {"goal": None}

    def test_create_goal_direct_human(self):
        store = InMemoryStore()
        tools = self._tools()
        rt = _FakeToolRuntime(store, messages=[HumanMessage(content="build it")])
        result = json.loads(_run(tools[1].coroutine("build the api", rt)))
        assert result["goal"]["phase"] == "active" and result["activation"] == "armed"

    def test_create_goal_rejects_goal_round(self):
        store = InMemoryStore()
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        tools = self._tools()
        rt = _FakeToolRuntime(store, messages=[]); rt.state = _goal_round_state(g.id, g.revision, 0)
        try:
            _run(tools[1].coroutine("y", rt))
        except GoalError as exc:
            assert exc.code == "GOAL_TOOL_AUTHORITY_REQUIRED"
            return
        raise AssertionError("create_goal 应拒绝 goal 回合")

    def test_pause_rejected_in_goal_round(self):
        store = InMemoryStore()
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        tools = self._tools()
        rt = _FakeToolRuntime(store, messages=[]); rt.state = _goal_round_state(g.id, g.revision, 0)
        try:
            _run(tools[2].coroutine(g.id, g.revision, "pause", rt, tool_call_id="c1"))
        except GoalError as exc:
            assert exc.code == "GOAL_TOOL_AUTHORITY_REQUIRED"
            return
        raise AssertionError("goal 回合应禁止 pause")

    def test_blocked_before_threshold_rejected(self):
        store = InMemoryStore()
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        tools = self._tools(blocked=3)
        rt = _FakeToolRuntime(store, messages=[]); rt.state = _goal_round_state(g.id, g.revision, 0)
        try:
            _run(tools[2].coroutine(g.id, g.revision, "blocked", rt, blocked_reason="stuck", tool_call_id="c1"))
        except GoalError as exc:
            assert exc.code == "GOAL_TOOL_BLOCK_THRESHOLD"
            return
        raise AssertionError("提前 blocked 应被拒")

    def test_complete_in_goal_round_returns_wrapup(self):
        store = InMemoryStore()
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        tools = self._tools()
        rt = _FakeToolRuntime(store, messages=[]); rt.state = _goal_round_state(g.id, g.revision, 0)
        result = _run(tools[2].coroutine(g.id, g.revision, "complete", rt, tool_call_id="c1"))
        assert "<goal_complete>" in result and '"phase": "complete"' in result

    def test_edit_direct_human(self):
        store = InMemoryStore()
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        tools = self._tools()
        rt = _FakeToolRuntime(store, messages=[HumanMessage(content="edit it")])
        result = json.loads(_run(tools[2].coroutine(g.id, g.revision, "edit", rt, objective="x2", tool_call_id="c1")))
        assert result["goal"]["objective"] == "x2"


class TestGoalMiddlewareExecution:
    """用真实 InMemoryStore + 假 runtime 驱动 GoalModeMiddleware 的命令执行路径。"""

    def _mw(self):
        return GoalModeMiddleware(GoalModeConfig(enabled=True))

    def test_create(self):
        store = InMemoryStore()
        rt = _FakeToolRuntime(store)
        state = {"messages": [HumanMessage(content="/goal build the api", id="m1")]}
        result = _run(self._mw().abefore_agent(state, rt))
        assert result is not None and result["messages"][0].content.startswith("Goal created")
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").get())
        assert g is not None and g.objective == "build the api"

    def test_pause(self):
        store = InMemoryStore()
        g = _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        rt = _FakeToolRuntime(store)
        state = {"messages": [HumanMessage(content="/goal pause", id="m2")]}
        result = _run(self._mw().abefore_agent(state, rt))
        assert result["messages"][0].content.startswith("Goal paused")
        assert "Status: paused" in result["messages"][0].content
        assert _run(GoalService(store=store, user_id="u1", thread_id="t1").get()).phase == "paused"

    def test_resume(self):
        store = InMemoryStore()
        svc = GoalService(store=store, user_id="u1", thread_id="t1")
        g = _run(svc.create("x"))
        _run(svc.pause({"id": g.id, "revision": 1}))
        rt = _FakeToolRuntime(store)
        state = {"messages": [HumanMessage(content="/goal resume", id="m3")]}
        result = _run(self._mw().abefore_agent(state, rt))
        assert result["messages"][0].content.startswith("Goal resumed")
        assert "Status: active" in result["messages"][0].content
        assert _run(svc.get()).phase == "active"

    def test_clear(self):
        store = InMemoryStore()
        _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        rt = _FakeToolRuntime(store)
        state = {"messages": [HumanMessage(content="/goal clear", id="m4")]}
        result = _run(self._mw().abefore_agent(state, rt))
        assert result["messages"][0].content == "Goal cleared."
        assert _run(GoalService(store=store, user_id="u1", thread_id="t1").get()) is None

    def test_non_command_not_intercepted(self):
        store = InMemoryStore()
        rt = _FakeToolRuntime(store)
        state = {"messages": [HumanMessage(content="hello", id="m5")]}
        assert _run(self._mw().abefore_agent(state, rt)) is None

    def test_create_over_active_rejected(self):
        store = InMemoryStore()
        _run(GoalService(store=store, user_id="u1", thread_id="t1").create("x"))
        rt = _FakeToolRuntime(store)
        state = {"messages": [HumanMessage(content="/goal build the api", id="m6")]}
        result = _run(self._mw().abefore_agent(state, rt))
        assert "already" in result["messages"][0].content
