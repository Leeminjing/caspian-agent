"""test_plugin_system — 插件系统核心行为测试：契约校验、注入冲突、稳定顺序、依赖、
失败/超时语义、撤销与 Hook 中间件。"""

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from caspian.config.extensions_config import ExtensionsConfig
from caspian.plugins.errors import (
    InjectionConflict,
    PluginConfigError,
    UnsupportedExtensionInterface,
)
from caspian.plugins.hooks import PluginHookMiddleware
from caspian.plugins.loader import (
    PLUGINS_PUBLIC_REAL_ROOT,
    iter_plugin_dirs,
    load_plugin_module,
)
from caspian.plugins.registry import PluginRegistry
from caspian.plugins.runtime import PluginRuntime
from caspian.plugins.spec import (
    PluginBundle,
    PluginImplementation,
    resolve_interface,
    validate_implementation,
)
from caspian.plugins.tools import plugin_tools


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 接口规范与实现契约
# ---------------------------------------------------------------------------


class TestInterfaceSpec:

    def test_resolve_system_interface(self):
        spec = resolve_interface("before_model")
        assert spec is not None
        assert spec.kind.value == "ordered_mutator"

    def test_resolve_service_interface(self):
        spec = resolve_interface("service:attachment")
        assert spec is not None
        assert spec.kind.value == "service"

    def test_unknown_interface_returns_none(self):
        assert resolve_interface("my-unknown-hook") is None

    def test_reject_unknown_interface(self):
        error = validate_implementation(
            PluginImplementation(interface="my-hook", provider=lambda: None)
        )
        assert error is not None
        assert "my-hook" in error

    def test_reject_non_tool_provider(self):
        error = validate_implementation(
            PluginImplementation(interface="tool", provider=lambda x: x)
        )
        assert error is not None

    def test_reject_non_callable_hook(self):
        error = validate_implementation(
            PluginImplementation(interface="before_model", provider="not-callable")
        )
        assert error is not None

    def test_observer_forced_read_only(self):
        impl = PluginImplementation(interface="after_model", provider=lambda p, c: p)
        assert validate_implementation(impl) is None
        assert impl.read_only is True


# ---------------------------------------------------------------------------
# 注册表：注入 / 冲突 / 顺序 / 依赖 / 撤销
# ---------------------------------------------------------------------------


class TestRegistry:

    def test_inject_and_stable_order(self):
        registry = PluginRegistry()
        hook = lambda v, c: v
        registry.inject("b", None, [PluginImplementation(interface="before_model", provider=hook)])
        registry.inject("a", None, [PluginImplementation(interface="before_model", provider=hook)])
        # 顺序稳定 = 注入序号（加载顺序），不受 revoke/重复注入影响
        plugins = [name for name, _ in registry.hook_chain("before_model")]
        assert plugins == ["b", "a"]
        assert registry.hook_chain("before_model") == registry.hook_chain("before_model")

    def test_service_single_conflict(self):
        registry = PluginRegistry()
        a = PluginImplementation(interface="service:storage", provider=object())
        b = PluginImplementation(interface="service:storage", provider=object())
        assert registry.inject("a", None, [a]) == [None]
        results = registry.inject("b", None, [b])
        assert results[0] is not None
        assert "Injection Conflict" in results[0]

    def test_service_multi_coexists(self):
        registry = PluginRegistry()
        a = PluginImplementation(interface="service:storage", provider=object(), multi=True)
        b = PluginImplementation(interface="service:storage", provider=object())
        assert registry.inject("a", None, [a]) == [None]
        assert registry.inject("b", None, [b]) == [None]
        assert len(registry.snapshot()["services"]["service:storage"]) == 2

    def test_revoke_re_resolves_conflict(self):
        registry = PluginRegistry()
        a = PluginImplementation(interface="service:storage", provider=object())
        b = PluginImplementation(interface="service:storage", provider=object())
        registry.inject("a", None, [a])
        registry.inject("b", None, [b])  # 冲突进入待定
        registry.revoke("a", None)
        plugins = [name for name, _ in registry.snapshot()["services"]["service:storage"]]
        assert plugins == ["b"]

    def test_revoke_only_removes_that_plugin(self):
        registry = PluginRegistry()
        hook = lambda v, c: v
        registry.inject("a", None, [PluginImplementation(interface="before_model", provider=hook)])
        registry.inject("b", None, [PluginImplementation(interface="before_model", provider=hook)])
        registry.revoke("b", None)
        plugins = [name for name, _ in registry.hook_chain("before_model")]
        assert plugins == ["a"]

    def test_requires_missing(self):
        registry = PluginRegistry()
        assert registry.requires_missing(["service:attachment"]) == ["service:attachment"]
        registry.inject("a", None, [PluginImplementation(interface="service:attachment", provider=object())])
        assert registry.requires_missing(["service:attachment"]) == []


