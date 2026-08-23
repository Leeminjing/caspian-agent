"""计划模式（plan-mode）单元测试：配置校验、命令拦截、策略段注入、评审退出工具。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from caspian.agents.lead_agent_state import LeadAgentState
from caspian.agents.plan import PlanModeMiddleware, build_exit_plan_mode_tool
from caspian.agents.plan.middleware import _match_plan_command
from caspian.config.plan_mode_config import PlanModeConfig


def _run(coro):
    return asyncio.run(coro)


class _FakeRuntime:
    def __init__(self, ctx=None, state=None):
        self.context = ctx or {"run_id": "r1", "user_id": "u1"}
        self.execution_info = type("Exec", (), {"thread_id": "t1"})()
        self.state = state or {"plan_active": False}


class _FakeRequest:
    def __init__(self, state, system_message):
        self.state = state
        self.system_message = system_message

    def override(self, **kwargs):
        return _FakeRequest(self.state, kwargs.get("system_message", self.system_message))


def _cfg():
    return PlanModeConfig(enabled=True, section="You are in plan mode. Explore first.")


class TestPlanModeConfig:
    def test_valid(self):
        cfg = _cfg()
        assert cfg.enabled is True
        assert cfg.section.strip().startswith("You are in plan mode")

    def test_empty_section_rejected(self):
        try:
            PlanModeConfig(enabled=True, section="   ")
        except ValueError:
            return
        raise AssertionError("空 section 应当被拒绝")

    def test_unknown_key_rejected(self):
        try:
            PlanModeConfig(enabled=True, section="x", bogus="y")
        except ValueError:
            return
        raise AssertionError("未知键应当被拒绝")


class TestMatchPlanCommand:
    def test_bare_plan(self):
        assert _match_plan_command("/plan", frozenset()) == ("on", None)

    def test_plan_off(self):
        assert _match_plan_command("/plan off", frozenset()) == ("off", None)

    def test_plan_message(self):
        assert _match_plan_command("/plan build the api", frozenset()) == (
            "on",
            "build the api",
        )

    def test_not_a_command(self):
        assert _match_plan_command("implement the feature", frozenset()) is None


class TestPlanModeMiddleware:
    def _mw(self):
        return PlanModeMiddleware(_cfg())

    def test_plan_on(self):
        mw = self._mw()
        state = {"messages": [HumanMessage(content="/plan", id="m1")]}
        update = _run(mw.abefore_agent(state, _FakeRuntime()))
        assert update["plan_active"] is True
        assert update["messages"][-1].content == "Plan mode on. Use /plan off to leave."

    def test_plan_off(self):
        mw = self._mw()
        state = {"messages": [HumanMessage(content="/plan off", id="m2")]}
        update = _run(mw.abefore_agent(state, _FakeRuntime()))
        assert update["plan_active"] is False
        assert update["messages"][-1].content == "Plan mode off."

    def test_plan_message(self):
        mw = self._mw()
        state = {"messages": [HumanMessage(content="/plan do research", id="m3")]}
        update = _run(mw.abefore_agent(state, _FakeRuntime()))
        assert update["plan_active"] is True
        assert update["messages"][-1].content == "do research"

    def test_plan_off_with_images_rejected(self):
        mw = self._mw()
        msg = HumanMessage(content="/plan off", id="m4")
        msg.additional_kwargs["files"] = [{"name": "a.png"}]
        state = {"messages": [msg]}
        update = _run(mw.abefore_agent(state, _FakeRuntime()))
        assert "plan_active" not in update
        assert update["messages"][-1].content == "Image attachments cannot accompany /plan off."

    def test_non_command_noop(self):
        mw = self._mw()
        state = {"messages": [HumanMessage(content="just a message", id="m5")]}
        assert _run(mw.abefore_agent(state, _FakeRuntime())) is None

    def test_wrap_model_call_active_injects_section(self):
        mw = self._mw()
        request = _FakeRequest(
            {"plan_active": True}, SystemMessage(content="base prompt")
        )

        async def handler(req):
            return req

        result = _run(mw.awrap_model_call(request, handler))
        blocks = result.system_message.content
        assert blocks[-1] == {"type": "text", "text": _cfg().section}
        assert {"type": "text", "text": "base prompt"} == blocks[0]

    def test_wrap_model_call_inactive_noop(self):
        mw = self._mw()
        request = _FakeRequest(
            {"plan_active": False}, SystemMessage(content="base prompt")
        )

        async def handler(req):
            return req

        result = _run(mw.awrap_model_call(request, handler))
        assert result.system_message.content == "base prompt"


class TestExitPlanModeTool:
    def _tool(self):
        return build_exit_plan_mode_tool()

    def test_inactive_rejected(self):
        runtime = _FakeRuntime(state={"plan_active": False})
        result = _run(self._tool().coroutine(runtime, "# Plan\nDo the work", "c1"))
        assert "only available in plan mode" in result

    def test_invalid_plan_rejected(self):
        runtime = _FakeRuntime(state={"plan_active": True})
        result = _run(self._tool().coroutine(runtime, "no heading", "c2"))
        assert "starting with a # heading" in result

    def test_approve_exits(self):
        runtime = _FakeRuntime(state={"plan_active": True})
        with patch("caspian.agents.plan.tools.interrupt", return_value={"decision": "approve"}):
            result = _run(self._tool().coroutine(runtime, "# Plan\nDo the work", "c3"))
        assert result.update["plan_active"] is False
        assert result.update["messages"][0].content.startswith("Plan approved")

    def test_keep_planning_feedback(self):
        runtime = _FakeRuntime(state={"plan_active": True})
        with patch(
            "caspian.agents.plan.tools.interrupt",
            return_value={"decision": "keep", "feedback": "add more detail"},
        ):
            result = _run(self._tool().coroutine(runtime, "# Plan\nDo the work", "c4"))
        assert "keep planning" in result
        assert "add more detail" in result

    def test_dismissed_wait(self):
        runtime = _FakeRuntime(state={"plan_active": True})
        with patch(
            "caspian.agents.plan.tools.interrupt",
            return_value={"decision": "dismiss"},
        ):
            result = _run(self._tool().coroutine(runtime, "# Plan\nDo the work", "c5"))
        assert "wait for their message" in result


class _PlanToolModel(BaseChatModel):
    """脚本化模型：首次调用返回 exit_plan_mode 工具调用，之后返回完成消息。"""

    calls: int = 0

    @property
    def _llm_type(self):
        return "plan-tool-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "exit_plan_mode",
                                    "args": {"plan": "# Plan\nDo the work"},
                                    "id": "call-plan-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


class TestPlanModeEndToEnd:
    def test_plan_review_interrupt_then_approve(self):
        async def scenario():
            cfg = _cfg()
            agent = create_agent(
                model=_PlanToolModel(),
                tools=[build_exit_plan_mode_tool()],
                state_schema=LeadAgentState,
                middleware=[PlanModeMiddleware(cfg)],
                system_prompt="You are a helpful agent.",
                checkpointer=InMemorySaver(),
            )
            config = {"configurable": {"thread_id": "th-plan-e2e"}}
            # 首次运行：/plan 进入计划模式 → 模型调用 exit_plan_mode → 触发中断
            interrupt_seen = None
            async for _, chunk in agent.astream(
                {"messages": [HumanMessage(content="/plan build it", id="m1")]},
                config=config,
                stream_mode=["values", "custom"],
            ):
                if "__interrupt__" in chunk:
                    interrupt_seen = chunk["__interrupt__"][0].value
            assert interrupt_seen is not None
            assert interrupt_seen["type"] == "plan_review"
            # 恢复：Approve → plan_active 置为 False，流程完成
            final = await agent.ainvoke(
                Command(resume={"decision": "approve"}), config=config
            )
            assert final["plan_active"] is False
            return True

        assert _run(scenario()) is True


class TestPlanModeBoundaryIsolation:
    """软引导边界：plan 模块 SHALL NOT 触碰沙箱或承诺层。"""

    def test_plan_module_does_not_touch_sandbox_or_commitment(self):
        import caspian.agents.plan as pkg

        base = Path(pkg.__file__).parent
        for name in ["middleware.py", "tools.py", "__init__.py"]:
            src = (base / name).read_text(encoding="utf-8")
            assert "from caspian.sandbox" not in src
            assert "from caspian.agents.commitment" not in src
            assert "import caspian.sandbox" not in src
            assert "import caspian.agents.commitment" not in src


def _fake_app_config(plan_enabled: bool):
    return SimpleNamespace(
        plan_mode=PlanModeConfig(
            enabled=plan_enabled, section="plan section"
        ),
        skills=SimpleNamespace(container_path=None),
        commitment=SimpleNamespace(enabled=False),
        subagents=SimpleNamespace(enabled=False),
        context_compression=None,
    )


class TestPlanModeAssemblyGate:
    """装配闸门：enabled 时注入，disabled 时零变化。"""

    def _run_make(self, plan_enabled: bool) -> dict:
        from caspian.agents.lead import agent as agent_mod

        captured: dict = {}

        def fake_create_agent(**kwargs):
            captured.update(kwargs)
            return object()

        with (
            patch("caspian.config.get_app_config", return_value=_fake_app_config(plan_enabled)),
            patch.object(agent_mod, "create_chat_model", return_value=object()),
            patch.object(agent_mod, "get_available_tools", return_value=[]),
            patch.object(
                agent_mod,
                "build_enabled_skill_catalog",
                return_value=SimpleNamespace(names=set(), skills=[]),
            ),
            patch(
                "caspian.tools.builtins.describe_skill_tool.build_describe_skill_tool",
                return_value=SimpleNamespace(name="describe_skill"),
            ),
            patch.object(agent_mod, "apply_prompt_template", return_value=""),
            patch("caspian.agents.lead.prompt.build_subagent_section", return_value=""),
            patch("caspian.plugins.runtime.get_plugin_runtime", return_value=None),
            patch.object(agent_mod, "create_agent", side_effect=fake_create_agent),
        ):
            _run(agent_mod.make_lead_agent(subagent_enabled=False))
        return captured

    def test_disabled_has_no_plan_middleware_or_tool(self):
        captured = self._run_make(False)
        names = [getattr(m, "__class__", None) for m in captured.get("middleware", [])]
        tool_names = [getattr(t, "name", None) for t in captured.get("tools", [])]
        assert "PlanModeMiddleware" not in {c.__name__ for c in names if c is not None}
        assert "exit_plan_mode" not in tool_names

    def test_enabled_adds_plan_middleware_and_tool(self):
        captured = self._run_make(True)
        names = [getattr(m, "__class__", None) for m in captured.get("middleware", [])]
        tool_names = [getattr(t, "name", None) for t in captured.get("tools", [])]
        assert "PlanModeMiddleware" in {c.__name__ for c in names if c is not None}
        assert "exit_plan_mode" in tool_names
