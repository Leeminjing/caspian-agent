"""hello-plugin — 示例插件：注入一个 Tool 与一个 before_model Hook。

系统侧契约只有 create_implementations(config) -> PluginBundle：
插件自身的环境、依赖、配置解释全部在插件侧完成。

启用方式: extensions_config.json 的 plugins 段添加
    "hello-plugin": {"enabled": true}
"""

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from caspian.plugins.spec import PluginBundle, PluginImplementation


@tool
def greeting_tool(name: str) -> str:
    """向指定名字问好。"""
    return f"你好，{name}！来自 hello-plugin。"


async def add_context_notice(value, ctx):
    """before_model 可修改链示例：在消息列表末尾追加一条上下文提示。"""
    messages = list(value.get("messages", []))
    notice = HumanMessage(
        content=f"[插件 hello-plugin 注入了上下文提示，thread={ctx.get('thread_id')}]"
    )
    return {"messages": [*messages, notice]}


async def create_implementations(config):
    return PluginBundle(
        display_name="hello-plugin",
        version="0.1.0",
        requires=[],
        implementations=[
            PluginImplementation(interface="tool", provider=greeting_tool),
            PluginImplementation(interface="before_model", provider=add_context_notice),
        ],
    )