# ---------------------------------------------------------------------------
# 运行时：激活 / 状态 / 配置隔离 / 坏插件
# ---------------------------------------------------------------------------


def _write_plugin(tmp_path, name, source):
    plugin_dir = tmp_path / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")
    return plugin_dir


GOOD_SOURCE = '''
from langchain_core.tools import tool
from caspian.plugins.spec import PluginBundle, PluginImplementation

@tool
def browser_navigate(url: str) -> str:
    """Navigate browser to url."""
    return "opened: " + url

async def create_implementations(config):
    return PluginBundle(display_name="good", requires=["service:attachment"],
        implementations=[PluginImplementation(interface="tool", provider=browser_navigate)])
'''


class TestRuntime:

    def _runtime(self, tmp_path, plugins_cfg):
        import caspian.plugins.loader as loader_mod

        monkey = pytest.MonkeyPatch()
        monkey.setattr(loader_mod, "PLUGINS_PUBLIC_REAL_ROOT", str(tmp_path))
        config = ExtensionsConfig(plugins=plugins_cfg)
        runtime = PluginRuntime(config)
        return runtime, monkey

    def test_activate_and_status(self, tmp_path):
        _write_plugin(tmp_path, "good", GOOD_SOURCE.replace(
            'requires=["service:attachment"]', 'requires=[]'))
        runtime, monkey = self._runtime(tmp_path, {"good": {"enabled": True}})
        try:
            _run(runtime.load_public())
            status = runtime.status_payload()[0]
            assert status["state"] == "active"
            assert status["injected"] == ["tool"]
            assert status["missing_dependencies"] == []
        finally:
            monkey.undo()

    def test_missing_dependency_not_injected(self, tmp_path):
        _write_plugin(tmp_path, "good", GOOD_SOURCE)
        runtime, monkey = self._runtime(tmp_path, {"good": {"enabled": True}})
        try:
            _run(runtime.load_public())
            status = runtime.status_payload()[0]
            assert status["state"] == "unavailable"
            assert status["missing_dependencies"] == ["service:attachment"]
            assert runtime.registry.tool_entries() == []
        finally:
            monkey.undo()

    def test_bad_plugin_isolated(self, tmp_path):
        _write_plugin(tmp_path, "bad", 'raise RuntimeError("boom")')
        _write_plugin(tmp_path, "good", GOOD_SOURCE.replace(
            'requires=["service:attachment"]', 'requires=[]'))
        runtime, monkey = self._runtime(
            tmp_path, {"bad": {"enabled": True}, "good": {"enabled": True}})
        try:
            _run(runtime.load_public())
            by_name = {s["name"]: s for s in runtime.status_payload()}
            assert by_name["bad"]["state"] == "unavailable"
            assert by_name["good"]["state"] == "active"
        finally:
            monkey.undo()

    def test_config_isolated_to_plugin(self, tmp_path):
        source = '''
from caspian.plugins.errors import PluginConfigError
from caspian.plugins.spec import PluginBundle
async def create_implementations(config):
    if config.get("api_key") != "secret": raise PluginConfigError("missing api_key")
    return PluginBundle(display_name="cfg", implementations=[])
'''
        _write_plugin(tmp_path, "cfg", source)
        runtime, monkey = self._runtime(tmp_path, {"cfg": {"enabled": True, "config": {"api_key": "secret"}}})
        try:
            _run(runtime.load_public())
            assert runtime.status_payload()[0]["state"] == "active"
        finally:
            monkey.undo()

    def test_config_error_reason(self, tmp_path):
        source = '''
from caspian.plugins.errors import PluginConfigError
async def create_implementations(config):
    raise PluginConfigError("missing api_key")
'''
        _write_plugin(tmp_path, "cfg", source)
        runtime, monkey = self._runtime(tmp_path, {"cfg": {"enabled": True}})
        try:
            _run(runtime.load_public())
            status = runtime.status_payload()[0]
            assert status["state"] == "unavailable"
            assert "Invalid configuration" in status["reason"]
        finally:
            monkey.undo()

    def test_revoke(self, tmp_path):
        _write_plugin(tmp_path, "good", GOOD_SOURCE.replace(
            'requires=["service:attachment"]', 'requires=[]'))
        runtime, monkey = self._runtime(tmp_path, {"good": {"enabled": True}})
        try:
            _run(runtime.load_public())
            _run(runtime.revoke("good", None))
            assert runtime.status_payload() == []
            assert runtime.registry.tool_entries() == []
        finally:
            monkey.undo()

    def test_ensure_user_custom_plugin(self, tmp_path):
        import caspian.plugins.loader as loader_mod

        custom_template = str(tmp_path / "users" / "{user_id}" / "plugins")
        plugin_dir = tmp_path / "users" / "u1" / "plugins" / "hello"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.py").write_text(GOOD_SOURCE.replace(
            'requires=["service:attachment"]', 'requires=[]'), encoding="utf-8")
        monkey = pytest.MonkeyPatch()
        monkey.setattr(loader_mod, "PLUGINS_PUBLIC_REAL_ROOT", str(tmp_path / "public"))
        monkey.setattr(loader_mod, "PLUGINS_CUSTOM_REAL_ROOT", custom_template)
        runtime = PluginRuntime(ExtensionsConfig(plugins={"hello": {"enabled": True}}))
        try:
            _run(runtime.ensure_user("u1"))
            status = runtime.status_payload("u1")[0]
            assert status["state"] == "active"
            assert status["scope"] == "custom"
            # 幂等：重复 ensure 不重复注入
            _run(runtime.ensure_user("u1"))
            assert len(runtime.registry.tool_entries("u1")) == 1
            # owner 过滤：其他用户看不到该 custom 插件
            assert runtime.registry.tool_entries("other") == []
            assert runtime.status_payload("other") == []
        finally:
            monkey.undo()


