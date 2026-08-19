"""test_plugin_remote — 跨语言插件通道测试：以子进程 Python 脚本模拟"外部语言"插件，
覆盖协议握手、tool 往返、Hook 链、超时、失败归因、撤销杀进程与声明拒绝。

注意: RemotePlugin 的管道与读循环任务绑定创建它的事件循环（生产网关为单持久循环），
每个测试场景 SHALL 在单个 asyncio.run 内完成 load → 调用 → close。
"""

import asyncio
import json
import sys

import pytest
from langchain_core.messages import HumanMessage

from caspian.config.extensions_config import ExtensionsConfig
from caspian.plugins.hooks import PluginHookMiddleware
from caspian.plugins.remote import RemotePlugin, load_manifest
from caspian.plugins.runtime import PluginRuntime
from caspian.plugins.tools import plugin_tools


def _run(coro):
    return asyncio.run(coro)


class _FakeRuntime:
    def __init__(self, ctx=None):
        self.context = ctx or {"run_id": "r1", "user_id": "u1"}
        self.execution_info = type("Exec", (), {"thread_id": "t1"})()


REMOTE_SCRIPT = r'''
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

DECLS = json.loads("__DECLS__")

def send(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    kind = message.get("type")
    if kind == "init":
        send({"type": "declarations", "display_name": "remote-test",
              "version": "0.1", "requires": [], "implementations": DECLS})
    elif kind == "call":
        interface = message.get("interface")
        value = message.get("value") or {}
        if interface == "tool":
            name = value.get("name")
            if name == "greet":
                send({"type": "result", "id": message["id"],
                      "value": "hello " + str(value.get("arguments", {}).get("name", ""))})
            elif name == "fail_tool":
                send({"type": "error", "id": message["id"], "error": "boom"})
            else:
                send({"type": "result", "id": message["id"], "value": "unknown"})
        elif interface in ("before_model", "before_agent"):
            messages = list(value.get("messages", []))
            if any(m.get("content") == "SLOW" for m in messages):
                time.sleep(2)  # 触发系统侧超时
            messages.append({"type": "human", "content": "remote-notice"})
            send({"type": "result", "id": message["id"], "value": {"messages": messages}})
        elif interface == "after_model":
            send({"type": "result", "id": message["id"], "value": "IGNORED"})
        else:
            send({"type": "result", "id": message["id"], "value": value})
    elif kind == "shutdown":
        break
'''


def _write_remote_plugin(tmp_path, name, decls, script=None):
    plugin_dir = tmp_path / name
    plugin_dir.mkdir(parents=True)
    script = script or REMOTE_SCRIPT
    script = script.replace("__DECLS__", json.dumps(decls).replace('"', '\\"'))
    (plugin_dir / "main.py").write_text(script, encoding="utf-8")
    (plugin_dir / "plugin.json").write_text(json.dumps({
        "display_name": name,
        "version": "0.1",
        "requires": [],
        "entry": {"command": sys.executable, "args": ["main.py"]},
    }), encoding="utf-8")
    return plugin_dir


def _runtime(tmp_path, plugins_cfg):
    import caspian.plugins.loader as loader_mod

    monkey = pytest.MonkeyPatch()
    monkey.setattr(loader_mod, "PLUGINS_PUBLIC_REAL_ROOT", str(tmp_path))
    runtime = PluginRuntime(ExtensionsConfig(plugins=plugins_cfg))
    return runtime, monkey


TOOL_DECL = {
    "interface": "tool",
    "tool": {
        "name": "greet",
        "description": "Greet someone.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "who"}},
            "required": ["name"],
        },
    },
}