# ---------------------------------------------------------------------------
# 插件 Tool 贡献：加法与同名冲突
# ---------------------------------------------------------------------------


class TestPluginTools:

    def test_tool_merge_and_conflict(self, tmp_path):
        _write_plugin(tmp_path, "good", GOOD_SOURCE.replace(
            'requires=["service:attachment"]', 'requires=[]'))
        runtime, monkey = TestRuntime()._runtime(tmp_path, {"good": {"enabled": True}})
        try:
            _run(runtime.load_public())
            names = [t.name for t in plugin_tools(runtime=runtime, existing_names=set())]
            assert names == ["browser_navigate"]
            names = [t.name for t in plugin_tools(runtime=runtime, existing_names={"browser_navigate"})]
            assert names == []
            # 冲突写入插件状态 issues（幂等）
            plugin_tools(runtime=runtime, existing_names={"browser_navigate"})
            status = runtime.status_payload()[0]
            assert any("同名" in issue for issue in status["issues"])
            assert len(status["issues"]) == 1
        finally:
            monkey.undo()


# ---------------------------------------------------------------------------
# PluginHookMiddleware：链执行 / 变更 / 失败 / 超时 / 只读 / 空链
# ---------------------------------------------------------------------------


class _FakeRuntime:
    def __init__(self, ctx=None):
        self.context = ctx or {"run_id": "r1", "user_id": "u1"}
        self.execution_info = type("Exec", (), {"thread_id": "t1"})()


class TestHookMiddleware:

    def _middleware(self, impls, runtime=None):
        registry = PluginRegistry()
        for plugin, impl in impls:
            registry.inject(plugin, None, [impl])
        plugin_runtime = PluginRuntime(ExtensionsConfig())
        plugin_runtime.registry = registry
        return PluginHookMiddleware(plugin_runtime, user_id=None)

    def test_empty_chain_noop(self):
        middleware = PluginHookMiddleware(None, user_id=None)
        state = {"messages": [HumanMessage(content="hi")]}
        assert _run(middleware.abefore_model(state, _FakeRuntime())) is None

    def test_before_agent_mutator(self):
        def mutator(value, ctx):
            return {"messages": [HumanMessage(content="agent-context")]}

        middleware = self._middleware(
            [("pa", PluginImplementation(interface="before_agent", provider=mutator))])
        state = {"messages": [HumanMessage(content="orig")]}
        update = _run(middleware.abefore_agent(state, _FakeRuntime()))
        assert update is not None
        assert update["messages"][-1].content == "agent-context"
        assert len(update["messages"]) == 2  # RemoveMessage REMOVE_ALL + 替换消息

    def test_mutator_chain_and_translation(self):
        calls = []

        def a(value, ctx):
            calls.append("a")
            return {"messages": [HumanMessage(content="from-a")]}

        def b(value, ctx):
            calls.append(("b", [m.content for m in value["messages"]]))
            return {"messages": [HumanMessage(content="from-b")]}

        middleware = self._middleware(
            [("pa", PluginImplementation(interface="before_model", provider=a)),
             ("pb", PluginImplementation(interface="before_model", provider=b))])
        state = {"messages": [HumanMessage(content="orig")]}
        update = _run(middleware.abefore_model(state, _FakeRuntime()))
        # B 收到 A 修改后的值（变更链传值）
        assert calls[1] == ("b", ["from-a"])
        # 状态更新为原位替换（RemoveMessage REMOVE_ALL + 最终消息）
        assert update is not None
        messages = update["messages"]
        assert len(messages) == 2
        assert messages[-1].content == "from-b"

    def test_failure_skipped_and_attributed(self):
        def a(value, ctx):
            raise RuntimeError("boom")

        def b(value, ctx):
            return {"messages": value["messages"]}

        middleware = self._middleware(
            [("pa", PluginImplementation(interface="before_model", provider=a)),
             ("pb", PluginImplementation(interface="before_model", provider=b))])
        _run(middleware.abefore_model(
            {"messages": [HumanMessage(content="x")]}, _FakeRuntime()))
        traces = middleware._runtime.trace.recent(run_id="r1")
        statuses = {t["plugin"]: t["status"] for t in traces}
        assert statuses["pa"] == "failed"
        assert statuses["pb"] == "ok"

    def test_timeout_skipped(self):
        async def slow(value, ctx):
            await asyncio.sleep(1)
            return value

        middleware = self._middleware(
            [("ps", PluginImplementation(
                interface="before_model", provider=slow, timeout_seconds=0.1))])
        result = _run(middleware.abefore_model(
            {"messages": [HumanMessage(content="x")]}, _FakeRuntime()))
        assert result is None
        traces = middleware._runtime.trace.recent(run_id="r1")
        assert traces[-1]["status"] == "timeout"

    def test_observer_result_discarded(self):
        middleware = self._middleware(
            [("po", PluginImplementation(
                interface="after_model", provider=lambda payload, ctx: "IGNORED"))])

        async def handler(request):
            return "RAW-RESPONSE"

        result = _run(middleware.awrap_model_call(
            type("Req", (), {
                "runtime": _FakeRuntime(),
                "override": lambda self, **kw: self,
            })(),
            handler))
        assert result == "RAW-RESPONSE"
        traces = middleware._runtime.trace.recent(run_id="r1")
        assert traces[-1]["status"] == "ok"
        assert traces[-1]["changed"] is False

    def test_before_tool_mutates_args(self):
        def mutator(tool_call, ctx):
            return {**tool_call, "args": {**tool_call["args"], "url": "https://mutated"}}

        middleware = self._middleware(
            [("pm", PluginImplementation(interface="before_tool", provider=mutator))])
        seen = {}

        async def handler(request):
            seen["args"] = request.tool_call["args"]
            return "tool-result"

        request = type("Req", (), {
            "tool_call": {"name": "navigate", "args": {"url": "https://orig"}},
            "runtime": _FakeRuntime(),
            "override": lambda self, **kw: type("R", (), kw)(),
        })()
        result = _run(middleware.awrap_tool_call(request, handler))
        assert result == "tool-result"
        assert seen["args"]["url"] == "https://mutated"