class TestRemotePlugin:

    def test_tool_roundtrip(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [TOOL_DECL])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            status = runtime.status_payload()[0]
            assert status["state"] == "active"
            assert status["injected"] == ["tool"]
            tools = plugin_tools(runtime=runtime, existing_names=set())
            assert [t.name for t in tools] == ["greet"]
            result = await tools[0].ainvoke({"name": "world"})
            assert result == "hello world"
            await runtime.close()

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_before_model_mutation_chain(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [{"interface": "before_model"}])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            middleware = PluginHookMiddleware(runtime, user_id=None)
            state = {"messages": [HumanMessage(content="orig")]}
            update = await middleware.abefore_model(state, _FakeRuntime())
            assert update is not None
            assert update["messages"][-1].content == "remote-notice"
            traces = runtime.trace.recent(run_id="r1")
            assert traces[-1]["plugin"] == "r"
            assert traces[-1]["changed"] is True
            await runtime.close()

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_after_model_observer_discarded(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [{"interface": "after_model"}])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            middleware = PluginHookMiddleware(runtime, user_id=None)

            async def handler(request):
                return "RAW-RESPONSE"

            result = await middleware.awrap_model_call(
                type("Req", (), {"runtime": _FakeRuntime(), "override": lambda s, **kw: s})(),
                handler)
            assert result == "RAW-RESPONSE"
            await runtime.close()

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_hook_timeout_skipped(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [
            {"interface": "before_model", "timeout_seconds": 0.2},
        ])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            middleware = PluginHookMiddleware(runtime, user_id=None)
            result = await middleware.abefore_model(
                {"messages": [HumanMessage(content="SLOW")]}, _FakeRuntime())
            assert result is None
            traces = runtime.trace.recent(run_id="r1")
            assert traces[-1]["status"] == "timeout"
            await runtime.close()

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_tool_failure_attributed(self, tmp_path):
        fail_decl = {
            "interface": "tool",
            "tool": {"name": "fail_tool", "description": "Always fails.",
                     "parameters": {"type": "object", "properties": {}}},
        }
        _write_remote_plugin(tmp_path, "r", [fail_decl])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            tools = plugin_tools(runtime=runtime, existing_names=set())
            with pytest.raises(Exception, match="boom"):
                await tools[0].ainvoke({})
            await runtime.close()

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_revoke_terminates_process(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [TOOL_DECL])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            remote = runtime._remotes[(None, "r")]
            assert remote.alive
            await runtime.revoke("r", None)
            assert not remote.alive
            assert runtime.status_payload() == []

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_double_entry_rejected(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [TOOL_DECL])
        (tmp_path / "r" / "plugin.py").write_text("", encoding="utf-8")
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            status = runtime.status_payload()[0]
            assert status["state"] == "unavailable"
            assert "配置错误" in status["reason"]

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_remote_service_declaration_rejected(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [
            TOOL_DECL,
            {"interface": "service:attachment"},
        ])
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            status = runtime.status_payload()[0]
            assert status["state"] == "active"
            assert status["injected"] == ["tool"]
            assert any("service" in issue for issue in status["issues"])
            await runtime.close()

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_process_death_unavailable(self, tmp_path):
        _write_remote_plugin(tmp_path, "r", [], script="import sys\nsys.exit(1)\n")
        runtime, monkey = _runtime(tmp_path, {"r": {"enabled": True}})

        async def scenario():
            await runtime.load_public()
            status = runtime.status_payload()[0]
            assert status["state"] == "unavailable"
            assert "环境不可用" in status["reason"]

        try:
            _run(scenario())
        finally:
            monkey.undo()

    def test_handshake_timeout(self, tmp_path):
        # 脚本读输入但从不回复 declarations → 握手超时
        plugin_dir = _write_remote_plugin(
            tmp_path, "r", [], script="import sys\nfor _ in sys.stdin:\n    pass\n")
        manifest = load_manifest(plugin_dir)

        async def scenario():
            plugin = RemotePlugin(manifest, plugin_dir, {}, handshake_timeout=0.5)
            with pytest.raises(TimeoutError):
                await plugin.start()
            await plugin.shutdown()

        _run(scenario())
